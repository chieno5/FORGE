from __future__ import annotations

import argparse
import shlex
import sys
import tomllib
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
except ImportError:  # Static analysis does not require .env support.
    load_dotenv = None

from ai_recommender import (
    AIRecommendationError,
    DEFAULT_MODEL,
    recommend_solutions,
)
from application_classifier import classify_application
from analyzer import analyze_functions
from forge_database import ForgeDatabase
from models import AnalysisReport, FunctionAnalysis
from parser import CParserError, parse_c_file
from report import (
    print_human_report,
    write_data_report,
    write_experiment_reports,
    write_json_report,
)
from scorer import score_report
from vitis_generator import VitisGenerationError, generate_vitis_projects
from vitis_runner import (
    DEFAULT_TOOL_TIMEOUT_SECONDS,
    VitisExecutionError,
    package_best_project,
    run_experiments,
    select_best_result,
)


PROJECT_NAME = "FORGE: FPGA Optimization and Reconfiguration Generation Engine"
DEFAULT_THRESHOLD = 60
REPORT_DIR = Path("report")
DEFAULT_OUTPUT_ROOT = Path("generated")
DEFAULT_DATABASE = Path("data") / "forge_test.db"
CONFIG_PATH = Path("forge.toml")
ASCII_LOGO = """
 ███████╗ ██████╗ ██████╗  ██████╗ ███████╗
 ██╔════╝██╔═══██╗██╔══██╗██╔════╝ ██╔════╝
 █████╗  ██║   ██║██████╔╝██║  ███╗█████╗
 ██╔══╝  ██║   ██║██╔══██╗██║   ██║██╔══╝
 ██║     ╚██████╔╝██║  ██║╚██████╔╝███████╗
 ╚═╝      ╚═════╝ ╚═╝  ╚═╝ ╚═════╝ ╚══════╝

              Forge Hardware. Explore Design Space.
"""


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="forge",
        description=(
            "FORGE analyzes C code, recommends Vitis HLS pragmas and generates variants."
        ),
    )
    parser.add_argument("input", help="Path to the C source file to analyze.")
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
        help="Ask OpenAI for whole-design energy-LUT design points.",
    )
    parser.add_argument(
        "--generate",
        action="store_true",
        help="Generate a baseline plus complete Vitis HLS design points.",
    )
    parser.add_argument(
        "--design-points",
        type=int,
        default=3,
        help="Number of AI design points. Default: 3; baseline is added separately.",
    )
    parser.add_argument(
        "--model",
        help="OpenAI model override.",
    )
    parser.add_argument(
        "--run-vitis",
        action="store_true",
        help="Run Vitis HLS, Vivado power estimation, rank results and package the best project.",
    )
    parser.add_argument(
        "--tool-timeout",
        type=float,
        help="Maximum seconds for one Vitis or Vivado command.",
    )
    parser.add_argument(
        "--vitis-hls",
        help="Vitis HLS executable override.",
    )
    parser.add_argument(
        "--vivado",
        help="Vivado executable override.",
    )
    parser.add_argument(
        "--amd-root",
        help="AMD tool root override.",
    )
    parser.add_argument(
        "--database",
        help="SQLite history database path override.",
    )
    parser.add_argument(
        "--top",
        help="Vitis top function override. Default: detected call-graph entry function.",
    )
    parser.add_argument(
        "--output-root",
        help="Directory used for generated Vitis projects.",
    )
    parser.add_argument(
        "--part",
        help="Vitis FPGA part override.",
    )
    parser.add_argument(
        "--clock",
        type=float,
        help="Target clock period in ns override.",
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
    parser.add_argument(
        "--include-dir",
        action="append",
        default=[],
        help="Directory containing quoted local headers required by the input source. Repeat if needed.",
    )
    return parser


def main(
    argv: list[str] | None = None,
    config_overrides: dict[str, Any] | None = None,
) -> int:
    _configure_console_encoding()
    if load_dotenv is not None:
        load_dotenv()
    try:
        config = _load_configuration()
        if config_overrides:
            _merge_config(config, config_overrides)
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    command_args = sys.argv[1:] if argv is None else argv
    if not command_args:
        _print_banner()
        return _interactive_shell(config)
    return _run_cli(command_args, show_banner=True, config=config)


def _run_cli(argv: list[str], show_banner: bool, config: dict[str, Any]) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        _apply_configuration_defaults(args, config)
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    if show_banner:
        _print_banner()
    _print_tool_progress("Static analysis: parsing C source")
    threshold = DEFAULT_THRESHOLD
    needs_ai = args.ai or args.generate
    if args.design_points < 1:
        print("Error: --design-points must be at least 1.", file=sys.stderr)
        return 2
    if args.tool_timeout <= 0:
        print("Error: --tool-timeout must be greater than 0.", file=sys.stderr)
        return 2
    if args.run_vitis and not args.generate:
        print("Error: --run-vitis is only used with --generate.", file=sys.stderr)
        return 2
    if args.run_vitis and not (args.testbench or args.auto_testbench):
        print(
            "Error: --run-vitis requires --testbench or --auto-testbench for csim/cosim.",
            file=sys.stderr,
        )
        return 2
    if args.auto_testbench and not args.generate:
        print("Error: --auto-testbench is only used with --generate.", file=sys.stderr)
        return 2
    if args.auto_testbench and args.testbench:
        print("Error: use either --testbench or --auto-testbench, not both.", file=sys.stderr)
        return 2

    try:
        parsed = parse_c_file(args.input, args.include_dir)
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

    _print_tool_progress("Static analysis: complete")
    print_human_report(analysis_report, verbose=args.verbose)

    static_report_path: Path | None = None
    if args.json_output:
        static_report_path = _resolve_report_path(args.json_output)
    elif needs_ai:
        static_report_path = REPORT_DIR / f"{Path(args.input).stem}_analysis_report.json"

    if static_report_path:
        write_json_report(analysis_report, static_report_path)
        _print_tool_progress(f"Static report: written to {static_report_path}")

    if not needs_ai:
        return 0

    source_text = Path(args.input).read_text(encoding="utf-8")
    application = classify_application(args.input, source_text, analysis_report)
    database = ForgeDatabase(args.database)
    try:
        top_function = _select_top_function(functions, args.top)
        part = args.part
        clock_period_ns = args.clock
        model = args.model
        _print_tool_progress(
            f"AI recommendation: contacting OpenAI for {args.design_points} design points"
        )
        ai_result = recommend_solutions(
            analysis_report,
            top_function,
            part=part,
            clock_period_ns=clock_period_ns,
            design_point_count=args.design_points,
            model=model,
            experience_context=database.history_context(
                application.key,
                source_path=args.input,
                source_text=source_text,
            ),
            retry_callback=_print_ai_retry,
            source_text=source_text,
        )
    except (AIRecommendationError, ValueError, VitisGenerationError) as exc:
        print(f"AI recommendation error: {exc}", file=sys.stderr)
        return 3

    _print_tool_progress(
        f"AI recommendation: received {len(ai_result.solutions)} design points"
    )
    if ai_result.fallback_reason:
        _print_tool_progress(
            "AI recommendation: using local safe fallback after invalid AI responses"
        )
    _print_ai_summary(ai_result.summary, ai_result.solutions)

    run = database.create_run(
        args.input,
        source_text,
        application.key,
        top_function,
        str(static_report_path) if static_report_path else None,
    )
    pragma_report_path = REPORT_DIR / f"{Path(args.input).stem}_pragma_report.json"
    pragma_report = {
        "project": PROJECT_NAME,
        "source_file": str(Path(args.input)),
        "top_function": top_function,
        "optimization_objective": "energy_lut_efficiency",
        "application": application.to_dict(),
        "analysis_run_id": run.id,
        "design_point_count": args.design_points,
        "target_part": part,
        "clock_period_ns": clock_period_ns,
        "static_report": str(static_report_path) if static_report_path else None,
        "ai": ai_result.to_dict(),
        "generated_projects": [],
    }
    write_data_report(pragma_report, pragma_report_path)

    generated_projects = []
    experiment_results = []
    best_result = None
    experiment_report_paths = {}
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
                include_dirs=args.include_dir,
            )
        except VitisGenerationError as exc:
            print(f"Vitis generation error: {exc}", file=sys.stderr)
            return 4

        design_point_ids = database.record_design_points(
            run.id,
            [item.to_dict() for item in generated_projects],
        )
        if args.run_vitis:
            try:
                experiment_results = run_experiments(
                    generated_projects,
                    top_function,
                    part,
                    vitis_hls_command=args.vitis_hls,
                    vivado_command=args.vivado,
                amd_root=args.amd_root,
                tool_timeout_seconds=args.tool_timeout,
                progress_callback=_print_tool_progress,
            )
            except VitisExecutionError as exc:
                print(f"Vitis execution error: {exc}", file=sys.stderr)
                pragma_report["execution_error"] = str(exc)
                write_data_report(pragma_report, pragma_report_path)
                return 5
            try:
                best_result = package_best_project(
                    select_best_result(experiment_results),
                    top_function,
                    vitis_hls_command=args.vitis_hls,
                    amd_root=args.amd_root,
                    tool_timeout_seconds=args.tool_timeout,
                    progress_callback=_print_tool_progress,
                )
                experiment_results = [
                    best_result if item.project_directory == best_result.project_directory else item
                    for item in experiment_results
                ]
            except VitisExecutionError as exc:
                _record_experiment_results(database, design_point_ids, generated_projects, experiment_results)
                experiment_report_paths = write_experiment_reports(
                    [item.to_dict() for item in experiment_results],
                    REPORT_DIR,
                    Path(args.input).stem,
                )
                pragma_report["experiment_results"] = [item.to_dict() for item in experiment_results]
                pragma_report["experiment_reports"] = experiment_report_paths
                pragma_report["execution_error"] = str(exc)
                write_data_report(pragma_report, pragma_report_path)
                print(f"Vitis execution error: {exc}", file=sys.stderr)
                return 5
            _record_experiment_results(database, design_point_ids, generated_projects, experiment_results)
            experiment_report_paths = write_experiment_reports(
                [item.to_dict() for item in experiment_results],
                REPORT_DIR,
                Path(args.input).stem,
            )

    pragma_report["generated_projects"] = [
        item.to_dict() for item in generated_projects
    ]
    pragma_report["experiment_results"] = [item.to_dict() for item in experiment_results]
    pragma_report["best_design_point"] = best_result.to_dict() if best_result else None
    pragma_report["experiment_reports"] = experiment_report_paths
    write_data_report(pragma_report, pragma_report_path)

    _print_tool_progress(f"Selected top function: {top_function}")
    _print_tool_progress("Optimisation objective: energy_lut_efficiency")
    _print_tool_progress("AI pragma details:")
    for solution in ai_result.solutions:
        print(f"  {solution.rank}. {solution.name} ({len(solution.pragmas)} pragmas)")
        for directive in solution.pragmas:
            target = directive.target_loop_id or directive.target_function
            print(f"     - {directive.pragma} -> {target}")
    _print_tool_progress(f"AI pragma report: written to {pragma_report_path}")
    if generated_projects:
        _print_tool_progress(
            "Generated projects: written to "
            f"{Path(args.output_root) / Path(args.input).stem}"
        )
    if best_result:
        _print_tool_progress(
            f"Best design point: {best_result.name} | "
            f"efficiency_score={best_result.efficiency_score:.4f}"
        )
        _print_tool_progress(f"Final package: {best_result.package_path}")
        _print_tool_progress(f"Experiment reports: {experiment_report_paths}")
    database.close()
    return 0


