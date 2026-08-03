import tempfile
import unittest
from pathlib import Path

from analyzer import analyze_functions, find_structural_constraints
from models import AnalysisReport
from parser import parse_c_file
from source_transformer import apply_reduction_preflight_transform


class SourceTransformerTests(unittest.TestCase):
    def test_transforms_integer_add_and_max_reductions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "reduction.c"
            source.write_text(
                "void reduction(const int a[16], int out[2]) {\n"
                "  int sum = 0;\n"
                "  int max_value = -1000;\n"
                "  for (int i = 0; i < 16; ++i) {\n"
                "    sum += a[i];\n"
                "    if (a[i] > max_value) { max_value = a[i]; }\n"
                "  }\n"
                "  out[0] = sum; out[1] = max_value;\n"
                "}\n",
                encoding="utf-8",
            )
            report = self._report(source)

            attempt = apply_reduction_preflight_transform(
                source.read_text(encoding="utf-8"), report, "reduction"
            )

            self.assertTrue(attempt.applied)
            self.assertIn("sum__forge_partial[4]", attempt.source_text)
            self.assertIn("max_value__forge_partial[4]", attempt.source_text)
            self.assertIn("ARRAY_PARTITION variable=sum__forge_partial complete", attempt.source_text)
            transformed = Path(temp_dir) / "transformed.c"
            transformed.write_text(attempt.source_text, encoding="utf-8")
            transformed_report = self._report(transformed)
            transformed_loop = transformed_report.functions[0].loop_regions[0]
            self.assertFalse(transformed_loop.features["has_scalar_loop_carried_dependency"])
            self.assertTrue(transformed_loop.features["pipeline_eligible"])
            self.assertEqual(
                report.functions[0].parameters,
                transformed_report.functions[0].parameters,
            )

    def test_rejects_floating_point_reassociation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "float_reduction.c"
            source.write_text(
                "float reduction(const float a[16]) {\n"
                "  float sum = 0.0f;\n"
                "  for (int i = 0; i < 16; ++i) { sum += a[i]; }\n"
                "  return sum;\n"
                "}\n",
                encoding="utf-8",
            )
            report = self._report(source)

            attempt = apply_reduction_preflight_transform(
                source.read_text(encoding="utf-8"), report, "reduction"
            )

            self.assertFalse(attempt.applied)
            self.assertIn("integer accumulator", attempt.reason)

    @staticmethod
    def _report(source: Path) -> AnalysisReport:
        functions = analyze_functions(parse_c_file(source).functions)
        return AnalysisReport(
            str(source),
            60,
            functions,
            structural_constraints=find_structural_constraints(functions),
        )


if __name__ == "__main__":
    unittest.main()
