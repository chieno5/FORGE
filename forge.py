from __future__ import annotations

import argparse
import hashlib
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
from analyzer import analyze_functions, find_structural_constraints
from forge_database import ForgeDatabase, build_evaluation_context_key
from models import AnalysisReport, FunctionAnalysis
from parser import CParserError, parse_c_file
from report import (
    print_human_report,
    write_data_report,
    write_json_report,
)
from scorer import score_report
from source_transformer import (
    SourceTransformation,
    TransformationAttempt,
    apply_reduction_preflight_transform,
)
from testbench_generator import DEFAULT_TEST_SEED, TESTBENCH_PROFILES
from vitis_generator import (
    TestbenchInput,
    VitisGenerationError,
    freeze_testbench,
    generate_vitis_projects,
)
from vitis_runner import (
    DEFAULT_TOOL_TIMEOUT_SECONDS,
    ExperimentResult,
    VitisExecutionError,
    package_best_project,
    run_baseline_preflight,
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
        "--exploration-mode",
        choices=("explore", "verify"),
        default="explore",
        help="Use new pragma plans for the same experiment context, or allow historical plan verification.",
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
        help="Generate a deterministic self-checking testbench when --generate is used.",
    )
    parser.add_argument(
        "--testbench-profile",
        choices=TESTBENCH_PROFILES,
        default="full",
        help="Coverage level for --auto-testbench. Default: full.",
    )
    parser.add_argument(
        "--testbench-seed",
        type=int,
        default=DEFAULT_TEST_SEED,
        help=f"Fixed random seed for --auto-testbench. Default: {DEFAULT_TEST_SEED}.",
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
    if args.testbench_seed < 0 or args.testbench_seed > 0xFFFFFFFF:
        print("Error: --testbench-seed must be between 0 and 4294967295.", file=sys.stderr)
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
        structural_constraints=find_structural_constraints(functions),
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

    original_source_path = Path(args.input)
    source_text = original_source_path.read_text(encoding="utf-8")
    application = classify_application(args.input, source_text, analysis_report)
    database = ForgeDatabase(args.database)
    preflight_projects: list[object] = []
    frozen_testbench: TestbenchInput | None = None
    original_baseline_schedule = None
    baseline_schedule = None
    active_source_path = original_source_path
    active_source_text = source_text
    active_report = analysis_report
    transformed_static_report_path: Path | None = None
    transformation_attempt = TransformationAttempt(
        False,
        source_text,
        "source preflight was not requested for this run",
    )
    try:
        top_function = _select_top_function(functions, args.top)
        part = args.part
        clock_period_ns = args.clock
        model = args.model
        if args.generate:
            frozen_testbench = freeze_testbench(
                original_source_path,
                analysis_report,
                top_function,
                testbench_path=args.testbench,
                auto_testbench=args.auto_testbench,
                testbench_profile=args.testbench_profile,
                testbench_seed=args.testbench_seed,
            )
            if args.auto_testbench and frozen_testbench:
                _print_tool_progress(
                    "Testbench preflight: frozen "
                    f"{frozen_testbench.manifest.get('profile')} profile with "
                    f"{frozen_testbench.manifest.get('case_count')} self-checking cases"
                )
        if args.generate and application.key == "reduction_dot":
            transformation_attempt = apply_reduction_preflight_transform(
                source_text,
                analysis_report,
                top_function,
            )
            if transformation_attempt.applied and transformation_attempt.transformation:
                candidate_path = _write_preflight_source(
                    args.output_root,
                    original_source_path,
                    transformation_attempt.transformation,
                    transformation_attempt.source_text,
                )
                try:
                    candidate_report = _analysis_report_for_source(
                        candidate_path,
                        [str(original_source_path.parent), *args.include_dir],
                        threshold,
                    )
                except CParserError as exc:
                    transformation_attempt = TransformationAttempt(
                        False,
                        source_text,
                        f"transformed source did not parse: {exc}",
                    )
                else:
                    if not _same_top_interface(
                        analysis_report, candidate_report, top_function
                    ):
                        transformation_attempt = TransformationAttempt(
                            False,
                            source_text,
                            "transformation changed the top function interface",
                        )
                    else:
                        active_source_path = candidate_path
                        active_source_text = transformation_attempt.source_text
                        active_report = candidate_report
                        transformed_static_report_path = (
                            REPORT_DIR
                            / f"{original_source_path.stem}_refactored_analysis_report.json"
                        )
                        write_json_report(active_report, transformed_static_report_path)
                        _print_tool_progress(
                            "Source preflight: applied controlled partial-accumulator "
                            f"refactor to {transformation_attempt.transformation.loop_id}"
                        )
            if not transformation_attempt.applied:
                _print_tool_progress(
                    f"Source preflight: no refactored baseline selected - {transformation_attempt.reason}"
                )

        testbench_signature = (
            "frozen:" + frozen_testbench.identity
            if frozen_testbench is not None
            else _testbench_signature(args)
        )
        original_context_key = build_evaluation_context_key(
            source_text,
            top_function,
            part,
            clock_period_ns,
            testbench_signature,
        )
        evaluation_context_key = build_evaluation_context_key(
            active_source_text,
            top_function,
            part,
            clock_period_ns,
            testbench_signature,
        )
        run = database.create_run(
            source_text,
            application.key,
            top_function,
            evaluation_context_key,
        )
        batch_number = database.reserve_generated_batch(run.id) if args.generate else None
        experience_context = database.history_context(
            application.key,
            source_text=active_source_text,
            evaluation_context_key=evaluation_context_key,
        )
        if args.generate and args.run_vitis:
            _print_tool_progress(
                "Baseline preflight: synthesizing the unmodified source before AI recommendation"
            )
            original_project = generate_vitis_projects(
                source_path=original_source_path,
                report=analysis_report,
                solutions=[],
                top_function=top_function,
                output_root=args.output_root,
                part=part,
                clock_period_ns=clock_period_ns,
                frozen_testbench=frozen_testbench,
                include_dirs=args.include_dir,
                batch_number=batch_number,
            )[0]
            preflight_projects.append(original_project)
            original_baseline_schedule = run_baseline_preflight(
                original_project,
                vitis_hls_command=args.vitis_hls,
                amd_root=args.amd_root,
                tool_timeout_seconds=args.tool_timeout,
                progress_callback=_print_tool_progress,
            )
            baseline_schedule = original_baseline_schedule
            if transformation_attempt.applied and transformation_attempt.transformation:
                refactored_project = generate_vitis_projects(
                    source_path=active_source_path,
                    report=active_report,
                    solutions=[],
                    top_function=top_function,
                    output_root=args.output_root,
                    part=part,
                    clock_period_ns=clock_period_ns,
                    frozen_testbench=frozen_testbench,
                    include_dirs=[str(original_source_path.parent), *args.include_dir],
                    batch_number=batch_number,
                    baseline_project_label="refactored_baseline",
                    baseline_display_name="Refactored Baseline",
                    baseline_result_kind="solution",
                    baseline_design_role="refactored_baseline",
                    baseline_record_key="refactored_baseline",
                    baseline_rationale=transformation_attempt.reason,
                    transformation=transformation_attempt.transformation.to_dict(),
                    project_root_name=original_source_path.stem,
                )[0]
                try:
                    refactored_schedule = run_baseline_preflight(
                        refactored_project,
                        vitis_hls_command=args.vitis_hls,
                        amd_root=args.amd_root,
                        tool_timeout_seconds=args.tool_timeout,
                        progress_callback=_print_tool_progress,
                    )
                except VitisExecutionError as exc:
                    transformation_attempt = TransformationAttempt(
                        False,
                        source_text,
                        f"refactored baseline failed HLS/testbench preflight: {exc}",
                    )
                    active_source_path = original_source_path
                    active_source_text = source_text
                    active_report = analysis_report
                    evaluation_context_key = original_context_key
                    database.update_run_context(run.id, evaluation_context_key)
                    experience_context = database.history_context(
                        application.key,
                        source_text=source_text,
                        evaluation_context_key=evaluation_context_key,
                    )
                    _print_tool_progress(
                        "Source preflight: refactored baseline rejected; using original baseline"
                    )
                else:
                    preflight_projects.append(refactored_project)
                    baseline_schedule = refactored_schedule
                    _print_tool_progress(
                        "Source preflight: refactored baseline passed the frozen-testbench "
                        "and HLS preflight"
                    )
            experience_context["baseline_schedule"] = baseline_schedule
            experience_context["source_preflight"] = _source_preflight_context(
                analysis_report,
                transformation_attempt,
                frozen_testbench,
                original_baseline_schedule,
                baseline_schedule,
            )
            _print_tool_progress(
                "Baseline preflight: achieved schedule added to the AI context"
            )
        if "source_preflight" not in experience_context:
            experience_context["source_preflight"] = _source_preflight_context(
                analysis_report,
                transformation_attempt,
                frozen_testbench,
                original_baseline_schedule,
                baseline_schedule,
            )
        state = experience_context.get("exploration_state", {})
        if args.exploration_mode == "explore" and state.get("converged"):
            _print_tool_progress(
                "Exploration state: design space is converged after "
                f"{state.get('stagnant_batches', 0)} stagnant batches; "
                "only bounded refinement or verification will be requested"
            )
        _print_tool_progress(
            f"AI recommendation: contacting OpenAI for {args.design_points} design points"
        )
        ai_result = recommend_solutions(
            active_report,
            top_function,
            part=part,
            clock_period_ns=clock_period_ns,
            design_point_count=args.design_points,
            model=model,
            experience_context=experience_context,
            retry_callback=_print_ai_retry,
            source_text=active_source_text,
            exploration_mode=args.exploration_mode,
        )
    except VitisExecutionError as exc:
        database.close()
        print(f"Baseline preflight error: {exc}", file=sys.stderr)
        return 5
    except (AIRecommendationError, ValueError, VitisGenerationError) as exc:
        database.close()
        print(f"AI recommendation error: {exc}", file=sys.stderr)
        return 3

    _print_tool_progress(
        f"AI recommendation: received {len(ai_result.solutions)} design points"
    )
    if ai_result.fallback_reason:
        fallback_count = sum("local_safe" in item.name for item in ai_result.solutions)
        if 0 < fallback_count < len(ai_result.solutions):
            _print_tool_progress(
                "AI recommendation: retained "
                f"{len(ai_result.solutions) - fallback_count} validated AI design point(s) "
                f"and used local safe fallback for {fallback_count} unrepaired point(s)"
            )
        else:
            _print_tool_progress(
                "AI recommendation: using local safe fallback after invalid AI responses"
            )
        _print_tool_progress(
            "Recommendation set: accepted; "
            f"{len(ai_result.solutions)} design points passed FORGE pre-generation validation"
        )
    else:
        _print_tool_progress(
            "AI recommendation: accepted; "
            f"{len(ai_result.solutions)} design points passed FORGE pre-generation validation"
        )
    _print_ai_summary(ai_result.summary, ai_result.solutions)

    report_stem = _batch_report_stem(Path(args.input).stem, batch_number)
    pragma_report_path = REPORT_DIR / f"{report_stem}_pragma_report.json"
    pragma_report = {
        "project": PROJECT_NAME,
        "source_file": str(Path(args.input)),
        "active_source_file": str(active_source_path),
        "top_function": top_function,
        "optimization_objective": "energy_lut_efficiency",
        "application": application.to_dict(),
        "analysis_run_id": run.id,
        "generation_batch": batch_number,
        "requested_design_point_count": args.design_points,
        "design_point_count": len(ai_result.solutions),
        "exploration_mode": args.exploration_mode,
        "evaluation_context_key": evaluation_context_key,
        "target_part": part,
        "clock_period_ns": clock_period_ns,
        "baseline_schedule": baseline_schedule or experience_context.get("baseline_schedule"),
        "original_baseline_schedule": original_baseline_schedule,
        "source_preflight": experience_context["source_preflight"],
        "testbench_identity": frozen_testbench.identity if frozen_testbench else None,
        "testbench_manifest": frozen_testbench.manifest if frozen_testbench else None,
        "exploration_state": experience_context.get("exploration_state"),
        "static_report": str(static_report_path) if static_report_path else None,
        "active_static_report": (
            str(transformed_static_report_path)
            if transformed_static_report_path and transformation_attempt.applied
            else str(static_report_path) if static_report_path else None
        ),
        "ai": ai_result.to_dict(),
        "generated_projects": [],
    }
    write_data_report(pragma_report, pragma_report_path)

    generated_projects = []
    experiment_results = []
    best_result = None
    batch_best_result = None
    selection_source = None
    if args.generate:
        try:
            generated_projects = list(preflight_projects)
            if not generated_projects:
                generated_projects.extend(generate_vitis_projects(
                    source_path=original_source_path,
                    report=analysis_report,
                    solutions=[],
                    top_function=top_function,
                    output_root=args.output_root,
                    part=part,
                    clock_period_ns=clock_period_ns,
                    frozen_testbench=frozen_testbench,
                    include_dirs=args.include_dir,
                    batch_number=batch_number,
                ))
                if transformation_attempt.applied and transformation_attempt.transformation:
                    generated_projects.extend(generate_vitis_projects(
                        source_path=active_source_path,
                        report=active_report,
                        solutions=[],
                        top_function=top_function,
                        output_root=args.output_root,
                        part=part,
                        clock_period_ns=clock_period_ns,
                        frozen_testbench=frozen_testbench,
                        include_dirs=[str(original_source_path.parent), *args.include_dir],
                        batch_number=batch_number,
                        baseline_project_label="refactored_baseline",
                        baseline_display_name="Refactored Baseline",
                        baseline_result_kind="solution",
                        baseline_design_role="refactored_baseline",
                        baseline_record_key="refactored_baseline",
                        baseline_rationale=transformation_attempt.reason,
                        transformation=transformation_attempt.transformation.to_dict(),
                        project_root_name=original_source_path.stem,
                    ))
            generated_projects.extend(generate_vitis_projects(
                source_path=active_source_path,
                report=active_report,
                solutions=ai_result.solutions,
                top_function=top_function,
                output_root=args.output_root,
                part=part,
                clock_period_ns=clock_period_ns,
                frozen_testbench=frozen_testbench,
                include_dirs=(
                    [str(original_source_path.parent), *args.include_dir]
                    if active_source_path != original_source_path else args.include_dir
                ),
                batch_number=batch_number,
                include_baseline=False,
                project_root_name=original_source_path.stem,
            ))
        except VitisGenerationError as exc:
            database.close()
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
                    reuse_hls_directories=[
                        project.directory for project in preflight_projects
                    ],
                )
            except VitisExecutionError as exc:
                print(f"Vitis execution error: {exc}", file=sys.stderr)
                pragma_report["execution_error"] = str(exc)
                write_data_report(pragma_report, pragma_report_path)
                database.close()
                return 5
            _record_experiment_results(
                database, design_point_ids, generated_projects, experiment_results
            )
            _print_vitis_validation_summary(experiment_results)
            try:
                batch_best_result = select_best_result(experiment_results)
                best_result, selection_source = _choose_overall_best(
                    batch_best_result, experience_context.get("incumbent_best")
                )
                if selection_source == "historical_overall_best":
                    _print_tool_progress(
                        "Current batch did not exceed the historical incumbent; "
                        f"using overall best {best_result.name} "
                        f"(efficiency_score={best_result.efficiency_score:.4f})"
                    )
                best_result = package_best_project(
                    best_result,
                    top_function,
                    vitis_hls_command=args.vitis_hls,
                    amd_root=args.amd_root,
                    tool_timeout_seconds=args.tool_timeout,
                    progress_callback=_print_tool_progress,
                )
                if selection_source == "current_batch":
                    experiment_results = [
                        best_result
                        if item.project_directory == best_result.project_directory else item
                        for item in experiment_results
                    ]
                    _record_experiment_results(
                        database, design_point_ids, generated_projects, experiment_results
                    )
                else:
                    historical_id = experience_context["incumbent_best"].get("id")
                    if historical_id is not None:
                        database.record_experiment(
                            int(historical_id), best_result.to_dict(), best_result.status
                        )
            except VitisExecutionError as exc:
                pragma_report["batch_evaluation"] = _batch_evaluation_summary(
                    experiment_results
                )
                pragma_report["execution_error"] = str(exc)
                write_data_report(pragma_report, pragma_report_path)
                print(f"Vitis execution error: {exc}", file=sys.stderr)
                database.close()
                return 5

    pragma_report["generated_projects"] = [
        item.to_dict() for item in generated_projects
    ]
    pragma_report["batch_evaluation"] = _batch_evaluation_summary(experiment_results)
    pragma_report["relative_gain_diagnostics"] = _relative_gain_diagnostics(
        experiment_results
    )
    pragma_report["batch_best_design_point"] = (
        batch_best_result.to_dict() if batch_best_result else None
    )
    pragma_report["best_design_point"] = best_result.to_dict() if best_result else None
    pragma_report["selection_source"] = selection_source
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
            f"Generated batch {batch_number:02d}: written to "
            f"{generated_projects[0].workspace_directory}"
        )
    if best_result:
        _print_tool_progress(
            f"Best design point: {best_result.name} | "
            f"efficiency_score={best_result.efficiency_score:.4f}"
        )
        _print_tool_progress(f"Final package: {best_result.package_path}")
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


