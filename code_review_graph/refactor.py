"""Graph-powered refactoring operations.

Provides rename previews, dead code detection, refactoring suggestions,
and safe application of refactoring edits to source files. All file writes
go through a preview-then-apply workflow with expiry enforcement and path
traversal prevention.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from .flows import _has_framework_decorator, _matches_entry_name
from .graph import GraphStore, _sanitize_name

logger = logging.getLogger(__name__)

# Base class names that indicate a framework-managed class (ORM models,
# Pydantic schemas, settings).  Classes inheriting from these are invoked
# via metaclass/framework magic and should not be flagged as dead code.
_FRAMEWORK_BASE_CLASSES = frozenset({
    "Base", "DeclarativeBase", "Model", "BaseModel", "BaseSettings",
    "db.Model", "TableBase",
})

# ---------------------------------------------------------------------------
# Thread-safe pending refactors storage
# ---------------------------------------------------------------------------

_refactor_lock = threading.Lock()
_pending_refactors: dict[str, dict] = {}
REFACTOR_EXPIRY_SECONDS = 600  # 10 minutes


def _cleanup_expired() -> int:
    """Remove expired refactors from the pending dict.  Returns count removed."""
    now = time.time()
    expired = [
        rid for rid, r in _pending_refactors.items()
        if now - r["created_at"] > REFACTOR_EXPIRY_SECONDS
    ]
    for rid in expired:
        del _pending_refactors[rid]
    return len(expired)


# ---------------------------------------------------------------------------
# 1. rename_preview
# ---------------------------------------------------------------------------


def rename_preview(
    store: GraphStore,
    old_name: str,
    new_name: str,
) -> Optional[dict[str, Any]]:
    """Build a rename edit list for *old_name* -> *new_name*.

    Finds the node via ``store.search_nodes(old_name)``, collects
    definition and reference sites, generates a unique ``refactor_id``,
    and stores the preview in the thread-safe ``_pending_refactors`` dict.

    Returns:
        A refactor preview dict, or ``None`` if the node is not found.
    """
    candidates = store.search_nodes(old_name, limit=10)
    # Pick the best match: prefer exact name match.
    node = None
    for c in candidates:
        if c.name == old_name:
            node = c
            break
    if node is None and candidates:
        node = candidates[0]
    if node is None:
        logger.warning("rename_preview: node %r not found", old_name)
        return None

    edits: list[dict[str, Any]] = []

    # --- Definition site ---
    edits.append({
        "file": node.file_path,
        "line": node.line_start,
        "old": old_name,
        "new": new_name,
        "confidence": "high",
    })

    # --- Call sites (CALLS edges targeting this node) ---
    call_edges = store.get_edges_by_target(node.qualified_name)
    for edge in call_edges:
        if edge.kind == "CALLS":
            edits.append({
                "file": edge.file_path,
                "line": edge.line,
                "old": old_name,
                "new": new_name,
                "confidence": "high",
            })

    # Also search by bare name for unqualified edges.
    bare_edges = store.search_edges_by_target_name(old_name, kind="CALLS")
    seen = {(e["file"], e["line"]) for e in edits}
    for edge in bare_edges:
        key = (edge.file_path, edge.line)
        if key not in seen:
            edits.append({
                "file": edge.file_path,
                "line": edge.line,
                "old": old_name,
                "new": new_name,
                "confidence": "high",
            })
            seen.add(key)

    # --- Import sites (IMPORTS_FROM edges targeting this node) ---
    import_edges = store.get_edges_by_target(node.qualified_name)
    for edge in import_edges:
        if edge.kind == "IMPORTS_FROM":
            key = (edge.file_path, edge.line)
            if key not in seen:
                edits.append({
                    "file": edge.file_path,
                    "line": edge.line,
                    "old": old_name,
                    "new": new_name,
                    "confidence": "high",
                })
                seen.add(key)

    # --- Stats ---
    stats = {"high": 0, "medium": 0, "low": 0}
    for e in edits:
        stats[e["confidence"]] += 1

    refactor_id = uuid.uuid4().hex[:8]
    preview: dict[str, Any] = {
        "refactor_id": refactor_id,
        "type": "rename",
        "old_name": _sanitize_name(old_name),
        "new_name": _sanitize_name(new_name),
        "edits": edits,
        "stats": stats,
        "created_at": time.time(),
    }

    with _refactor_lock:
        _cleanup_expired()
        _pending_refactors[refactor_id] = preview

    logger.info(
        "rename_preview: created refactor %s (%s -> %s, %d edits)",
        refactor_id, old_name, new_name, len(edits),
    )
    return preview


# ---------------------------------------------------------------------------
# 2. find_dead_code
# ---------------------------------------------------------------------------


def _is_entry_point(node: Any) -> bool:
    """Check if a node looks like an entry point by name or decorator.

    Unlike ``flows.detect_entry_points()`` which treats ALL uncalled functions
    as entry points, this checks only for conventional name patterns and
    framework decorators -- the indicators that a function is *intentionally*
    an entry point rather than simply unreferenced dead code.
    """
    if _has_framework_decorator(node):
        return True
    if _matches_entry_name(node):
        return True
    return False


# Matches identifiers inside type annotations (e.g. "GoalCreate" in
# "body: GoalCreate", "Optional[UserResponse]", "list[Item]").
_TYPE_IDENT_RE = re.compile(r"[A-Z][A-Za-z0-9_]*")


def _collect_type_referenced_names(store: GraphStore) -> set[str]:
    """Collect class names that appear in function params or return types."""
    funcs = store.get_nodes_by_kind(kinds=["Function", "Test"])
    names: set[str] = set()
    for f in funcs:
        for text in (f.params, f.return_type):
            if text:
                names.update(_TYPE_IDENT_RE.findall(text))
    return names


def find_dead_code(
    store: GraphStore,
    kind: Optional[str] = None,
    file_pattern: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Find functions/classes with no callers, no test refs, and no importers.

    Entry points (functions matching framework decorators or conventional name
    patterns like ``main``, ``test_*``, ``handle_*``) are excluded.

    Args:
        store: The GraphStore instance.
        kind: Optional filter (e.g. ``"Function"`` or ``"Class"``).
        file_pattern: Optional file-path substring filter.

    Returns:
        List of dead-code dicts with name, qualified_name, kind, file, line.
    """
    # Query candidate nodes.
    candidates = store.get_nodes_by_kind(
        kinds=[kind] if kind else ["Function", "Class"],
        file_pattern=file_pattern,
    )

    # Build set of class names referenced in function type annotations.
    type_ref_names = _collect_type_referenced_names(store)

    dead: list[dict[str, Any]] = []

    for node in candidates:

        # Skip test nodes.
        if node.is_test:
            continue

        # Skip dunder methods -- invoked by runtime, never have explicit callers.
        if node.name.startswith("__") and node.name.endswith("__"):
            continue

        # Skip entry points (by name pattern or decorator, not just "uncalled").
        if _is_entry_point(node):
            continue

        # Skip classes referenced in type annotations (Pydantic schemas, etc.).
        if node.kind == "Class" and node.name in type_ref_names:
            continue

        # Skip classes inheriting from known framework bases (ORM models, etc.).
        if node.kind == "Class":
            outgoing = store.get_edges_by_source(node.qualified_name)
            base_names = {
                e.target_qualified.rsplit("::", 1)[-1]
                for e in outgoing if e.kind == "INHERITS"
            }
            if base_names & _FRAMEWORK_BASE_CLASSES:
                continue

        # Check for callers (CALLS), test refs (TESTED_BY), importers (IMPORTS_FROM).
        incoming = store.get_edges_by_target(node.qualified_name)
        has_callers = any(e.kind == "CALLS" for e in incoming)
        has_test_refs = any(e.kind == "TESTED_BY" for e in incoming)
        has_importers = any(e.kind == "IMPORTS_FROM" for e in incoming)

        if not has_callers and not has_test_refs and not has_importers:
            dead.append({
                "name": _sanitize_name(node.name),
                "qualified_name": _sanitize_name(node.qualified_name),
                "kind": node.kind,
                "file": node.file_path,
                "line": node.line_start,
            })

    logger.info("find_dead_code: found %d dead symbols", len(dead))
    return dead


