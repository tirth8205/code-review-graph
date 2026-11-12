"""The settings backup keeps the user's own copy, and uninstall cleans up ours.

Install backs a settings file up to ``<name>.bak`` before it merges hooks in.
A later install that changed the file wrote the backup again, so it held our
own earlier edit and the user's copy was lost. And uninstall never removed a
backup that held nothing but our hooks or server entry. Issue crg-n8n.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from code_review_graph import skills, uninstall

_OTHER_COPY = skills._HookProgram(
    "[ -x '/other venv/bin/python3' ]", "'/other venv/bin/python3' -m code_review_graph"
)


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("HERMES_HOME", str(home / ".hermes"))
    monkeypatch.setenv("DSH_HOME", str(home / ".dsh"))
    return home


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".git" / "hooks").mkdir(parents=True)
    return repo


def _install_twice(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Install, then install again from another copy, so the second one edits the file."""
    with monkeypatch.context() as patch:
        patch.setattr(skills, "_hook_program", lambda: _OTHER_COPY)
        assert skills.install_hooks(repo) is not None
    assert skills.install_hooks(repo) is not None


def test_a_second_install_keeps_the_first_backup(repo, monkeypatch):
    settings = repo / ".claude" / "settings.json"
    settings.parent.mkdir()
    original = '{\n  "model": "mine"\n}\n'
    settings.write_text(original, encoding="utf-8")

    before = settings.read_text(encoding="utf-8")
    _install_twice(repo, monkeypatch)

    assert settings.read_text(encoding="utf-8") != before
    assert (repo / ".claude" / "settings.json.bak").read_text(encoding="utf-8") == original


def test_uninstall_removes_a_backup_holding_only_our_hooks(repo, fake_home, monkeypatch):
    _install_twice(repo, monkeypatch)
    backup = repo / ".claude" / "settings.json.bak"
    assert backup.exists()

    report = uninstall.run(repo=repo, keep_data=True)

    assert report.errors == []
    assert not backup.exists()


def test_uninstall_keeps_a_backup_with_the_users_settings(repo, fake_home, monkeypatch):
    settings = repo / ".claude" / "settings.json"
    settings.parent.mkdir()
    original = '{"model": "mine"}\n'
    settings.write_text(original, encoding="utf-8")
    _install_twice(repo, monkeypatch)

    uninstall.run(repo=repo, keep_data=True)

    assert (repo / ".claude" / "settings.json.bak").read_text(encoding="utf-8") == original


def test_uninstall_removes_a_gemini_backup_of_our_server_entry(repo, fake_home):
    """Gemini CLI install writes the server entry, then backs the file up for its hooks."""
    assert skills.install_platform_configs(repo, target="gemini-cli") == ["Gemini CLI"]
    assert skills.install_gemini_cli_hooks(repo) is not None
    backup = repo / ".gemini" / "settings.json.bak"
    assert "code-review-graph" in json.loads(backup.read_text(encoding="utf-8"))["mcpServers"]

    uninstall.run(repo=repo, keep_data=True)

    assert not backup.exists()


def test_no_steering_leaves_the_backup_alone(repo, monkeypatch):
    _install_twice(repo, monkeypatch)
    backup = repo / ".claude" / "settings.json.bak"
    before = backup.read_bytes()

    uninstall.remove_steering_hook(repo)

    assert backup.read_bytes() == before