def _analysis_report_for_source(
    source_path: str | Path,
    include_dirs: list[str | Path],
    threshold: int,
) -> AnalysisReport:
    parsed = parse_c_file(source_path, include_dirs)
    functions = analyze_functions(parsed.functions)
    score_report(functions, threshold)
    return AnalysisReport(
        file=str(Path(source_path)),
        threshold=threshold,
        functions=functions,
        limitations=[
            "Scores are based on static heuristics.",
            "Complex C syntax may need cleanup before analysis.",
        ],
        structural_constraints=find_structural_constraints(functions),
    )


def _write_preflight_source(
    output_root: str | Path,
    original_source: str | Path,
    transformation: SourceTransformation,
    source_text: str,
) -> Path:
    original = Path(original_source)
    directory = (
        Path(output_root)
        / original.stem
        / "preflight_sources"
        / transformation.transformed_source_hash[:12]
    )
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / original.name
    path.write_text(source_text, encoding="utf-8")
    return path


def _same_top_interface(
    original_report: AnalysisReport,
    transformed_report: AnalysisReport,
    top_function: str,
) -> bool:
    original = next(
        (item for item in original_report.functions if item.name == top_function), None
    )
    transformed = next(
        (item for item in transformed_report.functions if item.name == top_function), None
    )
    return bool(
        original
        and transformed
        and original.return_type == transformed.return_type
        and original.parameters == transformed.parameters
    )


