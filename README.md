# memory-lint

Deterministic, read-only linting for Markdown memory and documentation corpora.

## Why

Markdown knowledge bases rarely fail loudly. Links stop resolving, index rows
drift away from the files they describe, managed sections lose their markers,
and stale notes continue to look current. The prose still renders, so the
damage can remain invisible until someone relies on it.

Multi-agent work makes that failure mode more expensive. Several agents can
read, generate, reorganize, and hand off the same corpus in parallel, while
each assumes its inputs still satisfy the project's conventions. Reviewers
need a repeatable structural check, not another subjective pass over every
file.

`memory-lint` turns those conventions into a deterministic check. It reports
findings without changing the corpus, gives agents and CI the same result for
the same corpus, configuration, and reference date, and can compare revisions
without writing to Git.

## Quick Start

The first run is an install plus a small YAML configuration. Every run after
that is one command.

### With your coding agent

Give your agent this prompt:

```text
Add a read-only Markdown corpus check to this repository with memory-lint 0.1.0
from https://pypi.org/project/memory-lint/.

Inspect the repository's Markdown layout and create a memory-lint YAML config
whose profiles and indexes match the files that actually exist. Install the
exact package version in the project's Python environment, then run
memory-lint with --config, --format table, and an explicit current UTC date in
--now. Report the exact command, exit status, finding count, and findings
grouped by severity and code. Exit 1 means a successful lint with findings.
Do not edit or auto-fix any corpus file unless I approve the proposed changes.
```

### Manually

Install the exact published release:

```bash
python -m pip install "memory-lint==0.1.0"
```

Python 3.10 or newer is required.

Copy [`sample-config.yaml`](sample-config.yaml), then adjust `corpus_root`,
profile globs, and index declarations to match your repository. Run from the
repository root:

```bash
memory-lint \
  --config memory-lint.yaml \
  --format table \
  --now "$(date -u +%F)"
```

Use the current UTC date for a normal run. Pinning `--now` makes staleness
results reproducible in CI, tests, and recorded examples.

## How It Works

### One configuration, independent profiles

The YAML configuration defines a corpus root, one or more profiles, and any
indexes that must cover matching files. Each profile has its own include and
exclude globs plus the checks that apply to that surface. `--corpus-root` can
point the same policy at another snapshot without editing the configuration.

Patterns and index paths must stay inside the corpus: they cannot be absolute,
contain `..`, or end in a bare `**`. Use a terminal file pattern such as
`**/*.md` for recursive discovery.

### Stable finding codes

Findings carry a severity, code, location, profile, and message. The codes are
grouped by contract:

- **Files and frontmatter:** `file-not-utf8`, `file-read-error`,
  `frontmatter-missing`, `frontmatter-unclosed`, `frontmatter-invalid`,
  `frontmatter-required-key`, `frontmatter-unquoted-value`, and
  `frontmatter-type-enum`.
- **Links:** `broken-link` and `ambiguous-wikilink` for relative Markdown links
  and corpus-contained wikilinks.
- **Managed markers:** `marker-nested`, `marker-orphaned`, and
  `marker-unclosed`.
- **Staleness and structure:** `updated-invalid`, `updated-in-future`,
  `stale-updated`, `updated-missing`, `line-too-long`, `duplicate-heading`,
  and `duplicate-anchor`.
- **Indexes:** `index-file-missing`, `index-row-invalid`,
  `index-missing-target`, `index-duplicate-entry`, and `index-missing-entry`.
- **Revision comparisons:** `noop-identical`, `noop-whitespace-only`,
  `file-missing-vs-ref`, and `file-shrunk-vs-ref`.

### Read-only output contract

`memory-lint` reads corpus files and, when requested, calls read-only Git
commands. It has no corpus write or auto-fix path.

`--format table` is for direct reading. `--format json` emits a versioned
object with `corpus_root`, `finding_count`, and sorted finding objects.

| Exit | Meaning |
| --- | --- |
| `0` | The configured corpus is clean. |
| `1` | The lint completed and found one or more findings. |
| `2` | A CLI, configuration, filesystem, or Git error prevented a valid run. |

## Examples

### A clean corpus

This is the released CLI run against the repository's clean synthetic fixture:

```console
$ memory-lint --config sample-config.yaml --now 2026-08-10
Clean — no findings.
```

### Machine-readable findings

```bash
memory-lint \
  --config memory-lint.yaml \
  --format json \
  --now 2026-08-10
```

The JSON finding objects are stable inputs for an agent, CI annotation step,
or reporting script. Keep the exit status: an exit of `1` is findings, not a
failure to execute.

### Compare revisions

Compare every linted file changed from a Git ref:

```bash
memory-lint --config memory-lint.yaml --against origin/main
```

Or compare one explicit before/after pair:

```bash
memory-lint \
  --config memory-lint.yaml \
  --compare-before before.md \
  --compare-after after.md
```

`--against` uses read-only `git rev-parse`, `git diff`, and `git show` calls.
Added files have no earlier revision and are skipped by the no-op comparison.

## Project

- [PyPI package](https://pypi.org/project/memory-lint/)
- [Source](https://github.com/kiloloop/memory-lint)
- [Agent skill](https://github.com/kiloloop/kiloloop-skills/tree/main/skills/memory-lint)

## License

Apache-2.0. See [`LICENSE`](LICENSE).
