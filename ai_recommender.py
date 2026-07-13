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
# This subset is intentionally smaller than the full Vitis HLS pragma language.
# Each directive has a structural validation rule before FORGE generates a project.
AUTO_GENERATION_DIRECTIVES = {
    "ALLOCATION",
    "ARRAY_PARTITION",
    "ARRAY_RESHAPE",
    "INLINE",
    "PIPELINE",
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
    fallback_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result = {
            "model": self.model,
            "summary": self.summary,
            "solutions": [item.to_dict() for item in self.solutions],
        }
        if self.fallback_reason:
            result["fallback_reason"] = self.fallback_reason
        return result


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
Every solution becomes a complete Vitis project and should contain 2 to 4 coordinated pragmas
across the top function and its analyzed helper functions when the source structure supports it.
The sole selection metric is efficiency_score based on energy and LUT after Vitis reports are available.
Use latency, initiation interval, resource growth and expected switching activity only as ways to reduce
candidate energy multiplied by LUT use. Do not optimize only for raw performance or only for low power.
Use only valid '#pragma HLS ...' syntax and loop IDs present in the input JSON.
Only use these automatic-exploration directives: PIPELINE, UNROLL, ARRAY_PARTITION,
ARRAY_RESHAPE, ALLOCATION and INLINE. Never use BIND_STORAGE, INTERFACE, DATAFLOW,
BIND_OP, RESOURCE, DEPENDENCE or any directive not in that list.
Write directive names in uppercase. Use an empty target_loop_id for function-level pragmas.
Do not invent functions, loops or array names. Do not repeat or contradict pragmas in one solution.
Set name to a short descriptive snake_case label, such as balanced_mac_pipeline. Do not include
dp01, an ordinal, or the word candidate in name; FORGE adds the run-local dpNN prefix itself.
Never apply PIPELINE to a function or to a loop that contains deeper nested loops. PIPELINE is
only allowed on an innermost analyzed loop. Do not apply ARRAY_PARTITION complete to an external
function argument. For ARRAY_PARTITION, use a supplied array parameter, cyclic/block style,
factor 2, 4 or 8, and dim=1.
For ARRAY_RESHAPE, use only a supplied array parameter, cyclic/block style, factor 2, 4 or 8,
and dim=1. Never use complete reshaping. Use ALLOCATION only on a function listed in
allocation_allowed_functions, with no loop target and exactly `operation instances=mul limit=N`
where N is 1, 2 or 4. ALLOCATION is a resource-sharing alternative: use it in at most one
solution in a batch, and do not use it when lower-risk loop or memory options are sufficient.
The input contains pipeline_allowed_loop_ids and unroll_allowed_loop_ids. Use PIPELINE or UNROLL
only on the corresponding exact loop IDs. Do not apply either directive to a loop marked with a
loop-carried dependency; recommend a refactor-oriented function-level alternative instead.
The history may include earlier plans for the same source. Prefer unexplored design points, but do
not treat a prior plan as forbidden: repeat it only when it is a useful verification or benchmark
and explain that choice in the strategy and risk fields.
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
    exploration_mode: str = "explore",
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
    if exploration_mode not in {"explore", "verify"}:
        raise AIRecommendationError("exploration_mode must be 'explore' or 'verify'.")

    request_payload = _build_request_payload(
        report=report,
        top_function=top_function,
        part=part,
        clock_period_ns=clock_period_ns,
        experience_context=experience_context,
        design_point_count=design_point_count,
        source_text=source_text,
        exploration_mode=exploration_mode,
    )

    correction: str | None = None
    for attempt in range(1, MAX_RECOMMENDATION_ATTEMPTS + 1):
        instructions = (
            f"{SYSTEM_PROMPT}\nReturn exactly {design_point_count} solutions, "
            f"ranked consecutively from 1 to {design_point_count}."
        )
        if exploration_mode == "explore":
            instructions += (
                "\nThis is an exploration batch for one unchanged experiment context. "
                "Do not return an exact pragma plan listed in current_source_plans. "
                "If every earlier candidate was below baseline, return entirely novel plans. "
                "If a candidate already exceeds baseline, return two novel plans and one "
                "parameter-level refinement around the current best plan."
            )
        else:
            instructions += (
                "\nThis is a verification batch. Earlier plans may be repeated when their "
                "strategy explains why a new measurement is useful."
            )
        if correction:
            instructions += (
                "\nThe previous response failed FORGE validation with this error:\n"
                f"{correction}\nRegenerate every solution from scratch and correct this issue. "
                "Use only the supplied pipeline_allowed_loop_ids and unroll_allowed_loop_ids."
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
            if exploration_mode == "explore":
                _reject_current_context_duplicates(solutions, experience_context)
        except (TypeError, json.JSONDecodeError) as exc:
            correction = "OpenAI did not return valid JSON."
        except AIRecommendationError as exc:
            correction = str(exc)
        except Exception as exc:
            correction = f"OpenAI request failed: {exc}"
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
        return _fallback_recommendations(
            report,
            top_function,
            design_point_count,
            selected_model,
            correction or "OpenAI did not return a usable recommendation.",
            _previous_plan_signatures(experience_context) if exploration_mode == "explore" else set(),
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
                name=_run_scoped_solution_name(
                    str(raw_solution["name"]).strip(),
                    int(raw_solution["rank"]),
                    str(raw_solution["strategy"]).strip(),
                ),
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
    source_text: str | None,
    exploration_mode: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "project": "FORGE: FPGA Optimization and Reconfiguration Generation Engine",
        "top_function": top_function,
        "optimization_objective": ENERGY_EFFICIENCY_OBJECTIVE,
        "target_part": part,
        "clock_period_ns": clock_period_ns,
        "design_point_count": design_point_count,
        "exploration_mode": exploration_mode,
        "source_code": source_text or "",
        "static_analysis": report.to_dict(),
        "pragma_constraints": {
            "automatic_directives": sorted(AUTO_GENERATION_DIRECTIVES),
            "pipeline_allowed_loop_ids": _pipeline_allowed_loop_ids(report),
            "unroll_allowed_loop_ids": _unroll_allowed_loop_ids(report),
            "dependency_restricted_loop_ids": _dependency_restricted_loop_ids(report),
            "array_partition_allowed_variables": _array_parameter_names(report),
            "array_reshape_allowed_variables": _array_parameter_names(report),
            "allocation_allowed_functions": _allocation_allowed_functions(report),
            "external_parameter_rules": [
                "ARRAY_PARTITION complete is not allowed.",
                "ARRAY_RESHAPE complete is not allowed.",
            ],
        },
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
    if name not in AUTO_GENERATION_DIRECTIVES:
        raise AIRecommendationError(
            f"Directive is not enabled for automatic generation: {name}"
        )
    if name == "PIPELINE":
        if not directive.target_loop_id:
            raise AIRecommendationError("PIPELINE must target an innermost loop, not a function.")
        loop = next(region for region in target.loop_regions if region.id == directive.target_loop_id)
        if not _is_innermost_loop(target.loop_regions, loop.id):
            raise AIRecommendationError(
                f"PIPELINE target is not an innermost loop: {directive.target_loop_id}"
            )
        if not loop.features.get("pipeline_eligible", True):
            raise AIRecommendationError(
                f"PIPELINE target has a loop-carried dependency: {directive.target_loop_id}"
            )
    if name == "UNROLL":
        if not directive.target_loop_id:
            raise AIRecommendationError("UNROLL must target an analyzed loop, not a function.")
        loop = next(region for region in target.loop_regions if region.id == directive.target_loop_id)
        if not loop.features.get("unroll_eligible", True):
            raise AIRecommendationError(
                f"UNROLL target has a loop-carried dependency: {directive.target_loop_id}"
            )
    if name in {"ARRAY_PARTITION", "ARRAY_RESHAPE"}:
        variable = _pragma_option(directive.pragma, "variable")
        array_parameters = _array_parameter_names_for_function(target)
        if not variable or variable not in array_parameters:
            raise AIRecommendationError(
                f"{name} must target a supplied array parameter: {variable or '<missing>'}"
            )
        if _is_complete_array_partition(directive.pragma):
            raise AIRecommendationError(
                f"{name} complete cannot target external function parameter: {variable}"
            )
    if name == "ALLOCATION":
        operation = _pragma_option(directive.pragma, "instances")
        limit = _pragma_integer_option(directive.pragma, "limit")
        if directive.target_loop_id:
            raise AIRecommendationError("ALLOCATION must target a function, not a loop.")
        if not target.features.get("has_multiplication", False):
            raise AIRecommendationError(
                f"ALLOCATION is only allowed for a function with multiplication: {target.name}"
            )
        if operation != "mul" or limit not in {1, 2, 4}:
            raise AIRecommendationError(
                "ALLOCATION must use operation instances=mul with limit 1, 2 or 4."
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


def _pipeline_allowed_loop_ids(report: AnalysisReport) -> dict[str, list[str]]:
    return {
        function.name: [
            loop.id
            for loop in function.loop_regions
            if _is_innermost_loop(function.loop_regions, loop.id)
            and loop.features.get("pipeline_eligible", True)
        ]
        for function in report.functions
    }


def _unroll_allowed_loop_ids(report: AnalysisReport) -> dict[str, list[str]]:
    return {
        function.name: [
            loop.id
            for loop in function.loop_regions
            if loop.features.get("unroll_eligible", True)
        ]
        for function in report.functions
    }


def _dependency_restricted_loop_ids(report: AnalysisReport) -> dict[str, list[str]]:
    return {
        function.name: [
            loop.id
            for loop in function.loop_regions
            if loop.features.get("has_loop_carried_dependency", False)
        ]
        for function in report.functions
    }


def _array_parameter_names(report: AnalysisReport) -> dict[str, list[str]]:
    return {
        function.name: _array_parameter_names_for_function(function)
        for function in report.functions
    }


def _array_parameter_names_for_function(function: Any) -> list[str]:
    return [
        name
        for parameter in function.parameters
        if (name := _parameter_name(parameter))
        and ("[" in parameter or "*" in parameter)
    ]


def _allocation_allowed_functions(report: AnalysisReport) -> list[str]:
    return [
        function.name
        for function in report.functions
        if function.features.get("has_multiplication", False)
    ]


def _is_innermost_loop(loop_regions: list[Any], loop_id: str) -> bool:
    for index, loop in enumerate(loop_regions):
        if loop.id != loop_id:
            continue
        for later_loop in loop_regions[index + 1 :]:
            if later_loop.depth <= loop.depth:
                return True
            if later_loop.depth > loop.depth:
                return False
        return True
    return False


def _pragma_option(pragma: str, option: str) -> str | None:
    match = re.search(rf"\b{re.escape(option)}\s*=\s*([A-Za-z_]\w*)", pragma)
    return match.group(1) if match else None


def _pragma_integer_option(pragma: str, option: str) -> int | None:
    match = re.search(rf"\b{re.escape(option)}\s*=\s*(\d+)", pragma)
    return int(match.group(1)) if match else None


def _is_complete_array_partition(pragma: str) -> bool:
    return re.search(r"\bcomplete\b", pragma, flags=re.IGNORECASE) is not None


def _parameter_name(parameter: str) -> str:
    match = re.search(r"([A-Za-z_]\w*)\s*(?:\[[^\]]*\])*$", parameter.strip())
    return match.group(1) if match else ""


def _normalize_pragma(pragma: str) -> str:
    value = pragma.strip()
    match = re.fullmatch(
        r"(?:(?:#pragma\s+)?hls\s+)?([A-Za-z_][A-Za-z0-9_]*)(.*)",
        value,
        flags=re.IGNORECASE,
    )
    if match is None:
        return value
    return f"#pragma HLS {match.group(1).upper()}{match.group(2)}"


def _fallback_recommendations(
    report: AnalysisReport,
    top_function: str,
    design_point_count: int,
    model: str,
    reason: str,
    excluded_plan_signatures: set[tuple[tuple[str, str, str], ...]],
) -> AIRecommendationResult:
    """Create conservative design points when the remote response stays unusable."""

    functions = {function.name: function for function in report.functions}
    reachable_names = sorted(_reachable_function_names(functions, top_function))
    reachable = {name: functions[name] for name in reachable_names}
    pipeline_loops = [
        loop_id
        for function_name, loop_ids in _pipeline_allowed_loop_ids(report).items()
        if function_name in reachable
        for loop_id in loop_ids
    ]
    unroll_loops = [
        loop_id
        for function_name, loop_ids in _unroll_allowed_loop_ids(report).items()
        if function_name in reachable
        for loop_id in loop_ids
    ]
    arrays = [
        (function_name, array_name)
        for function_name in reachable_names
        for array_name in _array_parameter_names(report).get(function_name, [])
    ]
    solutions: list[OptimizationSolution] = []

    variation_offset = len(excluded_plan_signatures)
    for rank in range(1, design_point_count + 1):
        variation = rank - 1 + variation_offset
        pragmas: list[PragmaDirective] = []
        use_unroll = bool(unroll_loops) and (not pipeline_loops or variation % 3 == 2)
        if use_unroll:
            target_loop = unroll_loops[variation % len(unroll_loops)]
            pragmas.append(
                PragmaDirective(
                    _loop_function_name(target_loop),
                    target_loop,
                    f"#pragma HLS UNROLL factor={2 ** (1 + (variation // 3))}",
                    "Local fallback uses a small, bounded unroll factor.",
                )
            )
            strategy = "conservative_loop_unroll"
        elif pipeline_loops:
            target_loop = pipeline_loops[variation % len(pipeline_loops)]
            pragmas.append(
                PragmaDirective(
                    _loop_function_name(target_loop),
                    target_loop,
                    f"#pragma HLS PIPELINE II={1 + variation}",
                    "Local fallback pipelines a statically eligible innermost loop.",
                )
            )
            strategy = "conservative_loop_pipeline"
        else:
            strategy = "conservative_memory_exploration"

        if arrays:
            function_name, array_name = arrays[variation % len(arrays)]
            pragmas.append(
                PragmaDirective(
                    function_name,
                    "",
                    "#pragma HLS ARRAY_PARTITION "
                    f"variable={array_name} cyclic factor={2 ** (1 + (variation % 3))} dim=1",
                    "Local fallback uses a bounded cyclic partition on an array parameter.",
                )
            )
        if len(pragmas) < 2:
            pragmas.append(
                PragmaDirective(
                    top_function,
                    "",
                    "#pragma HLS INLINE off",
                    "Local fallback keeps hierarchy explicit for a resource-oriented alternative.",
                )
            )
        solutions.append(
            OptimizationSolution(
                rank=rank,
                name=f"dp{rank:02d}_local_safe_{strategy}_{rank}",
                strategy=strategy,
                expected_effect="Produces a conservative, executable HLS exploration point.",
                risk="The result is rule-based because the AI response could not be validated.",
                confidence=0.35,
                pragmas=pragmas,
            )
        )

    return AIRecommendationResult(
        model=model,
        summary=(
            "OpenAI recommendations remained invalid after retries; FORGE generated "
            "conservative local design points from the static-analysis constraints."
        ),
        solutions=solutions,
        fallback_reason=reason,
    )


def _loop_function_name(loop_id: str) -> str:
    return loop_id.rsplit(".loop_", 1)[0]


def _previous_plan_signatures(
    experience_context: dict[str, Any] | None,
) -> set[tuple[tuple[str, str, str], ...]]:
    if not experience_context:
        return set()
    return {
        _pragma_plan_signature(item.get("pragmas", []))
        for item in experience_context.get("current_source_plans", [])
        if isinstance(item, dict)
    }


def _reject_current_context_duplicates(
    solutions: list[OptimizationSolution],
    experience_context: dict[str, Any] | None,
) -> None:
    previous = _previous_plan_signatures(experience_context)
    for solution in solutions:
        if _pragma_plan_signature(solution.pragmas) in previous:
            raise AIRecommendationError(
                f"AI repeated a completed pragma plan in exploration mode: {solution.name}"
            )


def _pragma_plan_signature(pragmas: list[Any]) -> tuple[tuple[str, str, str], ...]:
    entries: list[tuple[str, str, str]] = []
    for pragma in pragmas:
        if isinstance(pragma, PragmaDirective):
            entries.append((pragma.target_function, pragma.target_loop_id, pragma.pragma))
        elif isinstance(pragma, dict):
            entries.append(
                (
                    str(pragma.get("target_function", "")),
                    str(pragma.get("target_loop_id", "")),
                    _normalize_pragma(str(pragma.get("pragma", ""))),
                )
            )
        else:
            entries.append(("", "", _normalize_pragma(str(pragma))))
    return tuple(sorted(entries))


def _run_scoped_solution_name(name: str, rank: int, strategy: str = "") -> str:
    suffix = re.sub(
        r"^(?:dp|design[_\s-]*point)\s*\d+[_\s-]*",
        "",
        name,
        flags=re.IGNORECASE,
    )
    suffix = suffix.strip("_ -")
    if not suffix or suffix.lower() == "candidate":
        words = re.findall(r"[A-Za-z0-9]+", strategy.lower())
        suffix = "_".join(words[:5]) or "generated_design"
    return f"dp{rank:02d}_{suffix}"
