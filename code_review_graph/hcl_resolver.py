"""Post-build resolution for Terraform module-scoped graph relationships."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .graph import GraphStore

logger = logging.getLogger(__name__)


def resolve_hcl_module_references(store: GraphStore) -> dict[str, int]:
    """Connect Terraform references across sibling files in one module.

    Terraform treats every ``.tf`` file in a directory as one module. The
    parser is intentionally file-local, so it emits a same-file placeholder
    target first; this pass replaces it only when one node with that name
    exists in the source file's directory. Local module sources are also
    connected to a parsed target file, preferring ``main.tf``.
    """
    conn = store._conn  # intentional: bounded post-build maintenance pass
    node_rows = conn.execute(
        "SELECT name, qualified_name, file_path, kind "
        "FROM nodes WHERE language = 'hcl'"
    ).fetchall()
    if not node_rows:
        return {
            "files_indexed": 0,
            "references_resolved": 0,
            "imports_resolved": 0,
        }

    hcl_files = {
        row["file_path"] for row in node_rows if row["kind"] == "File"
    }
    nodes_by_module_name: dict[tuple[str, str], list[str]] = {}
    known_qns: set[str] = set()
    for row in node_rows:
        if row["kind"] == "File":
            continue
        qn = row["qualified_name"]
        known_qns.add(qn)
        key = (str(Path(row["file_path"]).parent), row["name"])
        nodes_by_module_name.setdefault(key, []).append(qn)

    reference_updates: list[tuple[str, int]] = []
    for row in conn.execute(
        "SELECT id, target_qualified, file_path FROM edges "
        "WHERE kind = 'REFERENCES'"
    ).fetchall():
        if row["file_path"] not in hcl_files:
            continue
        target = row["target_qualified"]
        if target in known_qns:
            continue
        name = target.split("::", 1)[-1]
        key = (str(Path(row["file_path"]).parent), name)
        candidates = nodes_by_module_name.get(key, [])
        if len(candidates) == 1:
            reference_updates.append((candidates[0], row["id"]))

    files_by_dir: dict[str, list[str]] = {}
    for file_path in hcl_files:
        files_by_dir.setdefault(str(Path(file_path).parent), []).append(file_path)

    import_updates: list[tuple[str, int]] = []
    for row in conn.execute(
        "SELECT id, target_qualified, file_path FROM edges "
        "WHERE kind = 'IMPORTS_FROM'"
    ).fetchall():
        source_file = row["file_path"]
        target = row["target_qualified"]
        if source_file not in hcl_files or not target.startswith(("./", "../")):
            continue
        try:
            local_path = (Path(source_file).parent / target).resolve()
        except (OSError, RuntimeError, ValueError):
            continue

        if str(local_path) in hcl_files:
            resolved = str(local_path)
        else:
            candidates = sorted(files_by_dir.get(str(local_path), []))
            main_file = next(
                (candidate for candidate in candidates if Path(candidate).name == "main.tf"),
                None,
            )
            resolved = main_file or (candidates[0] if candidates else None)
        if resolved is not None:
            import_updates.append((resolved, row["id"]))

    conn.executemany(
        "UPDATE edges SET target_qualified = ? WHERE id = ?",
        reference_updates + import_updates,
    )
    conn.commit()
    if reference_updates or import_updates:
        store._invalidate_cache()

    result = {
        "files_indexed": len(hcl_files),
        "references_resolved": len(reference_updates),
        "imports_resolved": len(import_updates),
    }
    logger.info("Terraform/HCL module resolution: %s", result)
    return result
