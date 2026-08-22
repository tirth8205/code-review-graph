"""Hermes Agent install/uninstall: YAML preservation regressions.

Hermes' ``config.yaml`` is a hand-edited file full of comments, ordering, and
settings CRG knows nothing about. Every test here asserts the same contract:
CRG may add or remove exactly its own ``mcp_servers.code-review-graph`` entry
and must leave every other byte of the file alone.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from code_review_graph import skills, uninstall
from code_review_graph.skills import (
    PLATFORMS,
    _detect_serve_command,
    _hermes_config_path,
    _hermes_home,
    install_platform_configs,
)

# A realistic slice of a real Hermes config: comments, nested mappings, a
# sibling MCP server, and settings after the mcp_servers block.
_REAL_CONFIG = """\
# Hermes Agent configuration
model:
  default: claude-opus-5   # trailing comment
  provider: copilot

# Servers exposing extra tools
mcp_servers:
  browsermcp:
    command: npx
    args:
      - '@browsermcp/mcp@latest'
  wanderlog:
    command: npx
    args: [-y, wanderlog-mcp]
    env:
      WANDERLOG_COOKIE: s%3Asecret

tool_output:
  max_bytes: 50000
"""


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake user home, so the real ~/.hermes is unreachable from this suite."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


@pytest.fixture
def hermes_home(fake_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point HERMES_HOME at a temp dir under the fake home."""
    home = fake_home / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo root that is a sibling of the fake home, never a parent of it."""
    root = tmp_path / "repo"
    root.mkdir()
    return root


def _install(repo_root: Path) -> list[str]:
    return install_platform_configs(repo_root, target="hermes")


def _entry(config_path: Path) -> dict:
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    return data["mcp_servers"]["code-review-graph"]


class TestSuiteSafety:
    """The suite must never be able to reach a real Hermes config.

    A parametrized test walks every ``PLATFORMS`` entry and calls its
    ``config_path``. Unlike the other platforms, Hermes resolves to an
    absolute user path that ignores ``Path.home()`` patching, so a missing
    guard here silently overwrites the developer's own ``config.yaml``.
    """

    def test_hermes_home_is_pinned_to_a_temp_dir(self, tmp_path_factory) -> None:
        resolved = _hermes_home().resolve()
        real = (Path.home() / ".hermes").resolve()
        assert resolved != real
        assert resolved.is_relative_to(
            Path(tmp_path_factory.getbasetemp()).resolve().parent
        )


class TestHermesHome:
    def test_env_override_wins(self, hermes_home: Path) -> None:
        assert _hermes_home() == hermes_home
        assert _hermes_config_path() == hermes_home / "config.yaml"

    def test_defaults_to_dot_hermes(
        self, fake_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # fake_home patches Path.home(), so clearing the override here still
        # cannot reach the developer's real ~/.hermes.
        monkeypatch.delenv("HERMES_HOME", raising=False)
        monkeypatch.setattr(skills.platform, "system", lambda: "Darwin")
        assert _hermes_home() == fake_home / ".hermes"


class TestInstall:
    def test_creates_config_when_absent(self, repo: Path, hermes_home: Path) -> None:
        configured = _install(repo)
        assert "Hermes Agent" in configured

        entry = _entry(hermes_home / "config.yaml")
        expected_command, expected_args = _detect_serve_command()
        assert entry["command"] == expected_command
        # No repo is pinned. Hermes has no ``cwd`` and outlives any single
        # project, so a baked-in default would answer questions about the
        # wrong repo without saying so. Callers pass ``repo_root`` per tool
        # call; omitting it must fail loudly instead.
        assert entry["args"] == expected_args
        assert "--repo" not in entry["args"]
        assert str(repo) not in entry["args"]
        assert "cwd" not in entry
        assert "type" not in entry

    def test_preserves_comments_and_other_settings(
        self, repo: Path, hermes_home: Path
    ) -> None:
        config = hermes_home / "config.yaml"
        config.write_text(_REAL_CONFIG, encoding="utf-8")
        _install(repo)

        text = config.read_text(encoding="utf-8")
        # Every comment survives byte-for-byte.
        assert "# Hermes Agent configuration" in text
        assert "default: claude-opus-5   # trailing comment" in text
        assert "# Servers exposing extra tools" in text

        data = yaml.safe_load(text)
        assert data["model"] == {"default": "claude-opus-5", "provider": "copilot"}
        assert data["tool_output"] == {"max_bytes": 50000}
        assert data["mcp_servers"]["browsermcp"]["command"] == "npx"
        assert data["mcp_servers"]["wanderlog"]["env"]["WANDERLOG_COOKIE"] == "s%3Asecret"
        assert "code-review-graph" in data["mcp_servers"]

    def test_is_idempotent(self, repo: Path, hermes_home: Path) -> None:
        config = hermes_home / "config.yaml"
        config.write_text(_REAL_CONFIG, encoding="utf-8")
        _install(repo)
        first = config.read_text(encoding="utf-8")
        _install(repo)
        assert config.read_text(encoding="utf-8") == first
        assert first.count("code-review-graph:") == 1

    def test_appends_block_when_key_absent(self, repo: Path, hermes_home: Path) -> None:
        config = hermes_home / "config.yaml"
        config.write_text("model:\n  default: gpt-5.5\n", encoding="utf-8")
        _install(repo)
        data = yaml.safe_load(config.read_text(encoding="utf-8"))
        assert data["model"]["default"] == "gpt-5.5"
        assert "code-review-graph" in data["mcp_servers"]

    def test_dry_run_writes_nothing(self, repo: Path, hermes_home: Path) -> None:
        config = hermes_home / "config.yaml"
        config.write_text(_REAL_CONFIG, encoding="utf-8")
        install_platform_configs(repo, target="hermes", dry_run=True)
        assert config.read_text(encoding="utf-8") == _REAL_CONFIG

    def test_refuses_unparseable_yaml(self, repo: Path, hermes_home: Path) -> None:
        config = hermes_home / "config.yaml"
        broken = "mcp_servers:\n  a: [1, 2\n   bad: :\n"
        config.write_text(broken, encoding="utf-8")
        configured = _install(repo)
        assert configured == []
        assert config.read_text(encoding="utf-8") == broken

    def test_refuses_non_mapping_mcp_servers(self, repo: Path, hermes_home: Path) -> None:
        config = hermes_home / "config.yaml"
        hostile = "mcp_servers:\n  - not-a-mapping\n"
        config.write_text(hostile, encoding="utf-8")
        configured = _install(repo)
        assert configured == []
        assert config.read_text(encoding="utf-8") == hostile

    def test_refuses_flow_style_mcp_servers(self, repo: Path, hermes_home: Path) -> None:
        config = hermes_home / "config.yaml"
        flow = "mcp_servers: {other: {command: npx}}\n"
        config.write_text(flow, encoding="utf-8")
        configured = _install(repo)
        assert configured == []
        assert config.read_text(encoding="utf-8") == flow

    def test_detect_requires_hermes_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "nope"))
        assert PLATFORMS["hermes"]["detect"]() is False
        (tmp_path / "nope").mkdir()
        assert PLATFORMS["hermes"]["detect"]() is True


class TestUninstall:
    def _uninstall(self, repo_root: Path, home: Path, *, dry_run: bool = False):
        report = uninstall.UninstallReport()
        uninstall._process_platform_configs(
            repo_root,
            home,
            report,
            scope="user",
            dry_run=dry_run,
            platforms=frozenset({"hermes"}),
        )
        return report

    def test_removes_only_its_own_entry(self, repo: Path, hermes_home: Path) -> None:
        config = hermes_home / "config.yaml"
        config.write_text(_REAL_CONFIG, encoding="utf-8")
        _install(repo)

        self._uninstall(repo, hermes_home)

        text = config.read_text(encoding="utf-8")
        assert "code-review-graph" not in text
        # The original file is restored byte-for-byte.
        assert text == _REAL_CONFIG

    def test_dry_run_writes_nothing(self, repo: Path, hermes_home: Path) -> None:
        config = hermes_home / "config.yaml"
        config.write_text(_REAL_CONFIG, encoding="utf-8")
        _install(repo)
        installed = config.read_text(encoding="utf-8")

        report = self._uninstall(repo, hermes_home, dry_run=True)

        assert config.read_text(encoding="utf-8") == installed
        assert report.edited_paths

    def test_noop_when_entry_absent(self, repo: Path, hermes_home: Path) -> None:
        config = hermes_home / "config.yaml"
        config.write_text(_REAL_CONFIG, encoding="utf-8")
        report = self._uninstall(repo, hermes_home)
        assert config.read_text(encoding="utf-8") == _REAL_CONFIG
        assert not report.edited_paths

    def test_leaves_unparseable_yaml_alone(self, repo: Path, hermes_home: Path) -> None:
        config = hermes_home / "config.yaml"
        broken = "mcp_servers:\n  code-review-graph: [1, 2\n   bad: :\n"
        config.write_text(broken, encoding="utf-8")
        report = self._uninstall(repo, hermes_home)
        assert config.read_text(encoding="utf-8") == broken
        assert not report.edited_paths

    def test_relocated_hermes_home_is_still_cleanable(
        self, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """HERMES_HOME may sit outside the user's home directory.

        Install honours it, so uninstall must too — otherwise CRG writes a
        file it then refuses to clean up.
        """
        elsewhere = tmp_path / "elsewhere" / "hermes"
        elsewhere.mkdir(parents=True)
        monkeypatch.setenv("HERMES_HOME", str(elsewhere))
        config = elsewhere / "config.yaml"
        config.write_text(_REAL_CONFIG, encoding="utf-8")

        _install(repo)
        assert "code-review-graph" in config.read_text(encoding="utf-8")

        self._uninstall(repo, Path.home())
        assert config.read_text(encoding="utf-8") == _REAL_CONFIG

    def test_round_trip_when_only_entry(self, repo: Path, hermes_home: Path) -> None:
        config = hermes_home / "config.yaml"
        original = "model:\n  default: gpt-5.5\n"
        config.write_text(original, encoding="utf-8")
        _install(repo)
        self._uninstall(repo, hermes_home)

        data = yaml.safe_load(config.read_text(encoding="utf-8"))
        assert data["model"]["default"] == "gpt-5.5"
        assert not data.get("mcp_servers")
