"""Standards-index projection for compact review context."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .test_gap_config import path_matches_any

try:
    import yaml  # type: ignore[import-untyped]
except ModuleNotFoundError:  # pragma: no cover - exercised when PyYAML is absent
    yaml = None  # type: ignore[assignment]


DEFAULT_ACTIVITIES = (
    "creating or modifying authored source files, tests, scripts, standards docs, or agent skills",
    "reviewing code structure, module cohesion, duplicated logic, or redundant computation",
    "documenting OpenAPI responses",
    "writing python unit tests, db integration tests, or service-layer happy-path "
    "integration tests",
    "authoring or editing standards docs and agent guides",
)


def project_matched_standards(
    repo_root: Path | None,
    changed_files: list[str],
    activities: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Return compact standards-index matches for changed paths/activities."""
    if repo_root is None:
        return []
    index = _find_index(repo_root)
    if index is None:
        return []
    standards = _read_standards(index)
    active = tuple(activities or DEFAULT_ACTIVITIES)
    matches: list[dict[str, Any]] = []
    for standard in standards:
        applies = standard.get("applies_to", {})
        paths = _as_list(applies.get("paths"))
        configured_activities = _as_list(applies.get("activities"))
        path_hits = sorted(
            path for path in changed_files
            if paths and path_matches_any(path, tuple(paths))
        )
        activity_hits = sorted(set(active).intersection(configured_activities))
        if not path_hits and not activity_hits:
            continue
        reason = []
        if path_hits:
            reason.append(f"{len(path_hits)} changed path match(es)")
        if activity_hits:
            reason.append(f"{len(activity_hits)} activity match(es)")
        matches.append({
            "file": _standard_path(index, str(standard.get("file", ""))),
            "reason": "; ".join(reason),
            "matched_paths": path_hits[:5],
            "matched_activities": activity_hits[:3],
            "purpose": str(standard.get("purpose", ""))[:240],
        })
    return matches


def _find_index(repo_root: Path) -> Path | None:
    for rel in (
        ".zazz/standards/index.yaml",
        "docs/standards/index.yaml",
        ".zazz/standards/index.yml",
        "docs/standards/index.yml",
    ):
        candidate = repo_root / rel
        if candidate.exists():
            return candidate
    return None


def _read_standards(index: Path) -> list[dict[str, Any]]:
    text = index.read_text(encoding="utf-8")
    if yaml is not None:
        try:
            parsed = yaml.safe_load(text) or {}
            standards = parsed.get("standards", [])
            return [row for row in standards if isinstance(row, dict)]
        except Exception:
            return []
    return _read_standards_minimal(text)


def _read_standards_minimal(text: str) -> list[dict[str, Any]]:
    standards: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    section: str | None = None
    subkey: str | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- file:"):
            if current:
                standards.append(current)
            current = {"file": stripped.split(":", 1)[1].strip(), "applies_to": {}}
            section = None
            subkey = None
            continue
        if current is None:
            continue
        if stripped == "applies_to:":
            section = "applies_to"
            continue
        if stripped.startswith("purpose:"):
            current["purpose"] = stripped.split(":", 1)[1].strip(" >-")
            section = "purpose"
            continue
        if section == "applies_to" and stripped in {"paths:", "activities:"}:
            subkey = stripped[:-1]
            current["applies_to"].setdefault(subkey, [])
            continue
        if section == "applies_to" and stripped.startswith("- ") and subkey:
            current["applies_to"][subkey].append(stripped[2:])
        elif section == "purpose" and stripped:
            current["purpose"] = (current.get("purpose", "") + " " + stripped).strip()
    if current:
        standards.append(current)
    return standards


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        return [value]
    return []


def _standard_path(index: Path, file_name: str) -> str:
    if not file_name:
        return ""
    return (index.parent / file_name).as_posix()
