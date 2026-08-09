import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_recommender import OptimizationSolution, PragmaDirective
from models import AnalysisReport, FunctionAnalysis, LoopRegion
from testbench_generator import generate_local_testbench
from vitis_generator import (
    VitisGenerationError,
    freeze_testbench,
    generate_vitis_projects,
)


class VitisGeneratorTests(unittest.TestCase):
    def test_frozen_auto_testbench_is_generated_once_and_reused(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "kernel.c"
            source.write_text(
                "void kernel(int input[16], int output[16]) {\n"
                "  for (int i = 0; i < 16; ++i) output[i] = input[i];\n"
                "}\n",
                encoding="utf-8",
            )
            function = FunctionAnalysis(
                "kernel", "void", ["int input[16]", "int output[16]"], {}, 1
            )
            report = AnalysisReport(str(source), 60, [function])
            solution = OptimizationSolution(
                rank=1,
                name="same_source",
                strategy="test",
                expected_effect="test",
                risk="low",
                confidence=1.0,
                pragmas=[],
            )

            with patch(
                "vitis_generator.generate_local_testbench",
                wraps=generate_local_testbench,
            ) as generator:
                frozen = freeze_testbench(source, report, "kernel", auto_testbench=True)
                baseline = generate_vitis_projects(
                    source,
                    report,
                    [],
                    "kernel",
                    output_root=root / "generated",
                    frozen_testbench=frozen,
                )
                candidates = generate_vitis_projects(
                    source,
                    report,
                    [solution],
                    "kernel",
                    output_root=root / "generated",
                    frozen_testbench=frozen,
                    include_baseline=False,
                )

            self.assertEqual(generator.call_count, 1)
            self.assertEqual(baseline[0].testbench_identity, candidates[0].testbench_identity)
            self.assertEqual(
                Path(baseline[0].testbench).read_bytes(),
                Path(candidates[0].testbench).read_bytes(),
            )

    def test_does_not_create_testbench_when_not_supplied(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "kernel.c"
            source.write_text(
                "void kernel(int input[16], int output[16]) {\n"
                "    for (int i = 0; i < 8; i++) { output[i] = input[i]; }\n"
                "\n"
                "\n"
                "    for (int i = 8; i < 16; i++) { output[i] = input[i]; }\n"
                "}\n",
                encoding="utf-8",
            )

            projects = generate_vitis_projects(
                source,
                self._report(),
                self._solutions(),
                "kernel",
                output_root=root / "generated",
            )

            for project in projects:
                self.assertIsNone(project.testbench)
                self.assertFalse((Path(project.directory) / "tb").exists())
                tcl = Path(project.tcl_script).read_text(encoding="utf-8")
                self.assertIn("cd $workspace_dir", tcl)
                self.assertIn(
                    f"open_component -reset [file join $workspace_dir "
                    f"{{{project.component_name}}}]",
                    tcl,
                )
                self.assertIn("set script_dir [file dirname [info script]]", tcl)
                self.assertNotIn("open_project", tcl)
                self.assertNotIn("open_solution", tcl)
                self.assertNotIn("csim_design", tcl)
                self.assertNotIn("cosim_design", tcl)
                self.assertIn("csynth_design", tcl)

    def test_generates_baseline_and_three_multi_pragma_projects(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "kernel.c"
            source.write_text(
                "void kernel(int input[16], int output[16]) {\n"
                "    for (int i = 0; i < 8; i++) {\n"
                "        output[i] = input[i] * 2;\n"
                "    }\n"
                "    for (int i = 8; i < 16; i++) {\n"
                "        output[i] = input[i] + 1;\n"
                "    }\n"
                "}\n",
                encoding="utf-8",
            )
            testbench = root / "kernel_tb.c"
            testbench.write_text("int main(void) { return 0; }\n", encoding="utf-8")

            projects = generate_vitis_projects(
                source,
                self._report(),
                self._solutions(),
                "kernel",
                output_root=root / "generated",
                testbench_path=testbench,
                batch_number=2,
            )

            self.assertEqual(len(projects), 4)
            self.assertEqual(projects[0].kind, "baseline")
            self.assertEqual(
                Path(projects[0].directory).parent,
                root / "generated" / "kernel",
            )
            self.assertEqual(Path(projects[0].directory).name, "batch02_baseline")
            self.assertTrue(Path(projects[1].directory).name.startswith("batch02_dp01_"))
            workspace = root / "generated" / "kernel"
            self.assertTrue((workspace / "forge_workspace.json").is_file())
            baseline = Path(projects[0].source_file).read_text(encoding="utf-8")
            self.assertNotIn("#pragma HLS", baseline)

            solution = Path(projects[1].source_file).read_text(encoding="utf-8")
            self.assertIn("#pragma HLS PIPELINE II=1", solution)
            self.assertIn("#pragma HLS UNROLL factor=2", solution)
            self.assertGreater(
                solution.index("#pragma HLS PIPELINE II=1"),
                solution.index("for (int i = 0"),
            )
            self.assertGreater(
                solution.index("#pragma HLS UNROLL factor=2"),
                solution.index("for (int i = 8"),
            )
            self.assertIn(
                "for (int i = 0; i < 8; i++) {\n"
                "        #pragma HLS PIPELINE II=1",
                solution,
            )
            self.assertIn(
                "for (int i = 8; i < 16; i++) {\n"
                "        #pragma HLS UNROLL factor=2",
                solution,
            )
            for project in projects:
                self.assertIsNotNone(project.testbench)
                component_metadata = Path(project.directory) / "vitis-comp.json"
                self.assertTrue(component_metadata.is_file())
                self.assertIn(project.component_name, component_metadata.read_text(encoding="utf-8"))
                self.assertTrue((Path(project.directory) / "hls_config.cfg").is_file())
                runner = Path(project.directory) / "run_hls.bat"
                self.assertIn("vitis-run --mode hls --tcl", runner.read_text(encoding="utf-8"))
                self.assertIn(
                    "csim_design",
                    Path(project.tcl_script).read_text(encoding="utf-8"),
                )

    def test_auto_testbench_and_removes_non_top_main(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "kernel.c"
            source.write_text(
                "typedef unsigned char pixel_t;\n"
                "\n"
                "void kernel(pixel_t input[16], pixel_t output[16], int n) {\n"
                "    for (int i = 0; i < 8; i++) { output[i] = input[i]; }\n"
                "\n"
                "\n"
                "    for (int i = 8; i < n; i++) { output[i] = input[i]; }\n"
                "}\n"
                "\n"
                "int main(void) {\n"
                "    return 0;\n"
                "}\n",
                encoding="utf-8",
            )
            report = AnalysisReport(
                file="kernel.c",
                threshold=60,
                functions=[
                    FunctionAnalysis(
                        name="kernel",
                        return_type="void",
                        parameters=[
                            "pixel_t input[16]",
                            "pixel_t output[16]",
                            "int n",
                        ],
                        features={},
                        source_line=3,
                        loop_regions=[
                            LoopRegion("kernel.loop_1", "for", 1, {}, source_line=4),
                            LoopRegion("kernel.loop_2", "for", 1, {}, source_line=7),
                        ],
                    ),
                    FunctionAnalysis(
                        name="main",
                        return_type="int",
                        parameters=[],
                        features={},
                        source_line=10,
                    ),
                ],
            )

            projects = generate_vitis_projects(
                source,
                report,
                self._solutions(),
                "kernel",
                output_root=root / "generated",
                auto_testbench=True,
            )

            for project in projects:
                generated_source = Path(project.source_file).read_text(encoding="utf-8")
                self.assertNotIn("int main(void)", generated_source)
                self.assertTrue(project.testbench_generated)
                testbench = Path(project.testbench)
                testbench_text = testbench.read_text(encoding="utf-8")
                self.assertIn("typedef unsigned char pixel_t;", testbench_text)
                self.assertIn("kernel(input_dut, output_dut, n);", testbench_text)
                self.assertTrue((testbench.parent / "kernel_golden.c").is_file())
                self.assertTrue((testbench.parent / "testbench_manifest.json").is_file())
                self.assertIn("csim_design", Path(project.tcl_script).read_text(encoding="utf-8"))

    def test_auto_testbench_identity_includes_profile_and_seed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "kernel.c"
            source.write_text("void kernel(int data[8]) { data[0] += 1; }\n", encoding="utf-8")
            report = AnalysisReport(
                str(source), 60, [FunctionAnalysis("kernel", "void", ["int data[8]"], {})]
            )

            smoke = freeze_testbench(
                source, report, "kernel", auto_testbench=True,
                testbench_profile="smoke", testbench_seed=1,
            )
            full = freeze_testbench(
                source, report, "kernel", auto_testbench=True,
                testbench_profile="full", testbench_seed=2,
            )

            self.assertNotEqual(smoke.identity, full.identity)
            self.assertEqual(smoke.manifest["case_count"], 1)
            self.assertEqual(full.manifest["case_count"], 13)

    def test_rejects_interface_as_unsupported_automatic_directive(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "kernel.c"
            source.write_text(
                "void kernel(int input[16], int output[16]) {\n"
                "#pragma HLS INTERFACE m_axi port=output bundle=gmem\n"
                "for (int i = 0; i < 16; i++) { output[i] = input[i]; }\n}\n",
                encoding="utf-8",
            )
            unsafe = [
                self._solution(1, "Override", [
                    self._directive("", "#pragma HLS INTERFACE m_axi port=output bundle=other"),
                ])
            ]
            with self.assertRaisesRegex(VitisGenerationError, "not enabled"):
                generate_vitis_projects(source, self._report(), unsafe, "kernel", root / "generated")

    def test_rejects_complete_partition_on_external_parameter(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "kernel.c"
            source.write_text(
                "void kernel(int input[16], int output[16]) {\n"
                "    for (int i = 0; i < 16; i++) { output[i] = input[i]; }\n}\n",
                encoding="utf-8",
            )
            unsafe = [
                self._solution(1, "Complete partition", [
                    self._directive("", "#pragma HLS ARRAY_PARTITION variable=output complete dim=1"),
                ])
            ]
            with self.assertRaisesRegex(VitisGenerationError, "ARRAY_PARTITION complete"):
                generate_vitis_projects(source, self._report(), unsafe, "kernel", root / "generated")

    def test_rejects_pipeline_on_outer_loop(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "kernel.c"
            source.write_text(
                "void kernel(int input[16], int output[16]) {\n"
                "    for (int i = 0; i < 16; i++) {\n"
                "        for (int j = 0; j < 2; j++) { output[i] += input[i]; }\n"
                "    }\n}\n",
                encoding="utf-8",
            )
            report = self._report()
            report.functions[0].loop_regions[1].depth = 2
            unsafe = [
                self._solution(1, "Outer pipeline", [
                    self._directive("kernel.loop_1", "#pragma HLS PIPELINE II=1"),
                ])
            ]
            with self.assertRaisesRegex(VitisGenerationError, "PIPELINE must target an innermost loop"):
                generate_vitis_projects(source, report, unsafe, "kernel", root / "generated")

    def test_rejects_dataflow_without_existing_helper_stages(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "kernel.c"
            source.write_text(
                "void kernel(int input[16], int output[16]) {\n"
                "  for (int i=0;i<16;i++) output[i]=input[i];\n}\n",
                encoding="utf-8",
            )
            unsafe = [self._solution(1, "Dataflow", [
                self._directive("", "#pragma HLS DATAFLOW"),
            ])]
            with self.assertRaisesRegex(VitisGenerationError, "at least two existing helper stages"):
                generate_vitis_projects(source, self._report(), unsafe, "kernel", root / "generated")

    def test_accepts_bind_storage_only_for_existing_local_array(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "kernel.c"
            source.write_text(
                "void kernel(int input[16], int output[16]) {\n"
                "  int cache[16];\n"
                "  for (int i=0;i<16;i++) cache[i]=input[i];\n"
                "  for (int i=0;i<16;i++) output[i]=cache[i];\n}\n",
                encoding="utf-8",
            )
            solution = [self._solution(1, "Local storage", [
                self._directive("", "#pragma HLS BIND_STORAGE variable=cache type=ram_2p impl=bram"),
            ])]
            projects = generate_vitis_projects(
                source, self._report(), solution, "kernel", root / "generated"
            )
            generated = Path(projects[1].source_file).read_text(encoding="utf-8")
            self.assertIn("#pragma HLS BIND_STORAGE variable=cache", generated)

    def test_recovers_loop_locations_after_header_line_offset(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "kernel.c"
            source.write_text(
                '#include "kernel.h"\n\n'
                "void kernel(int input[16], int output[16]) {\n"
                "    for (int i = 0; i < 8; i++) { output[i] = input[i]; }\n"
                "    for (int i = 8; i < 16; i++) { output[i] = input[i]; }\n"
                "}\n",
                encoding="utf-8",
            )
            (root / "kernel.h").write_text("#define KERNEL_SIZE 16\n", encoding="utf-8")
            report = AnalysisReport(
                file=str(source),
                threshold=60,
                functions=[
                    FunctionAnalysis(
                        name="kernel",
                        return_type="void",
                        parameters=["int input[16]", "int output[16]"],
                        features={},
                        source_line=20,
                        loop_regions=[
                            LoopRegion("kernel.loop_1", "for", 1, {}, source_line=21),
                            LoopRegion("kernel.loop_2", "for", 1, {}, source_line=22),
                        ],
                    )
                ],
            )
            solution = self._solution(1, "Offset", [
                self._directive("", "#pragma HLS ARRAY_PARTITION variable=input cyclic factor=2 dim=1"),
                self._directive("kernel.loop_1", "#pragma HLS PIPELINE II=1"),
            ])

            projects = generate_vitis_projects(source, report, [solution], "kernel", root / "generated")

            generated = Path(projects[1].source_file).read_text(encoding="utf-8")
            self.assertIn(
                "#pragma HLS ARRAY_PARTITION variable=input cyclic factor=2 dim=1\n"
                "    for (int i = 0; i < 8; i++) {\n"
                "        #pragma HLS PIPELINE II=1",
                generated,
            )

    def test_recursively_copies_quoted_header_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            include_dir = root / "include"
            include_dir.mkdir()
            (include_dir / "constants.h").write_text("#define KERNEL_SIZE 16\n", encoding="utf-8")
            (include_dir / "kernel.h").write_text(
                '#include "constants.h"\nvoid kernel(int input[KERNEL_SIZE], int output[KERNEL_SIZE]);\n',
                encoding="utf-8",
            )
            source = root / "kernel.c"
            source.write_text(
                '#include "include/kernel.h"\n'
                "void kernel(int input[16], int output[16]) {\n"
                "    for (int i = 0; i < 8; i++) { output[i] = input[i]; }\n"
                "    for (int i = 8; i < 16; i++) { output[i] = input[i]; }\n}\n",
                encoding="utf-8",
            )
            report = self._report()
            report.functions[0].loop_regions[0].source_line = 3
            report.functions[0].loop_regions[1].source_line = 4

            projects = generate_vitis_projects(
                source,
                report,
                self._solutions(),
                "kernel",
                output_root=root / "generated",
            )

            baseline_src = Path(projects[0].directory) / "src"
            self.assertTrue((baseline_src / "include" / "kernel.h").is_file())
            self.assertTrue((baseline_src / "include" / "constants.h").is_file())

    @staticmethod
    def _report() -> AnalysisReport:
        return AnalysisReport(
            file="kernel.c",
            threshold=60,
            functions=[
                FunctionAnalysis(
                    name="kernel",
                    return_type="void",
                    parameters=["int[16] input", "int[16] output"],
                    features={},
                    source_line=1,
                    loop_regions=[
                        LoopRegion("kernel.loop_1", "for", 1, {}, source_line=2),
                        LoopRegion("kernel.loop_2", "for", 1, {}, source_line=5),
                    ],
                )
            ],
        )

    @classmethod
    def _solutions(cls) -> list[OptimizationSolution]:
        return [
            cls._solution(
                1,
                "Dual loop",
                [
                    cls._directive("kernel.loop_1", "#pragma HLS PIPELINE II=1"),
                    cls._directive("kernel.loop_2", "#pragma HLS UNROLL factor=2"),
                ],
            ),
            cls._solution(
                2,
                "Reshape",
                [
                    cls._directive("", "#pragma HLS ARRAY_RESHAPE variable=output cyclic factor=2 dim=1"),
                    cls._directive("kernel.loop_1", "#pragma HLS UNROLL factor=2"),
                ],
            ),
            cls._solution(
                3,
                "Partition",
                [
                    cls._directive("", "#pragma HLS ARRAY_PARTITION variable=input cyclic factor=2 dim=1"),
                    cls._directive("kernel.loop_2", "#pragma HLS PIPELINE II=2"),
                ],
            ),
        ]

    @staticmethod
    def _solution(
        rank: int,
        name: str,
        pragmas: list[PragmaDirective],
    ) -> OptimizationSolution:
        return OptimizationSolution(
            rank=rank,
            name=name,
            strategy="Strategy",
            expected_effect="Effect",
            risk="Risk",
            confidence=0.8,
            pragmas=pragmas,
        )

    @staticmethod
    def _directive(loop_id: str, pragma: str) -> PragmaDirective:
        return PragmaDirective("kernel", loop_id, pragma, "Reason")


if __name__ == "__main__":
    unittest.main()
