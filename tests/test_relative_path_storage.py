"""A graph stores repo-relative paths and still answers with absolute ones.

Phase 4 of the improvement plan: the path part of every key is relative to
the checkout, so a ``graph.db`` copied into another checkout (a linked
worktree) answers with that checkout's paths instead of the original's.
"""

import shutil
import sqlite3
import subprocess
from unittest.mock import patch

from code_review_graph.embeddings import EmbeddingStore
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build
from code_review_graph.parser import EdgeInfo, NodeInfo
from code_review_graph.relpaths import PATH_MODE_KEY, PATH_MODE_RELATIVE


def _git(repo, *args):
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=T", *args],
        cwd=repo, check=True, capture_output=True,
    )


def _repo(root):
    root.mkdir()
    (root / "m.py").write_text("def helper():\n    return 1\n")
    (root / "pkg").mkdir()
    (root / "pkg" / "b.py").write_text(
        "from m import helper\n\ndef run():\n    return helper()\n"
    )
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "initial")
    return root


def _raw(db_path, sql):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def test_rows_are_relative_and_the_api_is_absolute(tmp_path):
    repo = _repo(tmp_path / "repo").resolve()
    db = repo / ".code-review-graph" / "graph.db"
    store = GraphStore(db)
    try:
        full_build(repo, store)
        assert store.get_metadata(PATH_MODE_KEY) == PATH_MODE_RELATIVE
        names = {n.qualified_name for n in store.get_nodes_by_file(str(repo / "pkg/b.py"))}
        assert names == {f"{repo}/pkg/b.py", f"{repo}/pkg/b.py::run"}
        callers = store.get_edges_by_target(f"{repo}/m.py::helper")
        assert {e.source_qualified for e in callers} >= {f"{repo}/pkg/b.py::run"}
    finally:
        store.close()
    stored = {row[0] for row in _raw(db, "SELECT qualified_name FROM nodes")}
    assert "./pkg/b.py::run" in stored
    assert not any(str(repo) in name for name in stored)
    targets = {row[0] for row in _raw(db, "SELECT target_qualified FROM edges")}
    assert "./m.py::helper" in targets


def test_a_copied_graph_answers_with_the_new_checkout(tmp_path):
    first = _repo(tmp_path / "first").resolve()
    store = GraphStore(first / ".code-review-graph" / "graph.db")
    try:
        full_build(first, store)
    finally:
        store.close()
    second = tmp_path / "second"
    shutil.copytree(first, second)
    second = second.resolve()
    store = GraphStore(second / ".code-review-graph" / "graph.db")
    try:
        assert store.get_repo_root() == str(second)
        nodes = store.get_nodes_by_file(str(second / "m.py"))
        assert {n.qualified_name for n in nodes} == {
            f"{second}/m.py", f"{second}/m.py::helper",
        }
        assert store.get_nodes_by_file(str(first / "m.py")) == []
    finally:
        store.close()


def test_unresolved_names_and_outside_paths_are_left_alone(tmp_path):
    repo = (tmp_path / "repo")
    repo.mkdir()
    repo = repo.resolve()
    outside = (tmp_path / "elsewhere.py").resolve().as_posix()
    inside = f"{repo}/app.py"
    store = GraphStore(repo / ".code-review-graph" / "graph.db")
    try:
        store.upsert_node(NodeInfo("File", inside, inside, 1, 2, "python"))
        store.upsert_node(NodeInfo("Function", "run", inside, 1, 2, "python"))
        store.upsert_node(NodeInfo("File", outside, outside, 1, 2, "python"))
        store.upsert_edge(EdgeInfo("CALLS", f"{inside}::run", "std::vec::Vec", inside, 1))
        store.upsert_edge(EdgeInfo("CALLS", f"{inside}::run", "helper", inside, 2))
        # Raw values that already look relative are escaped, not re-anchored.
        store.upsert_edge(EdgeInfo("IMPORTS_FROM", inside, "./missing", inside, 3))
        store.upsert_edge(EdgeInfo("IMPORTS_FROM", inside, "!./odd", inside, 4))
        store.commit()
        targets = {e.target_qualified for e in store.get_edges_by_source(f"{inside}::run")}
        assert targets == {"std::vec::Vec", "helper"}
        imports = {e.target_qualified for e in store.get_edges_by_source(inside)}
        assert {"./missing", "!./odd"} <= imports
        assert [n.file_path for n in store.get_nodes_by_file(outside)] == [outside]
    finally:
        store.close()
    db = repo / ".code-review-graph" / "graph.db"
    assert ("./app.py::run",) in _raw(db, "SELECT qualified_name FROM nodes")
    raw_targets = {row[0] for row in _raw(db, "SELECT target_qualified FROM edges")}
    assert {"!./missing", "!!./odd"} <= raw_targets


