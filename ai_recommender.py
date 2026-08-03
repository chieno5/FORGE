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
    "BIND_STORAGE",
    "DATAFLOW",
    "INLINE",
    "PIPELINE",
    "UNROLL",
}


class AIRecommendationError(RuntimeError):
    """Raised when AI recommendation data cannot be requested or used."""


class _TargetedRepairRequired(RuntimeError):
    """Internal signal that only selected design-point ranks need replacement."""


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
Only use directives listed in automatic_directives. BIND_STORAGE is allowed only for a variable
listed in bind_storage_allowed_variables and must use type=ram_1p or ram_2p with impl=bram or
lutram. DATAFLOW is allowed only for a function listed in dataflow_allowed_functions. Never use
INTERFACE, BIND_OP, RESOURCE, DEPENDENCE or any directive not in automatic_directives.
Write directive names in uppercase. Use an empty target_loop_id for function-level pragmas.
Do not invent functions, loops or array names. Do not repeat or contradict pragmas in one solution.
Set name to a short descriptive snake_case label, such as balanced_mac_pipeline. Do not include
dp01, an ordinal, or the word candidate in name; FORGE adds the run-local dpNN prefix itself.
Never apply PIPELINE to a function or to a loop that contains deeper nested loops. PIPELINE is
only allowed on an innermost analyzed loop. Do not apply ARRAY_PARTITION complete to an external
function argument. For ARRAY_PARTITION, use a supplied array parameter or identified local array,
cyclic/block style, factor 2, 4 or 8, and dim=1.
For ARRAY_RESHAPE, use only a supplied array parameter or identified local array,
cyclic/block style, factor 2, 4 or 8, and dim=1. Never use complete reshaping.
Use ALLOCATION only on a function listed in
allocation_allowed_functions, with no loop target and exactly `operation instances=mul limit=N`
where N is 1, 2 or 4. ALLOCATION is a resource-sharing alternative: use it in at most one
solution in a batch, and do not use it when lower-risk loop or memory options are sufficient.
The input contains pipeline_allowed_loop_ids and unroll_allowed_loop_ids. Use PIPELINE or UNROLL
only on the corresponding exact loop IDs. Do not apply either directive to a loop marked with a
loop-carried dependency; recommend a refactor-oriented function-level alternative instead.
The history may include earlier plans for the same source. Prefer unexplored design points, but do
not treat a prior plan as forbidden: repeat it only when it is a useful verification or benchmark
and explain that choice in the strategy and risk fields.
The baseline_schedule contains the achieved HLS schedule when a baseline preflight was available.
The source_preflight context reports structural constraints found in the original source and any
controlled refactor already applied before this request. When applied is true, treat the supplied
source_code and baseline_schedule as the refactored baseline; do not recreate or undo that source
transformation. Recommend only pragmas that are legal for the supplied refactored source.
Its testbench summary reports the frozen validation profile, case count, B0 oracle and known
limits. Use this information when judging confidence, but do not claim that inferred cases prove
all possible C inputs. Never change the top function interface to fit the testbench.
Do not recommend a pragma that merely repeats an automatic baseline optimisation. Preserve an
outer-loop or flattened-loop baseline schedule unless the proposed change has a concrete reason to
improve total trip count, latency, or the energy-LUT product. Make the strategies meaningfully
different and return only the requested structured data."""


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
    exploration_state = (experience_context or {}).get("exploration_state", {})
    converged = bool(exploration_state.get("converged"))
    accepted: dict[int, OptimizationSolution] = {}
    repair_slots: list[int] = []
    rejected_for_repair: list[dict[str, Any]] = []
    for attempt in range(1, MAX_RECOMMENDATION_ATTEMPTS + 1):
        repair_mode = bool(repair_slots)
        requested_count = len(repair_slots) if repair_mode else design_point_count
        instructions = (
            f"{SYSTEM_PROMPT}\nReturn exactly {requested_count} solutions, "
            f"ranked consecutively from 1 to {requested_count}."
        )
        if repair_mode:
            instructions += (
                "\nThis is a targeted repair request. Keep every solution listed in "
                "repair_context.accepted_solutions unchanged. Return replacements only for "
                f"original ranks {repair_slots}, in that order, using temporary consecutive "
                f"ranks 1 through {requested_count}. Refine the rejected strategy by changing "
                "its actual pragma plan; changing only its name, rationale, or strategy text is "
                "not a replacement. Do not repeat any current_source_plans or accepted plan."
            )
        elif exploration_mode == "explore" and not converged:
            instructions += (
                "\nThis is an exploration batch for one unchanged experiment context. "
                "Do not return an exact pragma plan listed in current_source_plans. "
                "If every earlier candidate was below baseline, return entirely novel plans. "
                "If a candidate already exceeds baseline, return two novel plans and one "
                "parameter-level refinement around the current best plan."
            )
        elif exploration_mode == "verify":
            instructions += (
                "\nThis is a verification batch. Earlier plans may be repeated when their "
                "strategy explains why a new measurement is useful."
            )
        else:
            instructions += (
                "\nThe measured exploration state is converged after consecutive stagnant "
                "batches. Do not invent increasingly aggressive factors merely to be novel. "
                "Use bounded parameter refinements or repeat at most one incumbent-best plan "
                "as an explicit verification/benchmark point in both strategy and risk, and "
                "state that expected improvement is low."
            )
        if correction:
            if repair_mode:
                instructions += (
                    "\nThe previous targeted replacement failed FORGE validation:\n"
                    f"{correction}\nRepair only the requested replacement ranks."
                )
            else:
                instructions += (
                    "\nThe previous response failed FORGE validation with this error:\n"
                    f"{correction}\nRegenerate every solution from scratch and correct this issue. "
                    "Use only the supplied pipeline_allowed_loop_ids and unroll_allowed_loop_ids."
                )
        attempt_payload = dict(request_payload)
        if repair_mode:
            attempt_payload["repair_context"] = {
                "original_ranks_to_replace": repair_slots,
                "accepted_solutions": [
                    accepted[rank].to_dict() for rank in sorted(accepted)
                ],
                "rejected_solutions": rejected_for_repair,
                "replacement_rule": (
                    "Return only changed pragma plans for the rejected original ranks."
                ),
            }
        try:
            response = client.responses.create(
                model=selected_model,
                instructions=instructions,
                input=json.dumps(attempt_payload, ensure_ascii=False),
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
                design_point_count=requested_count,
                source_text=source_text,
            )
            summary = str(payload.get("summary", "")).strip()
            if repair_mode:
                candidates = [
                    _rerank_solution(solution, original_rank)
                    for solution, original_rank in zip(solutions, repair_slots)
                ]
            else:
                candidates = solutions

            rejected: list[OptimizationSolution] = []
            if exploration_mode == "explore":
                forbidden = _previous_plan_signatures(experience_context) | {
                    _pragma_plan_signature(solution.pragmas)
                    for solution in accepted.values()
                }
                incumbent_signature = _incumbent_plan_signature(experience_context)
                incumbent_verification_used = any(
                    _pragma_plan_signature(solution.pragmas) == incumbent_signature
                    for solution in accepted.values()
                )
                for solution in candidates:
                    signature = _pragma_plan_signature(solution.pragmas)
                    if signature in forbidden:
                        allowed_verification = (
                            converged
                            and not incumbent_verification_used
                            and incumbent_signature is not None
                            and signature == incumbent_signature
                            and _is_explicit_verification(solution)
                        )
                        if not allowed_verification:
                            rejected.append(solution)
                            continue
                        incumbent_verification_used = True
                    accepted[solution.rank] = solution
                    forbidden.add(signature)
            else:
                accepted.update({solution.rank: solution for solution in candidates})

            if rejected:
                repair_slots = [solution.rank for solution in rejected]
                rejected_for_repair = [solution.to_dict() for solution in rejected]
                names = ", ".join(solution.name for solution in rejected)
                correction = (
                    "Targeted repair required: AI repeated completed or already accepted "
                    "pragma plans in exploration "
                    f"mode for original rank(s) {repair_slots}: {names}"
                )
                raise _TargetedRepairRequired(correction)
            repair_slots = []
            rejected_for_repair = []
            if len(accepted) != design_point_count:
                missing = sorted(set(range(1, design_point_count + 1)) - set(accepted))
                repair_slots = missing
                correction = f"Missing replacement solutions for original rank(s): {missing}"
                raise _TargetedRepairRequired(correction)
        except (TypeError, json.JSONDecodeError) as exc:
            correction = "OpenAI did not return valid JSON."
        except _TargetedRepairRequired:
            pass
        except AIRecommendationError as exc:
            correction = str(exc)
        except Exception as exc:
            correction = f"OpenAI request failed: {exc}"
        else:
            return AIRecommendationResult(
                model=selected_model,
                # A rejected response must not leak into the final explanation.
                # In repair mode this is the summary of the accepted replacement only.
                summary=summary,
                solutions=[accepted[rank] for rank in sorted(accepted)],
            )

        if repair_mode and correction and not correction.startswith("Targeted repair"):
            correction = (
                f"Targeted repair for original rank(s) {repair_slots} failed validation: "
                f"{correction}"
            )
        if attempt < MAX_RECOMMENDATION_ATTEMPTS:
            if retry_callback is not None:
                retry_callback(attempt + 1, correction)
            continue
        missing = sorted(set(range(1, design_point_count + 1)) - set(accepted))
        fallback = _fallback_recommendations(
            report,
            top_function,
            len(missing),
            selected_model,
            correction or "OpenAI did not return a usable recommendation.",
            (
                _previous_plan_signatures(experience_context)
                | {_pragma_plan_signature(solution.pragmas) for solution in accepted.values()}
            ) if exploration_mode == "explore" else set(),
        )
        kept = len(accepted)
        for solution, original_rank in zip(fallback.solutions, missing):
            accepted[original_rank] = _rerank_solution(solution, original_rank)
        return AIRecommendationResult(
            model=selected_model,
            summary=(
                f"Kept {kept} validated AI design point(s). " if kept else ""
            ) + fallback.summary,
            solutions=[accepted[rank] for rank in sorted(accepted)],
            fallback_reason=fallback.fallback_reason,
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
    local_arrays = _local_array_names_by_function(source_text, functions_by_name)
    dataflow_functions = set(_dataflow_allowed_functions(report, source_text))

    solutions: list[OptimizationSolution] = []
    for raw_solution in raw_solutions:
        if not isinstance(raw_solution, dict):
            raise AIRecommendationError("Each solution must be a JSON object.")
        raw_pragmas = raw_solution.get("pragmas")
        if not isinstance(raw_pragmas, list) or len(raw_pragmas) < 2:
            raise AIRecommendationError("Each solution must contain at least two pragmas.")

        pragmas = [
            _parse_directive(
                item,
                functions_by_name,
                local_arrays,
                dataflow_functions,
            )
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
    local_arrays = _local_array_names_by_function(
        source_text, {function.name: function for function in report.functions}
    )
    parameter_arrays = _array_parameter_names(report)
    allowed_arrays = {
        name: sorted(set(parameter_arrays.get(name, [])) | set(local_arrays.get(name, [])))
        for name in {**parameter_arrays, **local_arrays}
    }
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
            "array_partition_allowed_variables": allowed_arrays,
            "array_reshape_allowed_variables": allowed_arrays,
            "bind_storage_allowed_variables": local_arrays,
            "dataflow_allowed_functions": _dataflow_allowed_functions(report, source_text),
            "allocation_allowed_functions": _allocation_allowed_functions(report),
            "external_parameter_rules": [
                "ARRAY_PARTITION complete is not allowed.",
                "ARRAY_RESHAPE complete is not allowed.",
            ],
        },
    }
    if experience_context:
        payload["experience_context"] = _ai_experience_context(experience_context)
    return payload


def _ai_experience_context(context: dict[str, Any]) -> dict[str, Any]:
    result = dict(context)
    incumbent = context.get("incumbent_best")
    if isinstance(incumbent, dict):
        result["incumbent_best"] = {
            key: incumbent.get(key)
            for key in (
                "name", "kind", "status", "pragma_plan", "rationale",
                "runtime_ns", "power_w", "energy_nj", "lut",
                "efficiency_score", "hls_schedule",
            )
        }
    return result


def _parse_directive(
    raw_item: Any,
    functions_by_name: dict[str, Any],
    local_arrays_by_function: dict[str, list[str]],
    dataflow_functions: set[str],
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
    _validate_directive(
        directive,
        functions_by_name,
        local_arrays_by_function,
        dataflow_functions,
    )
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
    local_arrays_by_function: dict[str, list[str]],
    dataflow_functions: set[str],
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
        ii = _pragma_integer_option(directive.pragma, "II")
        if re.search(r"\bII\s*=", directive.pragma, flags=re.IGNORECASE) and ii not in {1, 2, 3, 4}:
            raise AIRecommendationError("PIPELINE II must be an integer from 1 through 4.")
    if name == "UNROLL":
        if not directive.target_loop_id:
            raise AIRecommendationError("UNROLL must target an analyzed loop, not a function.")
        loop = next(region for region in target.loop_regions if region.id == directive.target_loop_id)
        if not loop.features.get("unroll_eligible", True):
            raise AIRecommendationError(
                f"UNROLL target has a loop-carried dependency: {directive.target_loop_id}"
            )
        factor = _pragma_integer_option(directive.pragma, "factor")
        has_factor = re.search(
            r"\bfactor\s*=", directive.pragma, flags=re.IGNORECASE
        )
        if has_factor and factor not in range(2, 9):
            raise AIRecommendationError("UNROLL factor must be an integer from 2 through 8.")
    if name in {"ARRAY_PARTITION", "ARRAY_RESHAPE"}:
        variable = _pragma_option(directive.pragma, "variable")
        array_parameters = _array_parameter_names_for_function(target)
        local_arrays = local_arrays_by_function.get(target.name, [])
        if not variable or variable not in {*array_parameters, *local_arrays}:
            raise AIRecommendationError(
                f"{name} must target a supplied array parameter or known local array: "
                f"{variable or '<missing>'}"
            )
        if variable in array_parameters and _is_complete_array_partition(directive.pragma):
            raise AIRecommendationError(
                f"{name} complete cannot target external function parameter: {variable}"
            )
        if directive.target_loop_id:
            raise AIRecommendationError(f"{name} must target a function, not a loop.")
        style_match = re.search(r"\b(cyclic|block)\b", directive.pragma, flags=re.IGNORECASE)
        factor = _pragma_integer_option(directive.pragma, "factor")
        dim = _pragma_integer_option(directive.pragma, "dim")
        if _is_complete_array_partition(directive.pragma) or not style_match:
            raise AIRecommendationError(f"{name} must use bounded cyclic or block style.")
        if factor not in {2, 4, 8} or dim != 1:
            raise AIRecommendationError(f"{name} requires factor 2, 4 or 8 and dim=1.")
    if name == "BIND_STORAGE":
        variable = _pragma_option(directive.pragma, "variable")
        storage_type = _pragma_option(directive.pragma, "type")
        implementation = _pragma_option(directive.pragma, "impl")
        if directive.target_loop_id:
            raise AIRecommendationError("BIND_STORAGE must target a function, not a loop.")
        if variable not in local_arrays_by_function.get(target.name, []):
            raise AIRecommendationError(
                f"BIND_STORAGE requires an existing local array: {variable or '<missing>'}"
            )
        if storage_type not in {"ram_1p", "ram_2p"} or implementation not in {"bram", "lutram"}:
            raise AIRecommendationError(
                "BIND_STORAGE must use type=ram_1p or ram_2p and impl=bram or lutram."
            )
    if name == "DATAFLOW":
        if directive.target_loop_id:
            raise AIRecommendationError("DATAFLOW must target a function, not a loop.")
        if target.name not in dataflow_functions:
            raise AIRecommendationError(
                f"DATAFLOW requires an existing multi-stage function: {target.name}"
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
def _local_array_names_by_function(
    source_text: str | None,
    functions_by_name: dict[str, Any],
) -> dict[str, list[str]]:
    text = source_text or ""
    result: dict[str, list[str]] = {}
    for name in functions_by_name:
        body = _function_body(text, name)
        result[name] = sorted(set(re.findall(
            r"\b(?:const\s+)?(?:unsigned\s+|signed\s+)?"
            r"(?:char|short|int|long|float|double|ap_[u]?int\s*<[^>]+>)\s+"
            r"([A-Za-z_]\w*)\s*(?:\[[^\]]+\])+\s*;",
            body,
        )))
    return result


def _function_body(source_text: str, function_name: str) -> str:
    match = re.search(rf"\b{re.escape(function_name)}\s*\([^;]*?\)\s*\{{", source_text, re.S)
    if match is None:
        return ""
    start = source_text.find("{", match.start())
    depth = 0
    for index in range(start, len(source_text)):
        if source_text[index] == "{":
            depth += 1
        elif source_text[index] == "}":
            depth -= 1
            if depth == 0:
                return source_text[start + 1:index]
    return ""


def _dataflow_allowed_functions(
    report: AnalysisReport,
    source_text: str | None,
) -> list[str]:
    del source_text
    local_functions = {function.name for function in report.functions}
    allowed: list[str] = []
    for function in report.functions:
        calls = function.features.get("called_functions", {})
        helper_calls = {
            name for name in calls if name in local_functions and name != function.name
        }
        has_dependency = any(
            loop.features.get("has_loop_carried_dependency", False)
            for loop in function.loop_regions
        )
        if len(helper_calls) >= 2 and not has_dependency:
            allowed.append(function.name)
    return allowed


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
    primary_options: list[tuple[str, PragmaDirective]] = []
    for loop_id in pipeline_loops:
        for ii in (1, 2, 4):
            primary_options.append((
                "conservative_loop_pipeline",
                PragmaDirective(
                    _loop_function_name(loop_id), loop_id,
                    f"#pragma HLS PIPELINE II={ii}",
                    "Local fallback uses a bounded pipeline target II from {1, 2, 4}.",
                ),
            ))
    for loop_id in unroll_loops:
        for factor in (2, 4, 8):
            primary_options.append((
                "conservative_loop_unroll",
                PragmaDirective(
                    _loop_function_name(loop_id), loop_id,
                    f"#pragma HLS UNROLL factor={factor}",
                    "Local fallback uses a bounded unroll factor from {2, 4, 8}.",
                ),
            ))
    memory_options = [
        PragmaDirective(
            function_name,
            "",
            f"#pragma HLS ARRAY_PARTITION variable={array_name} {partition_type} "
            f"factor={factor} dim=1",
            "Local fallback uses a bounded partition on an existing array parameter.",
        )
        for function_name, array_name in arrays
        for partition_type in ("cyclic", "block")
        for factor in (2, 4, 8)
    ]
    hierarchy = PragmaDirective(
        top_function,
        "",
        "#pragma HLS INLINE off",
        "Local fallback keeps hierarchy explicit for a resource-oriented alternative.",
    )
    candidates: list[tuple[str, list[PragmaDirective]]] = []
    if primary_options:
        for strategy, primary in primary_options:
            for secondary in memory_options or [hierarchy]:
                candidates.append((strategy, [primary, secondary]))
    elif memory_options:
        candidates = [("conservative_memory_exploration", [memory, hierarchy]) for memory in memory_options]
    else:
        candidates = [("conservative_hierarchy_verification", [hierarchy])]

    novel = [
        item for item in candidates
        if _pragma_plan_signature(item[1]) not in excluded_plan_signatures
    ]
    ordered = novel + [item for item in candidates if item not in novel]
    solutions: list[OptimizationSolution] = []
    for rank, (strategy, pragmas) in enumerate(
        ordered[:design_point_count], start=1
    ):
        verification = rank > len(novel)
        solutions.append(
            OptimizationSolution(
                rank=rank,
                name=f"dp{rank:02d}_local_safe_{strategy}_{rank}",
                strategy=strategy,
                expected_effect="Produces a conservative, executable HLS exploration point.",
                risk=(
                    "This bounded point repeats a prior plan for verification because the finite "
                    "safe fallback space is exhausted."
                    if verification else
                    "The result is rule-based because the AI response could not be validated."
                ),
                confidence=0.35,
                pragmas=list(pragmas),
            )
        )

    exhausted = len(solutions) < design_point_count
    return AIRecommendationResult(
        model=model,
        summary=(
            "OpenAI recommendations remained invalid after retries; FORGE generated "
            "conservative local design points from the static-analysis constraints."
            + (
                f" The bounded fallback space contains only {len(solutions)} unique point(s), "
                f"fewer than the {design_point_count} requested."
                if exhausted else ""
            )
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


def _incumbent_plan_signature(
    experience_context: dict[str, Any] | None,
) -> tuple[tuple[str, str, str], ...] | None:
    incumbent = (experience_context or {}).get("incumbent_best")
    if not isinstance(incumbent, dict):
        return None
    plan = incumbent.get("pragma_plan")
    pragmas = plan.get("pragmas") if isinstance(plan, dict) else incumbent.get("pragmas")
    if not isinstance(pragmas, list):
        return None
    return _pragma_plan_signature(pragmas)


def _is_explicit_verification(solution: OptimizationSolution) -> bool:
    description = f"{solution.strategy} {solution.risk}".lower()
    return any(
        marker in description
        for marker in ("verification", "verify", "benchmark", "re-measure", "remeasure", "retest")
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


def _rerank_solution(
    solution: OptimizationSolution,
    rank: int,
) -> OptimizationSolution:
    return OptimizationSolution(
        rank=rank,
        name=_run_scoped_solution_name(solution.name, rank, solution.strategy),
        strategy=solution.strategy,
        expected_effect=solution.expected_effect,
        risk=solution.risk,
        confidence=solution.confidence,
        pragmas=solution.pragmas,
    )
