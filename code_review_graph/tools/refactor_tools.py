"""Tools 17, 18: refactor_func, apply_refactor_func."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..hints import generate_hints, get_session
from ..incremental import find_project_root
from ..refactor import (
    apply_refactor,
    find_dead_code,
    rename_preview,
    suggest_refactorings,
)
from ._common import _get_store, _validate_repo_root

# ---------------------------------------------------------------------------
# Tool 17: refactor_tool  [REFACTOR]
# ---------------------------------------------------------------------------


def refactor_func(
    mode: str = "rename",
    old_name: str | None = None,
    new_name: str | None = None,
    kind: str | None = None,
    file_pattern: str | None = None,
    repo_root: str | None = None,
) -> dict[str, Any]:
    """Unified refactoring entry point.

    [REFACTOR] Supports three modes:
    - ``rename``: Preview renaming a symbol (requires *old_name* and
      *new_name*).
    - ``dead_code``: Find unreferenced functions/classes.
    - ``suggest``: Get community-driven refactoring suggestions.

    Args:
        mode: One of ``"rename"``, ``"dead_code"``, or ``"suggest"``.
        old_name: (rename mode) Current symbol name.
        new_name: (rename mode) Desired new name.
        kind: (dead_code mode) Optional node kind filter.
        file_pattern: (dead_code mode) Optional file path substring filter.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        Mode-specific results dict.
    """
    valid_modes = {"rename", "dead_code", "suggest"}
    if mode not in valid_modes:
        return {
            "status": "error",
            "error": (
                f"Invalid mode '{mode}'. "
                f"Must be one of: {', '.join(sorted(valid_modes))}"
            ),
        }

    store, root = _get_store(repo_root)
    try:
        if mode == "rename":
            if not old_name or not new_name:
                return {
                    "status": "error",
                    "error": (
                        "rename mode requires both old_name and new_name."
                    ),
                }
            preview = rename_preview(store, old_name, new_name)
            if preview is None:
                return {
                    "status": "not_found",
                    "summary": f"No node found matching '{old_name}'.",
                }
            result = {
                "status": "ok",
                "summary": (
                    f"Rename preview: {old_name} -> {new_name}, "
                    f"{len(preview['edits'])} edit(s). "
                    f"Use apply_refactor_tool(refactor_id="
                    f"'{preview['refactor_id']}') to apply."
                ),
                **preview,
            }
            result["_hints"] = generate_hints(
                "refactor", result, get_session()
            )
            return result

        elif mode == "dead_code":
            dead = find_dead_code(
                store, kind=kind, file_pattern=file_pattern
            )
            result = {
                "status": "ok",
                "summary": f"Found {len(dead)} dead code symbol(s).",
                "dead_code": dead,
                "total": len(dead),
            }
            result["_hints"] = generate_hints(
                "refactor", result, get_session()
            )
            return result

        else:  # suggest
            suggestions = suggest_refactorings(store)
            result = {
                "status": "ok",
                "summary": (
                    f"Generated {len(suggestions)} "
                    "refactoring suggestion(s)."
                ),
                "suggestions": suggestions,
                "total": len(suggestions),
            }
            result["_hints"] = generate_hints(
                "refactor", result, get_session()
            )
            return result

    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Tool 18: apply_refactor_tool  [REFACTOR]
# ---------------------------------------------------------------------------


def apply_refactor_func(
    refactor_id: str,
    repo_root: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Apply a previously previewed refactoring to source files.

    [REFACTOR] Validates the refactor_id, checks expiry, ensures all edit
    paths are within the repo root, then performs exact string replacements.

    Args:
        refactor_id: ID returned by a prior ``refactor_tool(mode="rename")``
            call.
        repo_root: Repository root path. Auto-detected if omitted.
        dry_run: If True, return a unified diff of what would change
            without touching disk. The refactor_id remains valid so the
            user can review the diff, then call again with ``dry_run=False``
            to actually write the changes. See: #176

    Returns:
        Status with count of applied edits and modified files. When
        ``dry_run=True`` the response additionally contains ``would_modify``
        (list of file paths) and ``diffs`` (map of file -> unified-diff
        string).
    """
    try:
        root = (
            _validate_repo_root(Path(repo_root))
            if repo_root
            else find_project_root()
        )
    except (RuntimeError, ValueError) as exc:
        return {"status": "error", "error": str(exc)}

    result = apply_refactor(refactor_id, root, dry_run=dry_run)
    return result
