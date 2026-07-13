from __future__ import annotations

from typing import Any

from models import FunctionAnalysis, LoopRegion


HIGH_PRIORITY = "HIGH_PRIORITY_FPGA_CANDIDATE"
MEDIUM_PRIORITY = "MEDIUM_PRIORITY_FPGA_CANDIDATE"
LOW_PRIORITY = "LOW_PRIORITY_OR_CPU_SUITABLE"
NOT_SUITABLE = "NOT_SUITABLE_FOR_HLS"


def score_report(functions: list[FunctionAnalysis], threshold: int) -> None:
    for function in functions:
        _score_module(function, threshold)
        for loop_region in function.loop_regions:
            _score_module(loop_region, threshold)


def classify_score(score: int) -> str:
    if score >= 75:
        return HIGH_PRIORITY
    if score >= 50:
        return MEDIUM_PRIORITY
    if score >= 30:
        return LOW_PRIORITY
    return NOT_SUITABLE


def _score_module(module: FunctionAnalysis | LoopRegion, threshold: int) -> None:
    features = module.features
    score = _calculate_score(features)
    module.score = score
    module.classification = classify_score(score)
    module.is_candidate = score >= threshold and module.classification != NOT_SUITABLE
    module.recommendations = _recommendations(features)
    module.reasoning = _reasoning(features, score, module.classification)


def _calculate_score(features: dict[str, Any]) -> int:
    score = 0

    if features["loop_count"] > 0:
        score += 15
    if features["max_loop_depth"] >= 2:
        score += 15
    if features["max_loop_depth"] >= 3:
        score += 10
    if features["arithmetic_op_count"] >= 5:
        score += 10
    elif features["arithmetic_op_count"] >= 1:
        score += 5
    if features["has_multiplication"] or features["has_mac_pattern"]:
        score += 15
    if features["has_regular_memory_access"]:
        score += 15
    if _has_regular_array_arithmetic_loop(features):
        score += 15
    if _has_large_loop_computation(features):
        score += 10
    if features["has_simple_loop_based_computation"]:
        score += 10

    unsupported = set(features["unsupported_constructs"])
    if "stdio" in unsupported or "file_io" in unsupported:
        score -= 20
    if "dynamic_memory" in unsupported:
        score -= 25
    if features["has_complex_pointer_usage"]:
        score -= 15
    if features["has_recursion"]:
        score -= 30
    if features["is_control_heavy"] and features["arithmetic_op_count"] < 3:
        score -= 15
    if features["unknown_function_call_count"] >= 3:
        score -= 10

    return min(100, max(0, score))


def _has_large_loop_computation(features: dict[str, Any]) -> bool:
    return (
        features["loop_count"] >= 2
        and features["arithmetic_op_count"] >= 4
        and features["array_access_count"] >= 4
    )


def _has_regular_array_arithmetic_loop(features: dict[str, Any]) -> bool:
    return (
        features["loop_count"] > 0
        and features["arithmetic_op_count"] > 0
        and features["array_access_count"] >= 2
        and features["has_regular_memory_access"]
    )


def _recommendations(features: dict[str, Any]) -> list[str]:
    recommendations: list[str] = []
    dependency_limited = features.get("has_loop_carried_dependency", False)
    if features["loop_count"] > 0 and not dependency_limited:
        recommendations.append("pipeline")
    if features["max_loop_depth"] >= 2 and not dependency_limited:
        recommendations.append("loop_unroll_for_small_fixed_bounds")
    if features["array_access_count"] >= 4 and features["has_regular_memory_access"]:
        recommendations.append("array_partition")
    if features["loop_count"] >= 3 and features["array_access_count"] >= 6:
        recommendations.append("dataflow")
    if features["has_reduction_pattern"]:
        recommendations.append("reduction_optimization")
    if dependency_limited:
        recommendations.append("dependency_aware_buffer_or_shift_register_refactor")
    if not recommendations:
        recommendations.append("keep_on_cpu_or_refactor_before_hls")
    return recommendations


def _reasoning(
    features: dict[str, Any],
    score: int,
    classification: str,
) -> str:
    positives: list[str] = []
    negatives: list[str] = []

    if features["loop_count"] > 0:
        positives.append(f"{features['loop_count']} loop(s)")
    if features["max_loop_depth"] >= 2:
        positives.append(f"nested loop depth {features['max_loop_depth']}")
    if features["has_mac_pattern"]:
        positives.append("multiply-accumulate pattern")
    elif features["has_multiplication"]:
        positives.append("multiplication")
    if features["has_regular_memory_access"]:
        positives.append("regular array access")
    if _has_regular_array_arithmetic_loop(features):
        positives.append("loop-based array arithmetic")
    if features["arithmetic_op_count"] >= 5:
        positives.append("many arithmetic operations")

    unsupported = features["unsupported_constructs"]
    if unsupported:
        negatives.append(f"unsupported constructs: {', '.join(unsupported)}")
    if features["has_complex_pointer_usage"]:
        negatives.append("possible complex pointer usage")
    if features["is_control_heavy"]:
        negatives.append("control-heavy structure")
    if features["unknown_function_call_count"] >= 3:
        negatives.append("many unknown function calls")
    if features.get("has_loop_carried_dependency", False):
        arrays = features.get("dependency_arrays", [])
        detail = f" on {', '.join(arrays)}" if arrays else ""
        negatives.append(f"loop-carried memory dependency{detail}")

    positive_text = ", ".join(positives) if positives else "limited compute-friendly structure"
    if negatives:
        return (
            f"Score {score}/100 ({classification}). Positive signals include {positive_text}; "
            f"penalties come from {', '.join(negatives)}."
        )
    return (
        f"Score {score}/100 ({classification}). Positive signals include {positive_text}, "
        "which suggests this module may be suitable for HLS exploration."
    )
