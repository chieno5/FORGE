import json
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ai_recommender import (
    AIRecommendationResult,
    OptimizationSolution,
    PragmaDirective,
    recommend_solutions,
)
from analyzer import analyze_functions
from models import AnalysisReport
from parser import parse_c_file
from scorer import score_report
from testbench_generator import generate_local_testbench
from forge import (
    _choose_overall_best,
    _print_ai_summary,
    _print_vitis_validation_summary,
    _run_cli,
)
from vitis_generator import generate_vitis_projects
from vitis_runner import ExperimentResult, VitisExecutionError


class ForgePipelineTests(unittest.TestCase):
    def test_failed_refactored_preflight_falls_back_to_original_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "reduction_dot.c"
            source.write_text(
                "void reduction_dot(const int a[8], int out[1]) {\n"
                "  int sum = 0;\n"
                "  for (int i = 0; i < 8; ++i) { sum += a[i]; }\n"
                "  out[0] = sum;\n"
                "}\n",
                encoding="utf-8",
            )

            def fake_preflight(project, *_args, **_kwargs):
                if project.design_role == "refactored_baseline":
                    raise VitisExecutionError("testbench rejected the transformed source")
                return {"latency_cycles": 80, "loops": []}

            def fake_recommend(_report, *_args, **kwargs):
                self.assertNotIn("__forge_partial", kwargs["source_text"])
                self.assertFalse(kwargs["experience_context"]["source_preflight"]["applied"])
                return AIRecommendationResult(
                    model="test",
                    summary="use original source",
                    solutions=[OptimizationSolution(
                        rank=1, name="original_noop", strategy="safe",
                        expected_effect="none", risk="low", confidence=0.8, pragmas=[],
                    )],
                )

            def fake_run(projects, *_args, **_kwargs):
                self.assertEqual(
                    [project.design_role for project in projects],
                    ["original_baseline", "candidate"],
                )
                return [
                    ExperimentResult(**{
                        **self._experiment_result(
                            project.name, Path(project.directory),
                            1.0 if project.kind == "baseline" else 0.9,
                        ).to_dict(),
                        "kind": project.kind,
                        "design_role": project.design_role,
                    })
                    for project in projects
                ]

            with patch("forge.run_baseline_preflight", side_effect=fake_preflight), patch(
                "forge.recommend_solutions", side_effect=fake_recommend
            ), patch("forge.run_experiments", side_effect=fake_run), patch(
                "forge.package_best_project",
                side_effect=lambda result, *_args, **_kwargs: result,
            ), patch("forge.REPORT_DIR", root / "report"):
                code = _run_cli(
                    [
                        str(source), "--generate", "--run-vitis", "--auto-testbench",
                        "--design-points", "1", "--database", str(root / "forge.db"),
                        "--output-root", str(root / "generated"), "--top", "reduction_dot",
                    ],
                    False,
                    {},
                )

            self.assertEqual(code, 0)
            report_data = json.loads(
                (root / "report" / "reduction_dot_batch01_pragma_report.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertFalse(report_data["source_preflight"]["applied"])
            self.assertIn("failed", report_data["source_preflight"]["reason"])

    def test_reduction_preflight_builds_one_comparable_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "reduction_dot.c"
            source.write_text(
                "void reduction_dot(const int a[16], int result[1], int n) {\n"
                "  int sum = 0;\n"
                "  for (int i = 0; i < n; ++i) { sum += a[i]; }\n"
                "  result[0] = sum;\n"
                "}\n",
                encoding="utf-8",
            )
            events: list[str] = []
            seen_projects: list[object] = []

            def fake_preflight(project, *_args, **_kwargs):
                events.append(f"preflight:{project.design_role}")
                return {
                    "latency_cycles": 100 if project.design_role == "original_baseline" else 60,
                    "loops": [],
                }

            def fake_recommend(report, *_args, **kwargs):
                events.append("recommend")
                context = kwargs["experience_context"]["source_preflight"]
                self.assertTrue(context["applied"])
                self.assertEqual(context["testbench"]["profile"], "full")
                self.assertEqual(context["testbench"]["case_count"], 13)
                self.assertEqual(context["testbench"]["oracle"], "original_b0_source")
                self.assertIn("sum__forge_partial", kwargs["source_text"])
                self.assertEqual(
                    context["structural_constraints"][0]["constraint_type"],
                    "scalar_loop_carried_dependency",
                )
                self.assertFalse(
                    report.functions[0].loop_regions[0].features[
                        "has_scalar_loop_carried_dependency"
                    ]
                )
                return AIRecommendationResult(
                    model="test",
                    summary="pipeline the refactored reduction",
                    solutions=[OptimizationSolution(
                        rank=1,
                        name="refactored_pipeline",
                        strategy="pipeline exposed lanes",
                        expected_effect="lower II",
                        risk="low",
                        confidence=0.9,
                        pragmas=[PragmaDirective(
                            "reduction_dot",
                            "reduction_dot.loop_1",
                            "#pragma HLS PIPELINE II=1",
                            "the scalar recurrence was split",
                        )],
                    )],
                )

            def fake_run(projects, *_args, **_kwargs):
                seen_projects.extend(projects)
                scores = {
                    "original_baseline": 1.0,
                    "refactored_baseline": 1.2,
                    "candidate": 1.5,
                }
                return [
                    ExperimentResult(**{
                        **self._experiment_result(
                            project.name,
                            Path(project.directory),
                            scores[project.design_role],
                        ).to_dict(),
                        "kind": project.kind,
                        "design_role": project.design_role,
                    })
                    for project in projects
                ]

            def fake_package(result, *_args, **_kwargs):
                return ExperimentResult(**{
                    **result.to_dict(),
                    "package_path": str(root / "best.zip"),
                })

            with patch("forge.run_baseline_preflight", side_effect=fake_preflight), patch(
                "forge.recommend_solutions", side_effect=fake_recommend
            ), patch("forge.run_experiments", side_effect=fake_run), patch(
                "forge.package_best_project", side_effect=fake_package
            ), patch(
                "vitis_generator.generate_local_testbench",
                wraps=generate_local_testbench,
            ) as testbench_generator, patch("forge.REPORT_DIR", root / "report"):
                code = _run_cli(
                    [
                        str(source), "--generate", "--run-vitis", "--auto-testbench",
                        "--design-points", "1", "--database", str(root / "forge.db"),
                        "--output-root", str(root / "generated"), "--top", "reduction_dot",
                    ],
                    show_banner=False,
                    config={},
                )

            self.assertEqual(code, 0)
            self.assertEqual(
                events,
                [
                    "preflight:original_baseline",
                    "preflight:refactored_baseline",
                    "recommend",
                ],
            )
            self.assertEqual(testbench_generator.call_count, 1)
            self.assertEqual(
                {project.design_role for project in seen_projects},
                {"original_baseline", "refactored_baseline", "candidate"},
            )
            self.assertEqual(
                len({project.testbench_identity for project in seen_projects}),
                1,
            )

            from forge_database import ForgeDatabase
            database = ForgeDatabase(root / "forge.db")
            rows = database.connection.execute(
                "SELECT id, design_role, parent_experiment_id, root_baseline_id, "
                "efficiency_score FROM experiments ORDER BY id"
            ).fetchall()
            self.assertEqual([row["design_role"] for row in rows], [
                "original_baseline", "refactored_baseline", "candidate"
            ])
            self.assertEqual(rows[1]["parent_experiment_id"], rows[0]["id"])
            self.assertEqual(rows[2]["parent_experiment_id"], rows[1]["id"])
            database.close()

            report_data = json.loads(
                (root / "report" / "reduction_dot_batch01_pragma_report.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertAlmostEqual(
                report_data["relative_gain_diagnostics"]["candidates"][0][
                    "relative_gain_vs_refactored_baseline"
                ],
                1.25,
            )
            self.assertEqual(report_data["testbench_manifest"]["case_count"], 13)

    def test_ai_console_summary_is_bounded(self) -> None:
        solution = SimpleNamespace(rank=1, name="bounded")
        output = io.StringIO()
        with redirect_stdout(output):
            _print_ai_summary("x" * 400, [solution])

        lines = output.getvalue().splitlines()
        self.assertTrue(lines[0].endswith("..."))
        self.assertLessEqual(len(lines[0]), len("[FORGE] AI summary: ") + 240)
        self.assertEqual(lines[1], "[FORGE] AI design points: 1. bounded")

    def test_vitis_validation_summary_distinguishes_partial_success(self) -> None:
        results = [
            SimpleNamespace(kind="baseline", status="completed"),
            SimpleNamespace(kind="solution", status="completed"),
            SimpleNamespace(kind="solution", status="invalid"),
        ]
        output = io.StringIO()
        with redirect_stdout(output):
            _print_vitis_validation_summary(results)

        self.assertIn(
            "1/2 design points passed Vitis validation; 1 invalid/failed",
            output.getvalue(),
        )

    def test_unclassified_source_completes_ai_only_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "custom_kernel.c"
            source.write_text(
                "void custom_kernel(int value[4]) { value[0] += 1; }\n",
                encoding="utf-8",
            )
            recommendation = AIRecommendationResult(
                model="test",
                summary="No safe pragma needed.",
                solutions=[OptimizationSolution(
                    rank=1,
                    name="no_change",
                    strategy="Retain the current structure.",
                    expected_effect="No regression.",
                    risk="Low.",
                    confidence=0.8,
                    pragmas=[],
                )],
            )

            console = io.StringIO()
            with patch("forge.recommend_solutions", return_value=recommendation), patch(
                "forge.REPORT_DIR", root / "report"
            ), redirect_stdout(console):
                code = _run_cli(
                    [str(source), "--ai", "--design-points", "1",
                     "--database", str(root / "forge.db"), "--top", "custom_kernel"],
                    show_banner=False,
                    config={},
                )

            self.assertEqual(code, 0)
            report_data = json.loads(
                (root / "report" / "custom_kernel_pragma_report.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(report_data["application"]["key"], "unclassified")
            self.assertIn(
                "AI recommendation: accepted; 1 design points passed FORGE pre-generation validation",
                console.getvalue(),
            )

    def test_baseline_schedule_is_available_before_ai_recommendation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "vector_saxpy.c"
            source.write_text(
                "void saxpy(float alpha, float input[16], float y[16], float output[16]) {\n"
                "  for (int i=0;i<16;i++) {\n"
                "    output[i]=alpha*input[i]+y[i];\n"
                "  }\n}\n",
                encoding="utf-8",
            )
            events: list[str] = []

            def fake_preflight(*_args, **_kwargs):
                events.append("preflight")
                return {"latency_cycles": 20, "loops": [{"name": "loop", "initiation_interval": 1}]}

            def fake_recommend(*_args, **kwargs):
                events.append("recommend")
                self.assertEqual(kwargs["experience_context"]["baseline_schedule"]["latency_cycles"], 20)
                return AIRecommendationResult(
                    model="test",
                    summary="safe",
                    solutions=[OptimizationSolution(
                        rank=1, name="safe", strategy="safe", expected_effect="safe",
                        risk="low", confidence=0.8,
                        pragmas=[
                            PragmaDirective("saxpy", "saxpy.loop_1", "#pragma HLS PIPELINE II=1", "safe"),
                            PragmaDirective("saxpy", "", "#pragma HLS ARRAY_PARTITION variable=input cyclic factor=2 dim=1", "safe"),
                        ],
                    )],
                )

            def fake_run(projects, *_args, **_kwargs):
                return [
                    ExperimentResult(**{
                        **self._experiment_result(
                            project.name, Path(project.directory),
                            1.0 if project.kind == "baseline" else 0.8,
                        ).to_dict(),
                        "kind": project.kind,
                    })
                    for project in projects
                ]

            def fake_package(result, *_args, **_kwargs):
                return ExperimentResult(**{**result.to_dict(), "package_path": str(root / "best.zip")})

            with patch("forge.run_baseline_preflight", side_effect=fake_preflight), patch(
                "forge.recommend_solutions", side_effect=fake_recommend
            ), patch("forge.run_experiments", side_effect=fake_run), patch(
                "forge.package_best_project", side_effect=fake_package
            ), patch("forge.REPORT_DIR", root / "report"):
                code = _run_cli(
                    [str(source), "--generate", "--run-vitis", "--auto-testbench",
                     "--design-points", "1", "--database", str(root / "forge.db"),
                     "--output-root", str(root / "generated"), "--top", "saxpy"],
                    show_banner=False,
                    config={},
                )

            self.assertEqual(code, 0)
            self.assertEqual(events, ["preflight", "recommend"])
            report_data = json.loads(
                (root / "report" / "vector_saxpy_batch01_pragma_report.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertNotIn("experiment_results", report_data)
            self.assertEqual(report_data["batch_evaluation"]["design_points"], 2)
            self.assertEqual(report_data["best_design_point"]["kind"], "baseline")

            from forge_database import ForgeDatabase
            database = ForgeDatabase(root / "forge.db")
            self.assertEqual(
                database.connection.execute("SELECT COUNT(*) FROM experiments").fetchone()[0],
                2,
            )
            self.assertEqual(
                database.connection.execute(
                    "SELECT COUNT(*) FROM experiments WHERE status='completed'"
                ).fetchone()[0],
                2,
            )
            database.close()

    def test_historical_incumbent_wins_over_a_worse_current_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            historical_project = Path(temp_dir) / "historical_best"
            historical_project.mkdir()
            batch = self._experiment_result("current", Path(temp_dir) / "current", 0.9)
            historical = {
                "name": "historical",
                "kind": "solution",
                "project_path": str(historical_project),
                "efficiency_score": 1.5,
                "metrics": {**batch.to_dict(), "efficiency_score": 1.5},
            }
            selected, source = _choose_overall_best(batch, historical)
            self.assertEqual(selected.name, "historical")
            self.assertEqual(source, "historical_overall_best")

    def test_current_batch_wins_when_it_improves_on_historical_incumbent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            historical_project = Path(temp_dir) / "historical"
            historical_project.mkdir()
            batch = self._experiment_result("current", Path(temp_dir) / "current", 1.6)
            historical = {
                "name": "historical", "kind": "solution",
                "project_path": str(historical_project), "efficiency_score": 1.5,
                "metrics": {**batch.to_dict(), "efficiency_score": 1.5},
            }
            selected, source = _choose_overall_best(batch, historical)
            self.assertEqual(selected.name, "current")
            self.assertEqual(source, "current_batch")

    def test_second_cli_batch_packages_historical_overall_best(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "vector_saxpy.c"
            source.write_text(
                "void saxpy(float a, float x[8], float y[8], float out[8]) {\n"
                "  for (int i=0;i<8;i++) { out[i]=a*x[i]+y[i]; }\n}\n",
                encoding="utf-8",
            )
            recommendation = AIRecommendationResult(
                model="test", summary="safe",
                solutions=[OptimizationSolution(
                    rank=1, name="safe", strategy="safe", expected_effect="safe",
                    risk="low", confidence=0.8,
                    pragmas=[
                        PragmaDirective("saxpy", "saxpy.loop_1", "#pragma HLS PIPELINE II=1", "safe"),
                        PragmaDirective("saxpy", "", "#pragma HLS ARRAY_PARTITION variable=x cyclic factor=2 dim=1", "safe"),
                    ],
                )],
            )
            run_count = 0
            packaged_directories: list[str] = []

            def fake_run(projects, *_args, **_kwargs):
                nonlocal run_count
                candidate_score = (1.5, 0.8)[run_count]
                run_count += 1
                return [
                    ExperimentResult(**{
                        **self._experiment_result(
                            project.name, Path(project.directory),
                            1.0 if project.kind == "baseline" else candidate_score,
                        ).to_dict(),
                        "kind": project.kind,
                    })
                    for project in projects
                ]

            def fake_package(result, *_args, **_kwargs):
                packaged_directories.append(result.project_directory)
                return ExperimentResult(**{
                    **result.to_dict(),
                    "package_path": str(root / f"package_{len(packaged_directories)}.zip"),
                })

            arguments = [
                str(source), "--generate", "--run-vitis", "--auto-testbench",
                "--design-points", "1", "--database", str(root / "forge.db"),
                "--output-root", str(root / "generated"), "--top", "saxpy",
            ]
            with patch("forge.run_baseline_preflight", return_value={"loops": []}), patch(
                "forge.recommend_solutions", return_value=recommendation
            ), patch("forge.run_experiments", side_effect=fake_run), patch(
                "forge.package_best_project", side_effect=fake_package
            ), patch("forge.REPORT_DIR", root / "report"):
                self.assertEqual(_run_cli(arguments, False, {}), 0)
                self.assertEqual(_run_cli(arguments, False, {}), 0)

            second_report = json.loads(
                (root / "report" / "vector_saxpy_batch02_pragma_report.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(second_report["selection_source"], "historical_overall_best")
            self.assertIn("batch01_dp01", packaged_directories[1])
            self.assertEqual(second_report["batch_best_design_point"]["efficiency_score"], 1.0)
            self.assertEqual(second_report["best_design_point"]["efficiency_score"], 1.5)

    def test_real_example_generates_baseline_and_three_solutions(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        source = project_root / "examples" / "vision_pipeline.c"
        parsed = parse_c_file(source)
        functions = analyze_functions(parsed.functions)
        score_report(functions, 60)
        report = AnalysisReport(str(source), 60, functions)
        payload = {
            "summary": "Three performance-oriented whole-design solutions",
            "solutions": [
                self._solution(1, "Pipeline kernels", [
                    self._pragma("rgb_to_luma", "rgb_to_luma.loop_1", "#pragma HLS PIPELINE II=1"),
                    self._pragma("sobel_edges", "sobel_edges.loop_4", "#pragma HLS PIPELINE II=1"),
                ]),
                self._solution(2, "Unroll windows", [
                    self._pragma("box_blur_3x3", "box_blur_3x3.loop_4", "#pragma HLS UNROLL"),
                    self._pragma("sobel_edges", "sobel_edges.loop_4", "#pragma HLS UNROLL factor=3"),
                ]),
                self._solution(3, "Top dataflow", [
                    self._pragma("inspect_frame", "", "#pragma HLS DATAFLOW"),
                    self._pragma("box_blur_3x3", "box_blur_3x3.loop_4", "#pragma HLS PIPELINE II=1"),
                ]),
            ],
        }

        class FakeResponses:
            @staticmethod
            def create(**_kwargs):
                return SimpleNamespace(output_text=json.dumps(payload))

        result = recommend_solutions(
            report,
            "inspect_frame",
            part="xc7z020clg400-1",
            clock_period_ns=10.0,
            model="test-model",
            client=SimpleNamespace(responses=FakeResponses()),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            projects = generate_vitis_projects(
                source,
                report,
                result.solutions,
                "inspect_frame",
                output_root=Path(temp_dir),
                testbench_path=project_root / "examples" / "vision_pipeline_tb.c",
            )

            self.assertEqual(len(projects), 4)
            self.assertEqual(projects[0].kind, "baseline")
            self.assertTrue(all(Path(item.source_file).exists() for item in projects))
            self.assertEqual(len(projects[1].pragmas), 2)

    @staticmethod
    def _solution(rank: int, name: str, pragmas: list[dict]) -> dict:
        return {
            "rank": rank,
            "name": name,
            "strategy": "Strategy",
            "expected_effect": "Effect",
            "risk": "Risk",
            "confidence": 0.8,
            "pragmas": pragmas,
        }

    @staticmethod
    def _pragma(function: str, loop_id: str, pragma: str) -> dict:
        return {
            "target_function": function,
            "target_loop_id": loop_id,
            "pragma": pragma,
            "rationale": "Reason",
        }

    @staticmethod
    def _experiment_result(name: str, directory: Path, score: float) -> ExperimentResult:
        return ExperimentResult(
            name=name, kind="solution", project_directory=str(directory), status="completed",
            latency_cycles=10, initiation_interval=1, clock_period_ns=10, runtime_ns=100,
            performance=0.01, lut=100, ff=100, bram=1, dsp=1, power_w=1,
            energy_nj=100, efficiency_score=score,
        )


if __name__ == "__main__":
    unittest.main()
