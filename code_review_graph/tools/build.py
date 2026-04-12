"""Tool 1: build_or_update_graph + run_postprocess."""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any

from ..incremental import full_build, incremental_update
from ._common import _get_store

logger = logging.getLogger(__name__)


def _run_postprocess(
    store: Any,
    build_result: dict[str, Any],
    postprocess: str,
    full_rebuild: bool = False,
    changed_files: list[str] | None = None,
) -> list[str]:
    """Run post-build steps based on *postprocess* level.

    When *full_rebuild* is False and *changed_files* are available,
    uses incremental flow/community detection for faster updates.

    Returns a list of warning strings (empty on success).
    """
    warnings: list[str] = []
    build_result["postprocess_level"] = postprocess

    if postprocess == "none":
        return warnings

    # -- Signatures + FTS (fast, always run unless "none") --
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
        build_result["signatures_updated"] = True
    except (sqlite3.OperationalError, TypeError, KeyError) as e:
        logger.warning("Signature computation failed: %s", e)
        warnings.append(f"Signature computation failed: {type(e).__name__}: {e}")

    try:
        from code_review_graph.search import rebuild_fts_index

        fts_count = rebuild_fts_index(store)
        build_result["fts_indexed"] = fts_count
        build_result["fts_rebuilt"] = True
    except (sqlite3.OperationalError, ImportError) as e:
        logger.warning("FTS index rebuild failed: %s", e)
        warnings.append(f"FTS index rebuild failed: {type(e).__name__}: {e}")

    if postprocess == "minimal":
        return warnings

    # -- Expensive: flows + communities (only for "full") --
    use_incremental = not full_rebuild and bool(changed_files)

    try:
        if use_incremental:
            from code_review_graph.flows import incremental_trace_flows

            count = incremental_trace_flows(store, changed_files)
        else:
            from code_review_graph.flows import store_flows as _store_flows
            from code_review_graph.flows import trace_flows as _trace_flows

            flows = _trace_flows(store)
            count = _store_flows(store, flows)
        build_result["flows_detected"] = count
    except (sqlite3.OperationalError, ImportError) as e:
        logger.warning("Flow detection failed: %s", e)
        warnings.append(f"Flow detection failed: {type(e).__name__}: {e}")

    try:
        if use_incremental:
            from code_review_graph.communities import (
                incremental_detect_communities,
            )

            count = incremental_detect_communities(store, changed_files)
        else:
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

    # -- Compute pre-computed summary tables --
    try:
        _compute_summaries(store)
        build_result["summaries_computed"] = True
    except (sqlite3.OperationalError, Exception) as e:
        logger.warning("Summary computation failed: %s", e)
        warnings.append(f"Summary computation failed: {type(e).__name__}: {e}")

    store.set_metadata(
        "last_postprocessed_at", time.strftime("%Y-%m-%dT%H:%M:%S"),
    )
    store.set_metadata("postprocess_level", postprocess)

    return warnings


