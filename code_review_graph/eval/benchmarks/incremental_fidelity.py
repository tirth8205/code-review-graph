"""Incremental-equals-rebuild benchmark.

A persistent graph rots invisibly. Nothing errors; caller lists quietly get
shorter, and a reviewer gets a confidently incomplete answer. A clean rebuild
of the same tree is a free and perfect oracle for that drift, so this
benchmark uses it.

For each of seven edit kinds the benchmark:

1. materialises the repository's tracked files into a throwaway git tree;
2. builds a clean graph and keeps it as the baseline;
3. applies the edit, commits it, and runs ``incremental_update`` plus the
   same post-processing the CLI and MCP tool run;
4. builds a second, clean graph from the edited tree;
5. compares the two databases table by table.

Nodes, edges and every derived table must match. Divergences are reported,
never raised: ``KNOWN_FAILURES`` names the edit kinds that are broken today
so a run stays green while the report stays honest.

Note on ``graph_diff``: the task brief placed ``take_snapshot`` and
``diff_snapshots`` in ``graph.py``; they actually live in
``code_review_graph/graph_diff.py`` and nothing called them before this
module. They are used here for the headline node/edge add/remove summary.
They are not sufficient on their own: a snapshot records only
``(kind, file, community_id)`` per node and ``source->target:kind`` per edge,
so it cannot see a node whose line range, signature, params or file hash
drifted, an edge whose confidence drifted, or any row in ``flows``,
``flow_memberships``, ``communities``, ``nodes_fts``, ``community_summaries``,
``flow_snapshots``, ``risk_index`` or ``metadata``. ``diff_snapshots`` also
truncates its lists to 100 entries. The per-table comparator below covers
what the snapshot cannot.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

#: The seven edit kinds, in report order. ``trailing_comment`` is the easy
#: case (a semantically neutral edit that only changes the file hash); the
#: other six are where a persistent graph actually rots.
EDIT_KINDS: tuple[str, ...] = (
    "trailing_comment",
    "rename_function",
    "delete_file",
    "add_file",
    "move_function",
    "change_import",
    "revert",
)

#: Edit kinds that diverge from a clean rebuild today. Listing one here keeps
#: CI green without hiding the defect: the row is still emitted, still counts
#: its differing rows, and is reported as ``known_failure``. Remove an entry
#: once the underlying bug is fixed and the regression guard takes over.
#:
#: All seven are listed because all seven diverge on this repository (see the
#: pull request that introduced this benchmark). Three independent defects
#: produce them:
#:
#: * ``flow_memberships.node_id`` is a bare integer with no foreign key and no
#:   cascade. ``GraphStore._replace_file_data`` deletes and re-inserts a
#:   re-parsed file's nodes, which allocates new ids, so every membership row
#:   for that file is left dangling. Flows silently get shorter and, because
#:   ``incremental_trace_flows`` finds affected flows by joining memberships to
#:   ``nodes``, the dangling rows are invisible to the repair path too.
#: * ``nodes.community_id`` is not repopulated for a re-parsed file, and
#:   ``incremental_detect_communities`` / ``incremental_trace_flows`` compare
#:   ``nodes.file_path`` (absolute) against ``incremental_update``'s
#:   ``changed_files`` (repo-relative), so both always see zero affected rows
#:   and skip.
#: * Edges whose target lived in a deleted or re-parsed file keep the
#:   qualified name they were resolved to, so a rebuild and an update disagree
#:   about which call targets are resolved.
KNOWN_FAILURES: frozenset[str] = frozenset(EDIT_KINDS)

#: Every table the comparator projects, in report order. ``node_community``
#: is not a table but the ``nodes.community_id`` foreign key resolved through
#: ``communities.name``, because the integer ids are not stable across builds.
COMPARED_TABLES: tuple[str, ...] = (
    "nodes",
    "node_community",
    "edges",
    "communities",
    "flows",
    "flow_memberships",
    "nodes_fts",
    "community_summaries",
    "flow_snapshots",
    "risk_index",
    "metadata",
)

#: Metadata keys that legitimately differ between a full build and an
#: incremental update: build provenance, not graph content.
_VOLATILE_METADATA_KEYS = frozenset({
    "last_updated",
    "last_build_type",
    "last_postprocessed_at",
})

#: Line-comment token per extension, for the semantically neutral edit.
_COMMENT_TOKENS: dict[str, str] = {
    ".py": "#", ".rb": "#", ".sh": "#", ".bash": "#", ".pl": "#", ".r": "#",
    ".jl": "#", ".ex": "#", ".exs": "#", ".yaml": "#", ".yml": "#",
    ".js": "//", ".jsx": "//", ".mjs": "//", ".cjs": "//", ".ts": "//",
    ".tsx": "//", ".go": "//", ".java": "//", ".c": "//", ".h": "//",
    ".cc": "//", ".cpp": "//", ".hpp": "//", ".cs": "//", ".rs": "//",
    ".php": "//", ".swift": "//", ".kt": "//", ".scala": "//", ".dart": "//",
    ".lua": "--", ".sql": "--", ".hs": "--",
    ".erl": "%",
}

#: Minimal, guaranteed-parseable new file per extension, for ``add_file``.
_PROBE_SOURCES: dict[str, str] = {
    ".py": (
        '"""Added by the incremental-fidelity benchmark."""\n'
        "\n\n"
        "def crg_fidelity_probe(value):\n"
        "    return value\n"
    ),
    ".js": (
        "// Added by the incremental-fidelity benchmark.\n"
        "function crgFidelityProbe(value) {\n"
        "  return value;\n"
        "}\n"
        "module.exports = { crgFidelityProbe };\n"
    ),
    ".mjs": (
        "// Added by the incremental-fidelity benchmark.\n"
        "export function crgFidelityProbe(value) {\n"
        "  return value;\n"
        "}\n"
    ),
    ".ts": (
        "// Added by the incremental-fidelity benchmark.\n"
        "export function crgFidelityProbe(value: string): string {\n"
        "  return value;\n"
        "}\n"
    ),
    ".go": (
        "package probe\n\n"
        "// CrgFidelityProbe is added by the incremental-fidelity benchmark.\n"
        "func CrgFidelityProbe(value string) string {\n"
        "\treturn value\n"
        "}\n"
    ),
    ".rb": (
        "# Added by the incremental-fidelity benchmark.\n"
        "def crg_fidelity_probe(value)\n"
        "  value\n"
        "end\n"
    ),
}

_MAX_EXAMPLES = 3
_RENAME_SUFFIX = "_crg_renamed"
_PROBE_STEM = "crg_fidelity_probe"


# ---------------------------------------------------------------------------
# Build helpers -- these mirror tools.build.build_or_update_graph exactly, so
# the benchmark measures the pipeline users actually run.
# ---------------------------------------------------------------------------


def _clean_build(tree: Path, db_path: Path) -> dict[str, Any]:
    """Full parse plus full post-processing into *db_path*."""
    from code_review_graph.graph import GraphStore
    from code_review_graph.incremental import full_build
    from code_review_graph.tools.build import _run_postprocess

    store = GraphStore(db_path)
    try:
        result = dict(full_build(tree, store))
        _run_postprocess(store, result, "full", full_rebuild=True)
        return result
    finally:
        store.close()


def _incremental_build(tree: Path, db_path: Path, base: str) -> dict[str, Any]:
    """Incremental update plus post-processing, as ``build_or_update_graph`` does.

    The early return when nothing was updated is reproduced deliberately: it
    is the real code path, and skipping post-processing there is exactly the
    kind of thing that leaves derived tables behind.
    """
    from code_review_graph.graph import GraphStore
    from code_review_graph.incremental import incremental_update
    from code_review_graph.tools.build import _run_postprocess

    store = GraphStore(db_path)
    try:
        result = dict(incremental_update(tree, store, base=base))
        if result["files_updated"] == 0 and not result["errors"]:
            return result
        _run_postprocess(
            store,
            result,
            "full",
            full_rebuild=False,
            changed_files=result.get("changed_files"),
        )
        return result
    finally:
        store.close()


def _copy_db(source: Path, target: Path) -> None:
    """Copy a closed SQLite database, sidecars included."""
    shutil.copy2(source, target)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(source) + suffix)
        if sidecar.exists():
            shutil.copy2(sidecar, Path(str(target) + suffix))


# ---------------------------------------------------------------------------
# Per-table projections
#
# Row ids are autoincrement and differ between two builds of the same tree, as
# do wall-clock columns. Every projection below is keyed on something stable
# (a qualified name, a community name, a flow's resolved path) and excludes
# ids and timestamps, so a reported difference is a real difference.
# ---------------------------------------------------------------------------

_Projection = dict[str, "Counter[str]"]


def _blank(value: object) -> str:
    return "" if value is None else str(value)


def _join(row: sqlite3.Row, columns: tuple[str, ...]) -> str:
    return "|".join(_blank(row[c]) for c in columns)


def _add(target: _Projection, key: str, value: str) -> None:
    target.setdefault(key, Counter())[value] += 1


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type IN ('table','view') "
        "AND name = ?",
        (name,),
    ).fetchone()
    return bool(row and row[0])


_NODE_COLUMNS = (
    "kind", "name", "file_path", "line_start", "line_end", "language",
    "parent_name", "params", "return_type", "modifiers", "is_test",
    "file_hash", "extra", "symbol", "signature",
)

_EDGE_KEY_COLUMNS = ("kind", "source_qualified", "target_qualified",
                     "file_path", "line")
_EDGE_VALUE_COLUMNS = ("extra", "confidence", "confidence_tier")


def _project_nodes(conn: sqlite3.Connection) -> _Projection:
    out: _Projection = {}
    for row in conn.execute(
        "SELECT qualified_name, " + ", ".join(_NODE_COLUMNS) + " FROM nodes"
    ):
        _add(out, row["qualified_name"], _join(row, _NODE_COLUMNS))
    return out


def _project_node_community(conn: sqlite3.Connection) -> _Projection:
    out: _Projection = {}
    for row in conn.execute(
        "SELECT n.qualified_name AS qn, c.name AS cname FROM nodes n "
        "LEFT JOIN communities c ON c.id = n.community_id"
    ):
        _add(out, row["qn"], _blank(row["cname"]))
    return out


def _project_edges(conn: sqlite3.Connection) -> _Projection:
    out: _Projection = {}
    columns = ", ".join(_EDGE_KEY_COLUMNS + _EDGE_VALUE_COLUMNS)
    for row in conn.execute(f"SELECT {columns} FROM edges"):  # noqa: S608
        _add(out, _join(row, _EDGE_KEY_COLUMNS), _join(row, _EDGE_VALUE_COLUMNS))
    return out


_COMMUNITY_VALUE_COLUMNS = (
    "level", "parent_name", "cohesion", "size", "dominant_language",
    "description",
)


def _project_communities(conn: sqlite3.Connection) -> _Projection:
    out: _Projection = {}
    if not _table_exists(conn, "communities"):
        return out
    for row in conn.execute(
        "SELECT c.name AS name, c.level AS level, c.cohesion AS cohesion, "
        "c.size AS size, c.dominant_language AS dominant_language, "
        "c.description AS description, p.name AS parent_name "
        "FROM communities c LEFT JOIN communities p ON p.id = c.parent_id"
    ):
        _add(out, row["name"], _join(row, _COMMUNITY_VALUE_COLUMNS))
    return out


def _node_names(conn: sqlite3.Connection) -> dict[int, str]:
    return {
        int(row[0]): str(row[1])
        for row in conn.execute("SELECT id, qualified_name FROM nodes")
    }


def _flow_keys(conn: sqlite3.Connection) -> dict[int, str]:
    """Map each flow id to an id-independent identity: entry point plus path."""
    if not _table_exists(conn, "flows"):
        return {}
    names = _node_names(conn)
    keys: dict[int, str] = {}
    for row in conn.execute(
        "SELECT id, entry_point_id, path_json FROM flows"
    ):
        try:
            path = json.loads(row["path_json"] or "[]")
        except (TypeError, ValueError):
            path = []
        entry = names.get(int(row["entry_point_id"]), "<unresolved>")
        walk = ">".join(names.get(int(n), "<unresolved>") for n in path)
        keys[int(row["id"])] = f"{entry}::{walk}"
    return keys


_FLOW_VALUE_COLUMNS = ("name", "depth", "node_count", "file_count",
                       "criticality")


def _project_flows(conn: sqlite3.Connection) -> _Projection:
    out: _Projection = {}
    if not _table_exists(conn, "flows"):
        return out
    keys = _flow_keys(conn)
    for row in conn.execute(
        "SELECT id, name, depth, node_count, file_count, criticality FROM flows"
    ):
        _add(out, keys[int(row["id"])], _join(row, _FLOW_VALUE_COLUMNS))
    return out


def _project_flow_memberships(conn: sqlite3.Connection) -> _Projection:
    out: _Projection = {}
    if not _table_exists(conn, "flow_memberships"):
        return out
    keys = _flow_keys(conn)
    for row in conn.execute(
        "SELECT fm.flow_id AS flow_id, fm.position AS position, "
        "n.qualified_name AS qn FROM flow_memberships fm "
        "LEFT JOIN nodes n ON n.id = fm.node_id"
    ):
        flow_key = keys.get(int(row["flow_id"]), "<orphan flow>")
        _add(out, f"{flow_key}|{_blank(row['qn'])}", _blank(row["position"]))
    return out


def _project_nodes_fts(conn: sqlite3.Connection) -> Optional[_Projection]:
    """Project the FTS5 index itself, not the content table behind it.

    ``nodes_fts`` is an external-content table, so ``SELECT ... FROM
    nodes_fts`` reads ``nodes`` and proves nothing. ``fts5vocab`` exposes the
    index's own terms and is rowid-independent, which is what makes it
    comparable across two builds.
    """
    if not _table_exists(conn, "nodes_fts"):
        return None
    vocab = "crg_fidelity_vocab"
    try:
        conn.execute(f"DROP TABLE IF EXISTS temp.{vocab}")
        conn.execute(
            f"CREATE VIRTUAL TABLE temp.{vocab} "
            f"USING fts5vocab(main, nodes_fts, 'row')"
        )
        out: _Projection = {}
        for row in conn.execute(f"SELECT term, doc, cnt FROM temp.{vocab}"):  # noqa: S608
            _add(out, _blank(row[0]), f"{_blank(row[1])}|{_blank(row[2])}")
        return out
    except sqlite3.OperationalError as exc:
        logger.warning("fts5vocab unavailable, skipping nodes_fts: %s", exc)
        return None
    finally:
        try:
            conn.execute(f"DROP TABLE IF EXISTS temp.{vocab}")
        except sqlite3.OperationalError:
            pass


_SUMMARY_VALUE_COLUMNS = ("name", "purpose", "key_symbols", "risk", "size",
                          "dominant_language")


def _project_community_summaries(conn: sqlite3.Connection) -> _Projection:
    out: _Projection = {}
    if not _table_exists(conn, "community_summaries"):
        return out
    for row in conn.execute(
        "SELECT s.name AS name, s.purpose AS purpose, "
        "s.key_symbols AS key_symbols, s.risk AS risk, s.size AS size, "
        "s.dominant_language AS dominant_language, c.name AS community_name "
        "FROM community_summaries s "
        "LEFT JOIN communities c ON c.id = s.community_id"
    ):
        key = _blank(row["community_name"]) or f"<orphan>{_blank(row['name'])}"
        _add(out, key, _join(row, _SUMMARY_VALUE_COLUMNS))
    return out


_SNAPSHOT_VALUE_COLUMNS = ("name", "entry_point", "critical_path",
                           "criticality", "node_count", "file_count")


def _project_flow_snapshots(conn: sqlite3.Connection) -> _Projection:
    out: _Projection = {}
    if not _table_exists(conn, "flow_snapshots"):
        return out
    keys = _flow_keys(conn)
    for row in conn.execute(
        "SELECT flow_id, name, entry_point, critical_path, criticality, "
        "node_count, file_count FROM flow_snapshots"
    ):
        key = keys.get(
            int(row["flow_id"]),
            f"<orphan>{_blank(row['entry_point'])}|{_blank(row['name'])}",
        )
        _add(out, key, _join(row, _SNAPSHOT_VALUE_COLUMNS))
    return out


_RISK_VALUE_COLUMNS = ("risk_score", "caller_count", "test_coverage",
                       "security_relevant")


def _project_risk_index(conn: sqlite3.Connection) -> _Projection:
    out: _Projection = {}
    if not _table_exists(conn, "risk_index"):
        return out
    for row in conn.execute(
        "SELECT qualified_name, risk_score, caller_count, test_coverage, "
        "security_relevant FROM risk_index"
    ):
        _add(out, row["qualified_name"], _join(row, _RISK_VALUE_COLUMNS))
    return out


def _project_metadata(conn: sqlite3.Connection) -> _Projection:
    out: _Projection = {}
    for row in conn.execute("SELECT key, value FROM metadata"):
        if row["key"] in _VOLATILE_METADATA_KEYS:
            continue
        _add(out, row["key"], _blank(row["value"]))
    return out


_PROJECTORS: dict[str, Callable[[sqlite3.Connection], Optional[_Projection]]] = {
    "nodes": _project_nodes,
    "node_community": _project_node_community,
    "edges": _project_edges,
    "communities": _project_communities,
    "flows": _project_flows,
    "flow_memberships": _project_flow_memberships,
    "nodes_fts": _project_nodes_fts,
    "community_summaries": _project_community_summaries,
    "flow_snapshots": _project_flow_snapshots,
    "risk_index": _project_risk_index,
    "metadata": _project_metadata,
}


def _diff_projection(
    left: _Projection,
    right: _Projection,
    path_prefix: str = "",
) -> tuple[int, list[str]]:
    """Count differing rows and collect a few concrete examples.

    A key present on one side only contributes its row count. A key present on
    both whose values differ contributes the larger of the two one-sided
    multiset deltas, so a single changed column counts as one differing row
    rather than as one removal plus one insertion.
    """
    differing = 0
    examples: list[str] = []
    for key in sorted(set(left) | set(right)):
        here = left.get(key, Counter())
        there = right.get(key, Counter())
        if here == there:
            continue
        delta = max(
            sum((here - there).values()),
            sum((there - here).values()),
        )
        differing += delta
        if len(examples) < _MAX_EXAMPLES:
            examples.append(
                f"{_clean(key, path_prefix)} :: "
                f"incremental={_clean(_describe(here), path_prefix)} "
                f"rebuild={_clean(_describe(there), path_prefix)}"
            )
    return differing, examples


def _clean(text: str, path_prefix: str) -> str:
    """Shorten an example to the part a reader can act on.

    The repository is materialised under a temporary directory, so every
    stored path carries a prefix that is noise in a report. Stripping it keeps
    the repo-relative identifier inside ``_sanitize_name``'s 256-character cap
    instead of truncating the interesting half away.
    """
    from code_review_graph.graph import _sanitize_name

    if path_prefix:
        text = text.replace(path_prefix, "")
    return _sanitize_name(text)


def _describe(values: "Counter[str]") -> str:
    if not values:
        return "<absent>"
    return "; ".join(
        f"{value}" if count == 1 else f"{value} (x{count})"
        for value, count in sorted(values.items())
    )[:300]


def _open(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def compare_graphs(
    incremental_db: Path,
    rebuild_db: Path,
    path_prefix: str = "",
) -> dict[str, Any]:
    """Compare an incrementally updated graph against a clean rebuild.

    Args:
        incremental_db: Database that was built once and then updated.
        rebuild_db: Database built clean from the same tree. The oracle.
        path_prefix: Stripped from example strings so they stay readable.

    Returns:
        ``tables`` maps every entry of :data:`COMPARED_TABLES` to
        ``{"differing_rows": int, "examples": [str, ...], "checked": bool}``;
        ``total_differing_rows`` is their sum; ``worst_table`` names the
        biggest contributor; ``snapshot_summary`` is
        ``graph_diff.diff_snapshots``' node/edge summary.
    """
    tables: dict[str, dict[str, Any]] = {}
    left_conn = _open(incremental_db)
    right_conn = _open(rebuild_db)
    try:
        for name in COMPARED_TABLES:
            projector = _PROJECTORS[name]
            left = projector(left_conn)
            right = projector(right_conn)
            if left is None or right is None:
                tables[name] = {
                    "differing_rows": 0, "examples": [], "checked": False,
                }
                continue
            differing, examples = _diff_projection(left, right, path_prefix)
            tables[name] = {
                "differing_rows": differing,
                "examples": examples,
                "checked": True,
            }
    finally:
        left_conn.close()
        right_conn.close()

    total = sum(t["differing_rows"] for t in tables.values())
    worst = max(
        COMPARED_TABLES,
        key=lambda name: tables[name]["differing_rows"],
    )
    return {
        "tables": tables,
        "total_differing_rows": total,
        "worst_table": worst if tables[worst]["differing_rows"] else "",
        "unchecked": [n for n in COMPARED_TABLES if not tables[n]["checked"]],
        "snapshot_summary": _snapshot_summary(incremental_db, rebuild_db),
    }


def _snapshot_summary(
    incremental_db: Path,
    rebuild_db: Path,
) -> dict[str, Any]:
    """Headline node/edge deltas via the previously unused graph_diff helpers."""
    from code_review_graph.graph import GraphStore
    from code_review_graph.graph_diff import diff_snapshots, take_snapshot

    left = GraphStore(incremental_db)
    try:
        before = take_snapshot(left)
    finally:
        left.close()
    right = GraphStore(rebuild_db)
    try:
        after = take_snapshot(right)
    finally:
        right.close()
    # "before" is the incremental graph and "after" the rebuild, so
    # diff_snapshots' "added" means "only the rebuild has it".
    return dict(diff_snapshots(before, after)["summary"])


# ---------------------------------------------------------------------------
# Throwaway git tree
# ---------------------------------------------------------------------------

_GIT_IDENTITY = (
    "-c", "user.email=benchmark@code-review-graph.invalid",
    "-c", "user.name=crg-benchmark",
    "-c", "commit.gpgsign=false",
)


def _git(tree: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *_GIT_IDENTITY, *args],
        cwd=str(tree),
        capture_output=True,
        text=True,
        timeout=120,
        stdin=subprocess.DEVNULL,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed in {tree}: {proc.stderr.strip()}"
        )
    return proc.stdout


def _tracked_files(repo_path: Path) -> list[str]:
    proc = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=str(repo_path),
        capture_output=True,
        timeout=120,
        stdin=subprocess.DEVNULL,
    )
    if proc.returncode != 0:
        return []
    raw = proc.stdout.decode("utf-8", errors="replace")
    return [p for p in raw.split("\0") if p]


def _materialise_tree(repo_path: Path, destination: Path) -> Path:
    """Copy the repository's tracked files into a standalone git repository.

    A fresh ``git init`` rather than a clone: the benchmark commits an edit per
    scenario and resets between them, and it must never write to the
    repository under evaluation.

    The destination is resolved before use. ``full_build`` anchors every
    stored ``file_path`` to ``_canonical_repo_root``, so an unresolved root
    (on macOS ``/var/...`` for ``/private/var/...``) would make every
    ``relpath`` computed against it meaningless.
    """
    destination = destination.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    tracked = _tracked_files(repo_path)
    if not tracked:
        raise RuntimeError(f"{repo_path} has no git-tracked files to copy")
    for rel in tracked:
        source = repo_path / rel
        if not source.is_file() or source.is_symlink():
            continue
        target = destination / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    _git(destination, "init", "-q")
    _git(destination, "add", "-A")
    _git(destination, "commit", "-q", "--no-verify", "-m", "baseline")
    return destination


def _head(tree: Path) -> str:
    return _git(tree, "rev-parse", "HEAD").strip()


def _commit_all(tree: Path, message: str) -> str:
    _git(tree, "add", "-A")
    _git(tree, "commit", "-q", "--no-verify", "--allow-empty", "-m", message)
    return _head(tree)


def _reset(tree: Path, sha: str) -> None:
    _git(tree, "reset", "-q", "--hard", sha)
    _git(tree, "clean", "-qfd")


# ---------------------------------------------------------------------------
# Choosing what to edit
# ---------------------------------------------------------------------------


@dataclass
class _Symbol:
    qualified_name: str
    name: str
    rel_path: str
    line_start: int
    line_end: int


@dataclass
class _Targets:
    primary: str = ""
    sibling: str = ""
    functions: list[_Symbol] = field(default_factory=list)
    import_sites: list[tuple[str, int]] = field(default_factory=list)
    deletable: str = ""


def _relative(tree: Path, stored: str) -> str:
    try:
        return os.path.relpath(stored, str(tree)).replace(os.sep, "/")
    except ValueError:
        return stored


def _pick_targets(db_path: Path, tree: Path) -> _Targets:
    """Choose edit targets from the baseline graph, deterministically."""
    conn = _open(db_path)
    try:
        symbols: list[_Symbol] = []
        for row in conn.execute(
            "SELECT qualified_name, name, file_path, line_start, line_end "
            "FROM nodes WHERE kind = 'Function' AND is_test = 0 "
            "AND line_start IS NOT NULL AND line_end IS NOT NULL "
            "ORDER BY file_path, line_start"
        ):
            symbols.append(_Symbol(
                qualified_name=row["qualified_name"],
                name=row["name"],
                rel_path=_relative(tree, row["file_path"]),
                line_start=int(row["line_start"]),
                line_end=int(row["line_end"]),
            ))
        per_file: Counter[str] = Counter(s.rel_path for s in symbols)
        ranked = sorted(
            (p for p in per_file if Path(p).suffix in _COMMENT_TOKENS),
            key=lambda p: (-per_file[p], p),
        )
        primary = ranked[0] if ranked else ""
        suffix = Path(primary).suffix if primary else ""
        sibling = next(
            (p for p in ranked[1:] if Path(p).suffix == suffix),
            "",
        )
        import_sites = [
            (_relative(tree, row["file_path"]), int(row["line"]))
            for row in conn.execute(
                "SELECT file_path, line FROM edges WHERE kind = 'IMPORTS_FROM' "
                "AND line > 0 ORDER BY file_path, line"
            )
        ]
        depended_on = [
            _relative(tree, row["file_path"])
            for row in conn.execute(
                "SELECT n.file_path AS file_path, COUNT(*) AS hits "
                "FROM edges e JOIN nodes n "
                "ON n.qualified_name = e.target_qualified "
                "WHERE e.file_path != n.file_path "
                "GROUP BY n.file_path ORDER BY hits DESC, n.file_path"
            )
        ]
        deletable = next(
            (p for p in depended_on if Path(p).suffix in _COMMENT_TOKENS),
            "",
        )
        return _Targets(
            primary=primary,
            sibling=sibling,
            functions=[s for s in symbols if s.rel_path == primary],
            import_sites=import_sites,
            deletable=deletable,
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The seven edits
# ---------------------------------------------------------------------------


@dataclass
class _Edit:
    """What a scenario did, or why it could not run."""

    target: str = ""
    note: str = ""
    skipped: str = ""


def _read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8", errors="replace").splitlines(True)


def _write_lines(path: Path, lines: list[str]) -> None:
    path.write_text("".join(lines), encoding="utf-8")


def _edit_trailing_comment(tree: Path, targets: _Targets) -> _Edit:
    if not targets.primary:
        return _Edit(skipped="no source file with a known comment syntax")
    token = _COMMENT_TOKENS[Path(targets.primary).suffix]
    path = tree / targets.primary
    text = path.read_text(encoding="utf-8", errors="replace")
    if not text.endswith("\n"):
        text += "\n"
    path.write_text(
        f"{text}{token} incremental-fidelity benchmark: neutral edit\n",
        encoding="utf-8",
    )
    return _Edit(target=targets.primary, note="appended one comment line")


def _rename_candidate(tree: Path, targets: _Targets) -> tuple[_Symbol, int] | None:
    """Find a function whose definition line carries its own name."""
    if not targets.primary:
        return None
    lines = _read_lines(tree / targets.primary)
    for symbol in targets.functions:
        if not symbol.name or not symbol.name.isidentifier():
            continue
        for offset in range(0, 3):
            index = symbol.line_start - 1 + offset
            if 0 <= index < len(lines) and re.search(
                rf"\b{re.escape(symbol.name)}\b", lines[index]
            ):
                return symbol, index
    return None


def _edit_rename_function(tree: Path, targets: _Targets) -> _Edit:
    candidate = _rename_candidate(tree, targets)
    if candidate is None:
        return _Edit(skipped="no function definition line found to rename")
    symbol, index = candidate
    path = tree / targets.primary
    lines = _read_lines(path)
    lines[index] = re.sub(
        rf"\b{re.escape(symbol.name)}\b",
        symbol.name + _RENAME_SUFFIX,
        lines[index],
        count=1,
    )
    _write_lines(path, lines)
    return _Edit(
        target=targets.primary,
        note=f"renamed {symbol.name} to {symbol.name}{_RENAME_SUFFIX}",
    )


def _edit_delete_file(tree: Path, targets: _Targets) -> _Edit:
    if not targets.deletable:
        return _Edit(skipped="no depended-on file to delete")
    path = tree / targets.deletable
    if not path.is_file():
        return _Edit(skipped=f"{targets.deletable} is not a file")
    path.unlink()
    return _Edit(target=targets.deletable, note="deleted a depended-on file")


def _edit_add_file(tree: Path, targets: _Targets) -> _Edit:
    suffix = Path(targets.primary).suffix if targets.primary else ""
    source = _PROBE_SOURCES.get(suffix)
    if source is None:
        return _Edit(skipped=f"no probe source for {suffix or 'unknown suffix'}")
    rel = str(Path(targets.primary).with_name(_PROBE_STEM + suffix))
    (tree / rel).write_text(source, encoding="utf-8")
    return _Edit(target=rel, note="added a new source file")


def _edit_move_function(tree: Path, targets: _Targets) -> _Edit:
    if not targets.primary or not targets.sibling:
        return _Edit(skipped="need two same-language files to move between")
    source_path = tree / targets.primary
    lines = _read_lines(source_path)
    movable = [
        s for s in targets.functions
        if 1 <= s.line_start <= s.line_end <= len(lines)
        and s.line_end > s.line_start
        and not lines[s.line_start - 1][:1].isspace()
    ]
    if not movable:
        return _Edit(skipped="no top-level multi-line function to move")
    symbol = movable[-1]
    block = lines[symbol.line_start - 1:symbol.line_end]
    remaining = lines[:symbol.line_start - 1] + lines[symbol.line_end:]
    _write_lines(source_path, remaining)
    target_path = tree / targets.sibling
    tail = target_path.read_text(encoding="utf-8", errors="replace")
    if tail and not tail.endswith("\n"):
        tail += "\n"
    target_path.write_text(tail + "\n\n" + "".join(block), encoding="utf-8")
    return _Edit(
        target=f"{targets.primary} -> {targets.sibling}",
        note=f"moved {symbol.name}",
    )


def _ranked_import_sites(targets: _Targets) -> list[tuple[str, int]]:
    """Prefer an import inside the busiest file, then its language.

    Taking the first import site in path order picks whichever file sorts
    first, which on a polyglot repository is usually a build script with no
    callers, no flows and no community. Removing an import there proves
    almost nothing.
    """
    suffix = Path(targets.primary).suffix if targets.primary else ""

    def rank(site: tuple[str, int]) -> tuple[int, str, int]:
        rel = site[0]
        if rel == targets.primary:
            return (0, rel, site[1])
        if suffix and Path(rel).suffix == suffix:
            return (1, rel, site[1])
        return (2, rel, site[1])

    return sorted(targets.import_sites, key=rank)


def _edit_change_import(tree: Path, targets: _Targets) -> _Edit:
    for rel, line_no in _ranked_import_sites(targets):
        path = tree / rel
        if not path.is_file():
            continue
        lines = _read_lines(path)
        if not 1 <= line_no <= len(lines):
            continue
        text = lines[line_no - 1]
        stripped = text.strip()
        if not stripped or stripped.endswith(("(", "\\", "{", ",")):
            continue
        del lines[line_no - 1]
        _write_lines(path, lines)
        return _Edit(target=rel, note=f"removed import at line {line_no}")
    return _Edit(skipped="no single-line import statement found")


_APPLIERS: dict[str, Callable[[Path, _Targets], _Edit]] = {
    "trailing_comment": _edit_trailing_comment,
    "rename_function": _edit_rename_function,
    "delete_file": _edit_delete_file,
    "add_file": _edit_add_file,
    "move_function": _edit_move_function,
    "change_import": _edit_change_import,
}


# ---------------------------------------------------------------------------
# Scenario driver
# ---------------------------------------------------------------------------


def _blank_row(repo: str, kind: str) -> dict[str, Any]:
    row: dict[str, Any] = {
        "repo": repo,
        "edit_kind": kind,
        "status": "skipped",
        "known_failure": 1 if kind in KNOWN_FAILURES else 0,
        "target": "",
        "total_differing_rows": 0,
        "worst_table": "",
        "snapshot_nodes_only_in_rebuild": 0,
        "snapshot_nodes_only_in_incremental": 0,
        "snapshot_edges_only_in_rebuild": 0,
        "snapshot_edges_only_in_incremental": 0,
        "unchecked_tables": "",
        "example": "",
        "note": "",
        "seconds": 0.0,
        "benchmark_seconds": 0.0,
    }
    for table in COMPARED_TABLES:
        row[f"{table}_diff"] = 0
    return row


def _record(row: dict[str, Any], diff: dict[str, Any]) -> None:
    for table in COMPARED_TABLES:
        row[f"{table}_diff"] = diff["tables"][table]["differing_rows"]
    row["total_differing_rows"] = diff["total_differing_rows"]
    row["worst_table"] = diff["worst_table"]
    summary = diff["snapshot_summary"]
    row["snapshot_nodes_only_in_rebuild"] = summary["nodes_added"]
    row["snapshot_nodes_only_in_incremental"] = summary["nodes_removed"]
    row["snapshot_edges_only_in_rebuild"] = summary["edges_added"]
    row["snapshot_edges_only_in_incremental"] = summary["edges_removed"]
    row["unchecked_tables"] = ",".join(diff["unchecked"])
    worst = diff["worst_table"]
    if worst:
        examples = diff["tables"][worst]["examples"]
        row["example"] = f"{worst}: {examples[0]}" if examples else worst


def _run_scenario(
    kind: str,
    repo: str,
    tree: Path,
    workdir: Path,
    baseline_db: Path,
    base_sha: str,
    targets: _Targets,
) -> dict[str, Any]:
    row = _blank_row(repo, kind)
    started = time.perf_counter()
    try:
        applier = _APPLIERS[kind] if kind != "revert" else _edit_trailing_comment
        edit = applier(tree, targets)
        if edit.skipped:
            row["note"] = edit.skipped
            return row
        row["target"] = edit.target
        row["note"] = edit.note

        incremental_db = workdir / f"{kind}_incremental.db"
        _copy_db(baseline_db, incremental_db)

        edited_sha = _commit_all(tree, f"benchmark: {kind}")
        _incremental_build(tree, incremental_db, base=base_sha)

        if kind == "revert":
            # Put the content back, update again, and let the rebuild below
            # judge whether the graph returned to where it started.
            _reset(tree, base_sha)
            _commit_all(tree, "benchmark: revert")
            _incremental_build(tree, incremental_db, base=edited_sha)
            row["note"] = "appended then removed one comment line"

        rebuild_db = workdir / f"{kind}_rebuild.db"
        _clean_build(tree, rebuild_db)

        diff = compare_graphs(
            incremental_db, rebuild_db, path_prefix=str(tree) + "/",
        )
        _record(row, diff)
        if diff["total_differing_rows"] == 0:
            row["status"] = "ok"
        elif kind in KNOWN_FAILURES:
            row["status"] = "known_failure"
        else:
            row["status"] = "diverged"
        return row
    except Exception as exc:  # pragma: no cover - reported, never raised
        logger.warning("incremental_fidelity %s failed: %s", kind, exc)
        row["status"] = "error"
        row["note"] = f"{type(exc).__name__}: {exc}"[:300]
        return row
    finally:
        row["seconds"] = round(time.perf_counter() - started, 3)
        try:
            _reset(tree, base_sha)
        except RuntimeError as exc:
            logger.warning("could not reset the benchmark tree: %s", exc)


def run(repo_path: Path, store: Any, config: dict) -> list[dict]:
    """Prove, or disprove, that an incremental update equals a clean rebuild.

    ``store`` is the runner's already-built graph for *repo_path*. It is not
    used: the benchmark needs two graphs of the same tree at the same instant,
    so it builds both itself in a throwaway copy and never writes to the
    repository under evaluation.
    """
    repo = config.get("name", str(repo_path))
    kinds = [k for k in EDIT_KINDS if k in set(config.get("fidelity_edits", EDIT_KINDS))]
    started = time.perf_counter()
    workdir = Path(tempfile.mkdtemp(prefix="crg-fidelity-")).resolve()
    try:
        tree = _materialise_tree(Path(repo_path), workdir / "tree")
        base_sha = _head(tree)
        baseline_db = workdir / "baseline.db"
        _clean_build(tree, baseline_db)
        targets = _pick_targets(baseline_db, tree)
        rows = [
            _run_scenario(
                kind, repo, tree, workdir, baseline_db, base_sha, targets,
            )
            for kind in kinds
        ]
    except Exception as exc:
        logger.error("incremental_fidelity setup failed: %s", exc)
        rows = []
        for kind in kinds:
            row = _blank_row(repo, kind)
            row["status"] = "error"
            row["note"] = f"{type(exc).__name__}: {exc}"[:300]
            rows.append(row)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    elapsed = round(time.perf_counter() - started, 3)
    for row in rows:
        row["benchmark_seconds"] = elapsed
    return rows
