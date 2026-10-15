"""``repos`` keeps listing when a registered data_dir cannot be inspected.

PR #1080 made ``code-review-graph repos`` print each entry's registered
``data_dir`` and mark a missing one with ``[MISSING]``. The marker comes from
``Path.is_dir()``, which on Python 3.10 to 3.13 re-raises every ``stat``
error other than "not found" instead of returning ``False``: a parent
directory the user cannot search (``EACCES``), or a dead network or FUSE
mount (``ENOTCONN``, ``ESTALE``, ``EIO``), which FAQ.md names as a use for
``--data-dir``. One unreachable entry then aborted the whole listing with a
traceback and hid every other registered repository, where the listing
printed all of them before.
"""

from __future__ import annotations

import errno
import os
import pathlib
import subprocess
import sys

import pytest

from code_review_graph.registry import Registry


def _git_repo(path):
    path.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "-q", str(path)], check=True, capture_output=True, timeout=30,
    )
    return path


def _register_pair(tmp_path, data_dir):
    relocated = _git_repo(tmp_path / "relocated")
    plain = _git_repo(tmp_path / "plain")
    registry = Registry()
    registry.set_data_dir(str(relocated), str(data_dir))
    registry.register(str(plain), alias="plain")
    return relocated.resolve(), plain.resolve()


def _run_repos(monkeypatch):
    from code_review_graph.cli import main

    monkeypatch.setattr(sys, "argv", ["code-review-graph", "repos"])
    main()


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="needs POSIX permission bits, which root bypasses",
)
def test_repos_lists_every_entry_when_a_data_dir_parent_is_unsearchable(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.delenv("CRG_DATA_DIR", raising=False)
    locked = tmp_path / "locked"
    data_dir = locked / "graphs" / "relocated"
    data_dir.mkdir(parents=True)
    relocated, plain = _register_pair(tmp_path, data_dir)

    locked.chmod(0)
    try:
        _run_repos(monkeypatch)
    finally:
        locked.chmod(0o755)

    out = capsys.readouterr().out
    assert str(relocated) in out
    assert str(plain) in out
    assert "(plain)" in out


@pytest.mark.parametrize("name", ["ENOTCONN", "ESTALE", "EIO"])
def test_repos_lists_every_entry_when_a_data_dir_mount_is_dead(
    tmp_path, monkeypatch, capsys, name,
):
    code = getattr(errno, name, None)
    if code is None:
        pytest.skip(f"errno.{name} is not defined on this platform")
    monkeypatch.delenv("CRG_DATA_DIR", raising=False)
    data_dir = (tmp_path / "mnt" / "share" / "relocated").resolve()
    relocated, plain = _register_pair(tmp_path, data_dir)

    real_stat = pathlib.Path.stat
    dead = str(data_dir)

    def dead_mount_stat(self, *args, **kwargs):
        if str(self).startswith(dead):
            raise OSError(code, os.strerror(code), str(self))
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "stat", dead_mount_stat)
    _run_repos(monkeypatch)

    out = capsys.readouterr().out
    assert str(relocated) in out
    assert str(plain) in out
    assert "(plain)" in out
