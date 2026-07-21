import tempfile
import unittest
from pathlib import Path

from parser import parse_c_file


class ParserTests(unittest.TestCase):
    def test_expands_quoted_headers_for_static_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "types.h").write_text("typedef int sample_t;\n", encoding="utf-8")
            source = root / "kernel.c"
            source.write_text(
                '#include "types.h"\n'
                "void kernel(sample_t input[4], sample_t output[4]) {\n"
                "    for (int i = 0; i < 4; i++) { output[i] = input[i]; }\n}\n",
                encoding="utf-8",
            )

            parsed = parse_c_file(source)

            self.assertEqual([function.name for function in parsed.functions], ["kernel"])


if __name__ == "__main__":
    unittest.main()
