from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from typing import Any

from models import AnalysisReport


DEFAULT_MODEL = "gpt-5.4-mini"
OPTIMIZATION_FACTORS = {"performance", "power"}
SUPPORTED_DIRECTIVES = {
    "ALLOCATION",
    "ARRAY_PARTITION",
    "ARRAY_RESHAPE",
    "BIND_OP",
    "BIND_STORAGE",
    "DATAFLOW",
    "DEPENDENCE",
    "FUNCTION_INSTANTIATE",
    "INLINE",
    "INTERFACE",
    "LATENCY",
    "LOOP_FLATTEN",
    "LOOP_MERGE",
    "LOOP_TRIPCOUNT",
    "OCCURRENCE",
    "PIPELINE",
    "PROTOCOL",
    "RESET",
    "RESOURCE",
    "STABLE",
    "STREAM",
    "UNROLL",
}


class AIRecommendationError(RuntimeError):
    """AI 请求或返回数据无法使用时抛出。"""


@dataclass(frozen=True)
class PragmaDirective:
    target_function: str
    target_loop_id: str
    pragma: str
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OptimizationSolution:
    rank: int
    name: str
    strategy: str
    expected_effect: str
    risk: str
    confidence: float
    pragmas: list[PragmaDirective]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["pragmas"] = [item.to_dict() for item in self.pragmas]
        return data


@dataclass(frozen=True)
class AIRecommendationResult:
    model: str
    factor: str
    summary: str
    solutions: list[OptimizationSolution]

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "factor": self.factor,
            "summary": self.summary,
            "solutions": [item.to_dict() for item in self.solutions],
        }


PRAGMA_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "target_function": {"type": "string"},
        "target_loop_id": {"type": "string"},
        "pragma": {"type": "string"},
        "rationale": {"type": "string"},
    },
    "required": ["target_function", "target_loop_id", "pragma", "rationale"],
    "additionalProperties": False,
}

RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "solutions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "rank": {"type": "integer"},
                    "name": {"type": "string"},
                    "strategy": {"type": "string"},
                    "expected_effect": {"type": "string"},
                    "risk": {"type": "string"},
                    "confidence": {"type": "number"},
                    "pragmas": {"type": "array", "items": PRAGMA_SCHEMA},
                },
                "required": [
                    "rank",
                    "name",
                    "strategy",
                    "expected_effect",
                    "risk",
                    "confidence",
                    "pragmas",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "solutions"],
    "additionalProperties": False,
}


SYSTEM_PROMPT = """You are a Vitis HLS optimisation expert working inside FORGE.
Create exactly three distinct whole-design solutions for the requested optimisation factor.
Every solution becomes a complete Vitis project and should contain 2 to 6 coordinated pragmas
across the top function and its analyzed helper functions when the source structure supports it.
For factor 'performance', prioritize throughput, initiation interval and latency while avoiding
obviously infeasible resource growth. For factor 'power', prefer lower switching activity and
resource use while retaining useful performance; HLS-level power conclusions are only proxies.
Use only valid '#pragma HLS ...' syntax and loop IDs present in the input JSON.
Prefer PIPELINE, UNROLL, DATAFLOW, ARRAY_PARTITION, ARRAY_RESHAPE, ALLOCATION,
BIND_OP, BIND_STORAGE, INLINE, DEPENDENCE, INTERFACE, LATENCY, STREAM or STABLE.
Write directive names in uppercase. Use an empty target_loop_id for function-level pragmas.
Do not invent functions, loops or array names. Do not repeat or contradict pragmas in one solution.
Make the three strategies meaningfully different and return only the requested structured data."""


