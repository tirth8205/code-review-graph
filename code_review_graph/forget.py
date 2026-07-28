"""Forget parsed files from the graph while keeping every derived layer sane.

Dropping a file's own nodes and edges is not enough to match the graph a full
rebuild without that file would produce:

* surviving files that referenced it keep dangling, still-qualified edges
  (a call resolved to ``other.py::helper`` stays pointing at a node that no
  longer exists instead of falling back to the bare ``helper``);
* the derived layers — execution flows, communities, the FTS index, and
  embeddings — continue to reference the deleted nodes.

``forget_files`` therefore removes the files, re-parses the surviving referrers
so their cross-file edges are re-derived exactly as a build would, re-runs the
repository-wide Python import resolver and shared post-processing pipeline
(which fully recomputes flows, communities, signatures, and FTS and re-resolves
bare endpoints), and purges embedding vectors whose node is gone. The result is
equivalent to building the graph without the forgotten files.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

from .graph import GraphStore

logger = logging.getLogger(__name__)

# Keep IN-clause windows comfortably under SQLite's default 999-variable limit.
_SQL_PARAM_CHUNK = 400


def _referrer_files(
    store: GraphStore,
    deleted_qualified_names: set[str],
    forgotten: set[str],
) -> list[str]:
    """Return surviving files whose edges point at any forgotten node.

    Those edges are precisely the ones a rebuild would re-derive (usually
    dropping back to a bare endpoint), so the files owning them must be
    re-parsed for parity.
    """
    if not deleted_qualified_names:
        return []
    conn = store._conn
    referrers: set[str] = set()
    names = list(deleted_qualified_names)
    for start in range(0, len(names), _SQL_PARAM_CHUNK):
        window = names[start:start + _SQL_PARAM_CHUNK]
        placeholders = ",".join("?" for _ in window)
        rows = conn.execute(
            f"SELECT DISTINCT file_path FROM edges "
            f"WHERE target_qualified IN ({placeholders}) "
            f"OR source_qualified IN ({placeholders})",
            window + window,
        ).fetchall()
        referrers.update(row["file_path"] for row in rows)
    return sorted(referrers - forgotten)


def _purge_orphan_embeddings(store: GraphStore) -> int:
    """Delete embedding vectors whose graph node no longer exists.

    Mirrors :meth:`embeddings.EmbeddingStore.purge_orphans` but runs on the
    graph's own connection so we never open a second writer. A graph without
    an embeddings table is a no-op.
    """
    conn = store._conn
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'embeddings'"
    ).fetchone()
    if has_table is None:
        return 0
    cursor = conn.execute(
        "DELETE FROM embeddings WHERE NOT EXISTS ("
        "SELECT 1 FROM nodes WHERE nodes.qualified_name = embeddings.qualified_name"
        ")"
    )
    return max(cursor.rowcount, 0)


def forget_files(
    store: GraphStore,
    repo_root: Path,
    targets: list[str],
) -> dict[str, Any]:
    """Remove ``targets`` from the graph and repair every derived layer.

    Args:
        store: An open graph store.
        repo_root: Repository root, used to re-parse surviving referrers.
        targets: Absolute file paths (as stored in the graph) to forget.

    Returns:
        A summary dict with the forgotten files, the referrer files that were
        re-parsed, and the number of orphaned embedding vectors purged.
    """
    from .parser import CodeParser
    from .postprocessing import run_post_processing
    from .python_resolver import resolve_python_imports

    forgotten = set(targets)

    # 1. Snapshot the qualified names about to disappear so we can find the
    #    surviving files that reference them (before we delete anything).
    deleted_qualified_names: set[str] = set()
    for file_path in targets:
        for node in store.get_nodes_by_file(file_path):
            deleted_qualified_names.add(node.qualified_name)

    referrers = _referrer_files(store, deleted_qualified_names, forgotten)

    # 2. Drop the forgotten files' own nodes and edges.
    for file_path in targets:
        store.remove_file_data(file_path)
    # Persist deletions before store_file_nodes_edges() opens its own
    # explicit transaction (BEGIN IMMEDIATE) during the re-parse below.
    store.commit()

    # 3. Re-parse the surviving referrers so their cross-file edges are
    #    re-derived exactly as a build would: edges that had resolved into a
    #    forgotten file fall back to bare and are re-resolved against the
    #    smaller graph, while edges into other survivors are preserved. The
    #    forgotten files are hidden from import resolution so a still-on-disk
    #    file is not silently re-resolved (forget removes it from the graph,
    #    not from the working tree).
    parser = CodeParser(repo_root)
    parser.exclude_files(forgotten)
    reparsed: list[str] = []
    for file_path in referrers:
        abs_path = Path(file_path)
        if not abs_path.is_file():
            # Referrer is gone from disk; nothing to re-parse. Its stale edges
            # are cleaned up by post-processing's bare re-resolution below.
            continue
        if parser.detect_language(abs_path) is None:
            continue
        try:
            source = abs_path.read_bytes()
            fhash = hashlib.sha256(source).hexdigest()
            nodes, edges = parser.parse_bytes(abs_path, source)
            store.store_file_nodes_edges(str(abs_path), nodes, edges, fhash)
            reparsed.append(file_path)
        except (OSError, PermissionError) as exc:
            logger.warning("Could not re-parse referrer %s: %s", file_path, exc)
        except Exception as exc:  # noqa: BLE001 - a parser failure is non-fatal
            logger.warning("Error re-parsing referrer %s: %s", file_path, exc)

    # 4. Re-run repository-wide Python import resolution. A forgotten file can
    #    turn an ambiguous module suffix into a unique survivor even when the
    #    import edge did not directly target the forgotten node, so referrer
    #    re-parsing alone cannot discover this transition.
    try:
        resolve_python_imports(store)
    except Exception as exc:  # noqa: BLE001 - resolver failure is non-fatal
        logger.warning("Python import resolver failed after forget: %s", exc)

    # 5. Re-run the shared post-processing pipeline. store_flows and
    #    store_communities clear their tables first, so flows and communities
    #    are fully recomputed; signatures and FTS are rebuilt; and any edge
    #    left bare by the re-parse is re-resolved.
    run_post_processing(store)

    # 6. Drop embedding vectors that now reference a deleted node.
    purged = _purge_orphan_embeddings(store)

    return {
        "forgotten": sorted(forgotten),
        "reparsed": reparsed,
        "embeddings_purged": purged,
    }