def _interactive_shell(config: dict[str, Any]) -> int:
    print("[FORGE] Interactive mode. Type help to view commands.")
    while True:
        try:
            command = input("forge> ").strip()
        except EOFError:
            print()
            return 0
        except KeyboardInterrupt:
            print()
            continue

        if not command:
            continue
        if command.lower() in {"exit", "quit"}:
            return 0
        if command.lower() == "help":
            _print_interactive_help()
            continue

        arguments = shlex.split(command, posix=False)
        if arguments and arguments[0].lower() == "run":
            arguments = arguments[1:]
        if not arguments:
            print("[FORGE] Missing input file. Type help for usage.")
            continue
        try:
            _run_cli(arguments, show_banner=False, config=config)
        except SystemExit as exc:
            if exc.code not in (0, None):
                print("[FORGE] Invalid command. Type help for usage.")
        except Exception as exc:
            print(f"[FORGE] Command failed: {exc}")


def _print_banner() -> None:
    print(ASCII_LOGO.rstrip())
    print(PROJECT_NAME)


def _print_interactive_help() -> None:
    print("[FORGE] Interactive commands")
    print("  <input-file> [options]    Run FORGE with a source file and options.")
    print("  run <input-file> [options] Run FORGE with an explicit run prefix.")
    print("  help                      Show this help and all CLI options.")
    print("  exit | quit               Leave the interactive mode.")
    print()
    build_arg_parser().print_help()