def test_a_store_without_a_known_root_stays_absolute(tmp_path):
    store = GraphStore(tmp_path / "graph.db")
    path = (tmp_path / "app.py").as_posix()
    try:
        store.upsert_node(NodeInfo("File", path, path, 1, 2, "python"))
        store.commit()
        assert store.get_metadata(PATH_MODE_KEY) is None
    finally:
        store.close()
    assert _raw(tmp_path / "graph.db", "SELECT file_path FROM nodes") == [(path,)]


def test_suffix_lookups_find_relative_top_level_files(tmp_path):
    repo = _repo(tmp_path / "repo").resolve()
    store = GraphStore(repo / ".code-review-graph" / "graph.db")
    try:
        full_build(repo, store)
        assert store.get_files_matching(f"{repo}/m.py") == [f"{repo}/m.py"]
        assert store.get_files_matching("/m.py") == [f"{repo}/m.py"]
        assert store.get_files_matching("b.py") == [f"{repo}/pkg/b.py"]
        assert store.get_files_matching("/b.py") == [f"{repo}/pkg/b.py"]
    finally:
        store.close()


class _Provider:
    name = "test:relpath"

    def embed(self, texts):
        return [[1.0, 0.0] for _ in texts]

    def embed_query(self, text):
        return [1.0, 0.0]

    @property
    def dimension(self):
        return 2


def test_embedding_keys_follow_the_graph(tmp_path):
    repo = _repo(tmp_path / "repo").resolve()
    db = repo / ".code-review-graph" / "graph.db"
    store = GraphStore(db)
    try:
        full_build(repo, store)
        nodes = store.get_nodes_by_file(str(repo / "m.py"))
    finally:
        store.close()
    with patch("code_review_graph.embeddings.get_provider", return_value=_Provider()):
        emb = EmbeddingStore(db)
        try:
            assert emb.embed_nodes(nodes) == 1
            # Same node again: found under its stored key, nothing re-embedded.
            assert emb.embed_nodes(nodes) == 0
            assert [name for name, _ in emb.search("helper")] == [f"{repo}/m.py::helper"]
        finally:
            emb.close()
    assert _raw(db, "SELECT qualified_name FROM embeddings") == [("./m.py::helper",)]


def test_another_spelling_of_the_same_root_answers_with_the_written_one(tmp_path):
    # Absolute storage kept the writer's spelling, and ``extra`` JSON still does.
    real = tmp_path / "real"
    (real / ".code-review-graph").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    written = f"{link.as_posix()}/app.py"
    store = GraphStore(link / ".code-review-graph" / "graph.db")
    try:
        store.upsert_node(NodeInfo("File", written, written, 1, 2, "python"))
        store.commit()
    finally:
        store.close()
    store = GraphStore(real / ".code-review-graph" / "graph.db")
    try:
        assert store.path_root == link.absolute().as_posix()
        assert [n.file_path for n in store.get_nodes_by_file(written)] == [written]
    finally:
        store.close()


def test_size_queries_and_file_lists_take_absolute_paths(tmp_path):
    repo = _repo(tmp_path / "repo").resolve()
    outside = (tmp_path / "zz_outside.py").resolve().as_posix()
    store = GraphStore(repo / ".code-review-graph" / "graph.db")
    try:
        full_build(repo, store)
        store.upsert_node(NodeInfo("File", outside, outside, 1, 2, "python"))
        store.commit()
        files = store.get_all_files()
        assert files == sorted(files)
        assert outside in files
        by_dir = store.get_nodes_by_size(1, kind="File", file_path_pattern=f"{repo}/pkg")
        assert [n.file_path for n in by_dir] == [f"{repo}/pkg/b.py"]
        whole = store.get_nodes_by_size(1, kind="File", file_path_pattern=str(repo))
        assert {n.file_path for n in whole} == {f"{repo}/m.py", f"{repo}/pkg/b.py"}
        assert store.count_nodes_by_size(1, kind="File", file_path_pattern=str(repo)) == 2
        # A plain substring still matches anywhere.
        assert [n.file_path for n in store.get_nodes_by_size(
            1, kind="File", file_path_pattern="pkg/",
        )] == [f"{repo}/pkg/b.py"]
    finally:
        store.close()


def test_only_metadata_statements_skip_translation(tmp_path):
    """A graph statement that mentions the word is still translated.

    Before, any statement containing ``metadata`` kept its parameters as
    given, so a path compared in such a statement never matched the stored
    ``./`` form.
    """
    repo = _repo(tmp_path / "repo").resolve()
    store = GraphStore(repo / ".code-review-graph" / "graph.db")
    try:
        full_build(repo, store)
        conn = store._conn
        rows = conn.execute(
            "SELECT qualified_name FROM nodes WHERE file_path = ? -- no metadata here",
            (f"{repo}/m.py",),
        ).fetchall()
        assert f"{repo}/m.py::helper" in {r["qualified_name"] for r in rows}
        store.set_metadata("crg_test_root", str(repo))
        raw = _raw(
            repo / ".code-review-graph" / "graph.db",
            "SELECT value FROM metadata WHERE key = 'crg_test_root'",
        )
        assert raw == [(str(repo),)]
    finally:
        store.close()
