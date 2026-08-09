import subprocess
import tempfile
import unittest
from pathlib import Path

from models import FunctionAnalysis
from testbench_generator import generate_local_testbench


class TestbenchGeneratorTests(unittest.TestCase):
    def test_expression_macro_is_not_mistaken_for_a_function(self) -> None:
        source = (
            "#define BLOCKS 8\n"
            "#define LANES 4\n"
            "#define SIZE (BLOCKS * LANES)\n"
            "void reduction(const int input[SIZE], int output[1]) {\n"
            "  int sum = 0; for (int i=0;i<SIZE;i++) sum += input[i]; output[0]=sum;\n"
            "}\n"
        )

        generated = generate_local_testbench(
            source,
            "reduction",
            FunctionAnalysis(
                name="reduction",
                return_type="void",
                parameters=["const int input[SIZE]", "int output[1]"],
                features={},
            ),
        )

        self.assertNotIn("forge_golden_SIZE", generated.source)
        self.assertIn("#define reduction forge_golden_reduction", generated.source)

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

    def test_full_profile_is_self_checking_and_uses_original_source(self) -> None:
        function = FunctionAnalysis(
            name="kernel",
            return_type="int",
            parameters=["const int input[16]", "int output[16]", "int n"],
            features={},
        )
        generated = generate_local_testbench(
            "int kernel(const int input[16], int output[16], int n) {\n"
            "  for (int i = 0; i < n; ++i) output[i] = input[i] + 1;\n"
            "  return n;\n"
            "}\n",
            "kernel",
            function,
            profile="full",
            seed=42,
        )

        self.assertEqual(generated.manifest["case_count"], 13)
        self.assertEqual(generated.manifest["oracle"], "original_b0_source")
        self.assertIn('include "kernel_golden.c"', generated.source)
        self.assertIn("forge_golden_kernel(input_ref, output_ref, n)", generated.source)
        self.assertIn("kernel(input_dut, output_dut, n)", generated.source)
        self.assertIn("forge_same_return", generated.source)
        self.assertIn("return 1;", generated.source)
        self.assertIn("kernel_golden.c", generated.support_files)

    def test_profiles_and_seed_are_deterministic(self) -> None:
        function = FunctionAnalysis("kernel", "void", ["int data[8]"], {})
        source = "void kernel(int data[8]) { data[0] += 1; }\n"
        first = generate_local_testbench(source, "kernel", function, "smoke", 7)
        repeated = generate_local_testbench(source, "kernel", function, "smoke", 7)
        changed = generate_local_testbench(source, "kernel", function, "standard", 8)

        self.assertEqual(first, repeated)
        self.assertEqual(first.manifest["case_count"], 1)
        self.assertEqual(changed.manifest["case_count"], 6)
        self.assertNotEqual(first.source, changed.source)

    def test_manifest_infers_parameter_directions(self) -> None:
        function = FunctionAnalysis(
            "kernel",
            "void",
            ["const int input[8]", "int output[8]", "int state[8]"],
            {},
        )
        source = (
            "void kernel(const int input[8], int output[8], int state[8]) {\n"
            "  output[0] = input[0];\n"
            "  state[0] += input[0];\n"
            "}\n"
        )
        generated = generate_local_testbench(source, "kernel", function)
        directions = {
            item["name"]: item["direction"] for item in generated.manifest["parameters"]
        }

        self.assertEqual(directions["input"], "input")
        self.assertEqual(directions["output"], "output")
        self.assertEqual(directions["state"], "inout")

    def test_reference_copy_removes_an_existing_main(self) -> None:
        function = FunctionAnalysis("kernel", "void", [], {})
        generated = generate_local_testbench(
            "void kernel(void) {}\nint main(void) { return 0; }\n",
            "kernel",
            function,
        )

        self.assertNotIn("int main", generated.support_files["kernel_golden.c"])

    def test_generated_c_passes_host_syntax_check(self) -> None:
        compiler = Path("C:/AMDDesignTools/2025.2/Model_Composer/mingw64/bin/clang.exe")
        if not compiler.is_file():
            self.skipTest("AMD host compiler is not installed")
        function = FunctionAnalysis(
            "kernel", "int", ["const int input[16]", "int output[16]", "int n"], {}
        )
        original = (
            "int kernel(const int input[16], int output[16], int n) {\n"
            "  for (int i = 0; i < n; ++i) output[i] = input[i] + 1;\n"
            "  return n;\n"
            "}\n"
        )
        generated = generate_local_testbench(original, "kernel", function, "standard", 5)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            testbench = root / generated.filename
            golden = root / "kernel_golden.c"
            dut = root / "kernel.c"
            testbench.write_text(generated.source, encoding="utf-8")
            golden.write_text(generated.support_files[golden.name], encoding="utf-8")
            dut.write_text(original, encoding="utf-8")
            self._check_syntax(compiler, testbench, dut)

    @staticmethod
    def _check_syntax(compiler: Path, testbench: Path, dut: Path) -> None:
        result = subprocess.run(
            [compiler, "-fsyntax-only", "-std=c99", testbench, dut],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise AssertionError(
                f"compiler exit code {result.returncode}\n{result.stdout}{result.stderr}"
            )


if __name__ == "__main__":
    unittest.main()
