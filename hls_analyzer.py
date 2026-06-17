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
            "Analyze C code and score potential FPGA/HLS acceleration candidates."
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

    # 先解析 C 文件，再把 AST 交给分析器提取特征。
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

    print_human_report(analysis_report, verbose=args.verbose)

    if args.json_output:
        # JSON 统一写入 report/，避免散落在项目根目录。
        json_output_path = _resolve_report_path(args.json_output)
        write_json_report(analysis_report, json_output_path)
        print()
        print(f"JSON report written to: {json_output_path}")

    return 0


def _resolve_report_path(json_output: str) -> Path:
    # 即使用户传入子目录，也只取文件名，统一放到 report/ 下。
    filename = Path(json_output).name
    if not filename:
        filename = "analysis_report.json"
    return REPORT_DIR / filename


if __name__ == "__main__":
    raise SystemExit(main())
