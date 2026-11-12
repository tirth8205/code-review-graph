"""``build`` and ``update`` on a ``--repo`` folder that is not there print one line.

Since the write lock of PR #1126 the tools check the root before anything
makes the data folder, so a missing path ended in a ``ValueError``
traceback. Before that, the store made the folder. Making a folder from a
typed path is a worse outcome than stopping, so the command now stops with
one ``Error:`` line and exit 1, and makes nothing.
"""

from __future__ import annotations

import sys

import pytest

from code_review_graph import cli


@pytest.mark.parametrize("command", ["build", "update", "postprocess"])
def test_missing_repo_folder_is_one_error_line(tmp_path, monkeypatch, capsys, command):
    missing = tmp_path / "no-such-repo"
    monkeypatch.setattr(sys, "argv", ["code-review-graph", command, "--repo", str(missing)])

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    lines = [line for line in err.splitlines() if line.strip()]
    assert len(lines) == 1, err
    assert lines[0].startswith("Error: ")
    assert str(missing) in lines[0]
    assert "Traceback" not in err
    assert not missing.exists()


def test_repo_path_that_is_a_file_is_one_error_line(tmp_path, monkeypatch, capsys):
    a_file = tmp_path / "notes.txt"
    a_file.write_text("hello\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["code-review-graph", "build", "--repo", str(a_file)])

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("Error: ")
    assert "Traceback" not in err