def _source_preflight_context(
    report: AnalysisReport,
    attempt: TransformationAttempt,
    testbench: TestbenchInput | None,
    original_schedule: object | None,
    active_schedule: object | None,
) -> dict[str, Any]:
    manifest = testbench.manifest if testbench else {}
    return {
        "structural_constraints": [item.to_dict() for item in report.structural_constraints],
        "applied": attempt.applied,
        "reason": attempt.reason,
        "transformation": attempt.transformation.to_dict() if attempt.transformation else None,
        "testbench_identity": testbench.identity if testbench else None,
        "testbench": {
            "profile": manifest.get("profile"),
            "case_count": manifest.get("case_count"),
            "oracle": manifest.get("oracle"),
            "comparison": manifest.get("comparison"),
            "limitations": manifest.get("limitations", []),
        } if testbench else None,
        "original_baseline_schedule": original_schedule,
        "active_baseline_schedule": active_schedule,
    }


def _resolve_report_path(json_output: str) -> Path:
    filename = Path(json_output).name or "analysis_report.json"
    return REPORT_DIR / filename


def _batch_report_stem(source_stem: str, batch_number: int | None) -> str:
    if batch_number is None:
        return source_stem
    return f"{source_stem}_batch{batch_number:02d}"


def _record_experiment_results(
    database: ForgeDatabase,
    design_point_ids: dict[str, int],
    generated_projects: list[object],
    experiment_results: list[object],
) -> None:
    keys_by_directory = {
        _normalise_project_path(item.directory): item.record_key
        for item in generated_projects
    }
    for result in experiment_results:
        point_key = keys_by_directory.get(
            _normalise_project_path(result.project_directory)
        )
        if point_key is None:
            raise ValueError(
                "Experiment result does not match a generated design point: "
                f"{result.project_directory}"
            )
        database.record_experiment(
            design_point_ids[point_key],
            result.to_dict(),
            result.status,
        )