def recommend_solutions(
    report: AnalysisReport,
    top_function: str,
    factor: str,
    part: str,
    clock_period_ns: float,
    model: str | None = None,
    client: Any | None = None,
) -> AIRecommendationResult:
    """让 AI 针对同一目标生成三套多 pragma 组合方案。"""
    if factor not in OPTIMIZATION_FACTORS:
        raise AIRecommendationError(f"不支持的优化目标: {factor}")

    selected_model = model or os.getenv("FORGE_OPENAI_MODEL", DEFAULT_MODEL)
    if client is None:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise AIRecommendationError(
                "未找到 OPENAI_API_KEY。请在项目 .env 或 PyCharm 环境变量中设置。"
            )
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise AIRecommendationError(
                "缺少 openai 包，请运行 pip install -r requirements.txt。"
            ) from exc
        client = OpenAI(api_key=api_key)

    request_payload = {
        "project": "FORGE: FPGA Optimization and Reconfiguration Generation Engine",
        "top_function": top_function,
        "optimization_factor": factor,
        "target_part": part,
        "clock_period_ns": clock_period_ns,
        "static_analysis": report.to_dict(),
    }

    try:
        response = client.responses.create(
            model=selected_model,
            instructions=SYSTEM_PROMPT,
            input=json.dumps(request_payload, ensure_ascii=False),
            text={
                "format": {
                    "type": "json_schema",
                    "name": "forge_optimization_solutions",
                    "strict": True,
                    "schema": RESPONSE_SCHEMA,
                }
            },
        )
        raw_text = response.output_text
    except Exception as exc:
        raise AIRecommendationError(f"OpenAI 请求失败: {exc}") from exc

    try:
        payload = json.loads(raw_text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise AIRecommendationError("OpenAI 未返回有效的 JSON 推荐结果。") from exc

    solutions = parse_solution_payload(payload, report, top_function)
    return AIRecommendationResult(
        model=selected_model,
        factor=factor,
        summary=str(payload.get("summary", "")),
        solutions=solutions,
    )


def parse_solution_payload(
    payload: dict[str, Any],
    report: AnalysisReport,
    top_function: str,
) -> list[OptimizationSolution]:
    """本地校验组合方案，避免无效目标进入代码生成。"""
    if not isinstance(payload, dict):
        raise AIRecommendationError("推荐结果的根节点必须是 JSON 对象。")

    raw_solutions = payload.get("solutions")
    if not isinstance(raw_solutions, list) or len(raw_solutions) != 3:
        raise AIRecommendationError("AI 必须返回恰好三套完整方案。")

    all_functions = {function.name: function for function in report.functions}
    if top_function not in all_functions:
        raise AIRecommendationError(f"找不到顶层函数: {top_function}")
    reachable_names = _reachable_function_names(all_functions, top_function)
    functions_by_name = {
        name: all_functions[name] for name in reachable_names
    }

    solutions: list[OptimizationSolution] = []
    for raw_solution in raw_solutions:
        if not isinstance(raw_solution, dict):
            raise AIRecommendationError("每套方案都必须是 JSON 对象。")
        raw_pragmas = raw_solution.get("pragmas")
        if not isinstance(raw_pragmas, list) or len(raw_pragmas) < 2:
            raise AIRecommendationError("每套完整方案至少需要两个 pragma。")

        pragmas = [
            _parse_directive(item, functions_by_name) for item in raw_pragmas
        ]
        pragma_keys = {
            (item.target_function, item.target_loop_id, item.pragma) for item in pragmas
        }
        if len(pragma_keys) != len(pragmas):
            raise AIRecommendationError("同一方案中存在完全重复的 pragma。")

        try:
            solution = OptimizationSolution(
                rank=int(raw_solution["rank"]),
                name=str(raw_solution["name"]).strip(),
                strategy=str(raw_solution["strategy"]).strip(),
                expected_effect=str(raw_solution["expected_effect"]).strip(),
                risk=str(raw_solution["risk"]).strip(),
                confidence=float(raw_solution["confidence"]),
                pragmas=pragmas,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise AIRecommendationError("方案字段不完整或类型错误。") from exc
        if not 0 <= solution.confidence <= 1:
            raise AIRecommendationError("confidence 必须位于 0 到 1 之间。")
        solutions.append(solution)

    solutions.sort(key=lambda item: (item.rank, -item.confidence))
    if [item.rank for item in solutions] != [1, 2, 3]:
        raise AIRecommendationError("三套方案的 rank 必须分别为 1、2、3。")

    solution_keys = {
        tuple(
            sorted(
                (item.target_function, item.target_loop_id, item.pragma)
                for item in solution.pragmas
            )
        )
        for solution in solutions
    }
    if len(solution_keys) != 3:
        raise AIRecommendationError("AI 返回了内容完全相同的重复方案。")
    return solutions


def _parse_directive(
    raw_item: Any,
    functions_by_name: dict[str, Any],
) -> PragmaDirective:
    if not isinstance(raw_item, dict):
        raise AIRecommendationError("pragma 条目必须是 JSON 对象。")
    try:
        directive = PragmaDirective(
            target_function=str(raw_item["target_function"]).strip(),
            target_loop_id=str(raw_item["target_loop_id"]).strip(),
            pragma=_normalize_pragma(str(raw_item["pragma"])),
            rationale=str(raw_item["rationale"]).strip(),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise AIRecommendationError("pragma 字段不完整或类型错误。") from exc
    _validate_directive(directive, functions_by_name)
    return directive


def _reachable_function_names(
    functions_by_name: dict[str, Any],
    top_function: str,
) -> set[str]:
    """只允许优化顶层函数实际可达的代码。"""
    reachable: set[str] = set()
    pending = [top_function]
    while pending:
        name = pending.pop()
        if name in reachable:
            continue
        reachable.add(name)
        function = functions_by_name[name]
        called = function.features.get("called_functions", {})
        pending.extend(
            child
            for child in called
            if child in functions_by_name and child not in reachable
        )
    return reachable


def _validate_directive(
    directive: PragmaDirective,
    functions_by_name: dict[str, Any],
) -> None:
    target = functions_by_name.get(directive.target_function)
    if target is None:
        raise AIRecommendationError(f"AI 推荐了不存在的函数: {directive.target_function}")
    valid_loop_ids = {region.id for region in target.loop_regions}
    if directive.target_loop_id and directive.target_loop_id not in valid_loop_ids:
        raise AIRecommendationError(f"AI 推荐了不存在的循环: {directive.target_loop_id}")
    if "\n" in directive.pragma or "\r" in directive.pragma:
        raise AIRecommendationError("每个 pragma 条目只能包含一行指令。")
    if not re.fullmatch(r"#pragma HLS [A-Za-z0-9_\[\].=+\- ]+", directive.pragma):
        raise AIRecommendationError(f"pragma 格式无效: {directive.pragma}")
    parts = directive.pragma.split()
    name = parts[2] if len(parts) >= 3 else ""
    if name not in SUPPORTED_DIRECTIVES:
        raise AIRecommendationError(f"当前不支持该 HLS 指令: {name}")


def _normalize_pragma(pragma: str) -> str:
    value = pragma.strip()
    match = re.fullmatch(
        r"#pragma\s+hls\s+([A-Za-z_][A-Za-z0-9_]*)(.*)",
        value,
        flags=re.IGNORECASE,
    )
    if match is None:
        return value
    return f"#pragma HLS {match.group(1).upper()}{match.group(2)}"
