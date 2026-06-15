from __future__ import annotations

import argparse
import sys
from pathlib import Path

from analyzer import analyze_functions
from models import AnalysisReport
from parser import CParserError, parse_c_file
from report import print_human_report, write_json_report
from scorer import score_report


DEFAULT_THRESHOLD = 60
REPORT_DIR = Path("report")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Static C analyzer for identifying potential FPGA/Vitis HLS "
            "acceleration candidates."
        )
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
        help="Optional JSON filename. Reports are always written under ./report/.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show detailed loop-level feature data in the terminal report.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    threshold = min(100, max(0, args.threshold))

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
            "This MVP uses static heuristics and does not perfectly partition arbitrary C programs into CPU and FPGA modules.",
            "Parsing targets a simplified HLS-style C subset; preprocessing, compiler extensions, and complex C constructs may require cleanup first.",
            "Scores are explainable estimates intended to guide later Vitis HLS and AI-driven design-space exploration.",
        ],
    )

    print_human_report(analysis_report, verbose=args.verbose)

    if args.json_output:
        json_output_path = _resolve_report_path(args.json_output)
        write_json_report(analysis_report, json_output_path)
        print()
        print(f"JSON report written to: {json_output_path}")

    return 0


def _resolve_report_path(json_output: str) -> Path:
    filename = Path(json_output).name
    if not filename:
        filename = "analysis_report.json"
    return REPORT_DIR / filename


if __name__ == "__main__":
    raise SystemExit(main())
