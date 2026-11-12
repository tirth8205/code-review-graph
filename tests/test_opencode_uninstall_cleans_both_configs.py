"""Uninstall removes our OpenCode entry from both opencode.jsonc and opencode.json.

OpenCode loads both project files, so an entry left in either stays live.
Issue crg-27u.
"""

from __future__ import annotations

import json
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
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return home


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".git" / "hooks").mkdir(parents=True)
    return repo


def _read_jsonc(path: Path) -> dict:
    return json.loads(skills._strip_jsonc(path.read_text(encoding="utf-8")))


def _entry_in_both_files(repo: Path) -> tuple[Path, Path]:
    """Install into opencode.json, then again once the user adds opencode.jsonc."""
    json_config = repo / "opencode.json"
    jsonc_config = repo / "opencode.jsonc"
    json_config.write_text('{"mcp": {}}\n', encoding="utf-8")
    assert skills.install_platform_configs(repo, target="opencode") == ["OpenCode"]
    jsonc_config.write_text(
        '{\n  // mine\n  "mcp": {"other": {"type": "local", "command": ["other"]}}\n}\n',
        encoding="utf-8",
    )
    assert skills.install_platform_configs(repo, target="opencode") == ["OpenCode"]
    for path in (json_config, jsonc_config):
        assert "code-review-graph" in _read_jsonc(path)["mcp"]
    return json_config, jsonc_config


def test_opencode_lists_both_project_configs(fake_repo: Path) -> None:
    paths = skills.PLATFORMS["opencode"]["config_paths"](fake_repo)
    assert set(paths) == {fake_repo / "opencode.jsonc", fake_repo / "opencode.json"}


@pytest.mark.parametrize("platforms", [["opencode"], None], ids=["unbind", "full"])
def test_uninstall_cleans_both_opencode_configs(
    fake_repo: Path, fake_home: Path, platforms: list[str] | None
) -> None:
    json_config, jsonc_config = _entry_in_both_files(fake_repo)

    report = uninstall.run(repo=fake_repo, keep_data=True, platforms=platforms)

    assert report.errors == []
    assert "code-review-graph" not in _read_jsonc(json_config).get("mcp", {})
    assert _read_jsonc(jsonc_config)["mcp"] == {
        "other": {"type": "local", "command": ["other"]}
    }
    assert "// mine" in jsonc_config.read_text(encoding="utf-8")
