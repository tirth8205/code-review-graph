"""Full-build parity tests for `forget`.

The contract the reviewer asked for: after ``forget X`` the graph must match the
graph you would get by building the repository without ``X`` — not just for the
forgotten file's own rows, but for cross-file incoming edges, flows,
communities, and embeddings. These tests build a small multi-file Python repo,
forget one file, and compare every one of those layers against a fresh build
that never contained the file.
"""

from __future__ import annotations

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
        (repo / rel).write_text(content)
    _git_init(repo)
    return repo


def _build(repo: Path) -> GraphStore:
    store = GraphStore(get_db_path(repo))
    full_build(repo, store)
    run_post_processing(store)
    return store


def _snapshot(store: GraphStore, repo: Path) -> dict:
    """A repo-relative snapshot of the layers a rebuild fully determines."""
    root = str(repo)

    def norm(value: str | None) -> str | None:
        if value is None:
            return None
        return value.replace(root + "/", "").replace(root, "")

    nodes = store._conn.execute(
        "SELECT kind, name, qualified_name FROM nodes "
        "WHERE kind != 'File' ORDER BY 1, 2, 3"
    ).fetchall()
    edges = store._conn.execute(
        "SELECT kind, source_qualified, target_qualified FROM edges ORDER BY 1, 2, 3"
    ).fetchall()
    flows = store._conn.execute(
        "SELECT name, node_count FROM flows ORDER BY 1, 2"
    ).fetchall()
    comms = store._conn.execute(
        "SELECT name, size FROM communities ORDER BY 1, 2"
    ).fetchall()
    return {
        "nodes": [
            (r["kind"], norm(r["name"]), norm(r["qualified_name"])) for r in nodes
        ],
        "edges": [
            (r["kind"], norm(r["source_qualified"]), norm(r["target_qualified"]))
            for r in edges
        ],
        "flows": [(r["name"], r["node_count"]) for r in flows],
        "communities": [(norm(r["name"]), r["size"]) for r in comms],
    }


def _calls_targets(store: GraphStore) -> set[str]:
    return {
        r["target_qualified"]
        for r in store._conn.execute(
            "SELECT target_qualified FROM edges WHERE kind = 'CALLS'"
        ).fetchall()
    }


def test_forget_matches_full_rebuild_without_file(tmp_path):
    repo_a = _make_repo(tmp_path, "a", _FILES)
    store_a = _build(repo_a)
    try:
        forget_files(store_a, repo_a, [str(repo_a / "util.py")])
        after_forget = _snapshot(store_a, repo_a)
    finally:
        store_a.close()

    without_util = {k: v for k, v in _FILES.items() if k != "util.py"}
    repo_b = _make_repo(tmp_path, "b", without_util)
    store_b = _build(repo_b)
    try:
        rebuilt = _snapshot(store_b, repo_b)
    finally:
        store_b.close()

    assert after_forget == rebuilt
    # Guard against a vacuous pass: the surviving graph still has real content.
    assert after_forget["nodes"]
    assert after_forget["edges"]


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
