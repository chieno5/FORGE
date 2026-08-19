from pathlib import Path
import unittest

import forge
import forge_test


class ConfigurationTests(unittest.TestCase):
    def test_public_default_uses_formal_database(self) -> None:
        self.assertEqual(forge.DEFAULT_DATABASE, Path("data") / "forge.db")

    def test_test_wrapper_uses_separate_ignored_database(self) -> None:
        self.assertEqual(forge_test.TEST_DATABASE, "data/forge_test.db")


if __name__ == "__main__":
    unittest.main()
