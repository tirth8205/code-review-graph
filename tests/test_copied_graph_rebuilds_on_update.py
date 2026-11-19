"""A graph copied from another checkout is rebuilt by its first update.

Relative storage lets a ``graph.db`` copied into a worktree, fork or clone
answer with that checkout's paths, but ``extra`` JSON is not translated: it
still names the checkout the graph was built in (``claimed_endpoint``,
``receiver_module``, ``ambiguous_targets``). An incremental update keeps those
edges, so it rebuilds instead. A graph updated where it was built is untouched.
"""

import json
import shutil
import subprocess

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, incremental_update
from code_review_graph.relpaths import MOVED_FROM_KEY


def _git(repo, *args):
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=T", *args],
        cwd=repo, check=True, capture_output=True,
    )


def _built(root):
    root.mkdir(parents=True)
    (root / "m.py").write_text("def helper():\n    return 1\n")
    (root / "b.py").write_text("from m import helper\n\ndef run():\n    return helper()\n")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "initial")
    store = GraphStore(root / ".code-review-graph" / "graph.db")
    try:
        full_build(root, store)
        # What a resolver leaves behind: a path to the build's own tree in extra.
        store._conn.execute(
            "INSERT INTO edges (kind, source_qualified, target_qualified, file_path, "
            "line, extra, updated_at) VALUES (?, ?, ?, ?, ?, ?, 0)",
            ("CALLS", f"{root}/b.py::run", "helper", f"{root}/b.py", 4,
             json.dumps({"claimed_endpoint": f"{root}/m.py::helper"})),
        )
        store._conn.commit()
    finally:
        store.close()
    return root


def _extras(store):
    return [r[0] for r in store._conn.execute("SELECT extra FROM edges") if r[0]]


def test_the_first_update_of_a_copied_graph_rebuilds(tmp_path):
    origin = _built(tmp_path / "a" / "repo").resolve()
    copy = tmp_path / "b" / "repo"
    shutil.copytree(origin, copy, symlinks=True)
    copy = copy.resolve()

    store = GraphStore(copy / ".code-review-graph" / "graph.db")
    try:
        assert store.get_metadata(MOVED_FROM_KEY) == str(origin)
        result = incremental_update(copy, store, changed_files=[])
        assert result.get("identity_rebuild") is True
        assert store.get_metadata("last_build_type") == "full"
        assert not store.get_metadata(MOVED_FROM_KEY)
        assert not any(str(origin) in extra for extra in _extras(store))
        files = {r[0] for r in store._conn.execute(
            "SELECT file_path FROM nodes WHERE kind = 'File'",
        )}
        assert files == {f"{copy}/b.py", f"{copy}/m.py"}

        # Once rebuilt it is this checkout's graph: updates are incremental.
        again = incremental_update(copy, store, changed_files=[])
        assert "identity_rebuild" not in again
    finally:
        store.close()


def test_a_graph_updated_where_it_was_built_is_not_rebuilt(tmp_path):
    origin = _built(tmp_path / "repo").resolve()
    store = GraphStore(origin / ".code-review-graph" / "graph.db")
    try:
        assert not store.get_metadata(MOVED_FROM_KEY)
        result = incremental_update(origin, store, changed_files=[])
        assert "identity_rebuild" not in result
        # The planted edge survives: nothing was rebuilt.
        assert any("claimed_endpoint" in extra for extra in _extras(store))
    finally:
        store.close()
