"""Tests for the prune command surface (code_review_graph.prune)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from code_review_graph.daemon import load_config
from code_review_graph.prune import prune
from code_review_graph.registry import Registry


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "crg-home"
    home.mkdir()
    monkeypatch.setenv("CRG_HOME", str(home))
    return home


@pytest.fixture()
def live_and_dead(tmp_path: Path) -> tuple[Path, Path]:
    live = tmp_path / "live-repo"
    live.mkdir()
    _git(live, "init", "-q")
    dead = tmp_path / "dead-repo"
    dead.mkdir()
    (dead / ".git").mkdir()
    return live, dead


def test_prune_reports_dead_entries_without_applying(
    live_and_dead: tuple[Path, Path], home: Path
) -> None:
    live, dead = live_and_dead
    registry = Registry(home / "registry.json")
    registry.register(str(live))
    registry.register(str(dead))
    shutil.rmtree(dead)
    (home / "watch.toml").write_text(
        f'[daemon]\nsession_name = "crg-watch"\nlog_dir = "{home}/logs"\n'
        f'poll_interval = 2\n\n[[repos]]\npath = "{live}"\nalias = "live"\n\n'
        f'[[repos]]\npath = "{dead}"\nalias = "dead"\n',
        encoding="utf-8",
    )

    report = prune(registry=registry, config_path=home / "watch.toml", apply=False)

    assert report.applied is False
    assert report.removed_registry == [str(dead)]
    assert report.removed_watch == [str(dead)]
    assert registry.find_by_path(str(dead)) is not None
    raw = (home / "watch.toml").read_text(encoding="utf-8")
    assert str(dead) in raw


def test_prune_apply_rewrites_registry_and_watch(
    live_and_dead: tuple[Path, Path], home: Path
) -> None:
    live, dead = live_and_dead
    registry = Registry(home / "registry.json")
    registry.register(str(live))
    registry.register(str(dead))
    shutil.rmtree(dead)
    (home / "watch.toml").write_text(
        f'[daemon]\nsession_name = "crg-watch"\nlog_dir = "{home}/logs"\n'
        f'poll_interval = 2\n\n[[repos]]\npath = "{live}"\nalias = "live"\n\n'
        f'[[repos]]\npath = "{dead}"\nalias = "dead"\n',
        encoding="utf-8",
    )

    report = prune(registry=registry, config_path=home / "watch.toml", apply=True)

    assert report.applied is True
    assert report.removed_registry == [str(dead)]
    assert report.removed_watch == [str(dead)]
    assert registry.find_by_path(str(dead)) is None
    assert registry.find_by_path(str(live)) is not None
    config = load_config(home / "watch.toml")
    assert [repo.path for repo in config.repos] == [str(live)]


def test_prune_detects_orphan_external_data_dirs(
    live_and_dead: tuple[Path, Path], home: Path, tmp_path: Path
) -> None:
    live, dead = live_and_dead
    orphan_dir = tmp_path / "ext-data"
    orphan_dir.mkdir()
    registry = Registry(home / "registry.json")
    registry.register(str(live))
    registry.register(str(dead), data_dir=str(orphan_dir))
    shutil.rmtree(dead)

    report = prune(registry=registry, apply=False)

    assert (
        orphan_dir in [Path(p) for p in report.orphan_data_dirs]
        or str(orphan_dir) in report.orphan_data_dirs
    )
    assert orphan_dir.is_dir()


def test_prune_apply_with_data_dirs_removes_orphans(
    live_and_dead: tuple[Path, Path], home: Path, tmp_path: Path
) -> None:
    live, dead = live_and_dead
    orphan_dir = tmp_path / "ext-data"
    orphan_dir.mkdir()
    registry = Registry(home / "registry.json")
    registry.register(str(live))
    registry.register(str(dead), data_dir=str(orphan_dir))
    shutil.rmtree(dead)

    report = prune(registry=registry, apply=True, data_dirs=True)

    assert not orphan_dir.exists()
    assert report.removed_registry == [str(dead)]


def test_watch_config_round_trips_per_repo_options(home: Path, tmp_path: Path) -> None:
    from code_review_graph.daemon import (
        DaemonConfig,
        WatchRepo,
        default_config_path,
        save_config,
        watcher_env,
    )

    live = tmp_path / "repo"
    live.mkdir()
    (live / ".git").mkdir()

    tuned_root = tmp_path / "tuned"
    (tuned_root / ".git").mkdir(parents=True)

    config = DaemonConfig(
        repos=[
            WatchRepo(path=str(live), alias="plain"),
            WatchRepo(
                path=str(tuned_root),
                alias="tuned",
                data_dir="/tmp/ext-data",
                recurse_submodules=True,
                embedding="local/minilm",
            ),
        ]
    )
    save_config(config, default_config_path())
    loaded = load_config(default_config_path())

    assert [r.alias for r in loaded.repos] == ["plain", "tuned"]
    tuned = loaded.repos[1]
    assert tuned.data_dir == "/tmp/ext-data"
    assert tuned.recurse_submodules is True
    assert tuned.embedding == "local/minilm"
    assert loaded.repos[0].data_dir == ""

    env = watcher_env(tuned)
    assert env["CRG_DATA_DIR"] == "/tmp/ext-data"
    assert env["CRG_RECURSE_SUBMODULES"] == "1"
    assert env["CRG_EMBEDDING_MODEL"] == "local/minilm"
    plain_env = watcher_env(loaded.repos[0])
    assert "CRG_DATA_DIR" not in plain_env
    assert "CRG_RECURSE_SUBMODULES" not in plain_env
