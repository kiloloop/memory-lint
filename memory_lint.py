#!/usr/bin/env python3
"""Run memory-lint directly from a repository checkout."""

from pathlib import Path
import sys


TOOL_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOL_ROOT / "src"))

from memory_lint.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
