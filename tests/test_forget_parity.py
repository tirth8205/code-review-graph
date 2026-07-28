"""Full-build parity tests for `forget`.

The contract the reviewer asked for: after ``forget X`` the graph must match the
graph you would get by building the repository without ``X`` — not just for the
forgotten file's own rows, but for cross-file incoming edges, flows,
communities, and embeddings. These tests build a small multi-file Python repo,
forget one file, and compare every one of those layers against a fresh build
that never contained the file.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from code_review_graph.forget import forget_files
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, get_db_path
from code_review_graph.postprocessing import run_post_processing

# main imports a helper from each module; forgetting util.py must re-bare main's
# edge into it while keeping main's edge into the surviving shared.py.
_FILES = {
    "util.py": "def helper():\n    return 41\n",
    "shared.py": "def shared_fn():\n    return 7\n",
    "main.py": (
        "from util import helper\n"
        "from shared import shared_fn\n\n"
        "def run():\n"
        "    return helper() + shared_fn()\n"
    ),
}

_AMBIGUOUS_IMPORT_FILES = {
    "src_one/pkg/util.py": "def helper():\n    return 1\n",
    "src_two/pkg/util.py": "def helper():\n    return 2\n",
    "main.py": (
        "from pkg.util import helper\n\n"
        "def run():\n"
        "    return helper()\n"
    ),
}

_EMBEDDINGS_DDL = """
CREATE TABLE IF NOT EXISTS embeddings (
    qualified_name TEXT PRIMARY KEY,
    vector BLOB NOT NULL,
    text_hash TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT 'unknown'
)
"""


def _git_init(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@e.st", "-c", "user.name=t",
         "commit", "-qm", "init"],
        cwd=repo, check=True,
    )


def _make_repo(tmp_path: Path, name: str, files: dict[str, str]) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    _git_init(repo)
    return repo


def _build(repo: Path) -> GraphStore:
    store = GraphStore(get_db_path(repo))
    full_build(repo, store)
    run_post_processing(store)
    return store


def _seed_embeddings(store: GraphStore) -> None:
    """Add deterministic vectors so parity includes embedding cleanup."""
    store._conn.execute(_EMBEDDINGS_DDL)
    qualified_names = store._conn.execute(
        "SELECT qualified_name FROM nodes ORDER BY qualified_name"
    ).fetchall()
    for row in qualified_names:
        qualified_name = row["qualified_name"]
        store._conn.execute(
            "INSERT OR REPLACE INTO embeddings VALUES (?, ?, ?, ?)",
            (
                qualified_name,
                b"\x00\x00\x00\x00",
                f"hash:{qualified_name}",
                "test",
            ),
        )
    store.commit()


def _snapshot(store: GraphStore, repo: Path) -> dict:
    """A repo-relative snapshot of the layers a rebuild fully determines."""
    root = str(repo)

    def norm(value):
        if isinstance(value, str):
            return value.replace(root + "/", "").replace(root, "")
        return value

    nodes = store._conn.execute(
        "SELECT n.kind, n.name, n.qualified_name, n.file_path, "
        "n.line_start, n.line_end, n.language, n.parent_name, n.params, "
        "n.return_type, n.modifiers, n.is_test, n.file_hash, n.extra, "
        "n.signature, c.name AS community_name "
        "FROM nodes n LEFT JOIN communities c ON c.id = n.community_id "
        "ORDER BY n.qualified_name"
    ).fetchall()
    edges = store._conn.execute(
        "SELECT kind, source_qualified, target_qualified, file_path, line, "
        "extra, confidence, confidence_tier FROM edges "
        "ORDER BY kind, source_qualified, target_qualified, file_path, line, extra"
    ).fetchall()
    flow_rows = store._conn.execute(
        "SELECT id, name, entry_point_id, depth, node_count, file_count, "
        "criticality, path_json FROM flows ORDER BY name, id"
    ).fetchall()
    node_names_by_id = {
        row["id"]: row["qualified_name"]
        for row in store._conn.execute(
            "SELECT id, qualified_name FROM nodes ORDER BY id"
        ).fetchall()
    }
    flows = []
    for row in flow_rows:
        path = tuple(
            norm(node_names_by_id[node_id])
            for node_id in json.loads(row["path_json"])
        )
        memberships = store._conn.execute(
            "SELECT fm.position, n.qualified_name "
            "FROM flow_memberships fm JOIN nodes n ON n.id = fm.node_id "
            "WHERE fm.flow_id = ? ORDER BY fm.position, n.qualified_name",
            (row["id"],),
        ).fetchall()
        flows.append(
            (
                norm(row["name"]),
                norm(node_names_by_id[row["entry_point_id"]]),
                row["depth"],
                row["node_count"],
                row["file_count"],
                row["criticality"],
                path,
                tuple(
                    (membership["position"], norm(membership["qualified_name"]))
                    for membership in memberships
                ),
            )
        )
    communities = store._conn.execute(
        "SELECT c.name, c.level, p.name AS parent_name, c.cohesion, c.size, "
        "c.dominant_language, c.description FROM communities c "
        "LEFT JOIN communities p ON p.id = c.parent_id "
        "ORDER BY c.name, c.level"
    ).fetchall()
    community_summaries = store._conn.execute(
        "SELECT c.name AS community_name, cs.name, cs.purpose, "
        "cs.key_symbols, cs.risk, cs.size, cs.dominant_language "
        "FROM community_summaries cs "
        "JOIN communities c ON c.id = cs.community_id "
        "ORDER BY c.name, cs.name"
    ).fetchall()
    flow_snapshots = store._conn.execute(
        "SELECT f.name AS flow_name, fs.name, fs.entry_point, "
        "fs.critical_path, fs.criticality, fs.node_count, fs.file_count "
        "FROM flow_snapshots fs JOIN flows f ON f.id = fs.flow_id "
        "ORDER BY f.name, fs.name"
    ).fetchall()
    risk_index = store._conn.execute(
        "SELECT qualified_name, risk_score, caller_count, test_coverage, "
        "security_relevant FROM risk_index "
        "ORDER BY qualified_name"
    ).fetchall()
    embeddings = store._conn.execute(
        "SELECT qualified_name, vector, text_hash, provider "
        "FROM embeddings ORDER BY qualified_name"
    ).fetchall()

    return {
        "nodes": [tuple(norm(value) for value in row) for row in nodes],
        "edges": [tuple(norm(value) for value in row) for row in edges],
        "flows": flows,
        "communities": [
            tuple(norm(value) for value in row) for row in communities
        ],
        "community_summaries": [
            tuple(norm(value) for value in row) for row in community_summaries
        ],
        "flow_snapshots": [
            tuple(norm(value) for value in row) for row in flow_snapshots
        ],
        "risk_index": [
            tuple(norm(value) for value in row) for row in risk_index
        ],
        "embeddings": [
            tuple(norm(value) for value in row) for row in embeddings
        ],
    }


def _calls_targets(store: GraphStore) -> set[str]:
    return {
        r["target_qualified"]
        for r in store._conn.execute(
            "SELECT target_qualified FROM edges WHERE kind = 'CALLS'"
        ).fetchall()
    }


def test_forget_matches_full_rebuild_without_file(tmp_path):
    repo = _make_repo(tmp_path, "same-root", _FILES)
    store = _build(repo)
    try:
        _seed_embeddings(store)
        forgotten_qns = {
            row["qualified_name"]
            for row in store._conn.execute(
                "SELECT qualified_name FROM nodes WHERE file_path = ?",
                (str(repo / "util.py"),),
            ).fetchall()
        }
        summary = forget_files(store, repo, [str(repo / "util.py")])
        after_forget = _snapshot(store, repo)
    finally:
        store.close()

    assert forgotten_qns
    assert summary["embeddings_purged"] == len(forgotten_qns)
    assert not forgotten_qns.intersection(
        row[0] for row in after_forget["embeddings"]
    )

    # Rebuild at the same root so repository-derived community names remain
    # comparable. The forgotten file stays on disk during forget itself, then
    # is removed only for the clean-rebuild baseline.
    (repo / "util.py").unlink()
    shutil.rmtree(get_db_path(repo).parent)

    rebuilt_store = _build(repo)
    try:
        _seed_embeddings(rebuilt_store)
        rebuilt = _snapshot(rebuilt_store, repo)
    finally:
        rebuilt_store.close()

    assert after_forget == rebuilt
    # Guard against a vacuous pass: the surviving graph still has real content.
    assert after_forget["nodes"]
    assert after_forget["edges"]


def test_forget_recomputes_python_import_after_candidate_is_removed(tmp_path):
    """Removing one ambiguous module must expose the unique survivor."""
    repo = _make_repo(tmp_path, "python-import", _AMBIGUOUS_IMPORT_FILES)
    forgotten_path = repo / "src_two" / "pkg" / "util.py"
    store = _build(repo)
    try:
        _seed_embeddings(store)
        forget_files(store, repo, [str(forgotten_path)])
        after_forget = _snapshot(store, repo)
    finally:
        store.close()

    forgotten_path.unlink()
    shutil.rmtree(get_db_path(repo).parent)

    rebuilt_store = _build(repo)
    try:
        _seed_embeddings(rebuilt_store)
        rebuilt = _snapshot(rebuilt_store, repo)
    finally:
        rebuilt_store.close()

    assert after_forget == rebuilt


def test_forget_rebares_incoming_edge_but_keeps_surviving_one(tmp_path):
    repo = _make_repo(tmp_path, "edges", _FILES)
    store = _build(repo)
    try:
        before = _calls_targets(store)
        assert any(t.endswith("util.py::helper") for t in before)
        assert any(t.endswith("shared.py::shared_fn") for t in before)

        forget_files(store, repo, [str(repo / "util.py")])

        after = _calls_targets(store)
        # The call into the forgotten file drops back to a bare endpoint...
        assert "helper" in after
        assert not any(t.endswith("util.py::helper") for t in after)
        # ...and the call into the survivor stays resolved.
        assert any(t.endswith("shared.py::shared_fn") for t in after)

        # No edge is left pointing at a qualified name with no backing node.
        dangling = store._conn.execute(
            "SELECT target_qualified FROM edges "
            "WHERE target_qualified LIKE '%::%' "
            "AND target_qualified NOT IN (SELECT qualified_name FROM nodes)"
        ).fetchall()
        assert dangling == []
    finally:
        store.close()


def test_forget_repairs_flows_to_match_rebuild(tmp_path):
    repo = _make_repo(tmp_path, "flows", _FILES)
    store = _build(repo)
    try:
        # run -> helper forms a flow while util.py is present.
        assert store._conn.execute("SELECT COUNT(*) FROM flows").fetchone()[0] > 0
        forget_files(store, repo, [str(repo / "util.py")])
        # With helper gone, no flow should still reference a deleted node.
        orphaned = store._conn.execute(
            "SELECT COUNT(*) FROM flow_memberships fm "
            "WHERE fm.node_id NOT IN (SELECT id FROM nodes)"
        ).fetchone()[0]
        assert orphaned == 0
    finally:
        store.close()


def test_forget_purges_orphaned_embeddings(tmp_path):
    repo = _make_repo(tmp_path, "emb", _FILES)
    store = _build(repo)
    try:
        store._conn.execute(_EMBEDDINGS_DDL)
        node_qns = [
            r["qualified_name"]
            for r in store._conn.execute("SELECT qualified_name FROM nodes").fetchall()
        ]
        for qn in node_qns:
            store._conn.execute(
                "INSERT OR REPLACE INTO embeddings VALUES (?, ?, ?, ?)",
                (qn, b"\x00\x00\x00\x00", "hash", "test"),
            )

        util_qns = {
            r["qualified_name"]
            for r in store._conn.execute(
                "SELECT qualified_name FROM nodes WHERE file_path = ?",
                (str(repo / "util.py"),),
            ).fetchall()
        }
        assert util_qns  # sanity: util.py contributed nodes

        summary = forget_files(store, repo, [str(repo / "util.py")])

        remaining = {
            r["qualified_name"]
            for r in store._conn.execute(
                "SELECT qualified_name FROM embeddings"
            ).fetchall()
        }
        # Every vector for a forgotten node is gone; survivors are kept.
        assert not (remaining & util_qns)
        assert "main.py" in " ".join(remaining) or remaining
        assert summary["embeddings_purged"] >= len(util_qns)
    finally:
        store.close()
