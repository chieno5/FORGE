from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # Static analysis does not require .env support.
    load_dotenv = None

from ai_recommender import (
    AIRecommendationError,
    DEFAULT_MODEL,
    recommend_solutions,
)
from analyzer import analyze_functions
from models import AnalysisReport, FunctionAnalysis
from parser import CParserError, parse_c_file
from report import print_human_report, write_data_report, write_json_report
from scorer import score_report
from vitis_generator import VitisGenerationError, generate_vitis_projects


PROJECT_NAME = "FORGE: FPGA Optimization and Reconfiguration Generation Engine"
DEFAULT_THRESHOLD = 60
REPORT_DIR = Path("report")
DEFAULT_OUTPUT_ROOT = Path("generated")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="forge",
        description=(
            "FORGE analyzes C code, recommends Vitis HLS pragmas and generates variants."
        ),
    )
    parser.add_argument("input", help="Path to the C source file to analyze.")
    parser.add_argument(
        "--threshold",
        type=int,
        default=DEFAULT_THRESHOLD,
        help="Minimum score required to mark a module as an FPGA candidate.",
    )
    parser.add_argument(
        "--json",
        dest="json_output",
        help="Static analysis JSON filename. It is always written under ./report/.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show full function features, reasoning and loop details.",
    )
    parser.add_argument(
        "--ai",
        action="store_true",
        help="Ask OpenAI for three whole-design optimisation solutions.",
    )
    parser.add_argument(
        "--generate",
        action="store_true",
        help="Generate baseline plus three complete Vitis HLS solutions.",
    )
    parser.add_argument(
        "--model",
        help=f"OpenAI model. Default: FORGE_OPENAI_MODEL or {DEFAULT_MODEL}.",
    )
    parser.add_argument(
        "--top",
        help="Vitis top function. By default FORGE chooses the highest-scoring function.",
    )
    parser.add_argument(
        "--output-root",
        default=str(DEFAULT_OUTPUT_ROOT),
        help="Directory used for generated Vitis projects.",
    )
    parser.add_argument(
        "--part",
        help="Vitis FPGA part number. Default: FORGE_VITIS_PART or xc7z020clg400-1.",
    )
    parser.add_argument(
        "--clock",
        type=float,
        help="Target clock period in ns. Default: FORGE_VITIS_CLOCK_NS or 10.0.",
    )
    parser.add_argument(
        "--testbench",
        help="Optional C/C++ testbench copied into baseline and every solution.",
    )
    parser.add_argument(
        "--auto-testbench",
        action="store_true",
        help="Generate a local smoke testbench when --generate is used.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    if load_dotenv is not None:
        load_dotenv()
    args = build_arg_parser().parse_args(argv)
    threshold = min(100, max(0, args.threshold))
    needs_ai = args.ai or args.generate
    if args.auto_testbench and not args.generate:
        print("Error: --auto-testbench is only used with --generate.", file=sys.stderr)
        return 2
    if args.auto_testbench and args.testbench:
        print("Error: use either --testbench or --auto-testbench, not both.", file=sys.stderr)
        return 2

    try:
        parsed = parse_c_file(args.input)
    except CParserError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    functions = analyze_functions(parsed.functions)
    score_report(functions, threshold)
    analysis_report = AnalysisReport(
        file=str(Path(args.input)),
        threshold=threshold,
        functions=functions,
        limitations=[
            "Scores are based on static heuristics.",
            "Complex C syntax may need cleanup before analysis.",
        ],
    )

    print(PROJECT_NAME)
    print_human_report(analysis_report, verbose=args.verbose)

    static_report_path: Path | None = None
    if args.json_output:
        static_report_path = _resolve_report_path(args.json_output)
    elif needs_ai:
        static_report_path = REPORT_DIR / f"{Path(args.input).stem}_analysis_report.json"

    if static_report_path:
        write_json_report(analysis_report, static_report_path)
        print(f"\nStatic report written to: {static_report_path}")

    if not needs_ai:
        return 0

    try:
        top_function = _select_top_function(functions, args.top)
        part = args.part or os.getenv("FORGE_VITIS_PART", "xc7z020clg400-1")
        clock_period_ns = (
            args.clock if args.clock is not None else _clock_from_environment()
        )
        ai_result = recommend_solutions(
            analysis_report,
            top_function,
            part=part,
            clock_period_ns=clock_period_ns,
            model=args.model or os.getenv("FORGE_OPENAI_MODEL", DEFAULT_MODEL),
        )
    except (AIRecommendationError, ValueError, VitisGenerationError) as exc:
        print(f"AI recommendation error: {exc}", file=sys.stderr)
        return 3

    pragma_report_path = REPORT_DIR / f"{Path(args.input).stem}_pragma_report.json"
    pragma_report = {
        "project": PROJECT_NAME,
        "source_file": str(Path(args.input)),
        "top_function": top_function,
        "optimization_objective": "performance_per_watt_per_lut",
        "target_part": part,
        "clock_period_ns": clock_period_ns,
        "static_report": str(static_report_path) if static_report_path else None,
        "ai": ai_result.to_dict(),
        "generated_projects": [],
    }
    write_data_report(pragma_report, pragma_report_path)

    generated_projects = []
    if args.generate:
        try:
            generated_projects = generate_vitis_projects(
                source_path=args.input,
                report=analysis_report,
                solutions=ai_result.solutions,
                top_function=top_function,
                output_root=args.output_root,
                part=part,
                clock_period_ns=clock_period_ns,
                testbench_path=args.testbench,
                auto_testbench=args.auto_testbench,
            )
        except VitisGenerationError as exc:
            print(f"Vitis generation error: {exc}", file=sys.stderr)
            return 4

    pragma_report["generated_projects"] = [
        item.to_dict() for item in generated_projects
    ]
    write_data_report(pragma_report, pragma_report_path)

    print(f"\nSelected top function: {top_function}")
    print("Optimisation objective: performance_per_watt_per_lut")
    for solution in ai_result.solutions:
        print(f"{solution.rank}. {solution.name} ({len(solution.pragmas)} pragmas)")
        for directive in solution.pragmas:
            target = directive.target_loop_id or directive.target_function
            print(f"   - {directive.pragma} -> {target}")
    print(f"AI pragma report written to: {pragma_report_path}")
    if generated_projects:
        print(
            "Baseline and Vitis solutions written under: "
            f"{Path(args.output_root) / Path(args.input).stem}"
        )
    return 0


def _select_top_function(
    functions: list[FunctionAnalysis],
    requested: str | None,
) -> str:
    if not functions:
        raise ValueError("No analyzable C functions were found in the input file.")
    if requested:
        if not any(function.name == requested for function in functions):
            raise ValueError(f"Requested top function was not found: {requested}")
        return requested
    return max(functions, key=lambda function: function.score).name


def _clock_from_environment() -> float:
    raw_value = os.getenv("FORGE_VITIS_CLOCK_NS", "10.0")
    try:
        return float(raw_value)
    except ValueError as exc:
        raise VitisGenerationError(
            f"FORGE_VITIS_CLOCK_NS is not a valid number: {raw_value}"
        ) from exc


def _resolve_report_path(json_output: str) -> Path:
    filename = Path(json_output).name or "analysis_report.json"
    return REPORT_DIR / filename


if __name__ == "__main__":
    raise SystemExit(main())
