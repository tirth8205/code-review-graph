"""The OpenCode plugin goes where OpenCode looks: under ``$XDG_CONFIG_HOME``.

OpenCode reads its global config, plugins included, from
``$XDG_CONFIG_HOME/opencode`` (default ``~/.config/opencode``). Install and
uninstall used ``~/.config`` always. Issue crg-cdg.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from code_review_graph import skills, uninstall


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("HERMES_HOME", str(home / ".hermes"))
    monkeypatch.setenv("DSH_HOME", str(home / ".dsh"))
    return home


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".git" / "hooks").mkdir(parents=True)
    return repo


@pytest.mark.parametrize("inside_home", [True, False], ids=["in-home", "elsewhere"])
def test_install_and_uninstall_use_xdg_config_home(
    tmp_path, fake_home, fake_repo, monkeypatch, inside_home
):
    xdg = (fake_home / "dotfiles" if inside_home else tmp_path / "xdg") / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))

    plugin = skills.install_opencode_plugin(2)

    assert plugin == xdg / "opencode" / "plugins" / "crg-plugin.ts"
    assert plugin.is_file()
    assert not (fake_home / ".config").exists()

    report = uninstall.run(repo=fake_repo, keep_data=True)

    assert not plugin.exists()
    assert any(str(plugin) in line for line in report.removed_paths)


def test_install_defaults_to_dot_config(fake_home, monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    plugin = skills.install_opencode_plugin(2)
    assert plugin == fake_home / ".config" / "opencode" / "plugins" / "crg-plugin.ts"


def test_uninstall_also_removes_a_plugin_an_older_install_put_in_dot_config(
    tmp_path, fake_home, fake_repo, monkeypatch
):
    old = fake_home / ".config" / "opencode" / "plugins" / "crg-plugin.ts"
    old.parent.mkdir(parents=True)
    old.write_text("plugin", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

    uninstall.run(repo=fake_repo, keep_data=True)

    assert not old.exists()
