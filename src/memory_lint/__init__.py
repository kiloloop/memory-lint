"""Deterministic linting for Markdown memory and documentation corpora."""

from .core import Config, Finding, ToolError, compare_against_git, compare_files, lint_corpus, load_config

__all__ = [
    "Config",
    "Finding",
    "ToolError",
    "compare_against_git",
    "compare_files",
    "lint_corpus",
    "load_config",
]

__version__ = "0.1.0"
