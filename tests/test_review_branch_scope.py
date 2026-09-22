"""Branch review must exclude changes made only on the base branch."""

import subprocess

from code_review_graph.graph import GraphStore
from code_review_graph.tools.review import get_review_context


def test_review_context_excludes_base_only_changes(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", "-c", "user.email=t@test", "-c", "user.name=t", *args],
            cwd=repo,
            check=True,
            capture_output=True,
            timeout=10,
        )

    git("init", "-b", "main")
    (repo / "initial.txt").write_text("common\n")
    git("add", ".")
    git("commit", "-m", "common")
    git("checkout", "-b", "feature")
    (repo / "feature.py").write_text("def feature():\n    return 1\n")
    git("add", ".")
    git("commit", "-m", "feature")
    git("checkout", "main")
    (repo / "base_only.py").write_text("def unrelated():\n    return 2\n")
    git("add", ".")
    git("commit", "-m", "base change")
    git("checkout", "feature")
    store = GraphStore(tmp_path / "graph.db")
    monkeypatch.setattr(
        "code_review_graph.tools.review._get_store",
        lambda _root: (store, repo),
    )
    result = get_review_context(base="main", repo_root=str(repo), include_source=False)
    assert result["status"] == "ok"
    assert result["context"]["changed_files"] == ["feature.py"]