def _compute_summaries(store: Any) -> None:
    """Populate community_summaries, flow_snapshots, and risk_index tables.

    Each summary block (community_summaries, flow_snapshots, risk_index)
    is wrapped in an explicit transaction so the DELETE + INSERT sequence
    is atomic.  If a table doesn't exist yet the block is silently skipped.
    """
    import json as _json

    conn = store._conn

    # -- community_summaries --
    try:
        from os.path import commonprefix as _commonprefix
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM community_summaries")
        communities = conn.execute(
            "SELECT id, name, size, dominant_language FROM communities"
        ).fetchall()
        logger.info("Computing community summaries for %d communities...", len(communities))

        # Precompute top-5 symbols per community in a single aggregate query
        # (replaces 1 LEFT JOIN query per community).
        top_sym_rows = conn.execute(
            "SELECT n.community_id, n.name, "
            "COUNT(e1.id) + COUNT(e2.id) AS edge_count "
            "FROM nodes n "
            "LEFT JOIN edges e1 ON e1.source_qualified = n.qualified_name "
            "LEFT JOIN edges e2 ON e2.target_qualified = n.qualified_name "
            "WHERE n.community_id IS NOT NULL AND n.kind != 'File' "
            "GROUP BY n.community_id, n.id "
            "ORDER BY n.community_id, edge_count DESC"
        ).fetchall()
        top_syms_by_comm: dict[int, list[str]] = {}
        for sym_row in top_sym_rows:
            comm_id = sym_row[0]
            if comm_id not in top_syms_by_comm:
                top_syms_by_comm[comm_id] = []
            if len(top_syms_by_comm[comm_id]) < 5:
                top_syms_by_comm[comm_id].append(sym_row[1])

        # Precompute file paths per community in a single query
        # (replaces 1 SELECT DISTINCT query per community).
        file_path_rows = conn.execute(
            "SELECT community_id, file_path FROM nodes WHERE community_id IS NOT NULL"
        ).fetchall()
        paths_by_comm: dict[int, list[str]] = {}
        for fp_row in file_path_rows:
            comm_id = fp_row[0]
            if comm_id not in paths_by_comm:
                paths_by_comm[comm_id] = []
            paths_by_comm[comm_id].append(fp_row[1])

        # Build all rows in Python, then batch-insert in one statement.
        rows_to_insert = []
        for r in communities:
            cid, cname, csize, clang = r[0], r[1], r[2], r[3]
            key_syms = _json.dumps(top_syms_by_comm.get(cid, []))
            paths = paths_by_comm.get(cid, [])
            purpose = ""
            if paths:
                prefix = _commonprefix(paths)
                if "/" in prefix:
                    purpose = prefix.rsplit("/", 1)[0].split("/")[-1]
            rows_to_insert.append((cid, cname, purpose, key_syms, csize, clang or ""))

        conn.executemany(
            "INSERT OR REPLACE INTO community_summaries "
            "(community_id, name, purpose, key_symbols, size, dominant_language) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows_to_insert,
        )
        conn.commit()
        logger.info("Community summaries: %d rows written.", len(rows_to_insert))
    except sqlite3.OperationalError:
        conn.rollback()  # Table may not exist yet

    # -- flow_snapshots --
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM flow_snapshots")
        rows = conn.execute(
            "SELECT id, name, entry_point_id, criticality, node_count, "
            "file_count, path_json FROM flows"
        ).fetchall()
        for r in rows:
            fid = r[0]
            fname = r[1]
            ep_id = r[2]
            crit = r[3]
            ncount = r[4]
            fcount = r[5]
            # Get entry point name
            ep_row = conn.execute(
                "SELECT qualified_name FROM nodes WHERE id = ?", (ep_id,),
            ).fetchone()
            ep_name = ep_row[0] if ep_row else str(ep_id)
            # Compress path to entry + top 3 intermediate + exit
            path_ids = _json.loads(r[6]) if r[6] else []
            critical_path = []
            if path_ids:
                critical_path.append(ep_name)
                if len(path_ids) > 2:
                    # Pick up to 3 intermediate nodes
                    for nid in path_ids[1:4]:
                        nr = conn.execute(
                            "SELECT name FROM nodes WHERE id = ?", (nid,),
                        ).fetchone()
                        if nr:
                            critical_path.append(nr[0])
                if len(path_ids) > 1:
                    last = conn.execute(
                        "SELECT name FROM nodes WHERE id = ?",
                        (path_ids[-1],),
                    ).fetchone()
                    if last and last[0] not in critical_path:
                        critical_path.append(last[0])
            conn.execute(
                "INSERT OR REPLACE INTO flow_snapshots "
                "(flow_id, name, entry_point, critical_path, criticality, "
                "node_count, file_count) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (fid, fname, ep_name, _json.dumps(critical_path),
                 crit, ncount, fcount),
            )
        conn.commit()
    except sqlite3.OperationalError:
        conn.rollback()

    # -- risk_index --
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM risk_index")
        nodes = conn.execute(
            "SELECT id, qualified_name, name FROM nodes "
            "WHERE kind IN ('Function', 'Class', 'Test')"
        ).fetchall()
        security_kw = {
            "auth", "login", "password", "token", "session", "crypt",
            "secret", "credential", "permission", "sql", "execute",
        }
        logger.info("Computing risk index for %d nodes...", len(nodes))

        # Precompute caller counts for all nodes in one GROUP BY query
        # (replaces 1 COUNT query per node).
        caller_counts: dict[str, int] = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT target_qualified, COUNT(*) FROM edges "
                "WHERE kind = 'CALLS' GROUP BY target_qualified"
            ).fetchall()
        }

        # Precompute all tested qualified names in one query
        # (replaces 1 COUNT query per node).
        tested_qns: set[str] = {
            row[0]
            for row in conn.execute(
                "SELECT source_qualified FROM edges WHERE kind = 'TESTED_BY'"
            ).fetchall()
        }

        # Compute risk scores in Python, then batch-insert in one statement.
        risk_rows = []
        for n in nodes:
            nid, qn, name = n[0], n[1], n[2]
            caller_count = caller_counts.get(qn, 0)
            coverage = "tested" if qn in tested_qns else "untested"
            name_lower = name.lower()
            sec_relevant = 1 if any(kw in name_lower for kw in security_kw) else 0
            risk = 0.0
            if caller_count > 10:
                risk += 0.3
            elif caller_count > 3:
                risk += 0.15
            if coverage == "untested":
                risk += 0.3
            if sec_relevant:
                risk += 0.4
            risk = min(risk, 1.0)
            risk_rows.append((nid, qn, risk, caller_count, coverage, sec_relevant))

        conn.executemany(
            "INSERT OR REPLACE INTO risk_index "
            "(node_id, qualified_name, risk_score, caller_count, "
            "test_coverage, security_relevant, last_computed) "
            "VALUES (?, ?, ?, ?, ?, ?, datetime('now'))",
            risk_rows,
        )
        conn.commit()
        logger.info("Risk index: %d rows written.", len(risk_rows))
    except sqlite3.OperationalError:
        conn.rollback()


