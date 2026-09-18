"""A default update must not read the repository more than once."""

from __future__ import annotations

import pathlib
import subprocess

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, get_db_path, incremental_update


def test_noop_update_opens_each_indexed_file_at_most_once(tmp_path, monkeypatch):
    monkeypatch.setenv("CRG_SERIAL_PARSE", "1")
    repo = tmp_path / "repo"
    repo.mkdir()
    for index in range(6):
        (repo / f"m{index}.py").write_text(f"def f{index}():\n    return {index}\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=T", "commit", "-qm", "init"],
        cwd=repo,
        check=True,
    )
    with GraphStore(get_db_path(repo)) as store:
        full_build(repo, store)
        anchor = store.get_metadata("git_head_sha")

        opened: list[str] = []
        real_open = pathlib.Path.open

        def counting_open(self, *args, **kwargs):
            opened.append(str(self))
            return real_open(self, *args, **kwargs)

        monkeypatch.setattr(pathlib.Path, "open", counting_open)
        result = incremental_update(repo, store, base=anchor)

    source_opens = [path for path in opened if path.endswith(".py")]
    assert result["files_updated"] == 0
    assert len(source_opens) == 6, sorted(source_opens)
