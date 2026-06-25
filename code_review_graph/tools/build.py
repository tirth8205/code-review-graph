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
                row[0],
                row[1],
                row[2],
                row[3],
                row[4],
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
        "last_postprocessed_at",
        time.strftime("%Y-%m-%dT%H:%M:%S"),
    )
    store.set_metadata("postprocess_level", postprocess)

    return warnings


def _compute_summaries(store: Any) -> None:
    """Populate community_summaries, flow_snapshots, and risk_index tables.

    Uses batched aggregate queries and in-memory grouping instead of
    per-community/per-node loops. On graphs with ~100k edges this
    reduces the work from ``O(nodes + communities)`` SQLite round trips
    each doing their own B-tree scan to a handful of ``GROUP BY``
    queries, turning what used to be an effective hang into a few
    seconds.

    Each summary block (community_summaries, flow_snapshots, risk_index)
    is wrapped in an explicit transaction so the DELETE + INSERT sequence
    is atomic.  If a table doesn't exist yet the block is silently skipped.
    """
    import json as _json
    from collections import defaultdict
    from os.path import commonprefix

    conn = store._conn

    # -- community_summaries --
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM community_summaries")

        # Pre-compute per-qualified_name edge counts once. Previously
        # this section ran a per-community triple-JOIN aggregate query
        # (nodes LEFT JOIN edges LEFT JOIN edges), which on graphs with
        # thousands of communities was the second-biggest hang.
        edge_counts: dict[str, int] = defaultdict(int)
        for row in conn.execute(
            "SELECT source_qualified, COUNT(*) FROM edges GROUP BY source_qualified"
        ):
            edge_counts[row[0]] += row[1]
        for row in conn.execute(
            "SELECT target_qualified, COUNT(*) FROM edges GROUP BY target_qualified"
        ):
            edge_counts[row[0]] += row[1]

        # Group non-File nodes per community for top-symbol selection.
        nodes_by_comm: dict[int, list[tuple[str, int]]] = defaultdict(list)
        for row in conn.execute(
            "SELECT community_id, name, qualified_name FROM nodes "
            "WHERE community_id IS NOT NULL AND kind != 'File'"
        ):
            cid, name, qn = row[0], row[1], row[2]
            nodes_by_comm[cid].append((name, edge_counts.get(qn, 0)))

        # Group distinct file paths per community (preserving first-seen
        # order for stable output, same as DISTINCT in the old query).
        files_by_comm: dict[int, list[str]] = defaultdict(list)
        seen_files: dict[int, set[str]] = defaultdict(set)
        for row in conn.execute(
            "SELECT community_id, file_path FROM nodes WHERE community_id IS NOT NULL"
        ):
            cid, fp = row[0], row[1]
            if fp not in seen_files[cid]:
                seen_files[cid].add(fp)
                files_by_comm[cid].append(fp)

        community_rows = conn.execute(
            "SELECT id, name, size, dominant_language FROM communities"
        ).fetchall()
        for r in community_rows:
            cid, cname, csize, clang = r[0], r[1], r[2], r[3]

            # Top 5 symbols by total edge count (in + out). Python's
            # sorted() is stable so ties break by original row order.
            members = sorted(
                nodes_by_comm.get(cid, []),
                key=lambda nc: nc[1],
                reverse=True,
            )
            key_syms = _json.dumps([m[0] for m in members[:5]])

            # Auto-generate purpose from common file path prefix.
            paths = files_by_comm.get(cid, [])[:20]
            purpose = ""
            if paths:
                prefix = commonprefix(paths)
                if "/" in prefix:
                    purpose = prefix.rsplit("/", 1)[0].split("/")[-1] if "/" in prefix else ""

            conn.execute(
                "INSERT OR REPLACE INTO community_summaries "
                "(community_id, name, purpose, key_symbols, size, dominant_language) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (cid, cname, purpose, key_syms, csize, clang or ""),
            )
        conn.commit()
    except sqlite3.OperationalError:
        conn.rollback()  # Table may not exist yet

    # -- flow_snapshots --
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM flow_snapshots")
        flow_rows = conn.execute(
            "SELECT id, name, entry_point_id, criticality, node_count, "
            "file_count, path_json FROM flows"
        ).fetchall()

        # Collect every node id referenced by any flow, then fetch
        # their qualified_names in one batched query instead of per-flow
        # per-node lookups.
        needed_ids: set[int] = set()
        parsed_paths: list[list[int]] = []
        for r in flow_rows:
            needed_ids.add(r[2])  # entry_point_id
            path_ids = _json.loads(r[6]) if r[6] else []
            parsed_paths.append(path_ids)
            # Match the old semantics: entry + up to 3 intermediates + last
            for nid in path_ids[1:4]:
                needed_ids.add(nid)
            if path_ids:
                needed_ids.add(path_ids[-1])

        id_to_name: dict[int, str] = {}
        if needed_ids:
            # Batch the IN clause in chunks of 450 to stay under SQLite's
            # default SQLITE_MAX_VARIABLE_NUMBER (999), same strategy as
            # GraphStore.get_edges_among.
            id_list = list(needed_ids)
            for i in range(0, len(id_list), 450):
                batch = id_list[i : i + 450]
                placeholders = ",".join("?" for _ in batch)
                node_rows = conn.execute(
                    f"SELECT id, qualified_name FROM nodes WHERE id IN ({placeholders})",  # nosec B608
                    batch,
                ).fetchall()
                for nr in node_rows:
                    id_to_name[nr[0]] = nr[1]

        for r, path_ids in zip(flow_rows, parsed_paths):
            fid, fname, ep_id = r[0], r[1], r[2]
            crit, ncount, fcount = r[3], r[4], r[5]
            ep_name = id_to_name.get(ep_id, str(ep_id))
            critical_path: list[str] = []
            if path_ids:
                critical_path.append(ep_name)
                if len(path_ids) > 2:
                    for nid in path_ids[1:4]:
                        nm = id_to_name.get(nid)
                        if nm:
                            critical_path.append(nm)
                if len(path_ids) > 1:
                    last = id_to_name.get(path_ids[-1])
                    if last and last not in critical_path:
                        critical_path.append(last)
            conn.execute(
                "INSERT OR REPLACE INTO flow_snapshots "
                "(flow_id, name, entry_point, critical_path, criticality, "
                "node_count, file_count) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (fid, fname, ep_name, _json.dumps(critical_path), crit, ncount, fcount),
            )
        conn.commit()
    except sqlite3.OperationalError:
        conn.rollback()

    # -- risk_index --
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM risk_index")

        # Pre-compute caller and test-coverage counts in two aggregate
        # queries. Previously this section ran two COUNT(*) queries per
        # candidate node; on a ~100k-edge graph with tens of thousands
        # of Function/Class/Test nodes that was the primary hang
        # observed during Godot builds.
        caller_counts: dict[str, int] = {}
        for row in conn.execute(
            "SELECT target_qualified, COUNT(*) FROM edges "
            "WHERE kind = 'CALLS' GROUP BY target_qualified"
        ):
            caller_counts[row[0]] = row[1]

        tested_counts: dict[str, int] = {}
        for row in conn.execute(
            "SELECT source_qualified, COUNT(*) FROM edges "
            "WHERE kind = 'TESTED_BY' GROUP BY source_qualified"
        ):
            tested_counts[row[0]] = row[1]

        risk_nodes = conn.execute(
            "SELECT id, qualified_name, name FROM nodes WHERE kind IN ('Function', 'Class', 'Test')"
        ).fetchall()
        security_kw = {
            "auth",
            "login",
            "password",
            "token",
            "session",
            "crypt",
            "secret",
            "credential",
            "permission",
            "sql",
            "execute",
        }
        for n in risk_nodes:
            nid, qn, name = n[0], n[1], n[2]
            caller_count = caller_counts.get(qn, 0)
            tested = tested_counts.get(qn, 0)
            coverage = "tested" if tested > 0 else "untested"
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
            conn.execute(
                "INSERT OR REPLACE INTO risk_index "
                "(node_id, qualified_name, risk_score, caller_count, "
                "test_coverage, security_relevant, last_computed) "
                "VALUES (?, ?, ?, ?, ?, ?, datetime('now'))",
                (nid, qn, risk, caller_count, coverage, sec_relevant),
            )
        conn.commit()
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
            store,
            build_result,
            postprocess,
            full_rebuild=full_rebuild,
            changed_files=changed,
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
        try:
            rows = store.get_nodes_without_signature()
            for row in rows:
                node_id, name, kind, params, ret = (
                    row[0],
                    row[1],
                    row[2],
                    row[3],
                    row[4],
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
                store.rollback()
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
                store.rollback()
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
                store.rollback()
                logger.warning("Community detection failed: %s", e)
                warnings.append(f"Community detection failed: {type(e).__name__}: {e}")

        store.set_metadata(
            "last_postprocessed_at",
            time.strftime("%Y-%m-%dT%H:%M:%S"),
        )
        result["summary"] = "Post-processing complete."
        if warnings:
            result["warnings"] = warnings
        return result
    finally:
        store.close()
