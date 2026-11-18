"""The daemon must reuse the same external graph its child will open."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from code_review_graph.daemon import (
    DaemonConfig,
    WatchDaemon,
    WatchRepo,
    default_state_path,
    read_watch_health,
)
from code_review_graph.graph import GraphStore
from code_review_graph.locking import WriteLock
from code_review_graph.registry import Registry


@pytest.mark.parametrize("checkout_owner", [False, True])
def test_lock_excludes_other_processes_until_release(tmp_path, checkout_owner):
    db = tmp_path / "graph.db"
    path = tmp_path / "watch.lock" if checkout_owner else None
    owner = WriteLock(db, owner_path=path)
    probe = [
        sys.executable, "-c",
        "from pathlib import Path; from sys import argv; "
        "from code_review_graph.locking import WriteLock; "
        "lock = WriteLock(Path(argv[1]), owner_path=Path(argv[2]) if argv[2] else None); "
        "print(lock.acquire())",
        str(db), str(path) if path is not None else "",
    ]
    assert owner.acquire()
    try:
        assert subprocess.check_output(probe, text=True, timeout=10).strip() == "False"
    finally:
        owner.release()
    assert subprocess.check_output(probe, text=True, timeout=10).strip() == "True"


@pytest.mark.parametrize("operation", ["start", "reconcile"])
@pytest.mark.parametrize("placement", ["registry", "environment"])
def test_daemon_reuses_external_graph(tmp_path, monkeypatch, operation, placement):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    data = tmp_path / "external-graph"
    data.mkdir()
    with GraphStore(data / "graph.db") as store:
        store.set_metadata("reuse-witness", "keep-existing-index")
        store.commit()
    if placement == "registry":
        Registry().register(str(repo), data_dir=str(data))
    else:
        monkeypatch.setenv("CRG_DATA_DIR", str(data))

    config = DaemonConfig(repos=[WatchRepo(path=str(repo), alias="repo")])
    daemon = WatchDaemon(config=config, config_path=tmp_path / "watch.toml")
    try:
        with (
            patch.object(daemon, "_initial_build") as build,
            patch.object(daemon, "_start_watcher") as watch,
            patch.object(daemon, "start_config_watcher"),
            patch.object(daemon, "start_health_checker"),
        ):
            getattr(daemon, operation)()
            build.assert_not_called()
            watch.assert_called_once_with(config.repos[0])
        assert not (repo / ".code-review-graph").exists()
        with GraphStore(data / "graph.db") as store:
            assert store.get_metadata("reuse-witness") == "keep-existing-index"
    finally:
        daemon.stop()


@pytest.mark.parametrize("operation", ["start", "reconcile"])
def test_shared_admission_builds_only_the_unindexed_checkout(tmp_path, monkeypatch, operation):
    db = tmp_path / "shared.db"
    monkeypatch.setenv("CRG_SHARED_DB", str(db))
    built, fresh = (tmp_path / name for name in ("built", "fresh"))
    for path in (built, fresh):
        path.mkdir()
        (path / ".git").mkdir()
    with GraphStore(db, checkout=built) as store:
        store.set_metadata("last_build_type", "full")
        store.commit()
    # A schema left behind by an interrupted first writer is also unbuilt.
    with GraphStore(db, checkout=fresh):
        pass
    repos = [WatchRepo(path=str(path), alias=path.name) for path in (built, fresh)]
    daemon = WatchDaemon(DaemonConfig(repos=repos), config_path=tmp_path / "watch.toml")
    try:
        with (
            patch.object(daemon, "_initial_build") as build,
            patch.object(daemon, "_start_watcher"),
            patch.object(daemon, "start_config_watcher"),
            patch.object(daemon, "start_health_checker"),
        ):
            getattr(daemon, operation)()
            build.assert_called_once_with(repos[1])
    finally:
        daemon.stop()


@pytest.mark.parametrize("operation", ["start", "reconcile"])
def test_daemon_does_not_build_while_another_watcher_initializes(tmp_path, operation):
    (tmp_path / ".git").mkdir()
    repo = WatchRepo(path=str(tmp_path), alias="repo")
    daemon = WatchDaemon(DaemonConfig(repos=[repo]), config_path=tmp_path / "watch.toml")
    try:
        with (
            patch.object(daemon, "_existing_watcher", return_value=123),
            patch.object(daemon, "_initial_build") as build,
            patch.object(daemon, "start_config_watcher"),
            patch.object(daemon, "start_health_checker"),
            patch("code_review_graph.daemon.subprocess.Popen") as spawn,
        ):
            getattr(daemon, operation)()
            daemon._check_health()
            build.assert_not_called()
            spawn.assert_not_called()
            assert daemon.restart_count(repo.alias) == 0
    finally:
        daemon.stop()


@pytest.mark.skipif(sys.platform == "win32", reason="foreground daemon shutdown uses SIGTERM")
def test_daemon_start_and_reload_reuse_registered_external_graphs(tmp_path, monkeypatch):
    monkeypatch.delenv("CRG_DATA_DIR", raising=False)
    monkeypatch.delenv("CRG_REPO_ROOT", raising=False)
    monkeypatch.setenv("CRG_SERIAL_PARSE", "1")
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])
    home = Path(os.environ["CRG_HOME"])
    config = home / "watch.toml"

    def cli(*args):
        return subprocess.check_output(
            [sys.executable, "-m", "code_review_graph", *args],
            text=True, timeout=20,
        )

    def seed(alias):
        repo = tmp_path / alias
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True, timeout=10)
        source = repo / "app.py"
        source.write_text(f"def {alias}():\n    return 1\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "app.py"], check=True, timeout=10)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-qm", "Seed"], check=True, timeout=10,
        )
        db = tmp_path / f"{alias}-data" / "graph.db"
        cli("build", "--repo", str(repo), "--data-dir", str(db.parent))
        with GraphStore(db) as store:
            store.set_metadata("reuse-witness", alias)
        return repo, db

    def publish(*repos):
        text = "[daemon]\npoll_interval = 1\n"
        for repo in repos:
            text += f"\n[[repos]]\npath = {json.dumps(repo.as_posix())}\nalias = \"{repo.name}\"\n"
        staged = config.with_suffix(".new")
        staged.write_text(text, encoding="utf-8")
        staged.replace(config)

    def watcher(repo, daemon):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and daemon.poll() is None:
            health = read_watch_health(repo)
            if health and health["phase"] == "watching" and health["observer_alive"]:
                return health["pid"]
            time.sleep(0.05)
        pytest.fail(f"no live watcher for {repo}: {daemon_log.read_text(encoding='utf-8')}")

    first, first_db = seed("startup")
    second, second_db = seed("reload")
    publish(first)
    daemon_log = tmp_path / "daemon.log"
    with daemon_log.open("w", encoding="utf-8") as log, subprocess.Popen(
        [sys.executable, "-m", "code_review_graph.daemon_cli", "start", "--foreground"],
        stdout=log, stderr=subprocess.STDOUT,
    ) as daemon:
        try:
            first_pid = watcher(first, daemon)
            publish(first, second)
            second_pid = watcher(second, daemon)
            assert watcher(first, daemon) == first_pid
            assert second_pid != first_pid
        finally:
            if daemon.poll() is None:
                daemon.terminate()
            assert daemon.wait(timeout=10) == 0

    assert not default_state_path().exists()
    assert "Building initial graph" not in daemon_log.read_text(encoding="utf-8")
    for repo, db in ((first, first_db), (second, second_db)):
        with GraphStore(db) as store:
            assert store.get_metadata("reuse-witness") == repo.name
        assert not (repo / ".code-review-graph" / "graph.db").exists()