# ---------------------------------------------------------------------------
# 3. suggest_refactorings
# ---------------------------------------------------------------------------


def suggest_refactorings(store: GraphStore) -> list[dict[str, Any]]:
    """Produce community-driven refactoring suggestions.

    Currently two categories:
    - **move**: Functions in Community A only called by Community B.
    - **remove**: Dead code (no callers, tests, or importers and not entry points).

    Returns:
        List of suggestion dicts with type, description, symbols, rationale.
    """
    suggestions: list[dict[str, Any]] = []

    # --- Dead code suggestions ---
    dead = find_dead_code(store)
    for d in dead:
        suggestions.append({
            "type": "remove",
            "description": f"Remove unused {d['kind'].lower()} '{d['name']}'",
            "symbols": [d["qualified_name"]],
            "rationale": "No callers, no test references, no importers, not an entry point.",
        })

    # --- Cross-community move suggestions ---
    # Only attempt if communities table exists and has data.
    community_rows = store.get_communities_list()

    if community_rows:
        # Build node -> community_id mapping.
        node_community: dict[str, int] = {}
        for crow in community_rows:
            cid = crow["id"]
            member_qns = store.get_community_member_qns(cid)
            for qn in member_qns:
                node_community[qn] = cid

        community_names: dict[int, str] = {
            r["id"]: r["name"] for r in community_rows
        }

        # Check functions called only by members of a different community.
        all_funcs = store.get_nodes_by_kind(["Function"])

        for fnode in all_funcs:
            f_community = node_community.get(fnode.qualified_name)
            if f_community is None:
                continue

            incoming_calls = [
                e for e in store.get_edges_by_target(fnode.qualified_name)
                if e.kind == "CALLS"
            ]
            if not incoming_calls:
                continue

            caller_communities = set()
            for edge in incoming_calls:
                c_community = node_community.get(edge.source_qualified)
                if c_community is not None:
                    caller_communities.add(c_community)

            # If ALL callers are from a single *different* community, suggest move.
            if len(caller_communities) == 1:
                target_community = next(iter(caller_communities))
                if target_community != f_community:
                    src_name = community_names.get(f_community, f"community-{f_community}")
                    tgt_name = community_names.get(
                        target_community, f"community-{target_community}"
                    )
                    suggestions.append({
                        "type": "move",
                        "description": (
                            f"Move '{_sanitize_name(fnode.name)}' from "
                            f"'{src_name}' to '{tgt_name}'"
                        ),
                        "symbols": [_sanitize_name(fnode.qualified_name)],
                        "rationale": (
                            f"Function is in community '{src_name}' but only "
                            f"called by members of community '{tgt_name}'."
                        ),
                    })

    logger.info("suggest_refactorings: produced %d suggestions", len(suggestions))
    return suggestions


