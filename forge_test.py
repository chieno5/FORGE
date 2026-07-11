from __future__ import annotations

import forge


TEST_DATABASE = "data/forge_test.db"


def main(argv: list[str] | None = None) -> int:
    return forge.main(argv, config_overrides={"database": {"path": TEST_DATABASE}})


if __name__ == "__main__":
    raise SystemExit(main())
