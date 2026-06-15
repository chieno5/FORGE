from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from models import AnalysisReport, FunctionAnalysis, LoopRegion


def print_human_report(report: AnalysisReport, verbose: bool = False) -> None:
    print("HLS Static Analysis Report")
    print("=" * 80)
    print(f"File: {report.file}")
    print(f"Candidate threshold: {report.threshold}")
    print()

    if not report.functions:
        print("No C function definitions were found.")
        return

    for function in report.functions:
        _print_function(function, verbose)
        print()

    print("Limitations")
    print("-" * 80)
    for limitation in report.limitations:
        print(f"- {limitation}")


def write_json_report(report: AnalysisReport, output_path: str | Path) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report.to_dict(), indent=2, sort_keys=False),
        encoding="utf-8",
    )


def _print_function(function: FunctionAnalysis, verbose: bool) -> None:
    print(f"Function: {function.name}")
    print(f"Score: {function.score} / 100")
    print(f"Classification: {function.classification}")
    print(f"Candidate above threshold: {'yes' if function.is_candidate else 'no'}")
    print("Detected features:")
    _print_features(function.features)
    print("Reasoning:")
    print(f"  {function.reasoning}")
    print("Suggested next-stage optimisation directions:")
    for recommendation in function.recommendations:
        print(f"  - {recommendation}")

    if function.loop_regions:
        print("Loop-level regions:")
        for region in function.loop_regions:
            _print_loop_region(region, verbose)


def _print_loop_region(region: LoopRegion, verbose: bool) -> None:
    print(f"  Region: {region.id} ({region.kind}, depth {region.depth})")
    print(f"  Score: {region.score} / 100")
    print(f"  Classification: {region.classification}")
    if verbose:
        print("  Features:")
        _print_features(region.features, indent="    ")
    print(f"  Reasoning: {region.reasoning}")


def _print_features(features: dict[str, Any], indent: str = "  ") -> None:
    display_items = [
        ("Loops", "loop_count"),
        ("Max nested loop depth", "max_loop_depth"),
        ("Array accesses", "array_access_count"),
        ("Regular array accesses", "regular_array_access_count"),
        ("Arithmetic operations", "arithmetic_op_count"),
        ("Assignments", "assignment_count"),
        ("Function calls", "function_call_count"),
        ("Unknown function calls", "unknown_function_call_count"),
        ("Branches", "branch_count"),
    ]
    for label, key in display_items:
        print(f"{indent}- {label}: {features[key]}")
    print(f"{indent}- Multiplication: {_yes_no(features['has_multiplication'])}")
    print(f"{indent}- MAC pattern: {_yes_no(features['has_mac_pattern'])}")
    print(f"{indent}- Reduction-like pattern: {_yes_no(features['has_reduction_pattern'])}")
    print(f"{indent}- Regular memory access: {_yes_no(features['has_regular_memory_access'])}")
    print(f"{indent}- Compute heavy: {_yes_no(features['is_compute_heavy'])}")
    print(f"{indent}- Control heavy: {_yes_no(features['is_control_heavy'])}")
    unsupported = features["unsupported_constructs"]
    print(f"{indent}- Unsupported constructs: {', '.join(unsupported) if unsupported else 'none'}")


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"
