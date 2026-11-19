"""MCP tools answer with repo-relative paths when the caller's cwd is in the repo.

Phase 4 of the improvement plan: every offloaded tool takes an optional
``cwd``. Inside the repository root (or below it) the ``<root>/`` prefix is
dropped from paths in the result and the root itself becomes ``.``. Without
``cwd``, or with a ``cwd`` outside the repository, the output stays absolute.
"""

import asyncio
import json
import subprocess

from code_review_graph import main
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build
from code_review_graph.tools._common import relative_to_cwd


def _git(repo, *args):
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=T", *args],
        cwd=repo, check=True, capture_output=True,
    )


def _repo(root):
    root.mkdir(parents=True)
    (root / "m.py").write_text("def helper():\n    return 1\n")
    (root / "pkg").mkdir()
    (root / "pkg" / "b.py").write_text(
        "from m import helper\n\ndef run():\n    return helper()\n"
    )
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "initial")
    return root


def test_paths_inside_the_repo_lose_the_root(tmp_path):
    repo = _repo(tmp_path / "repo").resolve()
    result = {
        "repo_root": str(repo),
        "file": f"{repo}/pkg/b.py",
        "names": [f"{repo}/m.py::helper", ("x", f"{repo}/m.py")],
        f"{repo}/m.py": 1,
        "summary": f"2 callers in {repo}/pkg/b.py and {repo}/m.py",
        "count": 3,
    }
    out = relative_to_cwd(result, str(repo), str(repo / "pkg"))
    assert out == {
        "repo_root": ".",
        "file": "pkg/b.py",
        "names": ["m.py::helper", ("x", "m.py")],
        "m.py": 1,
        "summary": "2 callers in pkg/b.py and m.py",
        "count": 3,
    }


def test_output_stays_absolute_without_a_matching_cwd(tmp_path):
    repo = _repo(tmp_path / "a" / "repo").resolve()
    other = (tmp_path / "elsewhere")
    other.mkdir()
    result = {"file": f"{repo}/m.py"}
    assert relative_to_cwd(result, str(repo), None) == result
    assert relative_to_cwd(result, str(repo), "") == result
    assert relative_to_cwd(result, str(repo), "repo") == result
    assert relative_to_cwd(result, str(repo), str(other)) == result
    # A sibling whose name starts with the root's name is not inside it.
    sibling = tmp_path / "a" / "repo2"
    sibling.mkdir()
    assert relative_to_cwd(result, str(repo), str(sibling)) == result


def test_a_root_spelled_inside_a_longer_path_is_kept(tmp_path):
    repo = _repo(tmp_path / "repo").resolve()
    longer = f"/b{repo}/m.py"
    out = relative_to_cwd({"p": longer, "q": f"{repo}/m.py"}, str(repo), str(repo))
    assert out == {"p": longer, "q": "m.py"}


def test_the_tool_takes_cwd(tmp_path):
    repo = _repo(tmp_path / "repo").resolve()
    store = GraphStore(repo / ".code-review-graph" / "graph.db")
    try:
        full_build(repo, store)
    finally:
        store.close()

    def callers(**kw):
        return asyncio.run(main.query_graph_tool(
            pattern="callers_of", target="helper", repo_root=str(repo), **kw,
        ))

    absolute = json.dumps(callers())
    assert f"{repo}/pkg/b.py" in absolute

    outside = tmp_path / "elsewhere"
    outside.mkdir()
    assert json.dumps(callers(cwd=str(outside))) == absolute

    relative = callers(cwd=str(repo))
    text = json.dumps(relative)
    assert str(repo) not in text
    files = {r.get("file_path") for r in relative["results"]}
    assert "pkg/b.py" in files
