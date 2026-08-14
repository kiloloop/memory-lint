# memory-lint

`memory-lint` is a deterministic, flags-only linter for Markdown memory and
documentation corpora. It reads target files and reports findings; it has no
write or auto-fix path for the corpus.

## Install

Install the released package from PyPI:

```bash
python -m pip install memory-lint
memory-lint --help
```

For development from a clone:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
memory-lint --config sample-config.yaml --now 2026-08-10
memory-lint --config sample-config.yaml --format json --now 2026-08-10
```

Every input is a named flag. The CLI accepts no positional corpus path.
`corpus_root` resolves relative to the config file; `--corpus-root` can point
the same surface profiles at another corpus without editing the config.

## Checks

- **Frontmatter:** required top-level keys, quoted top-level scalar keys
  (including `description`), and a configurable `type` enum per surface
  profile.
- **Links:** corpus-contained `[[wikilinks]]` and relative Markdown `.md`
  links, including missing, escaping, and ambiguous targets.
- **Indexes:** Markdown list rows whose targets are missing, duplicate rows,
  and included files missing from the index. Table rows do not count as index
  entries.
- **Managed markers:** orphaned, nested, mismatched, and unclosed marker blocks.
  These always report at `error` severity.
- **Revision comparisons:** SHA-256-identical and whitespace-only file pairs;
  under `--against`, missing linted files are errors and files that lose at
  least half their lines are warnings.
- **Staleness and structure:** configurable age checks for every unfenced
  `*Updated:* YYYY-MM-DD` header, missing or invalid headers, duplicate heading
  anchors, duplicate explicit anchors, and configurable maximum line length.

Profiles select files and settings independently:

```yaml
version: 1
corpus_root: ./docs
profiles:
  notes:
    include: [notes/**/*.md]
    exclude: [notes/archive/**/*.md]
    checks:
      frontmatter:
        required_keys: [title, description, type]
        quoted_keys: [description]
        type_enum: [memory, note]
      links: true
      managed_markers:
        pairs:
          - begin: "<!-- BEGIN MANAGED -->"
            end: "<!-- END MANAGED -->"
      staleness:
        max_age_days: 90
        updated_required: true
      structure:
        max_line_length: 2000
indexes:
  - path: MEMORY.md
    include: [notes/**/*.md]
    exclude: [notes/archive/**/*.md]
```

Glob patterns and configured index paths may not be absolute, contain `..`, or
end in a bare `**` component. Use a terminal file pattern such as `**/*.md` for
recursive discovery. Resolved links are also contained to the corpus root.

## Revision comparisons

Compare two explicit revisions:

```bash
memory-lint \
  --config sample-config.yaml \
  --compare-before fixtures/revisions/identical-before.md \
  --compare-after fixtures/revisions/identical-after.md
```

Compare linted files changed from a Git commit:

```bash
memory-lint --config path/to/config.yaml --against origin/main
```

`--against` uses read-only `git rev-parse`, `git diff`, and `git show` calls.
Added files have no earlier revision and are skipped by the no-op comparison.
Files matched by a profile at the ref but deleted from the worktree emit
`file-missing-vs-ref` errors. Modified files that lose at least 50% of their
lines emit `file-shrunk-vs-ref` warnings.

## Output and exit codes

`--format table` prints a stable human-readable table. `--format json` emits a
versioned object containing `corpus_root`, `finding_count`, and sorted finding
objects (`code`, `severity`, `path`, `line`, `message`, `profile`).

| Exit | Meaning |
| --- | --- |
| `0` | Clean |
| `1` | One or more findings |
| `2` | CLI, config, filesystem, or Git tool error |

Use `--now YYYY-MM-DD` to pin staleness checks in CI and tests; otherwise the
current UTC date is used.

## Synthetic fixture suite

All fixtures in this repository are invented for this tool. `fixtures/clean`
must stay quiet. `fixtures/defects` plants all 13 core defect classes.
`fixtures/revisions` contains explicit no-op pairs. No real user memory or
vault content belongs here.

```bash
pytest -q
```

Releases are published from version tags through PyPI Trusted Publishing with
attestations.
