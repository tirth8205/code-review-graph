"""Tool 1: build_or_update_graph + run_postprocess."""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

from ..build_state import (
    BUILD_COMPLETE,
    BUILD_IN_PROGRESS,
    BUILD_STATE_KEY,
    INCOMPLETE_BUILD_STATES,
    advance_to_postprocess_pending,
    graph_contents_are_complete,
    read_build_state,
)
from ..incremental import (
    full_build,
    incremental_update,
    resolve_incremental_base,
)
from ..parser import normalize_file_path
from ._common import _get_store

logger = logging.getLogger(__name__)

# The build-state vocabulary is imported above rather than defined here:
# ``full_build`` has to advance it as soon as the last file is stored, and it
# cannot import this module without a cycle. See
# :mod:`code_review_graph.build_state` for what each state means.


def build_was_interrupted(store: Any) -> bool:
    """True when the previous build died before it finished post-processing.

    Both incomplete states answer yes: a build that never stored every file
    and a build that stored them all but left the derived data unbuilt are
    each a graph whose freshness metadata overstates it.  What separates them
    is which repair works, which is :func:`graph_contents_are_complete`.
    """
    return read_build_state(store) in INCOMPLETE_BUILD_STATES


def _run_embedding_refresh(
    store: Any,
    result: dict[str, Any],
    warnings: list[str],
    *,
    provider: str | None,
    model: str | None,
) -> None:
    """Run a provider-scoped embedding refresh only when explicitly requested."""
    if provider is None and model is None:
        return
    if not provider or not model:
        warning = "Embedding refresh requires both an explicit provider and model."
        logger.warning(warning)
        warnings.append(warning)
        return
    try:
        from code_review_graph.embeddings import refresh_embeddings

        refreshed = refresh_embeddings(store, provider=provider, model=model)
        if refreshed is not None:
            result["embeddings_refreshed"] = refreshed["embedded"]
            result["embeddings_purged"] = refreshed["purged"]
    except Exception as exc:
        logger.warning("Embedding refresh failed: %s", exc)
        warnings.append(
            f"Embedding refresh failed: {type(exc).__name__}: {exc}",
        )


# SQLITE_BUSY and SQLITE_LOCKED are the only two primary result codes that
# mean "somebody else holds it, come back later".  Extended result codes carry
# their detail in the high bits, so the low byte is what identifies the family.
_CONTENTION_ERRORCODES = frozenset({5, 6})

# ``sqlite_errorcode`` arrived in Python 3.11 and is only set on exceptions the
# sqlite3 module itself raises, so the message is the fallback for 3.10 and for
# any error re-raised by our own code.
_CONTENTION_MESSAGES = (
    "database is locked",
    "database table is locked",
    "database schema is locked",
)


def is_lock_contention(exc: BaseException) -> bool:
    """True only for SQLite refusing a write because another process holds it.

    Every other ``sqlite3.OperationalError`` — a malformed statement, a disk
    I/O error, a read-only database file — is a genuine failure that the next
    run will hit again in exactly the same way.  The distinction is not
    cosmetic: contention deliberately leaves the ``build_state`` marker set so
    the next run redoes the lost stage, so classifying a permanent error as
    contention marks the graph incomplete forever and promotes every later
    update to a full rebuild that cannot clear it.
    """
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    errorcode = getattr(exc, "sqlite_errorcode", None)
    if isinstance(errorcode, int):
        return (errorcode & 0xFF) in _CONTENTION_ERRORCODES
    message = str(exc).lower()
    return any(text in message for text in _CONTENTION_MESSAGES)


def _note_contention(build_result: dict[str, Any], exc: BaseException) -> None:
    """Classify a post-processing stage that failed.

    Contention is transient: another process held the SQLite write lock, and
    the next run can redo the stage, so the build-state marker stays set.  A
    genuine SQLite error is recorded separately — it downgrades the build to
    ``partial`` instead of being retried forever.  Anything else (a missing
    optional dependency, say) is left to the warning list alone: one absent
    extra must not pin the repository into permanent full rebuilds.
    """
    if not isinstance(exc, sqlite3.OperationalError):
        return
    if is_lock_contention(exc):
        build_result["postprocess_contended"] = True
    else:
        build_result["postprocess_failed"] = True