def _load_configuration() -> dict[str, Any]:
    config: dict[str, Any] = {
        "ai": {"model": DEFAULT_MODEL},
        "toolchain": {
            "part": "xc7z020clg400-1",
            "clock_ns": 10.0,
            "timeout_seconds": DEFAULT_TOOL_TIMEOUT_SECONDS,
            "amd_root": None,
            "vitis_hls": None,
            "vivado": None,
        },
        "output": {"root": str(DEFAULT_OUTPUT_ROOT)},
        "database": {"path": str(DEFAULT_DATABASE)},
    }
    for path in (CONFIG_PATH,):
        if not path.exists():
            continue
        try:
            with path.open("rb") as handle:
                loaded = tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"Invalid TOML in {path}: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ValueError(f"Configuration root must be a table: {path}")
        _merge_config(config, loaded)
    return config


def _merge_config(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge_config(target[key], value)
        else:
            target[key] = value


def _apply_configuration_defaults(args: argparse.Namespace, config: dict[str, Any]) -> None:
    args.model = args.model or _config_text(config, "ai", "model") or DEFAULT_MODEL
    args.part = args.part or _config_text(
        config, "toolchain", "part"
    ) or "xc7z020clg400-1"
    args.clock = args.clock if args.clock is not None else _config_float(
        config, "toolchain", "clock_ns", 10.0
    )
    args.tool_timeout = (
        args.tool_timeout
        if args.tool_timeout is not None
        else _config_float(
            config,
            "toolchain",
            "timeout_seconds",
            DEFAULT_TOOL_TIMEOUT_SECONDS,
        )
    )
    args.database = args.database or _config_text(
        config, "database", "path"
    ) or str(DEFAULT_DATABASE)
    args.output_root = args.output_root or _config_text(
        config, "output", "root"
    ) or str(DEFAULT_OUTPUT_ROOT)
    args.amd_root = args.amd_root or _config_text(config, "toolchain", "amd_root")
    args.vitis_hls = args.vitis_hls or _config_text(
        config, "toolchain", "vitis_hls"
    )
    args.vivado = args.vivado or _config_text(
        config, "toolchain", "vivado"
    )


def _config_text(config: dict[str, Any], section: str, key: str) -> str | None:
    value = config.get(section, {}).get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"Configuration value {section}.{key} must be a string.")
    return value or None


