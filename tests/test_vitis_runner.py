import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from vitis_runner import (
    ExperimentResult,
    ToolInvocation,
    parse_cosim_report,
    parse_csynth_report,
    parse_csynth_schedule,
    parse_power_report,
    resolve_toolchain,
    run_experiments,
    select_best_result,
    _select_latency,
    validate_pragma_effectiveness,
)


class VitisRunnerTests(unittest.TestCase):
    def test_uses_unified_vitis_run_arguments(self) -> None:
        tool = ToolInvocation(["vitis-run"], hls_style="vitis-run")
        self.assertEqual(
            tool.hls_arguments("baseline/run_hls.tcl"),
            ["--mode", "hls", "--tcl", "baseline/run_hls.tcl"],
        )

    def test_parses_hls_and_power_reports(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            csynth = root / "csynth.xml"
            csynth.write_text(
                """<Report><PerformanceEstimates><SummaryOfOverallLatency>
                <Worst-caseLatency>120</Worst-caseLatency><Interval-max>2</Interval-max>
                </SummaryOfOverallLatency><SummaryOfTimingAnalysis>
                <EstimatedClockPeriod>8.5</EstimatedClockPeriod>
                </SummaryOfTimingAnalysis></PerformanceEstimates><AreaEstimates><Resources>
                <LUT>345</LUT><FF>678</FF><BRAM_18K>4</BRAM_18K><DSP>8</DSP>
                </Resources></AreaEstimates></Report>""",
                encoding="utf-8",
            )
            power = root / "power_report.rpt"
            power.write_text("| Total On-Chip Power (W) | 0.432 |\n", encoding="utf-8")

            metrics = parse_csynth_report(csynth)
            self.assertEqual(metrics["latency_cycles"], 120.0)
            self.assertEqual(metrics["initiation_interval"], 2.0)
            self.assertEqual(metrics["clock_period_ns"], 8.5)
            self.assertEqual(metrics["lut"], 345)
            self.assertEqual(parse_power_report(power), 0.432)

    def test_extracts_baseline_loop_schedule_for_ai_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            report = Path(temp_dir) / "csynth.xml"
            report.write_text(
                """<Report><PerformanceEstimates><PipelineType>loop</PipelineType>
                <SummaryOfOverallLatency><Worst-caseLatency>64</Worst-caseLatency>
                <Interval-max>1</Interval-max></SummaryOfOverallLatency>
                <SummaryOfTimingAnalysis><EstimatedClockPeriod>7.5</EstimatedClockPeriod></SummaryOfTimingAnalysis>
                <SummaryOfLoopLatency><Loop><Name>load_loop</Name><TripCount>16</TripCount>
                <Latency>19</Latency><PipelineII>1</PipelineII><PipelineDepth>4</PipelineDepth>
                <PerformancePragma>pipeline</PerformancePragma></Loop></SummaryOfLoopLatency>
                </PerformanceEstimates><AreaEstimates><Resources><LUT>100</LUT><FF>80</FF>
                <BRAM_18K>2</BRAM_18K><DSP>1</DSP></Resources></AreaEstimates></Report>""",
                encoding="utf-8",
            )
            schedule = parse_csynth_schedule(report)
            self.assertEqual(schedule["latency_cycles"], 64.0)
            self.assertEqual(schedule["loops"][0]["name"], "load_loop")
            self.assertEqual(schedule["loops"][0]["initiation_interval"], 1.0)
            self.assertEqual(schedule["resources"]["lut"], 100)

    def test_validates_requested_pipeline_against_hls_log(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "kernel.c"
            source.write_text(
                "void kernel(int input[16], int output[16]) {\n"
                "    for (int i = 0; i < 16; i++) {\n"
                "        #pragma HLS PIPELINE II=1\n"
                "        output[i] = input[i];\n"
                "    }\n}\n",
                encoding="utf-8",
            )
            csynth = root / "csynth.xml"
            csynth.write_text(
                "<profile><PragmaReport>"
                '<Pragma type="pipeline" status="valid"/>'
                "</PragmaReport></profile>",
                encoding="utf-8",
            )
            log = root / "vitis_hls.log"
            log.write_text(
                "Pipelining result : Target II = 1, Final II = 1, Depth = 4, "
                "loop 'VITIS_LOOP_2_1'\n",
                encoding="utf-8",
            )
            project = SimpleNamespace(
                source_file=str(source),
                pragmas=[{
                    "pragma": "#pragma HLS PIPELINE II=1",
                    "target_function": "kernel",
                    "target_loop_id": "kernel.loop_1",
                }],
            )

            validation = validate_pragma_effectiveness(project, csynth, log)

            self.assertEqual(validation["status"], "passed")
            self.assertEqual(validation["details"][0]["actual_ii"], 1)

    def test_maps_repeated_pipeline_text_to_each_target_loop(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "kernel.c"
            source.write_text(
                "void kernel(int input[16], int output[16]) {\n"
                "    for (int i = 0; i < 16; i++) {\n"
                "        #pragma HLS PIPELINE II=1\n"
                "        output[i] = input[i];\n"
                "    }\n"
                "    for (int i = 0; i < 16; i++) {\n"
                "        #pragma HLS PIPELINE II=1\n"
                "        output[i] += 1;\n"
                "    }\n}\n",
                encoding="utf-8",
            )
            csynth = root / "csynth.xml"
            csynth.write_text(
                "<profile><PragmaReport>"
                '<Pragma type="pipeline" status="valid"/>'
                '<Pragma type="pipeline" status="valid"/>'
                "</PragmaReport></profile>",
                encoding="utf-8",
            )
            log = root / "vitis_hls.log"
            log.write_text(
                "Pipelining result : Target II = 1, Final II = 1, Depth = 4, "
                "loop 'VITIS_LOOP_2_1'\n"
                "Pipelining result : Target II = 1, Final II = 1, Depth = 3, "
                "loop 'VITIS_LOOP_6_1'\n",
                encoding="utf-8",
            )
            pragma = "#pragma HLS PIPELINE II=1"
            project = SimpleNamespace(source_file=str(source), pragmas=[
                {"pragma": pragma, "target_function": "kernel", "target_loop_id": "kernel.loop_1"},
                {"pragma": pragma, "target_function": "kernel", "target_loop_id": "kernel.loop_2"},
            ])

            validation = validate_pragma_effectiveness(project, csynth, log)

            self.assertEqual(
                [item["hls_loop"] for item in validation["details"]],
                ["VITIS_LOOP_2_1", "VITIS_LOOP_6_1"],
            )

    def test_rejects_pipeline_that_did_not_create_a_pipelined_loop(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "kernel.c"
            source.write_text(
                "void kernel(void) {\n"
                "    for (int i = 0; i < 16; i++) {\n"
                "        #pragma HLS PIPELINE II=1\n"
                "    }\n}\n",
                encoding="utf-8",
            )
            csynth = root / "csynth.xml"
            csynth.write_text(
                "<profile><PragmaReport>"
                '<Pragma type="pipeline" status="valid"/>'
                "</PragmaReport></profile>",
                encoding="utf-8",
            )
            log = root / "vitis_hls.log"
            log.write_text("Unable to satisfy pipeline directive\n", encoding="utf-8")
            project = SimpleNamespace(
                source_file=str(source),
                pragmas=[{
                    "pragma": "#pragma HLS PIPELINE II=1",
                    "target_function": "kernel",
                    "target_loop_id": "kernel.loop_1",
                }],
            )

            validation = validate_pragma_effectiveness(project, csynth, log)

            self.assertEqual(validation["status"], "failed")
            self.assertIn("did not produce", validation["issues"][0])

    def test_selects_largest_efficiency_score(self) -> None:
        baseline = self._result("Baseline", "baseline", 1.0)
        first = self._result("First", "solution", 1.15)
        second = self._result("Second", "solution", 1.44)
        self.assertEqual(select_best_result([baseline, first, second]).name, "Second")

    def test_selects_baseline_when_all_candidates_are_less_efficient(self) -> None:
        baseline = self._result("Baseline", "baseline", 1.0)
        candidate = self._result("Candidate", "solution", 0.82)
        self.assertEqual(select_best_result([baseline, candidate]).name, "Baseline")

    def test_parses_cosim_latency_when_hls_latency_is_dynamic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            report = Path(temp_dir) / "kernel_cosim.rpt"
            report.write_text(
                "|   Verilog|      Pass|             64|             64|             64|"
                "             NA|             NA|             NA|                    64|\n",
                encoding="utf-8",
            )
            self.assertEqual(parse_cosim_report(report), (64.0, None))

    def test_uses_cosim_latency_only_for_a_user_supplied_testbench(self) -> None:
        self.assertEqual(
            _select_latency(120.0, 2.0, 64.0, None, True),
            (64.0, 2.0, "cosim"),
        )
        self.assertEqual(
            _select_latency(120.0, 2.0, 64.0, None, False),
            (120.0, 2.0, "hls_worst_case"),
        )

    def test_rejects_allocation_when_vitis_reports_no_matching_operation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            csynth = root / "csynth.xml"
            csynth.write_text(
                "<profile><PragmaReport>"
                '<Pragma type="allocation" status="valid"/>'
                "</PragmaReport></profile>",
                encoding="utf-8",
            )
            log = root / "vitis_hls.log"
            log.write_text(
                "WARNING: [SYN 201-223] cannot find any operation of 'mul'.\n",
                encoding="utf-8",
            )
            project = SimpleNamespace(
                pragmas=[{
                    "pragma": "#pragma HLS ALLOCATION operation instances=mul limit=1",
                    "target_function": "kernel",
                    "target_loop_id": "",
                }]
            )

            validation = validate_pragma_effectiveness(project, csynth, log)

            self.assertEqual(validation["status"], "failed")
            self.assertIn("did not match", validation["issues"][0])

    def test_validates_controlled_advanced_pragmas_from_csynth_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            csynth = root / "csynth.xml"
            csynth.write_text(
                "<profile><PragmaReport>"
                '<Pragma type="bind_storage" status="valid"/>'
                '<Pragma type="dataflow" status="valid"/>'
                "</PragmaReport></profile>",
                encoding="utf-8",
            )
            log = root / "vitis_hls.log"
            log.write_text("", encoding="utf-8")
            project = SimpleNamespace(pragmas=[
                {"pragma": "#pragma HLS BIND_STORAGE variable=cache type=ram_2p impl=bram", "target_function": "kernel", "target_loop_id": ""},
                {"pragma": "#pragma HLS DATAFLOW", "target_function": "kernel", "target_loop_id": ""},
            ])

            validation = validate_pragma_effectiveness(project, csynth, log)
            self.assertEqual(validation["status"], "passed")
            self.assertEqual(validation["validated"], 2)

    def test_detects_modern_amd_toolchain_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vitis = root / "Vitis"
            vivado = root / "Vivado"
            (vitis / "bin").mkdir(parents=True)
            (vivado / "bin").mkdir(parents=True)
            (vitis / "settings64.bat").write_text("", encoding="utf-8")
            (vitis / "bin" / "vitis-run.bat").write_text("", encoding="utf-8")
            (vivado / "bin" / "vivado.bat").write_text("", encoding="utf-8")
            hls, vivado_tool = resolve_toolchain(amd_root=root)
            self.assertEqual(hls.hls_style, "vitis-run")
            self.assertTrue(str(hls.setup_script).endswith("settings64.bat"))
            self.assertTrue(vivado_tool.command[0].endswith("vivado.bat"))

    def test_keeps_failed_candidate_results_for_database_recording(self) -> None:
        baseline = self._result("Baseline", "baseline", 1.0)
        failed = ExperimentResult(
            **{
                **self._result("Slow candidate", "solution", 1.0).to_dict(),
                "status": "failed",
                "efficiency_score": None,
                "error": "Command timed out after 600s",
            }
        )
        project = SimpleNamespace(name="Baseline", kind="baseline", directory="baseline")
        candidate = SimpleNamespace(name="Slow candidate", kind="solution", directory="candidate")
        tool = ToolInvocation(["unused"])
        with patch("vitis_runner.resolve_toolchain", return_value=(tool, tool)), patch(
            "vitis_runner._run_one", side_effect=[baseline, failed]
        ):
            results = run_experiments([project, candidate], "kernel", "xc7z020clg400-1")

        self.assertEqual([item.status for item in results], ["completed", "failed"])
        self.assertIsNone(results[1].efficiency_score)

    def test_reuses_only_the_preflight_baseline_hls_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            baseline_dir = Path(temp_dir) / "baseline"
            candidate_dir = Path(temp_dir) / "candidate"
            baseline_dir.mkdir()
            candidate_dir.mkdir()
            baseline = self._result("Baseline", "baseline", 1.0)
            candidate = self._result("Candidate", "solution", 0.8)
            projects = [
                SimpleNamespace(name="Baseline", kind="baseline", directory=str(baseline_dir)),
                SimpleNamespace(name="Candidate", kind="solution", directory=str(candidate_dir)),
            ]
            tool = ToolInvocation(["unused"])
            with patch("vitis_runner.resolve_toolchain", return_value=(tool, tool)), patch(
                "vitis_runner._run_one", side_effect=[baseline, candidate]
            ) as run_one:
                run_experiments(
                    projects, "kernel", "part",
                    reuse_hls_directories=[baseline_dir],
                )

            self.assertTrue(run_one.call_args_list[0].args[-1])
            self.assertFalse(run_one.call_args_list[1].args[-1])

    @staticmethod
    def _result(name: str, kind: str, score: float) -> ExperimentResult:
        return ExperimentResult(
            name=name,
            kind=kind,
            project_directory="project",
            status="completed",
            latency_cycles=100,
            initiation_interval=1,
            clock_period_ns=10,
            runtime_ns=1000,
            performance=0.001,
            lut=100,
            ff=100,
            bram=1,
            dsp=1,
            power_w=1,
            energy_nj=1000,
            efficiency_score=score,
        )


if __name__ == "__main__":
    unittest.main()
