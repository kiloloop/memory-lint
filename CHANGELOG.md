# Changelog

All notable changes to memory-lint are documented in this file. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — 2026-08-14

### Added

- Initial release: a linter for agent memory files detecting 13 core defect
  classes across structural, link, and consistency checks in Markdown memory
  files.
- Command-line interface (`memory-lint`) with a module entry point
  (`python -m memory_lint`), configurable via `sample-config.yaml`.
- Test suite and 21 lint fixtures exercising the diagnostic codes.
- Tag-triggered PyPI release workflow using Trusted Publishing (OIDC) with
  build attestations.
