"""Tool 1: build_or_update_graph."""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from ..incremental import full_build, incremental_update
from ._common import _get_store

logger = logging.getLogger(__name__)


def build_or_update_graph(
    full_rebuild: bool = False,
    repo_root: str | None = None,
    base: str = "HEAD~1",
    recurse_submodules: bool | None = None,
) -> dict[str, Any]:
    """Build or incrementally update the code knowledge graph.

    Args:
        full_rebuild: If True, re-parse every file. If False (default),
                      only re-parse files changed since `base`.
        repo_root: Path to the repository root. Auto-detected if omitted.
        base: Git ref for incremental diff (default: HEAD~1).
        recurse_submodules: If True, include files from git submodules
            via ``git ls-files --recurse-submodules``. When None
            (default), falls back to the CRG_RECURSE_SUBMODULES
            environment variable. Default: disabled.

    Returns:
        Summary with files_parsed/updated, node/edge counts, and errors.
    """
    store, root = _get_store(repo_root)
    try:
        if full_rebuild:
            result = full_build(root, store, recurse_submodules)
            build_result = {
                "status": "ok",
                "build_type": "full",
                "summary": (
                    f"Full build complete: parsed {result['files_parsed']} files, "
                    f"created {result['total_nodes']} nodes and "
                    f"{result['total_edges']} edges."
                ),
                **result,
            }
        else:
            result = incremental_update(root, store, base=base)
            if result["files_updated"] == 0:
                return {
                    "status": "ok",
                    "build_type": "incremental",
                    "summary": "No changes detected. Graph is up to date.",
                    **result,
                }
            build_result = {
                "status": "ok",
                "build_type": "incremental",
                "summary": (
                    f"Incremental update: {result['files_updated']} files re-parsed, "
                    f"{result['total_nodes']} nodes and "
                    f"{result['total_edges']} edges updated. "
                    f"Changed: {result['changed_files']}. "
                    f"Dependents also updated: {result['dependent_files']}."
                ),
                **result,
            }

        # -- Post-build steps (non-fatal; failures are surfaced as warnings) --
        warnings: list[str] = []

        # Compute signatures for nodes that don't have them
        try:
            rows = store.get_nodes_without_signature()
            for row in rows:
                node_id, name, kind, params, ret = (
                    row[0], row[1], row[2], row[3], row[4],
                )
                if kind in ("Function", "Test"):
                    sig = f"def {name}({params or ''})"
                    if ret:
                        sig += f" -> {ret}"
                elif kind == "Class":
                    sig = f"class {name}"
                else:
                    sig = name
                store.update_node_signature(node_id, sig[:512])
            store.commit()
        except (sqlite3.OperationalError, TypeError, KeyError) as e:
            logger.warning("Signature computation failed: %s", e)
            warnings.append(f"Signature computation failed: {type(e).__name__}: {e}")

        # Rebuild FTS index
        try:
            from code_review_graph.search import rebuild_fts_index

            fts_count = rebuild_fts_index(store)
            build_result["fts_indexed"] = fts_count
        except (sqlite3.OperationalError, ImportError) as e:
            logger.warning("FTS index rebuild failed: %s", e)
            warnings.append(f"FTS index rebuild failed: {type(e).__name__}: {e}")

        # Trace execution flows
        try:
            from code_review_graph.flows import store_flows as _store_flows
            from code_review_graph.flows import trace_flows as _trace_flows

            flows = _trace_flows(store)
            count = _store_flows(store, flows)
            build_result["flows_detected"] = count
        except (sqlite3.OperationalError, ImportError) as e:
            logger.warning("Flow detection failed: %s", e)
            warnings.append(f"Flow detection failed: {type(e).__name__}: {e}")

        # Detect communities
        try:
            from code_review_graph.communities import (
                detect_communities as _detect_communities,
            )
            from code_review_graph.communities import (
                store_communities as _store_communities,
            )

            comms = _detect_communities(store)
            count = _store_communities(store, comms)
            build_result["communities_detected"] = count
        except (sqlite3.OperationalError, ImportError) as e:
            logger.warning("Community detection failed: %s", e)
            warnings.append(f"Community detection failed: {type(e).__name__}: {e}")

        if warnings:
            build_result["warnings"] = warnings
        return build_result
    finally:
        store.close()
