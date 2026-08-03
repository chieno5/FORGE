import tempfile
import unittest
from pathlib import Path

from analyzer import analyze_functions, find_structural_constraints
from parser import parse_c_file


class AnalyzerTests(unittest.TestCase):
    def test_scalar_dependency_is_attached_to_the_inner_loop_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "nested.c"
            source.write_text(
                "void nested(const int a[4][4], int out[4]) {\n"
                "  for (int i = 0; i < 4; ++i) {\n"
                "    int sum = 0;\n"
                "    for (int j = 0; j < 4; ++j) { sum += a[i][j]; }\n"
                "    out[i] = sum;\n"
                "  }\n"
                "}\n",
                encoding="utf-8",
            )

            function = analyze_functions(parse_c_file(source).functions)[0]

            self.assertFalse(
                function.loop_regions[0].features["has_scalar_loop_carried_dependency"]
            )
            self.assertTrue(
                function.loop_regions[1].features["has_scalar_loop_carried_dependency"]
            )

    def test_reports_scalar_reduction_as_a_structural_constraint(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "reduction.c"
            source.write_text(
                "void reduction(const int a[16], int out[1]) {\n"
                "  int sum = 0;\n"
                "  int max_value = -1000;\n"
                "  for (int i = 0; i < 16; ++i) {\n"
                "    sum += a[i];\n"
                "    if (a[i] > max_value) { max_value = a[i]; }\n"
                "  }\n"
                "  out[0] = sum + max_value;\n"
                "}\n",
                encoding="utf-8",
            )

            functions = analyze_functions(parse_c_file(source).functions)
            loop = functions[0].loop_regions[0]
            constraints = find_structural_constraints(functions)

            self.assertTrue(loop.features["has_scalar_loop_carried_dependency"])
            self.assertFalse(loop.features["pipeline_eligible"])
            self.assertEqual(loop.features["dependency_variables"], ["max_value", "sum"])
            self.assertEqual(len(constraints), 1)
            self.assertEqual(
                constraints[0].supported_transformations,
                ["partial_accumulator_v1"],
            )

    def test_marks_loop_carried_array_dependency_as_not_pipeline_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "recurrence.c"
            source.write_text(
                "void recurrence(int result[64], int n) {\n"
                "    for (int i = 1; i < n - 1; i++) {\n"
                "        result[i] = result[i - 1] + result[i + 1];\n"
                "    }\n"
                "}\n",
                encoding="utf-8",
            )

            function = analyze_functions(parse_c_file(source).functions)[0]
            features = function.loop_regions[0].features

            self.assertTrue(features["has_same_array_read_write"])
            self.assertTrue(features["has_neighbor_index_access"])
            self.assertTrue(features["has_loop_carried_dependency"])
            self.assertEqual(features["dependency_arrays"], ["result"])
            self.assertFalse(features["pipeline_eligible"])
            self.assertFalse(features["unroll_eligible"])


if __name__ == "__main__":
    unittest.main()