def _historical_experiment_result(record: dict[str, Any] | None) -> ExperimentResult | None:
    if not record or not record.get("project_path"):
        return None
    project_path = Path(str(record["project_path"]))
    if not project_path.is_dir():
        return None
    metrics = record.get("metrics") if isinstance(record.get("metrics"), dict) else {}

    def value(name: str, default: Any = None) -> Any:
        candidate = metrics.get(name)
        return record.get(name, default) if candidate is None else candidate

    return ExperimentResult(
        name=str(record.get("name") or project_path.name),
        kind=str(record.get("kind") or "solution"),
        project_directory=str(project_path),
        status="completed",
        latency_cycles=value("latency_cycles"),
        initiation_interval=value("initiation_interval"),
        clock_period_ns=value("clock_period_ns"),
        runtime_ns=value("runtime_ns"),
        performance=value("performance"),
        lut=value("lut"),
        ff=value("ff"),
        bram=value("bram"),
        dsp=value("dsp"),
        power_w=value("power_w"),
        energy_nj=value("energy_nj"),
        design_role=value("design_role", record.get("design_role", "candidate")),
        latency_source=value("latency_source"),
        hls_latency_cycles=value("hls_latency_cycles"),
        cosim_latency_cycles=value("cosim_latency_cycles"),
        efficiency_score=value("efficiency_score"),
        performance_norm=value("performance_norm"),
        power_norm=value("power_norm"),
        energy_norm=value("energy_norm"),
        lut_norm=value("lut_norm"),
        hls_report=value("hls_report"),
        cosim_report=value("cosim_report"),
        power_report=value("power_report"),
        package_path=value("package_path"),
        error=value("error"),
        pragma_validation=value("pragma_validation"),
        hls_schedule=value("hls_schedule"),
    )


