import io
import unittest
from contextlib import redirect_stdout

from models import AnalysisReport, FunctionAnalysis
from report import print_human_report


class ReportOutputTests(unittest.TestCase):
    def test_default_output_is_a_compact_summary(self) -> None:
        report = AnalysisReport(
            file="kernel.c",
            threshold=60,
            functions=[
                FunctionAnalysis(
                    name="kernel",
                    return_type="void",
                    parameters=[],
                    features={"loop_count": 2},
                    score=80,
                    classification="HIGH_PRIORITY_FPGA_CANDIDATE",
                    is_candidate=True,
                ),
                FunctionAnalysis(
                    name="helper",
                    return_type="int",
                    parameters=[],
                    features={"loop_count": 0},
                    score=20,
                    classification="NOT_SUITABLE_FOR_HLS",
                    is_candidate=False,
                ),
            ],
        )
        output = io.StringIO()

        with redirect_stdout(output):
            print_human_report(report)

        text = output.getvalue()
        self.assertIn("Functions: 2 | Loops: 2 | Candidates: 1", text)
        self.assertIn("kernel: 80/100", text)
        self.assertIn("helper: 20/100", text)
        self.assertIn("Highest score: kernel", text)
        self.assertNotIn("Detected features", text)
        self.assertNotIn("Reasoning", text)

if __name__ == "__main__":
    unittest.main()
