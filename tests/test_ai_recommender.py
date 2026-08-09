import json
import re
import unittest
from copy import deepcopy
from types import SimpleNamespace

from ai_recommender import (
    AIRecommendationError,
    RESPONSE_SCHEMA,
    parse_solution_payload,
    recommend_solutions,
)
from models import AnalysisReport, FunctionAnalysis, LoopRegion


class AIRecommendationTests(unittest.TestCase):
    def test_response_schema_has_valid_object_structure(self) -> None:
        self.assertEqual(RESPONSE_SCHEMA["type"], "object")
        self.assertEqual(RESPONSE_SCHEMA["required"], ["summary", "solutions"])
        self.assertNotIn("required", RESPONSE_SCHEMA["properties"])
        self.assertFalse(RESPONSE_SCHEMA["additionalProperties"])

    def test_sends_energy_objective_and_receives_three_complete_solutions(self) -> None:
        payload = self._payload()

        class FakeResponses:
            def __init__(self) -> None:
                self.arguments = None

            def create(self, **kwargs):
                self.arguments = kwargs
                return SimpleNamespace(output_text=json.dumps(payload))

        responses = FakeResponses()
        result = recommend_solutions(
            self._report(),
            "kernel",
            part="xc7z020clg400-1",
            clock_period_ns=10.0,
            model="test-model",
            client=SimpleNamespace(responses=responses),
            source_text="void kernel(void) {}",
        )

        self.assertEqual(len(result.solutions), 3)
        self.assertGreaterEqual(len(result.solutions[0].pragmas), 2)
        self.assertEqual(
            result.solutions[0].pragmas[0].pragma,
            "#pragma HLS PIPELINE II=1",
        )
        sent = json.loads(responses.arguments["input"])
        self.assertEqual(
            sent["optimization_objective"],
            "maximize energy-LUT efficiency_score after Vitis evaluation (minimize candidate energy multiplied by LUT use relative to baseline)",
        )
        self.assertEqual(sent["target_part"], "xc7z020clg400-1")
        self.assertEqual(sent["source_code"], "void kernel(void) {}")
        self.assertEqual(
            sent["pragma_constraints"]["pipeline_allowed_loop_ids"]["kernel"],
            ["kernel.loop_1", "kernel.loop_2"],
        )
        self.assertIn(
            "__forge_",
            sent["pragma_constraints"]["reserved_generated_identifier_rule"],
        )
        self.assertIn("Never target", responses.arguments["instructions"])

    def test_parses_and_ranks_solutions(self) -> None:
        result = parse_solution_payload(self._payload(), self._report(), "kernel")
        self.assertEqual([item.rank for item in result], [1, 2, 3])
        self.assertEqual(
            [item.name for item in result],
            ["dp01_Dual pipeline", "dp02_Selective unroll", "dp03_Memory balance"],
        )

    def test_resets_historical_design_point_name(self) -> None:
        payload = self._payload()
        payload["solutions"][1]["name"] = "dp16_balanced_pipeline"
        result = parse_solution_payload(payload, self._report(), "kernel")
        self.assertEqual(result[0].name, "dp01_balanced_pipeline")

    def test_derives_descriptive_name_when_ai_returns_only_dp_number(self) -> None:
        payload = self._payload()
        payload["solutions"][1]["name"] = "dp01"
        payload["solutions"][1]["strategy"] = "Balanced MAC pipeline with banking"
        result = parse_solution_payload(payload, self._report(), "kernel")
        self.assertEqual(result[0].name, "dp01_balanced_mac_pipeline_with_banking")

    def test_accepts_requested_design_point_count(self) -> None:
        payload = self._payload()
        payload["solutions"] = payload["solutions"][:2]
        result = parse_solution_payload(
            payload,
            self._report(),
            "kernel",
            design_point_count=2,
        )
        self.assertEqual([item.rank for item in result], [1, 2])

    def test_rejects_pipeline_on_outer_loop(self) -> None:
        report = self._report()
        report.functions[0].loop_regions[1].depth = 2
        payload = self._payload()
        payload["solutions"][0]["pragmas"] = [
            self._pragma("kernel.loop_1", "#pragma HLS PIPELINE II=1"),
            self._pragma("kernel.loop_2", "#pragma HLS UNROLL factor=2"),
        ]
        with self.assertRaisesRegex(AIRecommendationError, "innermost"):
            parse_solution_payload(payload, report, "kernel")

    def test_rejects_pipeline_and_unroll_on_loop_carried_dependency(self) -> None:
        report = self._report()
        report.functions[0].loop_regions[0].features = {
            "has_loop_carried_dependency": True,
            "pipeline_eligible": False,
            "unroll_eligible": False,
        }
        payload = self._payload()
        payload["solutions"][0]["pragmas"] = [
            self._pragma("kernel.loop_1", "#pragma HLS PIPELINE II=1"),
            self._pragma("kernel.loop_2", "#pragma HLS ARRAY_PARTITION variable=input cyclic factor=2"),
        ]
        with self.assertRaisesRegex(AIRecommendationError, "loop-carried dependency"):
            parse_solution_payload(payload, report, "kernel")

        payload["solutions"][0]["pragmas"] = [
            self._pragma("kernel.loop_1", "#pragma HLS UNROLL factor=2"),
            self._pragma("kernel.loop_2", "#pragma HLS ARRAY_PARTITION variable=input cyclic factor=2"),
        ]
        with self.assertRaisesRegex(AIRecommendationError, "loop-carried dependency"):
            parse_solution_payload(payload, report, "kernel")

    def test_allows_bounded_unroll_on_supported_scalar_reduction(self) -> None:
        report = self._report()
        report.functions[0].loop_regions[0].features = {
            "has_loop_carried_dependency": True,
            "pipeline_eligible": False,
            "unroll_eligible": False,
            "dependency_arrays": [],
            "scalar_recurrences": [{"variable": "sum", "operation": "add"}],
        }
        payload = self._payload()
        payload["solutions"][0]["pragmas"] = [
            self._pragma("kernel.loop_1", "#pragma HLS UNROLL factor=2"),
            self._pragma("", "#pragma HLS ARRAY_PARTITION variable=input cyclic factor=2 dim=1"),
        ]
        payload["solutions"][0]["rank"] = 1
        payload["solutions"] = payload["solutions"][:1]

        result = parse_solution_payload(payload, report, "kernel", design_point_count=1)

        self.assertEqual(result[0].pragmas[0].pragma, "#pragma HLS UNROLL factor=2")

        payload["solutions"][0]["pragmas"][0]["pragma"] = "#pragma HLS UNROLL factor=8"
        with self.assertRaisesRegex(AIRecommendationError, "loop-carried dependency"):
            parse_solution_payload(payload, report, "kernel", design_point_count=1)

    def test_replays_best_compatible_same_source_plan_without_ai_call(self) -> None:
        report = self._report()
        history_plan = self._payload()["solutions"][0]["pragmas"]
        context = {
            "current_source_hash": "same-source",
            "evaluation_context_key": "new-context",
            "current_source_plans": [],
            "historical_replay_candidates": [{
                "name": "old_winner",
                "efficiency_score": 1.5,
                "pragmas": history_plan,
                "pragma_plan": {"strategy": "Previously measured winner."},
            }],
        }

        result = recommend_solutions(
            report,
            "kernel",
            part="xc7z020clg400-1",
            clock_period_ns=10.0,
            design_point_count=1,
            model="test-model",
            experience_context=context,
            source_text=(
                "void kernel(int input[16], int output[16]) {\n"
                "  for (int i=0;i<16;i++) output[i]=input[i];\n"
                "  for (int i=0;i<16;i++) output[i]+=1;\n"
                "}\n"
            ),
        )

        self.assertIn("historical_replay", result.solutions[0].name)
        self.assertIn("current frozen testbench", result.summary)

    def test_allows_independent_loop_before_nested_loop(self) -> None:
        report = self._report()
        report.functions[0].loop_regions.extend([
            LoopRegion("kernel.loop_3", "for", 2, {}, source_line=7),
        ])
        payload = self._payload()
        payload["solutions"][1]["pragmas"] = [
            self._pragma("kernel.loop_1", "#pragma HLS PIPELINE II=1"),
            self._pragma("kernel.loop_2", "#pragma HLS UNROLL factor=2"),
        ]
        result = parse_solution_payload(payload, report, "kernel")
        self.assertEqual(result[0].pragmas[0].target_loop_id, "kernel.loop_1")

    def test_rejects_bind_storage_on_external_array(self) -> None:
        payload = self._payload()
        payload["solutions"][0]["pragmas"] = [
            self._pragma("", "#pragma HLS BIND_STORAGE variable=input type=ram_2p impl=bram"),
            self._pragma("kernel.loop_2", "#pragma HLS UNROLL factor=2"),
        ]
        with self.assertRaisesRegex(AIRecommendationError, "existing local array"):
            parse_solution_payload(payload, self._report(), "kernel")

    def test_accepts_bind_storage_on_existing_local_array(self) -> None:
        payload = self._payload()
        payload["solutions"][0]["pragmas"] = [
            self._pragma("", "#pragma HLS BIND_STORAGE variable=cache type=ram_2p impl=bram"),
            self._pragma("kernel.loop_2", "#pragma HLS UNROLL factor=2"),
        ]
        source = (
            "void kernel(int input[16], int output[16]) {\n"
            "  int cache[16];\n"
            "  for (int i=0;i<16;i++) cache[i]=input[i];\n"
            "  for (int i=0;i<16;i++) output[i]=cache[i];\n"
            "}\n"
        )
        result = parse_solution_payload(
            payload, self._report(), "kernel", source_text=source
        )
        self.assertTrue(
            any(
                "BIND_STORAGE" in pragma.pragma
                for solution in result
                for pragma in solution.pragmas
            )
        )

    def test_accepts_pragma_without_prefix(self) -> None:
        payload = self._payload()
        payload["solutions"][0]["pragmas"][1]["pragma"] = (
            "ARRAY_PARTITION variable=input cyclic factor=4 dim=1"
        )
        result = parse_solution_payload(payload, self._report(), "kernel")
        self.assertEqual(
            result[1].pragmas[1].pragma,
            "#pragma HLS ARRAY_PARTITION variable=input cyclic factor=4 dim=1",
        )

    def test_rejects_complete_partition_on_external_parameter(self) -> None:
        payload = self._payload()
        payload["solutions"][0]["pragmas"] = [
            self._pragma("", "#pragma HLS ARRAY_PARTITION variable=input complete dim=1"),
            self._pragma("kernel.loop_2", "#pragma HLS UNROLL factor=2"),
        ]
        with self.assertRaisesRegex(AIRecommendationError, "ARRAY_PARTITION complete"):
            parse_solution_payload(payload, self._report(), "kernel")

    def test_rejects_allocation_without_static_multiplication_evidence(self) -> None:
        payload = self._payload()
        payload["solutions"][0]["pragmas"] = [
            self._pragma("", "#pragma HLS ALLOCATION operation instances=mul limit=1"),
            self._pragma("kernel.loop_2", "#pragma HLS UNROLL factor=2"),
        ]
        with self.assertRaisesRegex(AIRecommendationError, "multiplication"):
            parse_solution_payload(payload, self._report(), "kernel")

    def test_accepts_constrained_allocation_with_static_multiplication_evidence(self) -> None:
        report = self._report()
        report.functions[0].features["has_multiplication"] = True
        payload = self._payload()
        payload["solutions"][0]["pragmas"] = [
            self._pragma("", "#pragma HLS ALLOCATION operation instances=mul limit=1"),
            self._pragma("kernel.loop_2", "#pragma HLS UNROLL factor=2"),
        ]
        result = parse_solution_payload(payload, report, "kernel")
        allocation = next(
            pragma
            for solution in result
            for pragma in solution.pragmas
            if "ALLOCATION" in pragma.pragma
        )
        self.assertEqual(
            allocation.pragma,
            "#pragma HLS ALLOCATION operation instances=mul limit=1",
        )

    def test_rejects_array_reshape_for_a_non_array_parameter(self) -> None:
        payload = self._payload()
        payload["solutions"][0]["pragmas"] = [
            self._pragma("", "#pragma HLS ARRAY_RESHAPE variable=missing cyclic factor=2 dim=1"),
            self._pragma("kernel.loop_2", "#pragma HLS UNROLL factor=2"),
        ]
        with self.assertRaisesRegex(AIRecommendationError, "supplied array parameter"):
            parse_solution_payload(payload, self._report(), "kernel")

    def test_rejects_partition_factor_larger_than_literal_array_extent(self) -> None:
        report = self._report()
        report.functions[0].parameters.append("int result[1]")
        payload = self._payload()
        payload["solutions"][0]["pragmas"] = [
            self._pragma("kernel.loop_1", "#pragma HLS PIPELINE II=1"),
            self._pragma(
                "",
                "#pragma HLS ARRAY_PARTITION variable=result cyclic factor=2 dim=1",
            ),
        ]
        with self.assertRaisesRegex(AIRecommendationError, "exceeds the declared"):
            parse_solution_payload(payload, report, "kernel")

    def test_request_omits_single_element_array_from_partition_targets(self) -> None:
        report = self._report()
        report.functions[0].parameters.append("int result[1]")
        response_payload = self._payload()

        class FakeResponses:
            def __init__(self) -> None:
                self.arguments = None

            def create(self, **kwargs):
                self.arguments = kwargs
                return SimpleNamespace(output_text=json.dumps(response_payload))

        responses = FakeResponses()
        recommend_solutions(
            report,
            "kernel",
            part="xc7z020clg400-1",
            clock_period_ns=10.0,
            model="test-model",
            client=SimpleNamespace(responses=responses),
        )
        sent = json.loads(responses.arguments["input"])
        allowed = sent["pragma_constraints"]["array_partition_allowed_variables"]["kernel"]
        self.assertNotIn("result", allowed)

    def test_rejects_unbounded_ai_factors_before_generation(self) -> None:
        payload = self._payload()
        payload["solutions"][0]["pragmas"] = [
            self._pragma("kernel.loop_1", "#pragma HLS UNROLL factor=64"),
            self._pragma("", "#pragma HLS ARRAY_PARTITION variable=input cyclic factor=2 dim=1"),
        ]
        with self.assertRaisesRegex(AIRecommendationError, "2 through 8"):
            parse_solution_payload(payload, self._report(), "kernel")

    def test_retries_when_ai_returns_an_invalid_pipeline_target(self) -> None:
        report = self._report()
        report.functions[0].loop_regions[1].depth = 2
        invalid_payload = self._payload()
        corrected_payload = deepcopy(invalid_payload)
        corrected_payload["solutions"][1]["pragmas"] = [
            self._pragma("kernel.loop_2", "#pragma HLS PIPELINE II=1"),
            self._pragma("kernel.loop_1", "#pragma HLS UNROLL factor=2"),
        ]

        class FakeResponses:
            def __init__(self) -> None:
                self.payloads = [invalid_payload, corrected_payload]
                self.instructions: list[str] = []

            def create(self, **kwargs):
                self.instructions.append(kwargs["instructions"])
                return SimpleNamespace(output_text=json.dumps(self.payloads.pop(0)))

        responses = FakeResponses()
        retries: list[tuple[int, str]] = []
        result = recommend_solutions(
            report,
            "kernel",
            part="xc7z020clg400-1",
            clock_period_ns=10.0,
            model="test-model",
            client=SimpleNamespace(responses=responses),
            retry_callback=lambda attempt, error: retries.append((attempt, error)),
        )

        self.assertEqual(len(result.solutions), 3)
        self.assertEqual(retries[0][0], 2)
        self.assertIn("innermost", retries[0][1])
        self.assertIn("previous response failed", responses.instructions[1])

    def test_uses_local_fallback_after_repeated_invalid_responses(self) -> None:
        invalid_payload = self._payload()
        invalid_payload["solutions"][0]["pragmas"][0]["pragma"] = (
            "#pragma HLS BIND_STORAGE variable=input type=ram_2p impl=bram"
        )

        class FakeResponses:
            def create(self, **kwargs):
                return SimpleNamespace(output_text=json.dumps(invalid_payload))

        result = recommend_solutions(
            self._report(),
            "kernel",
            part="xc7z020clg400-1",
            clock_period_ns=10.0,
            model="test-model",
            client=SimpleNamespace(responses=FakeResponses()),
        )

        self.assertIsNotNone(result.fallback_reason)
        self.assertEqual(len(result.solutions), 3)
        self.assertTrue(all(len(item.pragmas) >= 2 for item in result.solutions))
        self.assertTrue(
            all(
                "BIND_STORAGE" not in pragma.pragma
                for item in result.solutions
                for pragma in item.pragmas
            )
        )
        self.assertTrue(
            all(
                "ALLOCATION" not in pragma.pragma
                for item in result.solutions
                for pragma in item.pragmas
            )
        )

    def test_local_fallback_keeps_large_design_space_distinct(self) -> None:
        class FakeResponses:
            def create(self, **kwargs):
                return SimpleNamespace(output_text=json.dumps({"summary": "", "solutions": []}))

        result = recommend_solutions(
            self._report(),
            "kernel",
            part="xc7z020clg400-1",
            clock_period_ns=10.0,
            design_point_count=10,
            model="test-model",
            client=SimpleNamespace(responses=FakeResponses()),
        )

        pragma_sets = {
            tuple((pragma.target_loop_id, pragma.pragma) for pragma in solution.pragmas)
            for solution in result.solutions
        }
        self.assertEqual(len(result.solutions), 10)
        self.assertEqual(len(pragma_sets), 10)
        for solution in result.solutions:
            for directive in solution.pragmas:
                factor = re.search(r"\bfactor=(\d+)", directive.pragma)
                ii = re.search(r"\bII=(\d+)", directive.pragma)
                if factor:
                    self.assertLessEqual(int(factor.group(1)), 8)
                if ii:
                    self.assertLessEqual(int(ii.group(1)), 4)

    def test_local_fallback_prioritizes_wider_banking_for_many_loop_reads(self) -> None:
        report = self._report()
        report.functions[0].loop_regions[0].features["array_access_count"] = 16

        class FakeResponses:
            def create(self, **_kwargs):
                return SimpleNamespace(output_text=json.dumps({"summary": "", "solutions": []}))

        result = recommend_solutions(
            report,
            "kernel",
            part="xc7z020clg400-1",
            clock_period_ns=10.0,
            design_point_count=2,
            model="test-model",
            client=SimpleNamespace(responses=FakeResponses()),
        )

        partition_pragmas = [
            next(
                pragma.pragma
                for pragma in solution.pragmas
                if "ARRAY_PARTITION" in pragma.pragma
            )
            for solution in result.solutions
        ]
        self.assertIn("factor=8", partition_pragmas[0])
        self.assertIn("factor=4", partition_pragmas[1])

    def test_fallback_does_not_duplicate_points_when_safe_space_is_smaller(self) -> None:
        report = AnalysisReport(
            file="scalar.c", threshold=60,
            functions=[FunctionAnalysis(
                name="scalar", return_type="int", parameters=["int value"],
                features={}, source_line=1, loop_regions=[],
            )],
        )

        class FakeResponses:
            def create(self, **_kwargs):
                return SimpleNamespace(output_text=json.dumps({"summary": "", "solutions": []}))

        result = recommend_solutions(
            report, "scalar", part="part", clock_period_ns=10,
            design_point_count=3, model="test",
            client=SimpleNamespace(responses=FakeResponses()),
        )

        self.assertEqual(len(result.solutions), 1)
        self.assertIn("only 1 unique point", result.summary)

    def test_allows_a_previous_plan_as_a_verification_candidate(self) -> None:
        repeated_payload = self._payload()

        class FakeResponses:
            def create(self, **kwargs):
                return SimpleNamespace(output_text=json.dumps(repeated_payload))

        first_plan = repeated_payload["solutions"][0]["pragmas"]
        result = recommend_solutions(
            self._report(),
            "kernel",
            part="xc7z020clg400-1",
            clock_period_ns=10.0,
            model="test-model",
            client=SimpleNamespace(responses=FakeResponses()),
            experience_context={"current_source_plans": [{"pragmas": first_plan}]},
            exploration_mode="verify",
        )

        self.assertEqual(len(result.solutions), 3)
        self.assertEqual(result.solutions[1].pragmas[0].pragma, "#pragma HLS UNROLL factor=4")

    def test_converged_exploration_allows_incumbent_verification(self) -> None:
        repeated_payload = self._payload()
        repeated_payload["solutions"][0]["strategy"] = "Incumbent verification benchmark"
        repeated_payload["solutions"][0]["risk"] = "Re-measure for verification only"
        incumbent_pragmas = repeated_payload["solutions"][0]["pragmas"]

        class FakeResponses:
            def __init__(self) -> None:
                self.calls: list[dict] = []

            def create(self, **kwargs):
                self.calls.append(kwargs)
                return SimpleNamespace(output_text=json.dumps(repeated_payload))

        responses = FakeResponses()
        result = recommend_solutions(
            self._report(), "kernel", part="part", clock_period_ns=10,
            model="test", client=SimpleNamespace(responses=responses),
            experience_context={
                "current_source_plans": [{"pragmas": incumbent_pragmas}],
                "incumbent_best": {
                    "pragma_plan": {"pragmas": incumbent_pragmas},
                    "project_path": "generated/private/path",
                    "original_source_code": "duplicate source",
                    "metrics": {"hls_report": "private/report/path"},
                },
                "exploration_state": {"converged": True, "stagnant_batches": 2},
            },
            exploration_mode="explore",
        )

        self.assertEqual(len(result.solutions), 3)
        self.assertEqual(len(responses.calls), 1)
        self.assertIn("exploration state is converged", responses.calls[0]["instructions"])
        sent_incumbent = json.loads(responses.calls[0]["input"])["experience_context"]["incumbent_best"]
        self.assertNotIn("project_path", sent_incumbent)
        self.assertNotIn("original_source_code", sent_incumbent)
        self.assertNotIn("metrics", sent_incumbent)

    def test_converged_exploration_still_repairs_non_incumbent_repeat(self) -> None:
        payload = self._payload()
        incumbent = next(item for item in payload["solutions"] if item["rank"] == 1)
        repeated = next(item for item in payload["solutions"] if item["rank"] == 2)
        incumbent["strategy"] = "Incumbent verification"
        incumbent["risk"] = "Benchmark re-measure only"
        replacement = deepcopy(repeated)
        replacement["rank"] = 1
        replacement["name"] = "bounded_refinement"
        replacement["pragmas"][0]["pragma"] = "#pragma HLS UNROLL factor=8"
        response_payloads = [
            payload,
            {"summary": "Repaired non-incumbent repeat", "solutions": [replacement]},
        ]

        class FakeResponses:
            def __init__(self) -> None:
                self.calls: list[dict] = []

            def create(self, **kwargs):
                self.calls.append(kwargs)
                return SimpleNamespace(output_text=json.dumps(response_payloads.pop(0)))

        responses = FakeResponses()
        result = recommend_solutions(
            self._report(), "kernel", part="part", clock_period_ns=10,
            model="test", client=SimpleNamespace(responses=responses),
            experience_context={
                "current_source_plans": [
                    {"pragmas": incumbent["pragmas"]},
                    {"pragmas": repeated["pragmas"]},
                ],
                "incumbent_best": {"pragma_plan": {"pragmas": incumbent["pragmas"]}},
                "exploration_state": {"converged": True, "stagnant_batches": 2},
            },
            exploration_mode="explore",
        )

        self.assertEqual(len(result.solutions), 3)
        repair_context = json.loads(responses.calls[1]["input"])["repair_context"]
        self.assertEqual(repair_context["original_ranks_to_replace"], [2])

    def test_exploration_mode_retries_a_previous_plan(self) -> None:
        repeated_payload = self._payload()
        replacement = deepcopy(repeated_payload["solutions"][0])
        replacement["rank"] = 1
        replacement["name"] = "refined_unroll"
        replacement["pragmas"][0]["pragma"] = "#pragma HLS UNROLL factor=8"
        corrected_payload = {"summary": "Repaired only the duplicate.", "solutions": [replacement]}

        class FakeResponses:
            def __init__(self) -> None:
                self.payloads = [repeated_payload, corrected_payload]
                self.calls: list[dict] = []

            def create(self, **kwargs):
                self.calls.append(kwargs)
                return SimpleNamespace(output_text=json.dumps(self.payloads.pop(0)))

        first_plan = repeated_payload["solutions"][0]["pragmas"]
        retries: list[str] = []
        responses = FakeResponses()
        result = recommend_solutions(
            self._report(),
            "kernel",
            part="xc7z020clg400-1",
            clock_period_ns=10.0,
            model="test-model",
            client=SimpleNamespace(responses=responses),
            experience_context={"current_source_plans": [{"pragmas": first_plan}]},
            retry_callback=lambda attempt, error: retries.append(error),
            exploration_mode="explore",
        )

        self.assertEqual(len(result.solutions), 3)
        self.assertIn("exploration mode", retries[0])
        self.assertEqual(result.solutions[1].pragmas[0].pragma, "#pragma HLS UNROLL factor=8")
        self.assertEqual(
            result.solutions[0].pragmas[0].pragma,
            "#pragma HLS PIPELINE II=1",
        )
        repair_input = json.loads(responses.calls[1]["input"])
        self.assertEqual(repair_input["repair_context"]["original_ranks_to_replace"], [2])
        self.assertEqual(len(repair_input["repair_context"]["accepted_solutions"]), 2)
        self.assertIn("Return replacements only", responses.calls[1]["instructions"])
        self.assertEqual(result.summary, "Repaired only the duplicate.")

    def test_fallback_replaces_only_unrepaired_duplicate_rank(self) -> None:
        repeated_payload = self._payload()

        class FakeResponses:
            def __init__(self) -> None:
                self.calls = 0

            def create(self, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    return SimpleNamespace(output_text=json.dumps(repeated_payload))
                return SimpleNamespace(output_text=json.dumps({"summary": "", "solutions": []}))

        first_plan = repeated_payload["solutions"][0]["pragmas"]
        result = recommend_solutions(
            self._report(),
            "kernel",
            part="xc7z020clg400-1",
            clock_period_ns=10.0,
            model="test-model",
            client=SimpleNamespace(responses=FakeResponses()),
            experience_context={"current_source_plans": [{"pragmas": first_plan}]},
            exploration_mode="explore",
        )

        self.assertIsNotNone(result.fallback_reason)
        self.assertEqual(len(result.solutions), 3)
        self.assertEqual(result.solutions[0].name, "dp01_Dual pipeline")
        self.assertEqual(result.solutions[2].name, "dp03_Memory balance")
        self.assertIn("local_safe", result.solutions[1].name)

    def test_intra_batch_duplicate_repairs_only_the_later_rank(self) -> None:
        repeated_payload = self._payload()
        rank1 = next(item for item in repeated_payload["solutions"] if item["rank"] == 1)
        rank2 = next(item for item in repeated_payload["solutions"] if item["rank"] == 2)
        rank2["pragmas"] = deepcopy(rank1["pragmas"])
        replacement = deepcopy(self._payload()["solutions"][0])
        replacement["rank"] = 1
        replacement["name"] = "replacement_for_rank2"
        responses_payload = [
            repeated_payload,
            {"summary": "Repaired rank 2", "solutions": [replacement]},
        ]

        class FakeResponses:
            def __init__(self) -> None:
                self.calls: list[dict] = []

            def create(self, **kwargs):
                self.calls.append(kwargs)
                return SimpleNamespace(output_text=json.dumps(responses_payload.pop(0)))

        responses = FakeResponses()
        result = recommend_solutions(
            self._report(), "kernel", part="part", clock_period_ns=10,
            model="test", client=SimpleNamespace(responses=responses),
            exploration_mode="explore",
        )

        self.assertEqual(len(result.solutions), 3)
        self.assertEqual(
            json.loads(responses.calls[1]["input"])["repair_context"]["original_ranks_to_replace"],
            [2],
        )
        self.assertEqual(result.solutions[0].name, "dp01_Dual pipeline")
        self.assertIn("replacement_for_rank2", result.solutions[1].name)

    def test_dataflow_rejects_external_calls_as_helper_stages(self) -> None:
        report = self._report()
        report.functions[0].features["called_functions"] = {"memcpy": 1, "sqrt": 1}
        payload = self._payload()
        payload["solutions"][0]["pragmas"] = [
            self._pragma("", "#pragma HLS DATAFLOW"),
            self._pragma("kernel.loop_1", "#pragma HLS UNROLL factor=2"),
        ]
        with self.assertRaisesRegex(AIRecommendationError, "existing multi-stage"):
            parse_solution_payload(payload, report, "kernel")

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
                        LoopRegion(
                            id="kernel.loop_1",
                            kind="for",
                            depth=1,
                            features={},
                            source_line=2,
                        ),
                        LoopRegion(
                            id="kernel.loop_2",
                            kind="for",
                            depth=1,
                            features={},
                            source_line=5,
                        ),
                    ],
                )
            ],
        )

    @classmethod
    def _payload(cls) -> dict:
        return {
            "summary": "Three energy-efficiency strategies",
            "solutions": [
                cls._solution(
                    2,
                    "Selective unroll",
                    [
                        cls._pragma("kernel.loop_2", "#pragma HLS UNROLL factor=4"),
                        cls._pragma("", "#pragma HLS ARRAY_PARTITION variable=input cyclic factor=4 dim=1"),
                    ],
                ),
                cls._solution(
                    1,
                    "Dual pipeline",
                    [
                        cls._pragma("kernel.loop_1", "#pragma hls pipeline II=1"),
                        cls._pragma("kernel.loop_2", "#pragma HLS PIPELINE II=1"),
                    ],
                ),
                cls._solution(
                    3,
                    "Memory balance",
                    [
                        cls._pragma("", "#pragma HLS INLINE off"),
                        cls._pragma(
                            "",
                            "#pragma HLS ARRAY_PARTITION variable=input cyclic factor=2 dim=1",
                        ),
                    ],
                ),
            ],
        }

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
    def _pragma(loop_id: str, pragma: str) -> dict:
        return {
            "target_function": "kernel",
            "target_loop_id": loop_id,
            "pragma": pragma,
            "rationale": "Reason",
        }


if __name__ == "__main__":
    unittest.main()
