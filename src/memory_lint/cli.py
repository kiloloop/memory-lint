"""Command-line interface for memory-lint."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Sequence
import argparse
import json
import sys

from . import __version__
from .core import Finding, ToolError, compare_against_git, compare_files, lint_corpus, load_config


def _iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected an ISO date (YYYY-MM-DD)") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memory-lint",
        description="Deterministically lint a Markdown memory or documentation corpus without writing it.",
        allow_abbrev=False,
    )
    parser.add_argument("--config", required=True, help="YAML configuration file")
    parser.add_argument("--corpus-root", help="override config.corpus_root")
    parser.add_argument("--format", choices=("table", "json"), default="table", help="output format")
    parser.add_argument(
        "--now",
        type=_iso_date,
        help="date used for staleness checks (default: current UTC date)",
    )
    parser.add_argument("--against", help="compare changed linted files with this Git ref")
    parser.add_argument("--compare-before", help="first file in an explicit revision pair")
    parser.add_argument("--compare-after", help="second file in an explicit revision pair")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _render_table(findings: Sequence[Finding]) -> str:
    if not findings:
        return "Clean — no findings."
    headers = ("SEVERITY", "CODE", "LOCATION", "PROFILE", "MESSAGE")
    rows = [
        (
            item.severity.upper(),
            item.code,
            f"{item.path}:{item.line}" if item.line is not None else item.path,
            item.profile,
            item.message,
        )
        for item in findings
    ]
    widths = [max(len(headers[index]), *(len(row[index]) for row in rows)) for index in range(len(headers))]
    lines = ["  ".join(value.ljust(widths[index]) for index, value in enumerate(headers))]
    lines.append("  ".join("-" * width for width in widths))
    lines.extend("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)) for row in rows)
    lines.append(f"\n{len(findings)} finding(s).")
    return "\n".join(lines)


def _render_json(findings: Sequence[Finding], corpus_root: Path) -> str:
    payload = {
        "version": 1,
        "corpus_root": str(corpus_root),
        "finding_count": len(findings),
        "findings": [item.to_dict() for item in findings],
    }
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if bool(args.compare_before) != bool(args.compare_after):
        parser.error("--compare-before and --compare-after must be supplied together")

    try:
        config = load_config(args.config, args.corpus_root)
        today = args.now or datetime.now(timezone.utc).date()
        findings, linted_files = lint_corpus(config, today=today)
        if args.against:
            findings.extend(compare_against_git(config, args.against, linted_files))
        if args.compare_before and args.compare_after:
            findings.extend(compare_files(args.compare_before, args.compare_after, display_root=config.corpus_root))
        findings = sorted(
            {(
                item.code,
                item.severity,
                item.path,
                item.line,
                item.message,
                item.profile,
            ): item for item in findings}.values(),
            key=lambda item: (item.path, item.line if item.line is not None else 0, item.code, item.profile),
        )
    except ToolError as exc:
        print(f"memory-lint: {exc}", file=sys.stderr)
        return 2

    if args.format == "json":
        print(_render_json(findings, config.corpus_root))
    else:
        print(_render_table(findings))
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