def _batch_evaluation_summary(results: list[ExperimentResult]) -> dict[str, Any]:
    statuses: dict[str, int] = {}
    for result in results:
        statuses[result.status] = statuses.get(result.status, 0) + 1
    return {"design_points": len(results), "status_counts": statuses}


def _relative_gain_diagnostics(
    results: list[ExperimentResult],
) -> dict[str, Any] | None:
    reference = next(
        (
            item for item in results
            if item.design_role == "refactored_baseline"
            and item.status == "completed"
            and item.efficiency_score
        ),
        None,
    )
    if reference is None:
        return None
    candidates = []
    for item in results:
        if item.design_role != "candidate" or item.efficiency_score is None:
            continue
        candidates.append({
            "name": item.name,
            "efficiency_score_vs_original": item.efficiency_score,
            "relative_gain_vs_refactored_baseline": (
                item.efficiency_score / reference.efficiency_score
            ),
        })
    return {
        "refactored_baseline": reference.name,
        "refactored_baseline_efficiency_score": reference.efficiency_score,
        "candidates": candidates,
    }


def _choose_overall_best(
    batch_best: ExperimentResult,
    historical_record: dict[str, Any] | None,
) -> tuple[ExperimentResult, str]:
    historical = _historical_experiment_result(historical_record)
    if historical is None:
        return batch_best, "current_batch"
    if (historical.efficiency_score or float("-inf")) > (
        batch_best.efficiency_score or float("-inf")
    ):
        return historical, "historical_overall_best"
    return batch_best, "current_batch"


