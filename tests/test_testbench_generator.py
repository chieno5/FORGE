import unittest

from models import FunctionAnalysis
from testbench_generator import generate_local_testbench


class TestbenchGeneratorTests(unittest.TestCase):
    def test_copies_source_defines_needed_by_the_prototype(self) -> None:
        function = FunctionAnalysis(
            name="fir",
            return_type="void",
            parameters=["const int input[FIR_MAX]", "int output[FIR_MAX]"],
            features={},
        )
        testbench = generate_local_testbench(
            "#define FIR_MAX 256\nvoid fir(void) {}\n",
            "fir",
            function,
        )
        self.assertIn("#define FIR_MAX 256", testbench.source)


if __name__ == "__main__":
    unittest.main()
