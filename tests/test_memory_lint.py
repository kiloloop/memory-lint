from __future__ import annotations

from datetime import date
from pathlib import Path
import json
import os
import subprocess
import sys

import pytest


TOOL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_ROOT / "src"))

from memory_lint.core import (  # noqa: E402
    ToolError,
    compare_against_git,
    compare_files,
    lint_corpus,
    load_config,
)


SAMPLE_CONFIG = TOOL_ROOT / "sample-config.yaml"
FIXTURES = TOOL_ROOT / "fixtures"
PINNED_TODAY = date(2026, 8, 10)


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(TOOL_ROOT / "memory_lint.py"), *args],
        cwd=TOOL_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _codes(root: Path) -> tuple[set[str], list]:
    config = load_config(SAMPLE_CONFIG, root)
    findings, _ = lint_corpus(config, today=PINNED_TODAY)
    return {finding.code for finding in findings}, findings


def test_clean_corpus_is_quiet_and_unchanged() -> None:
    root = FIXTURES / "clean"
    before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}

    config = load_config(SAMPLE_CONFIG, root)
    findings, linted = lint_corpus(config, today=PINNED_TODAY)

    assert findings == []
    assert len(linted) == 4
    assert before == {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_seeded_defects_cover_every_v1_check() -> None:
    codes, findings = _codes(FIXTURES / "defects")

    assert {
        "frontmatter-required-key",
        "frontmatter-unquoted-value",
        "frontmatter-type-enum",
        "broken-link",
        "index-missing-target",
        "index-missing-entry",
        "marker-unclosed",
        "marker-nested",
        "marker-orphaned",
        "stale-updated",
        "duplicate-heading",
        "duplicate-anchor",
        "line-too-long",
    } <= codes
    marker_findings = [finding for finding in findings if finding.code.startswith("marker-")]
    assert marker_findings
    assert {finding.severity for finding in marker_findings} == {"error"}


def test_findings_are_deterministic() -> None:
    config = load_config(SAMPLE_CONFIG, FIXTURES / "defects")
    first, _ = lint_corpus(config, today=PINNED_TODAY)
    second, _ = lint_corpus(config, today=PINNED_TODAY)
    assert first == second


def test_explicit_revision_pairs_detect_both_noop_classes() -> None:
    identical = compare_files(
        FIXTURES / "revisions" / "identical-before.md",
        FIXTURES / "revisions" / "identical-after.md",
        display_root=FIXTURES,
    )
    whitespace = compare_files(
        FIXTURES / "revisions" / "whitespace-before.md",
        FIXTURES / "revisions" / "whitespace-after.md",
        display_root=FIXTURES,
    )
    assert [finding.code for finding in identical] == ["noop-identical"]
    assert [finding.code for finding in whitespace] == ["noop-whitespace-only"]


def test_against_git_detects_whitespace_only_change(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    corpus = repo / "corpus"
    corpus.mkdir(parents=True)
    note = corpus / "note.md"
    note.write_text("# Synthetic\n\nAlpha beta\n", encoding="utf-8")
    config_path = repo / "config.yaml"
    config_path.write_text(
        """version: 1
corpus_root: corpus
profiles:
  docs:
    include: [\"**/*.md\"]
    checks:
      structure:
        max_line_length: 120
""",
        encoding="utf-8",
    )
    env = {**os.environ, "GIT_AUTHOR_NAME": "Synthetic", "GIT_AUTHOR_EMAIL": "synthetic@example.invalid"}
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, env=env, check=True, capture_output=True)
    subprocess.run(["git", "add", "corpus/note.md"], cwd=repo, env=env, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Synthetic",
            "-c",
            "user.email=synthetic@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-m",
            "baseline",
        ],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
    )
    note.write_text("# Synthetic\n\nAlpha   beta\n", encoding="utf-8")

    config = load_config(config_path)
    _, linted = lint_corpus(config, today=PINNED_TODAY)
    findings = compare_against_git(config, "HEAD", linted)
    assert [finding.code for finding in findings] == ["noop-whitespace-only"]


def test_against_git_reports_deleted_and_substantially_shrunk_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    corpus = repo / "corpus"
    notes = corpus / "notes"
    notes.mkdir(parents=True)
    deleted = notes / "deep" / "nested" / "deleted.md"
    deleted.parent.mkdir(parents=True)
    deleted.write_text("# Deleted\n\nSynthetic baseline content.\n", encoding="utf-8")
    shrunk = notes / "shrunk.md"
    shrunk.write_text(
        "# Shrunk\n\nOne.\nTwo.\nThree.\nFour.\nFive.\nSix.\n",
        encoding="utf-8",
    )
    modest = notes / "modest.md"
    modest.write_text("# Modest\n\nOne.\nTwo.\n", encoding="utf-8")
    excluded = notes / "archive" / "excluded.md"
    excluded.parent.mkdir()
    excluded.write_text("# Excluded\n", encoding="utf-8")
    config_path = repo / "config.yaml"
    config_path.write_text(
        """version: 1
corpus_root: corpus
profiles:
  docs:
    include: ["notes/**/*.md"]
    exclude: ["notes/archive/**/*.md"]
    checks:
      structure:
        max_line_length: 120
""",
        encoding="utf-8",
    )
    env = {**os.environ, "GIT_AUTHOR_NAME": "Synthetic", "GIT_AUTHOR_EMAIL": "synthetic@example.invalid"}
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, env=env, check=True, capture_output=True)
    subprocess.run(["git", "add", "corpus"], cwd=repo, env=env, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Synthetic",
            "-c",
            "user.email=synthetic@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-m",
            "baseline",
        ],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
    )
    deleted.unlink()
    shrunk.write_text("# Shrunk\n", encoding="utf-8")
    modest.write_text("# Modest\n\nOne.\n", encoding="utf-8")
    excluded.unlink()

    config = load_config(config_path)
    _, linted = lint_corpus(config, today=PINNED_TODAY)
    findings = compare_against_git(config, "HEAD", linted)

    assert [(finding.code, finding.severity, finding.path) for finding in findings] == [
        ("file-missing-vs-ref", "error", "notes/deep/nested/deleted.md"),
        ("file-shrunk-vs-ref", "warning", "notes/shrunk.md"),
    ]

    cli = _run_cli("--config", str(config_path), "--against", "HEAD", "--format", "json")
    assert cli.returncode == 1
    assert [finding["code"] for finding in json.loads(cli.stdout)["findings"]] == [
        "file-missing-vs-ref",
        "file-shrunk-vs-ref",
    ]