def _fts_file_hint(
    repo_root: str | None,
    changed_files: list[str] | None,
) -> list[str] | None:
    """Spell *changed_files* the way ``nodes.file_path`` stores them.

    Change discovery reports repository-relative paths while the graph keys
    nodes by their normalised absolute path. A hint that matches nothing is
    harmless (the delta still repairs every added and removed row) but it
    buys nothing either, so resolve it whenever the root is known.
    """
    if not changed_files:
        return None
    if repo_root is None:
        return [normalize_file_path(path) for path in changed_files]
    root = Path(repo_root)
    return [normalize_file_path(root / path) for path in changed_files]


def _run_postprocess(
    store: Any,
    build_result: dict[str, Any],
    postprocess: str,
    full_rebuild: bool = False,
    changed_files: list[str] | None = None,
    repo_root: str | None = None,
    embedding_provider: str | None = None,
    embedding_model: str | None = None,
) -> list[str]:
    """Run post-build steps based on *postprocess* level.

    When *full_rebuild* is False and *changed_files* are available,
    uses incremental flow/community detection for faster updates.

    Records structured stage durations in ``build_result["postprocess_timing"]``.
    Minimal processing reports ``signatures_s`` and ``fts_s``; full processing
    additionally reports ``flows_s``, ``communities_s``, and ``summaries_s``.
    Each duration is a nonnegative float measured in seconds.

    Returns a list of warning strings (empty on success).
    """
    warnings: list[str] = []
    build_result["postprocess_level"] = postprocess

    if postprocess == "none":
        _run_embedding_refresh(
            store,
            build_result,
            warnings,
            provider=embedding_provider,
            model=embedding_model,
        )
        # No resolver runs at this level, but edges were still written, so
        # the certainty column must not be left stale for the query layer.
        try:
            store.refresh_target_resolution()
        except sqlite3.OperationalError as e:
            logger.warning("Target-resolution refresh failed: %s", e)
            _note_contention(build_result, e)
            warnings.append(
                f"Target-resolution refresh failed: {type(e).__name__}: {e}"
            )
        return warnings

    # Resolve bare and C++ scoped call targets before derived graph steps.
    try:
        resolved = store.resolve_bare_call_targets()
        resolved += store.resolve_bare_tested_by_sources()
        build_result["bare_edges_resolved"] = resolved
        build_result["cpp_scoped_edges_resolved"] = (
            store.resolve_cpp_scoped_call_targets()
        )
        # Resolvers rewrite bare targets into qualified ones, so the stored
        # certainty column is only correct once they have all run.
        store.refresh_target_resolution()
    except sqlite3.OperationalError as e:
        logger.warning("Call-target resolution failed: %s", e)
        _note_contention(build_result, e)
        warnings.append(
            f"Call-target resolution failed: {type(e).__name__}: {e}"
        )

    # -- Signatures + FTS (fast, always run unless "none") --
    timing: dict[str, float] = {}
    stage_started = time.perf_counter()
    try:
        rows = store.get_nodes_without_signature()
        signature_rows: list[tuple[str, int]] = []
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
            signature_rows.append((sig[:512], node_id))
        # Single transaction via executemany instead of one autocommitted
        # UPDATE per node (issue #721).
        store.update_node_signatures(signature_rows)
        build_result["signatures_updated"] = True
    except (sqlite3.OperationalError, TypeError, KeyError) as e:
        logger.warning("Signature computation failed: %s", e)
        _note_contention(build_result, e)
        warnings.append(f"Signature computation failed: {type(e).__name__}: {e}")
    timing["signatures_s"] = max(
        0.0,
        round(time.perf_counter() - stage_started, 6),
    )

    stage_started = time.perf_counter()
    try:
        from code_review_graph.search import rebuild_fts_index, update_fts_index

        if full_rebuild:
            build_result["fts_indexed"] = rebuild_fts_index(store)
            build_result["fts_rebuilt"] = True
        else:
            # An update knows which files it re-parsed, so the index only has
            # to rewrite those rows instead of being dropped and repopulated.
            mode: list[str] = []
            build_result["fts_indexed"] = update_fts_index(
                store,
                _fts_file_hint(repo_root, changed_files),
                _out_mode=mode,
            )
            build_result["fts_rebuilt"] = mode == ["rebuild"]
    except (sqlite3.OperationalError, ImportError) as e:
        logger.warning("FTS index rebuild failed: %s", e)
        _note_contention(build_result, e)
        warnings.append(f"FTS index rebuild failed: {type(e).__name__}: {e}")
    timing["fts_s"] = max(
        0.0,
        round(time.perf_counter() - stage_started, 6),
    )

    if postprocess == "minimal":
        _run_embedding_refresh(
            store,
            build_result,
            warnings,
            provider=embedding_provider,
            model=embedding_model,
        )
        build_result["postprocess_timing"] = timing
        return warnings

    # -- Expensive: flows + communities (only for "full") --
    use_incremental = not full_rebuild and bool(changed_files)

    stage_started = time.perf_counter()
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
        _note_contention(build_result, e)
        warnings.append(f"Flow detection failed: {type(e).__name__}: {e}")
    timing["flows_s"] = max(
        0.0,
        round(time.perf_counter() - stage_started, 6),
    )

    stage_started = time.perf_counter()
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
        _note_contention(build_result, e)
        warnings.append(f"Community detection failed: {type(e).__name__}: {e}")
    timing["communities_s"] = max(
        0.0,
        round(time.perf_counter() - stage_started, 6),
    )

    # -- Compute pre-computed summary tables --
    stage_started = time.perf_counter()
    try:
        _compute_summaries(store)
        build_result["summaries_computed"] = True
    except (sqlite3.OperationalError, Exception) as e:
        logger.warning("Summary computation failed: %s", e)
        _note_contention(build_result, e)
        warnings.append(f"Summary computation failed: {type(e).__name__}: {e}")
    timing["summaries_s"] = max(
        0.0,
        round(time.perf_counter() - stage_started, 6),
    )
    _run_embedding_refresh(
        store,
        build_result,
        warnings,
        provider=embedding_provider,
        model=embedding_model,
    )
    build_result["postprocess_timing"] = timing

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
    base: str | None = None,
    postprocess: str = "full",
    recurse_submodules: bool | None = None,
    embedding_provider: str | None = None,
    embedding_model: str | None = None,
) -> dict[str, Any]:
    """Build or incrementally update the code knowledge graph.

    Args:
        full_rebuild: If True, re-parse every file. If False (default),
                      only re-parse files changed since ``base``.
        repo_root: Path to the repository root. Auto-detected if omitted.
        base: Git ref for the incremental diff. When None (default), the base
              is resolved automatically to the commit the graph was last built
              at, so a single update reconciles everything since the last sync
              rather than only the most recent commit. Pass an explicit ref to
              override. Ignored when full_rebuild is True.
        postprocess: Post-processing level after build:
            ``"full"`` (default) — signatures, FTS, flows, communities.
            ``"minimal"`` — signatures + FTS only (fast, keeps search working).
            ``"none"`` — skip all post-processing (raw parse only).
        recurse_submodules: If True, include files from git submodules
            via ``git ls-files --recurse-submodules``. When None
            (default), falls back to the CRG_RECURSE_SUBMODULES
            environment variable. Default: disabled.
        embedding_provider: Exact provider to use for an explicitly requested
            post-build refresh. Must be supplied together with
            ``embedding_model``; omitted by default so builds never transmit
            source-derived text or load an embedding model unexpectedly.
        embedding_model: Exact model for an explicitly requested post-build
            embedding refresh. Must be supplied with ``embedding_provider``.

    Returns:
        Summary with files_parsed/updated, node/edge counts, and errors.
    """
    store, root = _get_store(repo_root)
    try:
        if not full_rebuild and not store.has_nodes():
            full_rebuild = True

        # A previous run that died between storing the last file and finishing
        # post-processing left a current anchor over a half-derived graph.
        # Trusting the anchor there is exactly what made that damage permanent,
        # so repair it with a full rebuild instead of believing it.
        interrupted = build_was_interrupted(store)
        if interrupted and not full_rebuild:
            logger.warning(
                "Previous build did not finish post-processing; rebuilding to repair it"
            )
            full_rebuild = True

        # An automatic (base is None) incremental update resolves its diff base
        # to the last-synced commit. When no usable anchor exists, fall back to
        # a full rebuild rather than a wrong HEAD~1 diff that could report the
        # graph as up to date while it is actually stale.
        base_resolved: str | None = base
        if not full_rebuild and base is None:
            base_resolved = resolve_incremental_base(root, store)
            if base_resolved is None:
                full_rebuild = True

        previous_state = store.get_metadata(BUILD_STATE_KEY)
        store.set_metadata(BUILD_STATE_KEY, BUILD_IN_PROGRESS)

        if full_rebuild:
            result = full_build(root, store, recurse_submodules)
            failed = [str(item.get("file", "?")) for item in result["errors"]]
            summary = (
                f"Full build complete: parsed {result['files_parsed']} files, "
                f"created {result['total_nodes']} nodes and "
                f"{result['total_edges']} edges."
            )
            if failed:
                summary += f" {len(failed)} file(s) failed to parse: {failed}."
            build_result = {
                **result,
                "status": "partial" if failed else "ok",
                "build_type": "full",
                "base_resolved": None,
                "summary": summary,
            }
        else:
            try:
                result = incremental_update(root, store, base=base_resolved)
            except RuntimeError as exc:
                # Change discovery or root validation failed before anything was
                # stored; report it the way every other failure is reported.
                # Nothing was written, so the previous completeness verdict
                # still holds — do not leave the graph flagged as half built.
                if previous_state is not None:
                    store.set_metadata(BUILD_STATE_KEY, previous_state)
                return {
                    "status": "error",
                    "build_type": "incremental",
                    "base_resolved": base_resolved,
                    "files_updated": 0,
                    "errors": [],
                    "error": str(exc),
                    "summary": f"Incremental update failed: {exc}",
                    "postprocess_level": postprocess,
                }
            failed = [str(item.get("file", "?")) for item in result["errors"]]
            if result["files_updated"] == 0 and not failed:
                summary = (
                    "No changes detected. Graph is up to date."
                    if result.get("freshness_advanced")
                    else "No graph changes detected. Freshness metadata was not advanced."
                )
                # Nothing changed, so there is nothing half built to repair;
                # leaving the marker set would make every later update a full
                # rebuild.
                store.set_metadata(BUILD_STATE_KEY, BUILD_COMPLETE)
                return {
                    **result,
                    "status": "ok",
                    "build_type": "incremental",
                    "base_resolved": base_resolved,
                    "summary": summary,
                    "postprocess_level": postprocess,
                }
            summary = (
                f"Incremental update: {result['files_updated']} files re-parsed, "
                f"{result['total_nodes']} nodes and "
                f"{result['total_edges']} edges updated. "
                f"Changed: {result['changed_files']}. "
                f"Dependents also updated: {result['dependent_files']}."
            )
            if failed:
                summary += (
                    f" {len(failed)} file(s) failed to parse and keep their "
                    f"previous graph rows: {failed}."
                )
            build_result = {
                **result,
                "status": "partial" if failed else "ok",
                "build_type": "incremental",
                "base_resolved": base_resolved,
                "summary": summary,
            }

        # Every file that could be stored is stored, so what is still
        # outstanding is derived data alone. Recording that distinction here is
        # what lets an explicit ``postprocess`` tell a graph it can genuinely
        # repair from one whose files never all landed. ``full_build`` already
        # advances it before writing the anchor, so a kill in the gap between
        # the two is on the right side of the line; this covers the
        # incremental path and is a no-op when the state is already advanced.
        advance_to_postprocess_pending(store)

        # Pass changed_files for incremental flow/community detection
        changed = result.get("changed_files") if not full_rebuild else None
        warnings = _run_postprocess(
            store,
            build_result,
            postprocess,
            full_rebuild=full_rebuild,
            changed_files=changed,
            repo_root=str(root),
            embedding_provider=embedding_provider,
            embedding_model=embedding_model,
        )
        if warnings:
            build_result["warnings"] = warnings
        # A genuine SQLite failure is not something the next run repairs, so it
        # is reported now rather than hidden behind an "ok" status.
        if build_result.get("postprocess_failed"):
            build_result["status"] = "partial"
        # Last thing written, after post-processing: only now is the graph
        # genuinely what its freshness metadata claims.  A stage that lost the
        # write lock keeps the marker set so the next run redoes it, rather
        # than leaving an empty FTS index behind a "complete" verdict.
        if not build_result.get("postprocess_contended"):
            store.set_metadata(BUILD_STATE_KEY, BUILD_COMPLETE)
        build_result["repaired_interrupted_build"] = interrupted
        return build_result
    finally:
        store.close()