def build_or_update_graph(
    full_rebuild: bool = False,
    repo_root: str | None = None,
    base: str = "HEAD~1",
    postprocess: str = "full",
    recurse_submodules: bool | None = None,
) -> dict[str, Any]:
    """Build or incrementally update the code knowledge graph.

    Args:
        full_rebuild: If True, re-parse every file. If False (default),
                      only re-parse files changed since ``base``.
        repo_root: Path to the repository root. Auto-detected if omitted.
        base: Git ref for incremental diff (default: HEAD~1).
        postprocess: Post-processing level after build:
            ``"full"`` (default) — signatures, FTS, flows, communities.
            ``"minimal"`` — signatures + FTS only (fast, keeps search working).
            ``"none"`` — skip all post-processing (raw parse only).
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
                    "postprocess_level": postprocess,
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

        # Pass changed_files for incremental flow/community detection
        changed = result.get("changed_files") if not full_rebuild else None
        warnings = _run_postprocess(
            store, build_result, postprocess,
            full_rebuild=full_rebuild, changed_files=changed,
        )
        if warnings:
            build_result["warnings"] = warnings
        return build_result
    finally:
        store.close()


def run_postprocess(
    flows: bool = True,
    communities: bool = True,
    fts: bool = True,
    repo_root: str | None = None,
) -> dict[str, Any]:
    """Run post-processing steps on an existing graph.

    Useful for running expensive steps (flows, communities) separately
    from the build, or for re-running after the graph has been updated
    with ``postprocess="none"``.

    Args:
        flows: Run flow detection. Default: True.
        communities: Run community detection. Default: True.
        fts: Rebuild FTS index. Default: True.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        Summary of what was computed.
    """
    store, _root = _get_store(repo_root)
    result: dict[str, Any] = {"status": "ok"}
    warnings: list[str] = []

    try:
        # Signatures are always fast — run them
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
            result["signatures_updated"] = True
        except (sqlite3.OperationalError, TypeError, KeyError) as e:
            logger.warning("Signature computation failed: %s", e)
            warnings.append(f"Signature computation failed: {type(e).__name__}: {e}")

        if fts:
            try:
                from code_review_graph.search import rebuild_fts_index

                fts_count = rebuild_fts_index(store)
                result["fts_indexed"] = fts_count
            except (sqlite3.OperationalError, ImportError) as e:
                logger.warning("FTS index rebuild failed: %s", e)
                warnings.append(f"FTS index rebuild failed: {type(e).__name__}: {e}")

        if flows:
            try:
                from code_review_graph.flows import store_flows as _store_flows
                from code_review_graph.flows import trace_flows as _trace_flows

                traced = _trace_flows(store)
                count = _store_flows(store, traced)
                result["flows_detected"] = count
            except (sqlite3.OperationalError, ImportError) as e:
                logger.warning("Flow detection failed: %s", e)
                warnings.append(f"Flow detection failed: {type(e).__name__}: {e}")

        if communities:
            try:
                from code_review_graph.communities import (
                    detect_communities as _detect_communities,
                )
                from code_review_graph.communities import (
                    store_communities as _store_communities,
                )

                comms = _detect_communities(store)
                count = _store_communities(store, comms)
                result["communities_detected"] = count
            except (sqlite3.OperationalError, ImportError) as e:
                logger.warning("Community detection failed: %s", e)
                warnings.append(f"Community detection failed: {type(e).__name__}: {e}")

        store.set_metadata(
            "last_postprocessed_at", time.strftime("%Y-%m-%dT%H:%M:%S"),
        )
        result["summary"] = "Post-processing complete."
        if warnings:
            result["warnings"] = warnings
        return result
    finally:
        store.close()
