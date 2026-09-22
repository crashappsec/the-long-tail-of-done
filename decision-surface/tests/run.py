#!/usr/bin/env python3
"""Run every test. Standard library only, so this works anywhere the tool does.

    python3 tests/run.py            everything
    python3 tests/run.py front      one module, by substring
    python3 tests/run.py -q         quieter

Exit code is 0 only if every test passes, so this is also the CI entry point.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))


def main(argv: list[str]) -> int:
    pattern = "test_*.py"
    verbosity = 2
    for arg in argv:
        if arg in ("-q", "--quiet"):
            verbosity = 1
        elif not arg.startswith("-"):
            pattern = f"test_*{arg}*.py"
    suite = unittest.defaultTestLoader.discover(
        start_dir=str(HERE), pattern=pattern, top_level_dir=str(HERE)
    )
    if not suite.countTestCases():
        print(f"no tests matched {pattern}", file=sys.stderr)
        return 2
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