def run_postprocess(
    flows: bool = True,
    communities: bool = True,
    fts: bool = True,
    repo_root: str | None = None,
    embedding_provider: str | None = None,
    embedding_model: str | None = None,
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
        embedding_provider: Exact provider for an explicit refresh. Must be
            supplied with ``embedding_model``. Default: disabled.
        embedding_model: Exact model for an explicit refresh. Must be supplied
            with ``embedding_provider``. Default: disabled.

    Returns:
        Summary of what was computed.
    """
    store, _root = _get_store(repo_root)
    result: dict[str, Any] = {"status": "ok"}
    warnings: list[str] = []

    try:
        # Read before anything runs: the stages below rebuild derived data
        # from whatever nodes are stored, and none of them can put back a file
        # the interrupted build never parsed.
        state_before = read_build_state(store)
        graph_complete = graph_contents_are_complete(state_before)

        try:
            resolved = store.resolve_bare_call_targets()
            resolved += store.resolve_bare_tested_by_sources()
            result["bare_edges_resolved"] = resolved
            result["cpp_scoped_edges_resolved"] = (
                store.resolve_cpp_scoped_call_targets()
            )
            # Resolvers rewrite bare targets into qualified ones, so the
            # stored certainty column is only correct once they have run.
            store.refresh_target_resolution()
        except sqlite3.OperationalError as e:
            logger.warning("Call-target resolution failed: %s", e)
            _note_contention(result, e)
            warnings.append(
                f"Call-target resolution failed: {type(e).__name__}: {e}"
            )

        try:
            rows = store.get_nodes_without_signature()
            signature_rows: list[tuple[str, int]] = []
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
                signature_rows.append((sig[:512], node_id))
            # Single transaction via executemany instead of one autocommitted
            # UPDATE per node (issue #721).
            store.update_node_signatures(signature_rows)
            result["signatures_updated"] = True
        except (sqlite3.OperationalError, TypeError, KeyError) as e:
            logger.warning("Signature computation failed: %s", e)
            _note_contention(result, e)
            warnings.append(f"Signature computation failed: {type(e).__name__}: {e}")

        if fts:
            try:
                from code_review_graph.search import rebuild_fts_index

                fts_count = rebuild_fts_index(store)
                result["fts_indexed"] = fts_count
            except (sqlite3.OperationalError, ImportError) as e:
                store.rollback()
                logger.warning("FTS index rebuild failed: %s", e)
                _note_contention(result, e)
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
                _note_contention(result, e)
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
                _note_contention(result, e)
                warnings.append(f"Community detection failed: {type(e).__name__}: {e}")

        _run_embedding_refresh(
            store,
            result,
            warnings,
            provider=embedding_provider,
            model=embedding_model,
        )

        store.set_metadata(
            "last_postprocessed_at",
            time.strftime("%Y-%m-%dT%H:%M:%S"),
        )
        # The marker clears when, and only when, post-processing finished over
        # a graph that holds every file.  Two conditions, both explicit:
        #
        # * The build that stored the graph got as far as storing all of it.
        #   An explicit postprocess is the hand repair for a build that died
        #   after that point, and must be able to finish it. Refusing on any
        #   warning meant it could not repair the very failures people reach
        #   for it to repair.  But it cannot conjure back files a dead build
        #   never parsed, so over an ``in-progress`` graph it is not a repair
        #   at all: the derived data it writes is complete for a graph that is
        #   missing files, and calling that healthy is the same lie from the
        #   other side.
        # * No stage lost the SQLite write lock.  Contention is transient and
        #   the lost stage has to be redone, so the marker stays set for it.
        if not graph_complete:
            warning = (
                "The last build stopped before every file was stored, so the "
                "graph is still incomplete. Post-processing rebuilt what it "
                "could from the stored nodes; run 'code-review-graph build' "
                "to finish the graph itself."
            )
            logger.warning(warning)
            warnings.append(warning)
            result["build_incomplete"] = True
            result["status"] = "partial"
            result["summary"] = (
                "Post-processing ran, but the graph is missing files from an "
                "unfinished build. Run a full build to repair it."
            )
        else:
            if not result.get("postprocess_contended"):
                store.set_metadata(BUILD_STATE_KEY, BUILD_COMPLETE)
            result["summary"] = "Post-processing complete."
        if warnings:
            result["warnings"] = warnings
        return result
    finally:
        store.close()
