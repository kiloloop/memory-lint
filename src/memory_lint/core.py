"""Core checks for memory-lint.

The module deliberately contains no write path for a target corpus. The only
subprocesses used are read-only Git queries for ``--against`` comparisons.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import unquote
import fnmatch
import hashlib
import re
import subprocess

import yaml


ALLOWED_CHECKS = {
    "frontmatter",
    "links",
    "managed_markers",
    "staleness",
    "structure",
}
WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")
UPDATED_RE = re.compile(r"^\*Updated:\*\s*(\d{4}-\d{2}-\d{2})\s*$")
UPDATED_PREFIX_RE = re.compile(r"^\*Updated:\*")
FENCE_RE = re.compile(r"^[ ]{0,3}(`{3,}|~{3,})(.*)$")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
EXPLICIT_ANCHOR_RE = re.compile(r"(?:\bid=[\"']([^\"']+)[\"']|\{#([A-Za-z0-9_-]+)\})")
INDEX_LIST_ROW_RE = re.compile(r"^\s*(?:[-+*]|\d+[.)])\s+")
SUBSTANTIAL_LINE_LOSS_FRACTION = 0.5


class ToolError(RuntimeError):
    """A configuration, filesystem, or Git failure that prevents linting."""


@dataclass(frozen=True, slots=True)
class Finding:
    """One stable, machine-readable lint result."""

    code: str
    severity: str
    path: str
    line: int | None
    message: str
    profile: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Profile:
    name: str
    include: tuple[str, ...]
    exclude: tuple[str, ...]
    checks: Mapping[str, Mapping[str, Any]]


@dataclass(frozen=True, slots=True)
class Config:
    path: Path
    corpus_root: Path
    profiles: tuple[Profile, ...]
    indexes: tuple[Mapping[str, Any], ...]


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ToolError(f"{label} must be a mapping")
    return value


def _string_list(value: Any, label: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ToolError(f"{label} must be a list of non-empty strings")
    if not value and not allow_empty:
        raise ToolError(f"{label} must not be empty")
    return tuple(value)


def _validate_glob(pattern: str, label: str) -> None:
    pure = PurePosixPath(pattern)
    if pure.is_absolute() or ".." in pure.parts:
        raise ToolError(f"{label} must stay within the corpus root: {pattern!r}")
    if pure.parts and pure.parts[-1] == "**":
        raise ToolError(f"{label} must not end in a bare '**' component: {pattern!r}")


def _normalize_checks(raw: Any, label: str) -> Mapping[str, Mapping[str, Any]]:
    checks = _mapping(raw, label)
    unknown = sorted(set(checks) - ALLOWED_CHECKS)
    if unknown:
        raise ToolError(f"{label} contains unsupported checks: {', '.join(unknown)}")

    normalized: dict[str, Mapping[str, Any]] = {}
    for name, settings in checks.items():
        if settings is False or settings is None:
            continue
        if settings is True:
            normalized[name] = {}
        elif isinstance(settings, Mapping):
            normalized[name] = dict(settings)
        else:
            raise ToolError(f"{label}.{name} must be true, false, or a mapping")
    return normalized


def load_config(config_path: str | Path, corpus_root_override: str | Path | None = None) -> Config:
    """Load and validate a version-1 YAML config."""

    path = Path(config_path).expanduser().resolve()
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ToolError(f"cannot read config {path}: {exc}") from exc

    try:
        raw = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ToolError(f"invalid YAML in config {path}: {exc}") from exc
    root = _mapping(raw, "config")

    if root.get("version") != 1:
        raise ToolError("config.version must be 1")

    configured_root = root.get("corpus_root")
    if corpus_root_override is not None:
        corpus_root = Path(corpus_root_override).expanduser().resolve()
    elif isinstance(configured_root, str) and configured_root:
        corpus_root = (path.parent / configured_root).resolve()
    else:
        raise ToolError("config.corpus_root must be a non-empty path")
    if not corpus_root.is_dir():
        raise ToolError(f"corpus root is not a directory: {corpus_root}")

    profiles_raw = _mapping(root.get("profiles"), "config.profiles")
    if not profiles_raw:
        raise ToolError("config.profiles must not be empty")
    profiles: list[Profile] = []
    for name, profile_value in profiles_raw.items():
        if not isinstance(name, str) or not name:
            raise ToolError("profile names must be non-empty strings")
        profile = _mapping(profile_value, f"profile {name!r}")
        include = _string_list(profile.get("include"), f"profiles.{name}.include")
        exclude_value = profile.get("exclude", [])
        exclude = _string_list(exclude_value, f"profiles.{name}.exclude", allow_empty=True)
        for pattern in include + exclude:
            _validate_glob(pattern, f"profiles.{name}")
        checks = _normalize_checks(profile.get("checks"), f"profiles.{name}.checks")
        if not checks:
            raise ToolError(f"profiles.{name}.checks must enable at least one check")
        profiles.append(Profile(name=name, include=include, exclude=exclude, checks=checks))

    indexes_value = root.get("indexes", [])
    if not isinstance(indexes_value, list):
        raise ToolError("config.indexes must be a list")
    indexes: list[Mapping[str, Any]] = []
    for number, index_value in enumerate(indexes_value, start=1):
        index = _mapping(index_value, f"indexes[{number}]")
        index_path = index.get("path")
        if not isinstance(index_path, str) or not index_path:
            raise ToolError(f"indexes[{number}].path must be a non-empty string")
        _validate_glob(index_path, f"indexes[{number}].path")
        include = _string_list(index.get("include"), f"indexes[{number}].include")
        exclude = _string_list(index.get("exclude", []), f"indexes[{number}].exclude", allow_empty=True)
        for pattern in include + exclude:
            _validate_glob(pattern, f"indexes[{number}]")
        indexes.append({"path": index_path, "include": include, "exclude": exclude})

    return Config(path=path, corpus_root=corpus_root, profiles=tuple(profiles), indexes=tuple(indexes))


def _expand_globs(root: Path, patterns: Sequence[str]) -> set[Path]:
    paths: set[Path] = set()
    resolved_root = root.resolve()
    for pattern in patterns:
        for path in root.glob(pattern):
            if not path.is_file():
                continue
            resolved = path.resolve()
            try:
                resolved.relative_to(resolved_root)
            except ValueError as exc:
                raise ToolError(f"glob target resolves outside the corpus root: {path}") from exc
            paths.add(resolved)
    return paths


def _profile_files(config: Config, profile: Profile) -> tuple[Path, ...]:
    included = _expand_globs(config.corpus_root, profile.include)
    excluded = _expand_globs(config.corpus_root, profile.exclude)
    return tuple(sorted(included - excluded, key=lambda item: item.as_posix()))


def _display(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _finding(
    *, code: str, severity: str, path: Path, root: Path, line: int | None, message: str, profile: str
) -> Finding:
    return Finding(code=code, severity=severity, path=_display(path, root), line=line, message=message, profile=profile)


def _read_text(path: Path, root: Path, profile: str, findings: list[Finding]) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        findings.append(
            _finding(
                code="file-not-utf8",
                severity="error",
                path=path,
                root=root,
                line=None,
                message="file is not valid UTF-8",
                profile=profile,
            )
        )
    except OSError as exc:
        findings.append(
            _finding(
                code="file-read-error",
                severity="error",
                path=path,
                root=root,
                line=None,
                message=f"cannot read file: {exc}",
                profile=profile,
            )
        )
    return None


def _check_frontmatter(
    path: Path, text: str, settings: Mapping[str, Any], root: Path, profile: str
) -> list[Finding]:
    findings: list[Finding] = []
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return [
            _finding(
                code="frontmatter-missing",
                severity="error",
                path=path,
                root=root,
                line=1,
                message="file must begin with YAML frontmatter",
                profile=profile,
            )
        ]

    closing = next((index for index in range(1, len(lines)) if lines[index].strip() == "---"), None)
    if closing is None:
        return [
            _finding(
                code="frontmatter-unclosed",
                severity="error",
                path=path,
                root=root,
                line=1,
                message="frontmatter has no closing delimiter",
                profile=profile,
            )
        ]

    raw_lines = lines[1:closing]
    try:
        data = yaml.safe_load("\n".join(raw_lines))
    except yaml.YAMLError as exc:
        return [
            _finding(
                code="frontmatter-invalid",
                severity="error",
                path=path,
                root=root,
                line=2,
                message=f"frontmatter is invalid YAML: {exc}",
                profile=profile,
            )
        ]
    if not isinstance(data, Mapping):
        return [
            _finding(
                code="frontmatter-invalid",
                severity="error",
                path=path,
                root=root,
                line=2,
                message="frontmatter must be a mapping",
                profile=profile,
            )
        ]

    required = _string_list(settings.get("required_keys", []), "frontmatter.required_keys", allow_empty=True)
    quoted = _string_list(settings.get("quoted_keys", []), "frontmatter.quoted_keys", allow_empty=True)
    type_enum = _string_list(settings.get("type_enum", []), "frontmatter.type_enum", allow_empty=True)

    for key in required:
        if key not in data:
            findings.append(
                _finding(
                    code="frontmatter-required-key",
                    severity="error",
                    path=path,
                    root=root,
                    line=2,
                    message=f"required key is missing: {key}",
                    profile=profile,
                )
            )

    for key in quoted:
        key_re = re.compile(rf"^{re.escape(key)}\s*:\s*(.*)$")
        match_info = next(
            ((number + 2, match.group(1).lstrip()) for number, line in enumerate(raw_lines) if (match := key_re.match(line))),
            None,
        )
        if match_info is None:
            continue
        line_number, value = match_info
        if not value.startswith(("'", '"')):
            findings.append(
                _finding(
                    code="frontmatter-unquoted-value",
                    severity="error",
                    path=path,
                    root=root,
                    line=line_number,
                    message=f"{key} must use a quoted scalar value",
                    profile=profile,
                )
            )

    if type_enum and "type" in data and data["type"] not in type_enum:
        findings.append(
            _finding(
                code="frontmatter-type-enum",
                severity="error",
                path=path,
                root=root,
                line=next(
                    (number + 2 for number, line in enumerate(raw_lines) if re.match(r"^\s*type\s*:", line)),
                    2,
                ),
                message=f"type must be one of: {', '.join(type_enum)}",
                profile=profile,
            )
        )
    return findings


def _target_from_markdown(raw: str) -> str:
    value = raw.strip()
    if value.startswith("<") and ">" in value:
        return value[1 : value.index(">")]
    # Markdown titles follow the destination after whitespace. Paths with
    # spaces should be angle-bracketed or percent-encoded for deterministic v1 parsing.
    return value.split(maxsplit=1)[0]


def _resolve_link(root: Path, source: Path, raw_target: str, *, wiki: bool) -> tuple[Path | None, str | None]:
    target = raw_target.split("|", 1)[0] if wiki else _target_from_markdown(raw_target)
    target = unquote(target.split("#", 1)[0].split("?", 1)[0]).strip()
    if not target:
        return source, None
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", target) or target.startswith("//"):
        return source, "external"
    if wiki and Path(target).suffix == "":
        target = f"{target}.md"

    candidates: list[Path]
    if target.startswith("/"):
        candidates = [root / target.lstrip("/")]
    else:
        candidates = [source.parent / target]
        if wiki:
            candidates.append(root / target)

    contained: list[Path] = []
    for candidate in candidates:
        resolved = candidate.resolve()
        try:
            resolved.relative_to(root.resolve())
        except ValueError:
            continue
        contained.append(resolved)
        if resolved.is_file():
            return resolved, None

    if wiki and "/" not in target:
        matches = sorted((item.resolve() for item in root.rglob(Path(target).name) if item.is_file()), key=str)
        if len(matches) == 1:
            return matches[0], None
        if len(matches) > 1:
            return None, "ambiguous"
    if not contained:
        return None, "outside"
    return None, "missing"


def _iter_links(text: str) -> Iterable[tuple[int, str, bool]]:
    for line_number, line in enumerate(text.splitlines(), start=1):
        for match in WIKILINK_RE.finditer(line):
            yield line_number, match.group(1), True
        for match in MARKDOWN_LINK_RE.finditer(line):
            yield line_number, match.group(1), False


def _check_links(path: Path, text: str, root: Path, profile: str) -> list[Finding]:
    findings: list[Finding] = []
    for line, target, wiki in _iter_links(text):
        resolved, error = _resolve_link(root, path, target, wiki=wiki)
        if error == "external" or resolved is not None:
            continue
        code = "ambiguous-wikilink" if error == "ambiguous" else "broken-link"
        explanation = {
            "ambiguous": "matches more than one file",
            "outside": "escapes the corpus root",
            "missing": "does not resolve to a file",
        }.get(error, "does not resolve")
        findings.append(
            _finding(
                code=code,
                severity="error",
                path=path,
                root=root,
                line=line,
                message=f"link target {target!r} {explanation}",
                profile=profile,
            )
        )
    return findings


def _marker_pairs(settings: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    value = settings.get("pairs")
    if not isinstance(value, list) or not value:
        raise ToolError("managed_markers.pairs must be a non-empty list")
    pairs: list[tuple[str, str]] = []
    for number, raw in enumerate(value, start=1):
        pair = _mapping(raw, f"managed_markers.pairs[{number}]")
        begin, end = pair.get("begin"), pair.get("end")
        if not isinstance(begin, str) or not begin or not isinstance(end, str) or not end:
            raise ToolError(f"managed_markers.pairs[{number}] needs non-empty begin and end strings")
        if begin == end:
            raise ToolError(f"managed_markers.pairs[{number}] begin and end must differ")
        pairs.append((begin, end))
    return tuple(pairs)


def _line_tokens(line: str, pairs: Sequence[tuple[str, str]]) -> list[tuple[int, str, int]]:
    tokens: list[tuple[int, str, int]] = []
    for pair_number, (begin, end) in enumerate(pairs):
        for kind, marker in (("begin", begin), ("end", end)):
            start = 0
            while True:
                position = line.find(marker, start)
                if position < 0:
                    break
                tokens.append((position, kind, pair_number))
                start = position + len(marker)
    return sorted(tokens, key=lambda item: (item[0], 0 if item[1] == "begin" else 1, item[2]))


def _check_markers(
    path: Path, text: str, settings: Mapping[str, Any], root: Path, profile: str
) -> list[Finding]:
    pairs = _marker_pairs(settings)
    findings: list[Finding] = []
    stack: list[tuple[int, int]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        for _, kind, pair_number in _line_tokens(line, pairs):
            if kind == "begin":
                if stack:
                    findings.append(
                        _finding(
                            code="marker-nested",
                            severity="error",
                            path=path,
                            root=root,
                            line=line_number,
                            message="managed marker block begins inside another managed block",
                            profile=profile,
                        )
                    )
                stack.append((pair_number, line_number))
            elif not stack:
                findings.append(
                    _finding(
                        code="marker-orphaned",
                        severity="error",
                        path=path,
                        root=root,
                        line=line_number,
                        message="managed end marker has no matching begin marker",
                        profile=profile,
                    )
                )
            elif stack[-1][0] != pair_number:
                findings.append(
                    _finding(
                        code="marker-orphaned",
                        severity="error",
                        path=path,
                        root=root,
                        line=line_number,
                        message="managed end marker does not match the active block",
                        profile=profile,
                    )
                )
            else:
                stack.pop()
    for _, begin_line in stack:
        findings.append(
            _finding(
                code="marker-unclosed",
                severity="error",
                path=path,
                root=root,
                line=begin_line,
                message="managed begin marker has no matching end marker",
                profile=profile,
            )
        )
    return findings


def _check_staleness(
    path: Path, text: str, settings: Mapping[str, Any], root: Path, profile: str, today: date
) -> list[Finding]:
    max_age = settings.get("max_age_days")
    if not isinstance(max_age, int) or isinstance(max_age, bool) or max_age < 0:
        raise ToolError("staleness.max_age_days must be a non-negative integer")
    required = settings.get("updated_required", False)
    if not isinstance(required, bool):
        raise ToolError("staleness.updated_required must be a boolean")

    findings: list[Finding] = []
    header_seen = False
    fence_character: str | None = None
    fence_length = 0
    for line_number, line in enumerate(text.splitlines(), start=1):
        fence = FENCE_RE.match(line)
        if fence:
            marker, trailing = fence.groups()
            if fence_character is None:
                fence_character, fence_length = marker[0], len(marker)
            elif marker[0] == fence_character and len(marker) >= fence_length and not trailing.strip():
                fence_character, fence_length = None, 0
            continue
        if fence_character is not None:
            continue
        match = UPDATED_RE.match(line.strip())
        if match:
            header_seen = True
            try:
                updated = date.fromisoformat(match.group(1))
            except ValueError:
                findings.append(
                    _finding(
                        code="updated-invalid",
                        severity="error",
                        path=path,
                        root=root,
                        line=line_number,
                        message="Updated header must contain an ISO date",
                        profile=profile,
                    )
                )
                continue
            age = (today - updated).days
            if age < 0:
                findings.append(
                    _finding(
                        code="updated-in-future",
                        severity="warning",
                        path=path,
                        root=root,
                        line=line_number,
                        message=f"Updated header is {-age} day(s) in the future",
                        profile=profile,
                    )
                )
            elif age > max_age:
                findings.append(
                    _finding(
                        code="stale-updated",
                        severity="warning",
                        path=path,
                        root=root,
                        line=line_number,
                        message=f"Updated header is {age} days old; maximum is {max_age}",
                        profile=profile,
                    )
                )
        elif UPDATED_PREFIX_RE.match(line.strip()):
            header_seen = True
            findings.append(
                _finding(
                    code="updated-invalid",
                    severity="error",
                    path=path,
                    root=root,
                    line=line_number,
                    message="Updated header must contain an ISO date",
                    profile=profile,
                )
            )
    if required and not header_seen:
        findings.append(
            _finding(
                code="updated-missing",
                severity="error",
                path=path,
                root=root,
                line=1,
                message="required *Updated:* header is missing",
                profile=profile,
            )
        )
    return findings


def _slugify_heading(value: str) -> str:
    without_anchor = re.sub(r"\s*\{#[A-Za-z0-9_-]+\}\s*$", "", value)
    without_markup = re.sub(r"[`*_~]", "", without_anchor).strip().lower()
    without_punctuation = re.sub(r"[^\w\s-]", "", without_markup, flags=re.UNICODE)
    return re.sub(r"[-\s]+", "-", without_punctuation).strip("-")


def _check_structure(
    path: Path, text: str, settings: Mapping[str, Any], root: Path, profile: str
) -> list[Finding]:
    max_length = settings.get("max_line_length")
    if not isinstance(max_length, int) or isinstance(max_length, bool) or max_length < 1:
        raise ToolError("structure.max_line_length must be a positive integer")
    findings: list[Finding] = []
    headings: dict[str, int] = {}
    anchors: dict[str, int] = {}
    for line_number, line in enumerate(text.splitlines(), start=1):
        if len(line) > max_length:
            findings.append(
                _finding(
                    code="line-too-long",
                    severity="warning",
                    path=path,
                    root=root,
                    line=line_number,
                    message=f"line has {len(line)} characters; maximum is {max_length}",
                    profile=profile,
                )
            )
        heading = HEADING_RE.match(line)
        if heading:
            slug = _slugify_heading(heading.group(2))
            if slug and slug in headings:
                findings.append(
                    _finding(
                        code="duplicate-heading",
                        severity="error",
                        path=path,
                        root=root,
                        line=line_number,
                        message=f"heading anchor {slug!r} duplicates line {headings[slug]}",
                        profile=profile,
                    )
                )
            elif slug:
                headings[slug] = line_number
        for anchor_match in EXPLICIT_ANCHOR_RE.finditer(line):
            anchor = anchor_match.group(1) or anchor_match.group(2)
            if anchor in anchors:
                findings.append(
                    _finding(
                        code="duplicate-anchor",
                        severity="error",
                        path=path,
                        root=root,
                        line=line_number,
                        message=f"explicit anchor {anchor!r} duplicates line {anchors[anchor]}",
                        profile=profile,
                    )
                )
            else:
                anchors[anchor] = line_number
    return findings


def _check_indexes(config: Config, cache: dict[Path, str | None]) -> list[Finding]:
    findings: list[Finding] = []
    for index_number, index in enumerate(config.indexes, start=1):
        profile = f"index:{index['path']}"
        index_path = (config.corpus_root / str(index["path"])).resolve()
        try:
            index_path.relative_to(config.corpus_root)
        except ValueError as exc:
            raise ToolError(f"index path escapes corpus root: {index['path']}") from exc
        if not index_path.is_file():
            findings.append(
                _finding(
                    code="index-file-missing",
                    severity="error",
                    path=index_path,
                    root=config.corpus_root,
                    line=None,
                    message="configured index file does not exist",
                    profile=profile,
                )
            )
            continue

        if index_path not in cache:
            cache[index_path] = _read_text(index_path, config.corpus_root, profile, findings)
        text = cache[index_path]
        if text is None:
            continue

        referenced: set[str] = set()
        for line_number, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or stripped.startswith("<!--"):
                continue
            if not INDEX_LIST_ROW_RE.match(line):
                continue
            row_targets = list(_iter_links(line))
            if not row_targets:
                findings.append(
                    _finding(
                        code="index-row-invalid",
                        severity="warning",
                        path=index_path,
                        root=config.corpus_root,
                        line=line_number,
                        message="index row has no Markdown or wikilink target",
                        profile=profile,
                    )
                )
                continue
            for _, raw_target, wiki in row_targets:
                target, error = _resolve_link(config.corpus_root, index_path, raw_target, wiki=wiki)
                if error == "external":
                    continue
                if target is None:
                    findings.append(
                        _finding(
                            code="index-missing-target",
                            severity="error",
                            path=index_path,
                            root=config.corpus_root,
                            line=line_number,
                            message=f"index target {raw_target!r} does not resolve within the corpus",
                            profile=profile,
                        )
                    )
                    continue
                relative = _display(target, config.corpus_root)
                if relative in referenced:
                    findings.append(
                        _finding(
                            code="index-duplicate-entry",
                            severity="warning",
                            path=index_path,
                            root=config.corpus_root,
                            line=line_number,
                            message=f"index target appears more than once: {relative}",
                            profile=profile,
                        )
                    )
                referenced.add(relative)

        candidates = _expand_globs(config.corpus_root, index["include"])
        candidates -= _expand_globs(config.corpus_root, index["exclude"])
        candidates.discard(index_path)
        for candidate in sorted(candidates, key=lambda item: item.as_posix()):
            relative = _display(candidate, config.corpus_root)
            if relative not in referenced:
                findings.append(
                    _finding(
                        code="index-missing-entry",
                        severity="error",
                        path=candidate,
                        root=config.corpus_root,
                        line=None,
                        message=f"file is missing from {index['path']}",
                        profile=profile,
                    )
                )
    return findings


def _sort_findings(findings: Iterable[Finding]) -> list[Finding]:
    unique = {
        (item.code, item.severity, item.path, item.line, item.message, item.profile): item for item in findings
    }
    return sorted(
        unique.values(),
        key=lambda item: (item.path, item.line if item.line is not None else 0, item.code, item.profile, item.message),
    )


def lint_corpus(config: Config, *, today: date) -> tuple[list[Finding], tuple[Path, ...]]:
    """Run configured checks and return stable findings plus the linted file set."""

    findings: list[Finding] = []
    cache: dict[Path, str | None] = {}
    linted: set[Path] = set()
    for profile in config.profiles:
        for path in _profile_files(config, profile):
            linted.add(path)
            if path not in cache:
                cache[path] = _read_text(path, config.corpus_root, profile.name, findings)
            text = cache[path]
            if text is None:
                continue
            for check_name in ("frontmatter", "links", "managed_markers", "staleness", "structure"):
                if check_name not in profile.checks:
                    continue
                settings = profile.checks[check_name]
                if check_name == "frontmatter":
                    findings.extend(_check_frontmatter(path, text, settings, config.corpus_root, profile.name))
                elif check_name == "links":
                    findings.extend(_check_links(path, text, config.corpus_root, profile.name))
                elif check_name == "managed_markers":
                    findings.extend(_check_markers(path, text, settings, config.corpus_root, profile.name))
                elif check_name == "staleness":
                    findings.extend(_check_staleness(path, text, settings, config.corpus_root, profile.name, today))
                elif check_name == "structure":
                    findings.extend(_check_structure(path, text, settings, config.corpus_root, profile.name))
    findings.extend(_check_indexes(config, cache))
    return _sort_findings(findings), tuple(sorted(linted, key=lambda item: item.as_posix()))


def _comparison_finding(code: str, path: Path, root: Path, message: str) -> Finding:
    return _finding(
        code=code,
        severity="warning",
        path=path,
        root=root,
        line=None,
        message=message,
        profile="revision",
    )


def _compare_bytes(before: bytes, after: bytes, path: Path, root: Path) -> list[Finding]:
    if hashlib.sha256(before).digest() == hashlib.sha256(after).digest():
        return [
            _comparison_finding(
                "noop-identical", path, root, "revisions have identical SHA-256 content hashes"
            )
        ]
    try:
        before_without_whitespace = "".join(before.decode("utf-8").split())
        after_without_whitespace = "".join(after.decode("utf-8").split())
    except UnicodeDecodeError:
        return []
    if before_without_whitespace == after_without_whitespace:
        return [
            _comparison_finding(
                "noop-whitespace-only", path, root, "revisions differ only in whitespace"
            )
        ]
    return []


def _glob_matches(relative_path: PurePosixPath, pattern: str) -> bool:
    """Match one config glob with the recursive semantics of Path.glob()."""

    path_parts = relative_path.parts
    pattern_parts = PurePosixPath(pattern).parts

    @lru_cache(maxsize=None)
    def matches(pattern_index: int, path_index: int) -> bool:
        if pattern_index == len(pattern_parts):
            return path_index == len(path_parts)
        pattern_part = pattern_parts[pattern_index]
        if pattern_part == "**":
            return matches(pattern_index + 1, path_index) or (
                path_index < len(path_parts) and matches(pattern_index, path_index + 1)
            )
        return (
            path_index < len(path_parts)
            and fnmatch.fnmatchcase(path_parts[path_index], pattern_part)
            and matches(pattern_index + 1, path_index + 1)
        )

    return matches(0, 0)


def _matches_profile(config: Config, path: Path) -> bool:
    try:
        relative = PurePosixPath(path.resolve().relative_to(config.corpus_root.resolve()).as_posix())
    except ValueError:
        return False
    return any(
        any(_glob_matches(relative, pattern) for pattern in profile.include)
        and not any(_glob_matches(relative, pattern) for pattern in profile.exclude)
        for profile in config.profiles
    )


def _substantial_line_loss(before: bytes, after: bytes) -> tuple[int, int] | None:
    before_lines = len(before.splitlines())
    after_lines = len(after.splitlines())
    if not before_lines or after_lines >= before_lines:
        return None
    lost_lines = before_lines - after_lines
    if lost_lines / before_lines < SUBSTANTIAL_LINE_LOSS_FRACTION:
        return None
    return lost_lines, before_lines


def compare_files(before_path: str | Path, after_path: str | Path, *, display_root: Path) -> list[Finding]:
    """Compare two explicitly supplied revisions without changing either file."""

    before, after = Path(before_path).expanduser().resolve(), Path(after_path).expanduser().resolve()
    try:
        before_bytes, after_bytes = before.read_bytes(), after.read_bytes()
    except OSError as exc:
        raise ToolError(f"cannot read revision pair: {exc}") from exc
    return _compare_bytes(before_bytes, after_bytes, after, display_root)


def _git(args: Sequence[str], *, cwd: Path, text: bool = True) -> subprocess.CompletedProcess[Any]:
    try:
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            check=False,
            capture_output=True,
            text=text,
        )
    except OSError as exc:
        raise ToolError(f"cannot execute git: {exc}") from exc


def compare_against_git(config: Config, ref: str, linted_files: Sequence[Path]) -> list[Finding]:
    """Flag no-ops, missing files, and substantial truncation relative to a Git ref."""

    repo_result = _git(["rev-parse", "--show-toplevel"], cwd=config.corpus_root)
    if repo_result.returncode != 0:
        raise ToolError(f"corpus root is not inside a Git repository: {repo_result.stderr.strip()}")
    repo_root = Path(repo_result.stdout.strip()).resolve()
    verify = _git(["rev-parse", "--verify", f"{ref}^{{commit}}"], cwd=repo_root)
    if verify.returncode != 0:
        raise ToolError(f"Git ref does not resolve to a commit: {ref}")
    try:
        corpus_relative = config.corpus_root.relative_to(repo_root).as_posix() or "."
    except ValueError as exc:
        raise ToolError("corpus root is outside its reported Git repository") from exc

    changed = _git(
        [
            "-c",
            "core.quotepath=false",
            "diff",
            "--no-renames",
            "--name-only",
            "-z",
            "--diff-filter=AM",
            ref,
            "--",
            corpus_relative,
        ],
        cwd=repo_root,
        text=False,
    )
    if changed.returncode != 0:
        stderr = changed.stderr.decode("utf-8", errors="replace").strip()
        raise ToolError(f"cannot compare against Git ref {ref}: {stderr}")
    changed_paths = {
        (repo_root / raw.decode("utf-8")).resolve() for raw in changed.stdout.split(b"\0") if raw
    }
    eligible = sorted(changed_paths.intersection(path.resolve() for path in linted_files), key=str)

    deleted = _git(
        [
            "-c",
            "core.quotepath=false",
            "diff",
            "--no-renames",
            "--name-only",
            "-z",
            "--diff-filter=D",
            ref,
            "--",
            corpus_relative,
        ],
        cwd=repo_root,
        text=False,
    )
    if deleted.returncode != 0:
        stderr = deleted.stderr.decode("utf-8", errors="replace").strip()
        raise ToolError(f"cannot compare deleted files against Git ref {ref}: {stderr}")
    deleted_paths = sorted(
        (
            (repo_root / raw.decode("utf-8")).resolve()
            for raw in deleted.stdout.split(b"\0")
            if raw and _matches_profile(config, repo_root / raw.decode("utf-8"))
        ),
        key=str,
    )

    findings = [
        _finding(
            code="file-missing-vs-ref",
            severity="error",
            path=missing,
            root=config.corpus_root,
            line=None,
            message=f"file exists at {ref} but is missing from the worktree",
            profile="revision",
        )
        for missing in deleted_paths
    ]
    for current in eligible:
        relative = current.relative_to(repo_root).as_posix()
        baseline = _git(["show", f"{ref}:{relative}"], cwd=repo_root, text=False)
        if baseline.returncode != 0:
            continue  # Added files have no earlier revision to compare.
        try:
            current_bytes = current.read_bytes()
        except OSError as exc:
            raise ToolError(f"cannot read current revision {current}: {exc}") from exc
        findings.extend(_compare_bytes(baseline.stdout, current_bytes, current, config.corpus_root))
        line_loss = _substantial_line_loss(baseline.stdout, current_bytes)
        if line_loss is not None:
            lost_lines, before_lines = line_loss
            percent = round(100 * lost_lines / before_lines)
            findings.append(
                _comparison_finding(
                    "file-shrunk-vs-ref",
                    current,
                    config.corpus_root,
                    f"file lost {lost_lines} of {before_lines} line(s) ({percent}%) compared with {ref}",
                )
            )
    return _sort_findings(findings)