# ---------------------------------------------------------------------------
# 4. apply_refactor
# ---------------------------------------------------------------------------


def apply_refactor(
    refactor_id: str,
    repo_root: Path,
) -> dict[str, Any]:
    """Apply a previously previewed refactoring to source files.

    Validates the refactor_id, checks expiry, ensures all edit paths are
    within the repo root, then performs exact string replacements on the
    target files.

    Args:
        refactor_id: ID from a prior ``rename_preview`` call.
        repo_root: Validated repository root path.

    Returns:
        Status dict with applied count and modified files.
    """
    repo_root = repo_root.resolve()

    with _refactor_lock:
        _cleanup_expired()
        preview = _pending_refactors.get(refactor_id)

    if preview is None:
        logger.warning("apply_refactor: unknown or expired refactor_id %s", refactor_id)
        return {"status": "error", "error": f"Refactor '{refactor_id}' not found or expired."}

    # Check expiry explicitly.
    age = time.time() - preview["created_at"]
    if age > REFACTOR_EXPIRY_SECONDS:
        with _refactor_lock:
            _pending_refactors.pop(refactor_id, None)
        logger.warning("apply_refactor: refactor %s expired (%.0fs old)", refactor_id, age)
        return {"status": "error", "error": f"Refactor '{refactor_id}' has expired."}

    edits = preview.get("edits", [])
    if not edits:
        return {"status": "ok", "applied": 0, "files_modified": [], "edits_applied": 0}

    # --- Path traversal validation ---
    for edit in edits:
        edit_path = Path(edit["file"]).resolve()
        try:
            edit_path.relative_to(repo_root)
        except ValueError:
            logger.error(
                "apply_refactor: path traversal blocked for %s (repo_root=%s)",
                edit_path, repo_root,
            )
            return {
                "status": "error",
                "error": f"Edit path '{edit['file']}' is outside repo root.",
            }

    # --- Apply edits ---
    files_modified: set[str] = set()
    edits_applied = 0

    for edit in edits:
        file_path = Path(edit["file"])
        old_text = edit["old"]
        new_text = edit["new"]

        if not file_path.is_file():
            logger.warning("apply_refactor: file not found: %s", file_path)
            continue

        try:
            content = file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning("apply_refactor: could not read %s: %s", file_path, exc)
            continue

        if old_text not in content:
            logger.warning(
                "apply_refactor: old text %r not found in %s", old_text, file_path,
            )
            continue

        # Line-targeted replacement to avoid corrupting unrelated occurrences.
        target_line = edit.get("line")
        if target_line is not None:
            lines = content.splitlines(keepends=True)
            idx = target_line - 1  # 0-indexed
            if 0 <= idx < len(lines) and old_text in lines[idx]:
                lines[idx] = lines[idx].replace(old_text, new_text, 1)
                new_content = "".join(lines)
            else:
                # Fall back to first-occurrence replacement if line doesn't match.
                new_content = content.replace(old_text, new_text, 1)
        else:
            new_content = content.replace(old_text, new_text, 1)
        try:
            file_path.write_text(new_content, encoding="utf-8")
            edits_applied += 1
            files_modified.add(str(file_path))
            logger.info("apply_refactor: applied edit to %s", file_path)
        except OSError as exc:
            logger.error("apply_refactor: could not write %s: %s", file_path, exc)

    # Remove from pending after successful application.
    with _refactor_lock:
        _pending_refactors.pop(refactor_id, None)

    result = {
        "status": "ok",
        "applied": edits_applied,
        "files_modified": sorted(files_modified),
        "edits_applied": edits_applied,
    }
    logger.info("apply_refactor: completed %s — %d edits applied", refactor_id, edits_applied)
    return result