def _normalise_project_path(path: str) -> str:
    return str(Path(path).resolve())


def _testbench_signature(args: argparse.Namespace) -> str:
    if args.testbench:
        contents = Path(args.testbench).read_bytes()
        return "custom:" + hashlib.sha256(contents).hexdigest()
    if args.auto_testbench:
        return f"auto:{args.testbench_profile}:{args.testbench_seed}"
    return "none"


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
        print(f"[FORGE] AI summary: {_short_console_text(compact_summary)}")
    names = ", ".join(f"{item.rank}. {item.name}" for item in solutions)
    if names:
        print(f"[FORGE] AI design points: {names}")


def _short_console_text(value: str, limit: int = 240) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def _print_ai_retry(attempt: int, error: str) -> None:
    action = "targeted refine" if error.startswith("Targeted repair") else "retry"
    print(f"[FORGE] AI recommendation: {action} {attempt}/3 - {error}")


def _print_vitis_validation_summary(results: list[ExperimentResult]) -> None:
    candidates = [
        item for item in results
        if item.kind != "baseline" and getattr(
            item,
            "design_role",
            "candidate",
        ) == "candidate"
    ]
    if not candidates:
        return
    passed = sum(item.status == "completed" for item in candidates)
    rejected = len(candidates) - passed
    if rejected == 0:
        _print_tool_progress(
            "Recommendation evaluation: all "
            f"{passed} design points passed Vitis validation"
        )
        return
    _print_tool_progress(
        "Recommendation evaluation: "
        f"{passed}/{len(candidates)} design points passed Vitis validation; "
        f"{rejected} invalid/failed"
    )


if __name__ == "__main__":
    raise SystemExit(main())
