"""Portable, compact projections for review-facing change analysis."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .context_savings import build_savings_record, estimate_tokens
from .review_contracts import build_review_advisories
from .test_gap_config import path_matches_any


@dataclass(frozen=True)
class ProjectionBudget:
    """Token budget controls for compact review output."""

    max_tokens: int | None
    minimum_envelope_tokens: int = 256


def to_repo_relative(path: str | None, repo_root: Path | None) -> str | None:
    """Return a portable repo-relative path when possible."""
    if path is None:
        return None
    normalized = path.replace("\\", "/")
    if repo_root is None:
        return normalized
    candidate = Path(path)
    try:
        return candidate.resolve().relative_to(repo_root.resolve()).as_posix()
    except (OSError, ValueError):
        return normalized


def priority_sort_key(row: dict[str, Any]) -> tuple[float, str, int, str]:
    """Sort by descending risk, then stable path/line/name keys."""
    risk = float(row.get("risk_score", 0.0))
    file_path = str(row.get("file_path") or row.get("file") or "")
    line = int(row.get("line_start") or row.get("line") or 0)
    name = str(row.get("qualified_name") or row.get("name") or "")
    return (-risk, file_path, line, name)


def _row_path(row: dict[str, Any]) -> str:
    return str(row.get("file_path") or row.get("file") or "")


def filter_by_path_globs(
    rows: Iterable[dict[str, Any]],
    globs: list[str] | None,
) -> list[dict[str, Any]]:
    """Return rows whose repo-relative file path matches the requested scope."""
    if not globs:
        return list(rows)
    return [row for row in rows if path_matches_any(_row_path(row), globs)]


def _compact_node(row: dict[str, Any], repo_root: Path | None) -> dict[str, Any]:
    file_path = to_repo_relative(str(row.get("file_path") or row.get("file") or ""), repo_root)
    line = row.get("line_start") or row.get("line")
    compact: dict[str, Any] = {
        "name": row.get("name"),
        "qualified_name": row.get("qualified_name"),
        "kind": row.get("kind"),
        "file": file_path,
        "file_path": file_path,
        "line_start": line,
        "line_end": row.get("line_end"),
        "risk_score": row.get("risk_score"),
    }
    return {key: value for key, value in compact.items() if value is not None}


def _compact_gap(row: dict[str, Any], repo_root: Path | None) -> dict[str, Any]:
    file_path = to_repo_relative(str(row.get("file") or row.get("file_path") or ""), repo_root)
    compact = {
        "name": row.get("name"),
        "qualified_name": row.get("qualified_name"),
        "file": file_path,
        "file_path": file_path,
        "line_start": row.get("line_start"),
        "line_end": row.get("line_end"),
    }
    return {key: value for key, value in compact.items() if value is not None}


def _compact_flow(row: dict[str, Any], repo_root: Path | None) -> dict[str, Any]:
    steps = []
    for step in row.get("steps", [])[:5]:
        file_path = to_repo_relative(step.get("file"), repo_root)
        steps.append({
            "name": step.get("name"),
            "kind": step.get("kind"),
            "file": file_path,
            "line_start": step.get("line_start"),
        })
    compact = {
        "id": row.get("id"),
        "name": row.get("name"),
        "criticality": row.get("criticality"),
        "depth": row.get("depth"),
        "node_count": row.get("node_count"),
        "file_count": row.get("file_count"),
        "steps": steps,
    }
    return {key: value for key, value in compact.items() if value not in (None, [])}


def _relative_changed_files(
    changed_files: list[str],
    repo_root: Path | None,
    path_globs: list[str] | None,
) -> list[str]:
    relative = [
        rel for file_path in changed_files
        if (rel := to_repo_relative(file_path, repo_root)) is not None
    ]
    return sorted(path for path in relative if path_matches_any(path, path_globs))


def project_for_review(
    analysis: dict[str, Any],
    *,
    repo_root: Path | None,
    changed_files: list[str],
    base: str,
    max_tokens: int | None,
    path_globs: list[str] | None = None,
    baseline_tokens: int | None = None,
) -> dict[str, Any]:
    """Build the compact, deterministic, budgeted review payload."""
    compact_functions = [
        _compact_node(row, repo_root)
        for row in analysis.get("changed_functions", [])
    ]
    compact_priorities = [
        _compact_node(row, repo_root)
        for row in analysis.get("review_priorities", [])
    ]
    compact_gaps = [
        _compact_gap(row, repo_root)
        for row in analysis.get("test_gaps", [])
    ]
    compact_flows = [
        _compact_flow(row, repo_root)
        for row in analysis.get("affected_flows", [])
    ]

    compact_functions = filter_by_path_globs(compact_functions, path_globs)
    compact_priorities = sorted(
        filter_by_path_globs(compact_priorities, path_globs),
        key=priority_sort_key,
    )
    compact_gaps = filter_by_path_globs(compact_gaps, path_globs)
    if path_globs:
        compact_flows = [
            flow for flow in compact_flows
            if any(
                path_matches_any(str(step.get("file", "")), path_globs)
                for step in flow.get("steps", [])
            )
        ]

    scoped_files = _relative_changed_files(changed_files, repo_root, path_globs)
    payload: dict[str, Any] = {
        "status": "ok",
        "summary": analysis.get("summary", ""),
        "base": base,
        "risk_score": analysis.get("risk_score", 0.0),
        "changed_file_count": len(scoped_files),
        "changed_files": scoped_files,
        "changed_functions": compact_functions,
        "review_priorities": compact_priorities,
        "test_gaps": compact_gaps,
        "affected_flows": compact_flows,
        "metadata": {
            "for_review": True,
            "measurement_scope": "change_analysis",
            "suppressed_test_gap_count": int(
                analysis.get("suppressed_test_gap_count", 0)
            ),
            "functions_truncated": bool(analysis.get("functions_truncated", False)),
            "scope": path_globs or [],
        },
    }
    payload.update(
        build_review_advisories(
            repo_root=repo_root,
            changed_files=scoped_files,
            changed_functions=compact_functions,
        )
    )
    baseline = baseline_tokens if baseline_tokens is not None else estimate_tokens(analysis)
    payload["savings_record"] = build_savings_record(
        base=base,
        changed_file_count=len(scoped_files),
        baseline_tokens=baseline,
        returned_tokens=estimate_tokens(payload),
    )
    payload = truncate_to_budget(payload, max_tokens)
    for _ in range(3):
        returned_tokens = estimate_tokens(payload)
        payload["savings_record"] = build_savings_record(
            base=base,
            changed_file_count=len(scoped_files),
            baseline_tokens=baseline,
            returned_tokens=returned_tokens,
        )
        if max_tokens is None or estimate_tokens(payload) <= max_tokens:
            break
        payload = truncate_to_budget(payload, max_tokens)
    if "budget" in payload:
        payload["budget"]["estimated_tokens"] = estimate_tokens(payload)
        if max_tokens is not None and payload["budget"]["estimated_tokens"] > max_tokens:
            payload = truncate_to_budget(payload, max_tokens)
            payload["savings_record"] = build_savings_record(
                base=base,
                changed_file_count=len(scoped_files),
                baseline_tokens=baseline,
                returned_tokens=estimate_tokens(payload),
            )
            payload["budget"]["estimated_tokens"] = estimate_tokens(payload)
        if max_tokens is not None:
            payload["budget"]["minimum_envelope_exceeded"] = (
                payload["budget"]["estimated_tokens"] > max_tokens
            )
    return payload


def _ensure_budget_metadata(payload: dict[str, Any], max_tokens: int | None) -> None:
    payload["budget"] = {
        "max_tokens": max_tokens,
        "estimated_tokens": estimate_tokens(payload),
        "truncated": False,
        "minimum_envelope_exceeded": False,
        "omitted": {},
    }


def truncate_to_budget(payload: dict[str, Any], max_tokens: int | None) -> dict[str, Any]:
    """Trim review lists until the payload fits the token budget."""
    projected = deepcopy(payload)
    _ensure_budget_metadata(projected, max_tokens)
    if max_tokens is None:
        return projected

    list_keys = [
        "review_priorities",
        "changed_functions",
        "test_gaps",
        "affected_flows",
        "synthetic_edges",
        "policy_gaps",
        "contract_shape_mismatches",
        "http_contract_mismatches",
        "matched_standards",
        "advisory_reconciliation",
    ]
    for key in list_keys:
        projected["budget"]["omitted"].setdefault(key, 0)

    while estimate_tokens(projected) > max_tokens:
        candidates = [
            key for key in list_keys
            if isinstance(projected.get(key), list) and projected[key]
        ]
        if not candidates:
            break
        key = max(candidates, key=lambda name: len(projected[name]))
        projected[key].pop()
        projected["budget"]["omitted"][key] += 1
        projected["budget"]["truncated"] = True

    projected["budget"]["estimated_tokens"] = estimate_tokens(projected)
    projected["budget"]["minimum_envelope_exceeded"] = (
        projected["budget"]["estimated_tokens"] > max_tokens
    )
    projected["budget"]["omitted_count"] = sum(projected["budget"]["omitted"].values())
    return projected