def _config_float(
    config: dict[str, Any],
    section: str,
    key: str,
    fallback: float,
) -> float:
    value = config.get(section, {}).get(key, fallback)
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Configuration value {section}.{key} must be a number.") from exc


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
    non_main_functions = [function for function in functions if function.name != "main"]
    candidates = non_main_functions or functions
    called_names = {
        name
        for function in candidates
        for name in function.features.get("called_functions", {})
    }
    entry_functions = [function for function in candidates if function.name not in called_names]
    if entry_functions:
        return max(
            entry_functions,
            key=lambda function: (
                function.features.get("function_call_count", 0),
                function.score,
            ),
        ).name
    return max(candidates, key=lambda function: function.score).name


def _resolve_report_path(json_output: str) -> Path:
    filename = Path(json_output).name or "analysis_report.json"
    return REPORT_DIR / filename


def _record_experiment_results(
    database: ForgeDatabase,
    design_point_ids: dict[str, int],
    generated_projects: list[object],
    experiment_results: list[object],
) -> None:
    ranks_by_directory = {
        _normalise_project_path(item.directory): item.rank
        for item in generated_projects
        if item.rank is not None
    }
    for result in experiment_results:
        if result.kind == "baseline":
            point_key = "baseline"
        else:
            rank = ranks_by_directory.get(_normalise_project_path(result.project_directory))
            if rank is None:
                raise ValueError(
                    "Experiment result does not match a generated design point: "
                    f"{result.project_directory}"
                )
            point_key = f"design_point_{rank:03d}"
        database.record_experiment(
            design_point_ids[point_key],
            result.to_dict(),
            result.status,
            {
                key: value
                for key, value in {
                    "hls_report": result.hls_report,
                    "cosim_report": result.cosim_report,
                    "power_report": result.power_report,
                    "package": result.package_path,
                }.items()
                if value
            },
        )


def _normalise_project_path(path: str) -> str:
    return str(Path(path).resolve())


def _print_tool_progress(message: str) -> None:
    print(f"[FORGE] {message}", flush=True)


def _configure_console_encoding() -> None:
    """Keep the Unicode FORGE logo readable in Windows terminals."""

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _print_ai_summary(summary: str, solutions: list[object]) -> None:
    compact_summary = " ".join(summary.split())
    if compact_summary:
        print(f"[FORGE] AI summary: {compact_summary}")
    names = ", ".join(f"{item.rank}. {item.name}" for item in solutions)
    if names:
        print(f"[FORGE] AI design points: {names}")


def _print_ai_retry(attempt: int, error: str) -> None:
    print(f"[FORGE] AI recommendation: retry {attempt}/3 - {error}")


if __name__ == "__main__":
    raise SystemExit(main())
