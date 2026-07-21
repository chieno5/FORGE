import tempfile
import unittest
from pathlib import Path

from analyzer import analyze_functions
from parser import parse_c_file


class AnalyzerTests(unittest.TestCase):
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
