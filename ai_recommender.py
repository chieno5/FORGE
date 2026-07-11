from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from typing import Any, Callable

from models import AnalysisReport


DEFAULT_MODEL = "gpt-5.4-mini"
MAX_RECOMMENDATION_ATTEMPTS = 3
ENERGY_EFFICIENCY_OBJECTIVE = (
    "maximize energy-LUT efficiency_score after Vitis evaluation "
    "(minimize candidate energy multiplied by LUT use relative to baseline)"
)
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
    """Raised when AI recommendation data cannot be requested or used."""


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
    summary: str
    solutions: list[OptimizationSolution]

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
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
Create the requested number of distinct whole-design design points for energy-LUT exploration.
Every solution becomes a complete Vitis project and should contain 2 to 6 coordinated pragmas
across the top function and its analyzed helper functions when the source structure supports it.
The later selection metric is efficiency_score based on energy and LUT after Vitis reports are available.
Balance latency, initiation interval, resource growth and expected switching activity. Do not optimize
only for raw performance or only for low power.
Use only valid '#pragma HLS ...' syntax and loop IDs present in the input JSON.
Prefer PIPELINE, UNROLL, DATAFLOW, ARRAY_PARTITION, ARRAY_RESHAPE, ALLOCATION,
BIND_OP, BIND_STORAGE, INLINE, DEPENDENCE, INTERFACE, LATENCY, STREAM or STABLE.
Write directive names in uppercase. Use an empty target_loop_id for function-level pragmas.
Do not invent functions, loops or array names. Do not repeat or contradict pragmas in one solution.
Never apply PIPELINE to a function or to a loop that contains deeper nested loops. PIPELINE is
only allowed on an innermost analyzed loop. Do not add INTERFACE pragmas for ports that already
have source-level interface directives, and do not bind external function arguments as local storage.
Make the three strategies meaningfully different and return only the requested structured data."""


def recommend_solutions(
    report: AnalysisReport,
    top_function: str,
    part: str,
    clock_period_ns: float,
    design_point_count: int = 3,
    model: str | None = None,
    client: Any | None = None,
    experience_context: dict[str, Any] | None = None,
    retry_callback: Callable[[int, str], None] | None = None,
    source_text: str | None = None,
) -> AIRecommendationResult:
    selected_model = model or DEFAULT_MODEL
    if client is None:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise AIRecommendationError(
                "OPENAI_API_KEY was not found. Set it in .env or in the PyCharm run environment."
            )
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise AIRecommendationError(
                "The openai package is missing. Run: pip install -r requirements.txt"
            ) from exc
        client = OpenAI(api_key=api_key)

    if design_point_count < 1:
        raise AIRecommendationError("design_point_count must be at least 1.")

    request_payload = _build_request_payload(
        report=report,
        top_function=top_function,
        part=part,
        clock_period_ns=clock_period_ns,
        experience_context=experience_context,
        design_point_count=design_point_count,
    )

    correction: str | None = None
    for attempt in range(1, MAX_RECOMMENDATION_ATTEMPTS + 1):
        instructions = (
            f"{SYSTEM_PROMPT}\nReturn exactly {design_point_count} solutions, "
            f"ranked consecutively from 1 to {design_point_count}."
        )
        if correction:
            instructions += (
                "\nThe previous response failed FORGE validation with this error:\n"
                f"{correction}\nRegenerate every solution from scratch and correct this issue."
            )
        try:
            response = client.responses.create(
                model=selected_model,
                instructions=instructions,
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
            payload = json.loads(response.output_text)
            solutions = parse_solution_payload(
                payload,
                report,
                top_function,
                design_point_count=design_point_count,
                source_text=source_text,
            )
        except (TypeError, json.JSONDecodeError) as exc:
            correction = "OpenAI did not return valid JSON."
        except AIRecommendationError as exc:
            correction = str(exc)
        except Exception as exc:
            raise AIRecommendationError(f"OpenAI request failed: {exc}") from exc
        else:
            return AIRecommendationResult(
                model=selected_model,
                summary=str(payload.get("summary", "")),
                solutions=solutions,
            )

        if attempt < MAX_RECOMMENDATION_ATTEMPTS:
            if retry_callback is not None:
                retry_callback(attempt + 1, correction)
            continue
        raise AIRecommendationError(
            f"AI returned invalid recommendations after {MAX_RECOMMENDATION_ATTEMPTS} attempts: "
            f"{correction}"
        )

    raise AssertionError("Recommendation retry loop unexpectedly completed.")


def parse_solution_payload(
    payload: dict[str, Any],
    report: AnalysisReport,
    top_function: str,
    design_point_count: int = 3,
    source_text: str | None = None,
) -> list[OptimizationSolution]:
    if not isinstance(payload, dict):
        raise AIRecommendationError("The recommendation root must be a JSON object.")

    raw_solutions = payload.get("solutions")
    if not isinstance(raw_solutions, list) or len(raw_solutions) != design_point_count:
        raise AIRecommendationError(
            f"AI must return exactly {design_point_count} complete solutions."
        )

    all_functions = {function.name: function for function in report.functions}
    if top_function not in all_functions:
        raise AIRecommendationError(f"Top function was not found: {top_function}")
    reachable_names = _reachable_function_names(all_functions, top_function)
    functions_by_name = {name: all_functions[name] for name in reachable_names}
    existing_interface_ports = _existing_interface_ports(source_text)

    solutions: list[OptimizationSolution] = []
    for raw_solution in raw_solutions:
        if not isinstance(raw_solution, dict):
            raise AIRecommendationError("Each solution must be a JSON object.")
        raw_pragmas = raw_solution.get("pragmas")
        if not isinstance(raw_pragmas, list) or len(raw_pragmas) < 2:
            raise AIRecommendationError("Each solution must contain at least two pragmas.")

        pragmas = [
            _parse_directive(item, functions_by_name, existing_interface_ports)
            for item in raw_pragmas
        ]
        pragma_keys = {
            (item.target_function, item.target_loop_id, item.pragma) for item in pragmas
        }
        if len(pragma_keys) != len(pragmas):
            raise AIRecommendationError("A solution contains duplicate pragmas.")

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
            raise AIRecommendationError("Solution fields are missing or invalid.") from exc
        if not 0 <= solution.confidence <= 1:
            raise AIRecommendationError("confidence must be between 0 and 1.")
        solutions.append(solution)

    solutions.sort(key=lambda item: (item.rank, -item.confidence))
    expected_ranks = list(range(1, design_point_count + 1))
    if [item.rank for item in solutions] != expected_ranks:
        raise AIRecommendationError(
            f"Solution ranks must be {expected_ranks}."
        )

    solution_keys = {
        tuple(
            sorted(
                (item.target_function, item.target_loop_id, item.pragma)
                for item in solution.pragmas
            )
        )
        for solution in solutions
    }
    if len(solution_keys) != design_point_count:
        raise AIRecommendationError("AI returned repeated solution content.")
    return solutions


def _build_request_payload(
    report: AnalysisReport,
    top_function: str,
    part: str,
    clock_period_ns: float,
    experience_context: dict[str, Any] | None,
    design_point_count: int,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "project": "FORGE: FPGA Optimization and Reconfiguration Generation Engine",
        "top_function": top_function,
        "optimization_objective": ENERGY_EFFICIENCY_OBJECTIVE,
        "target_part": part,
        "clock_period_ns": clock_period_ns,
        "design_point_count": design_point_count,
        "static_analysis": report.to_dict(),
    }
    if experience_context:
        payload["experience_context"] = experience_context
    return payload


def _parse_directive(
    raw_item: Any,
    functions_by_name: dict[str, Any],
    existing_interface_ports: set[str],
) -> PragmaDirective:
    if not isinstance(raw_item, dict):
        raise AIRecommendationError("Each pragma entry must be a JSON object.")
    try:
        directive = PragmaDirective(
            target_function=str(raw_item["target_function"]).strip(),
            target_loop_id=str(raw_item["target_loop_id"]).strip(),
            pragma=_normalize_pragma(str(raw_item["pragma"])),
            rationale=str(raw_item["rationale"]).strip(),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise AIRecommendationError("Pragma fields are missing or invalid.") from exc
    _validate_directive(directive, functions_by_name, existing_interface_ports)
    return directive


def _reachable_function_names(
    functions_by_name: dict[str, Any],
    top_function: str,
) -> set[str]:
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
    existing_interface_ports: set[str],
) -> None:
    target = functions_by_name.get(directive.target_function)
    if target is None:
        raise AIRecommendationError(
            f"AI recommended a function that does not exist or is not reachable: "
            f"{directive.target_function}"
        )
    valid_loop_ids = {region.id for region in target.loop_regions}
    if directive.target_loop_id and directive.target_loop_id not in valid_loop_ids:
        raise AIRecommendationError(f"AI recommended an unknown loop: {directive.target_loop_id}")
    if "\n" in directive.pragma or "\r" in directive.pragma:
        raise AIRecommendationError("Each pragma must contain one line only.")
    if not re.fullmatch(r"#pragma HLS [A-Za-z0-9_\[\].=+\- ]+", directive.pragma):
        raise AIRecommendationError(f"Invalid pragma format: {directive.pragma}")
    parts = directive.pragma.split()
    name = parts[2] if len(parts) >= 3 else ""
    if name not in SUPPORTED_DIRECTIVES:
        raise AIRecommendationError(f"Unsupported HLS directive: {name}")
    if name == "PIPELINE":
        if not directive.target_loop_id:
            raise AIRecommendationError("PIPELINE must target an innermost loop, not a function.")
        loop = next(region for region in target.loop_regions if region.id == directive.target_loop_id)
        if any(region.depth > loop.depth for region in target.loop_regions):
            raise AIRecommendationError(
                f"PIPELINE target is not an innermost loop: {directive.target_loop_id}"
            )
    if name == "BIND_STORAGE":
        variable = _pragma_option(directive.pragma, "variable")
        parameter_names = {_parameter_name(parameter) for parameter in target.parameters}
        if variable and variable in parameter_names:
            raise AIRecommendationError(
                f"BIND_STORAGE cannot target external function parameter: {variable}"
            )
    if name == "INTERFACE":
        port = _pragma_option(directive.pragma, "port")
        if port and port in existing_interface_ports:
            raise AIRecommendationError(
                f"INTERFACE cannot override existing source pragma for port: {port}"
            )


def _existing_interface_ports(source_text: str | None) -> set[str]:
    if not source_text:
        return set()
    return set(
        re.findall(
            r"#pragma\s+HLS\s+INTERFACE\b[^\n]*\bport\s*=\s*([A-Za-z_]\w*)",
            source_text,
            flags=re.IGNORECASE,
        )
    )


def _pragma_option(pragma: str, option: str) -> str | None:
    match = re.search(rf"\b{re.escape(option)}\s*=\s*([A-Za-z_]\w*)", pragma)
    return match.group(1) if match else None


def _parameter_name(parameter: str) -> str:
    match = re.search(r"([A-Za-z_]\w*)\s*(?:\[[^\]]*\])*$", parameter.strip())
    return match.group(1) if match else ""


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