def test_index_table_rows_do_not_count_as_list_entries(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    notes = corpus / "notes"
    notes.mkdir(parents=True)
    (notes / "alpha.md").write_text("# Alpha\n", encoding="utf-8")
    (corpus / "MEMORY.md").write_text(
        """# Synthetic table index

| Name | Target |
| --- | --- |
| Alpha | [Alpha](notes/alpha.md) |
""",
        encoding="utf-8",
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """version: 1
corpus_root: corpus
profiles:
  notes:
    include: ["notes/**/*.md"]
    checks:
      structure:
        max_line_length: 120
indexes:
  - path: MEMORY.md
    include: ["notes/**/*.md"]
""",
        encoding="utf-8",
    )

    findings, _ = lint_corpus(load_config(config_path), today=PINNED_TODAY)

    assert [(finding.code, finding.path) for finding in findings] == [
        ("index-missing-entry", "notes/alpha.md")
    ]


def test_quoted_frontmatter_check_uses_top_level_key(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "note.md").write_text(
        """---
metadata:
  description: "Synthetic nested value"
description: unquoted top-level value
---
""",
        encoding="utf-8",
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """version: 1
corpus_root: corpus
profiles:
  notes:
    include: ["*.md"]
    checks:
      frontmatter:
        required_keys: [description]
        quoted_keys: [description]
""",
        encoding="utf-8",
    )

    findings, _ = lint_corpus(load_config(config_path), today=PINNED_TODAY)

    assert [(finding.code, finding.line) for finding in findings] == [
        ("frontmatter-unquoted-value", 4)
    ]


def test_staleness_checks_every_updated_header(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "note.md").write_text(
        """*Updated:* 2026-08-09

```text
*Updated:* YYYY-MM-DD
```

## Synthetic older section
*Updated:* 2026-01-01
""",
        encoding="utf-8",
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """version: 1
corpus_root: corpus
profiles:
  notes:
    include: ["*.md"]
    checks:
      staleness:
        max_age_days: 30
        updated_required: true
""",
        encoding="utf-8",
    )

    findings, _ = lint_corpus(load_config(config_path), today=PINNED_TODAY)

    assert [(finding.code, finding.line) for finding in findings] == [
        ("stale-updated", 8)
    ]


def test_cli_json_and_exit_codes(tmp_path: Path) -> None:
    clean = _run_cli("--config", str(SAMPLE_CONFIG), "--format", "json", "--now", "2026-08-10")
    assert clean.returncode == 0
    assert json.loads(clean.stdout)["finding_count"] == 0

    defects = _run_cli(
        "--config",
        str(SAMPLE_CONFIG),
        "--corpus-root",
        str(FIXTURES / "defects"),
        "--format",
        "json",
        "--now",
        "2026-08-10",
    )
    assert defects.returncode == 1
    assert json.loads(defects.stdout)["finding_count"] > 0

    invalid = tmp_path / "invalid.yaml"
    invalid.write_text("version: 99\n", encoding="utf-8")
    tool_error = _run_cli("--config", str(invalid))
    assert tool_error.returncode == 2
    assert "config.version must be 1" in tool_error.stderr


def test_cli_is_flags_only() -> None:
    result = _run_cli("positional-corpus", "--config", str(SAMPLE_CONFIG))
    assert result.returncode == 2
    assert "unrecognized arguments" in result.stderr


def test_cli_requires_complete_revision_pair() -> None:
    result = _run_cli(
        "--config",
        str(SAMPLE_CONFIG),
        "--compare-before",
        str(FIXTURES / "revisions" / "identical-before.md"),
    )
    assert result.returncode == 2
    assert "must be supplied together" in result.stderr


def test_config_rejects_glob_that_escapes_root(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    config_path.write_text(
        """version: 1
corpus_root: corpus
profiles:
  docs:
    include: [\"../*.md\"]
    checks:
      links: true
""",
        encoding="utf-8",
    )
    with pytest.raises(ToolError, match="must stay within the corpus root"):
        load_config(config_path)


def test_config_rejects_glob_ending_in_bare_recursive_component(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    config_path.write_text(
        """version: 1
corpus_root: corpus
profiles:
  docs:
    include: ["notes/**"]
    checks:
      links: true
""",
        encoding="utf-8",
    )

    with pytest.raises(ToolError, match="must not end in a bare '\\*\\*' component"):
        load_config(config_path)


def test_lint_rejects_symlinked_file_outside_corpus(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("# Synthetic outside file\n", encoding="utf-8")
    try:
        (corpus / "escaped.md").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are unavailable on this platform")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """version: 1
corpus_root: corpus
profiles:
  docs:
    include: [\"*.md\"]
    checks:
      links: true
""",
        encoding="utf-8",
    )

    config = load_config(config_path)
    with pytest.raises(ToolError, match="resolves outside the corpus root"):
        lint_corpus(config, today=PINNED_TODAY)
