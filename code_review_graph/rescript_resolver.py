"""Post-build pass that resolves ReScript cross-module references.

The per-file parser emits CALLS/IMPORTS_FROM edges with bare targets like
``LogicUtils.safeParse`` because the parser only sees one file at a time.
This module runs after ``full_build`` / incremental updates and rewrites
those targets to canonical qualified names like
``<abs-path>/LogicUtils.res::safeParse`` so ``callers_of``,
``get_impact_radius`` and ``importers_of`` work correctly across files.

Resolutions performed:
    1. ``Module.fn`` / ``Module.Sub.fn`` CALLS edges → canonical node
       when a ``.res`` / ``.resi`` file with matching basename exists.
    2. Bare ``fn(...)`` CALLS edges in a file that ``open`` / ``include``\\s
       a module → canonical node in that module's file.
    3. IMPORTS_FROM edges targeting a module name (open / include / jsx /
       module_alias / external_module) → the target file path, so
       ``importers_of(<path>)`` finds every consuming file.

Only the ``target_qualified`` column is updated; source and edge kind are
preserved. Edges that cannot be resolved are left unchanged.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .graph import GraphStore

logger = logging.getLogger(__name__)


def resolve_rescript_cross_module(store: GraphStore) -> dict:
    """Resolve ReScript cross-module targets in the graph store.

    Safe to call multiple times: already-resolved edges (targets containing
    ``::``) are skipped.

    Returns a dict with resolution counts for telemetry.
    """
    conn = store._conn  # intentional: post-build maintenance pass

    # Basename (module name) → absolute file path, preferring .res over .resi.
    basename_to_path: dict[str, str] = {}
    rescript_files: set[str] = set()
    for file_path in store.get_all_files():
        p = Path(file_path)
        suffix = p.suffix.lower()
        if suffix not in (".res", ".resi"):
            continue
        rescript_files.add(file_path)
        stem = p.stem
        existing = basename_to_path.get(stem)
        if existing is None or existing.lower().endswith(".resi"):
            # Prefer implementation (.res) over interface (.resi).
            basename_to_path[stem] = file_path

    if not basename_to_path:
        return {"files_indexed": 0, "calls_resolved": 0, "imports_resolved": 0}

    # Per-file opens/includes so we can resolve bare calls.
    opens_by_file: dict[str, list[str]] = {}
    imports_rows = conn.execute(
        "SELECT source_qualified, target_qualified, file_path, extra "
        "FROM edges WHERE kind = 'IMPORTS_FROM'"
    ).fetchall()
    for row in imports_rows:
        fp = row["file_path"]
        if fp not in rescript_files:
            continue
        try:
            extra = json.loads(row["extra"] or "{}")
        except (json.JSONDecodeError, TypeError):
            extra = {}
        kind = extra.get("rescript_import_kind")
        if kind in ("open", "include"):
            # Strip nested submodule — root determines file.
            root = row["target_qualified"].split(".", 1)[0]
            opens_by_file.setdefault(fp, []).append(root)

    # --- 1 + 2. Resolve CALLS edges ---
    call_rows = conn.execute(
        "SELECT id, source_qualified, target_qualified, file_path "
        "FROM edges WHERE kind = 'CALLS'"
    ).fetchall()

    call_updates: list[tuple[str, int]] = []
    for row in call_rows:
        target = row["target_qualified"]
        if "::" in target:
            continue  # already resolved

        resolved = _resolve_call_target(
            target,
            row["file_path"],
            basename_to_path,
            opens_by_file,
            store,
        )
        if resolved and resolved != target:
            call_updates.append((resolved, row["id"]))

    # --- 3. Resolve IMPORTS_FROM edge targets to file paths ---
    import_updates: list[tuple[str, int]] = []
    import_rows_full = conn.execute(
        "SELECT id, target_qualified, file_path FROM edges "
        "WHERE kind = 'IMPORTS_FROM'"
    ).fetchall()
    for row in import_rows_full:
        target = row["target_qualified"]
        if target in rescript_files:
            continue  # already a file path
        if "/" in target or "\\" in target:
            continue  # looks like a path already (e.g. relative JS import)
        root = target.split(".", 1)[0]
        file_target = basename_to_path.get(root)
        if file_target and file_target != target:
            import_updates.append((file_target, row["id"]))

    cur = conn.cursor()
    for new_target, edge_id in call_updates:
        cur.execute(
            "UPDATE edges SET target_qualified = ? WHERE id = ?",
            (new_target, edge_id),
        )
    for new_target, edge_id in import_updates:
        cur.execute(
            "UPDATE edges SET target_qualified = ? WHERE id = ?",
            (new_target, edge_id),
        )
    conn.commit()
    store._invalidate_cache()

    result = {
        "files_indexed": len(basename_to_path),
        "calls_resolved": len(call_updates),
        "imports_resolved": len(import_updates),
    }
    logger.info("ReScript cross-module resolution: %s", result)
    return result


def _resolve_call_target(
    target: str,
    file_path: str,
    basename_to_path: dict[str, str],
    opens_by_file: dict[str, list[str]],
    store: GraphStore,
) -> str | None:
    """Resolve a CALLS edge's ``target_qualified`` to a canonical qualified
    node name. Returns None when no resolution is possible.
    """
    # Dotted: `Module.fn` or `Module.Sub.fn`.
    if "." in target:
        head, _, rest = target.partition(".")
        target_file = basename_to_path.get(head)
        if target_file is None:
            return None
        candidate = _pick_existing_qualified(target_file, rest, store)
        return candidate

    # Bare: `fn` — only resolvable via an open/include in the calling file.
    for opened in opens_by_file.get(file_path, []):
        target_file = basename_to_path.get(opened)
        if target_file is None:
            continue
        candidate = f"{target_file}::{target}"
        if store.get_node(candidate) is not None:
            return candidate
    return None


def _pick_existing_qualified(
    target_file: str, rest: str, store: GraphStore,
) -> str | None:
    """Given ``LogicUtils.foo.bar``, try ``file::foo.bar`` then
    ``file::Foo.bar`` then ``file::foo``. Return the first one that
    corresponds to an existing node.
    """
    # Direct: rest as the qualified name tail.
    direct = f"{target_file}::{rest}"
    if store.get_node(direct) is not None:
        return direct

    # Dotted rest like `Sub.fn`: parent_name = Sub, name = fn.
    # _qualify formats it the same way, so `direct` would already match if
    # the node was stored with that exact qualified name.

    # Some targets include a trailing member-access that isn't part of
    # the qualified node (e.g. `LogicUtils.safeParse.resp` — property on
    # the result). Try peeling from the right.
    parts = rest.split(".")
    while len(parts) > 1:
        parts.pop()
        candidate = f"{target_file}::{'.'.join(parts)}"
        if store.get_node(candidate) is not None:
            return candidate
    # Last resort: top-level `file::name` (first part only).
    first = rest.split(".", 1)[0]
    candidate = f"{target_file}::{first}"
    if store.get_node(candidate) is not None:
        return candidate
    return None
