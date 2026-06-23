"""Configured suppression rules for low-signal test-gap rows."""

from __future__ import annotations

import fnmatch
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from .incremental import _should_ignore


@dataclass(frozen=True)
class TestGapSuppression:
    """One configured rule for suppressing noisy missing-test rows."""

    __test__ = False

    path_globs: tuple[str, ...] = field(default_factory=tuple)
    kinds: tuple[str, ...] = field(default_factory=tuple)
    name_patterns: tuple[str, ...] = field(default_factory=tuple)
    reason: str = ""


def _as_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        return tuple(str(item) for item in value)
    return ()


def load_test_gap_suppressions(repo_root: Path | None) -> list[TestGapSuppression]:
    """Load optional suppressions from ``pyproject.toml``.

    The supported shape is either a list of inline tables or a table with a
    ``rules`` list under ``[tool.code-review-graph.test_gap_suppressions]``.
    """
    if repo_root is None:
        return []
    config_path = repo_root / "pyproject.toml"
    if not config_path.exists():
        return []

    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return []

    section = (
        data.get("tool", {})
        .get("code-review-graph", {})
        .get("test_gap_suppressions", {})
    )
    if isinstance(section, list):
        raw_rules = section
    elif isinstance(section, dict):
        raw_rules = section.get("rules", [])
    else:
        raw_rules = []

    suppressions: list[TestGapSuppression] = []
    for raw in raw_rules:
        if not isinstance(raw, dict):
            continue
        suppressions.append(
            TestGapSuppression(
                path_globs=_as_tuple(raw.get("path_globs") or raw.get("paths")),
                kinds=_as_tuple(raw.get("kinds") or raw.get("kind")),
                name_patterns=_as_tuple(
                    raw.get("name_patterns") or raw.get("names")
                ),
                reason=str(raw.get("reason") or ""),
            )
        )
    return suppressions


def path_matches_any(path: str, patterns: list[str] | tuple[str, ...] | None) -> bool:
    """Return True when a repo-relative path matches any ignore-style glob."""
    if not patterns:
        return True
    normalized = PurePosixPath(path.replace("\\", "/")).as_posix()
    for pattern in patterns:
        if _should_ignore(normalized, [pattern]):
            return True
        if fnmatch.fnmatch(normalized, pattern):
            return True
    return False


def _name_matches(name: str, patterns: tuple[str, ...]) -> bool:
    if not patterns:
        return True
    for pattern in patterns:
        if fnmatch.fnmatch(name, pattern):
            return True
        try:
            if re.search(pattern, name):
                return True
        except re.error:
            continue
    return False


def is_test_gap_suppressed(
    node: object,
    *,
    repo_relative_path: str,
    suppressions: list[TestGapSuppression],
) -> bool:
    """Return True when a changed node's missing test edge is configured noise."""
    if not suppressions:
        return False

    kind = str(getattr(node, "kind", ""))
    name = str(getattr(node, "name", ""))
    qualified_name = str(getattr(node, "qualified_name", ""))

    for suppression in suppressions:
        if not path_matches_any(repo_relative_path, suppression.path_globs):
            continue
        if suppression.kinds and kind not in suppression.kinds:
            continue
        if (
            not _name_matches(name, suppression.name_patterns)
            and not _name_matches(qualified_name, suppression.name_patterns)
        ):
            continue
        return True
    return False
