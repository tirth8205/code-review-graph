"""Prune dead registry entries, stale watch entries, and orphaned data dirs.

Reports by default; ``--apply`` performs the removals. Registry and watch
metadata are rewritten in place; data-dir deletion requires ``--data-dirs``
on top of ``--apply`` (epic aihub-3t7yh.3.3).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from .daemon import (
    DaemonConfig,
    WatchRepo,
    clear_watch_health,
    default_config_path,
    save_config,
)
from .registry import Registry


@dataclass
class PruneReport:
    """Outcome of one prune pass."""

    removed_registry: list[str] = field(default_factory=list)
    removed_watch: list[str] = field(default_factory=list)
    orphan_data_dirs: list[str] = field(default_factory=list)
    applied: bool = False

    def summary(self) -> str:
        mode = "applied" if self.applied else "dry-run"
        return (
            f"prune ({mode}): registry -{len(self.removed_registry)}, "
            f"watch -{len(self.removed_watch)}, "
            f"orphan data dirs {len(self.orphan_data_dirs)}"
        )


def _repo_is_alive(path: str) -> bool:
    repo = Path(path).expanduser()
    if not repo.is_dir():
        return False
    return (
        (repo / ".git").exists()
        or (repo / ".svn").exists()
        or (repo / ".code-review-graph").exists()
    )


def prune_registry(registry: Registry) -> list[str]:
    """Unregister every entry whose repository path is gone."""
    removed: list[str] = []
    for entry in registry.list_repos():
        path = str(entry.get("path", ""))
        if path and not _repo_is_alive(path):
            if registry.unregister(path):
                removed.append(path)
    return removed


def _raw_watch_paths(config_path: Path) -> list[str]:
    if not config_path.exists():
        return []
    with open(config_path, "rb") as handle:
        raw = tomllib.load(handle)
    return [
        str(entry.get("path", "")).strip() for entry in raw.get("repos", []) if entry.get("path")
    ]


def prune_watch(config_path: Path | None = None) -> tuple[list[str], list[str]]:
    """Drop dead entries from ``watch.toml``; returns (removed, kept)."""
    config_path = config_path or default_config_path()
    raw_paths = _raw_watch_paths(config_path)
    removed = [p for p in raw_paths if not _repo_is_alive(p)]
    if not removed:
        return [], raw_paths
    alive = [p for p in raw_paths if _repo_is_alive(p)]
    config = DaemonConfig(repos=[WatchRepo(path=p, alias=Path(p).name) for p in alive])
    save_config(config, config_path)
    for path in removed:
        try:
            clear_watch_health(path)
        except OSError:
            pass
    return removed, alive


def find_orphan_data_dirs(registry: Registry) -> list[str]:
    """Report registered external data dirs that no longer hold a graph."""
    orphans: list[str] = []
    for entry in registry.list_repos():
        data_dir = entry.get("data_dir")
        if not data_dir:
            continue
        if not (Path(data_dir) / "graph.db").is_file():
            orphans.append(str(data_dir))
    return orphans


def remove_orphan_data_dirs(orphans: list[str]) -> list[str]:
    """Delete empty-or-stale external data dirs; returns what was removed."""
    import shutil

    removed: list[str] = []
    for orphan in orphans:
        path = Path(orphan)
        if not path.is_dir():
            continue
        shutil.rmtree(path)
        removed.append(orphan)
    return removed


def prune(
    registry: Registry | None = None,
    *,
    config_path: Path | None = None,
    apply: bool = False,
    data_dirs: bool = False,
) -> PruneReport:
    """Run one prune pass over registry and watch metadata."""
    registry = registry or Registry()
    report = PruneReport(applied=apply)
    report.removed_registry = [
        str(entry.get("path", ""))
        for entry in registry.list_repos()
        if entry.get("path") and not _repo_is_alive(str(entry["path"]))
    ]
    report.removed_watch = [
        p for p in _raw_watch_paths(config_path or default_config_path()) if not _repo_is_alive(p)
    ]
    report.orphan_data_dirs = find_orphan_data_dirs(registry)
    if not apply:
        return report
    report.removed_registry = prune_registry(registry)
    report.removed_watch, _ = prune_watch(config_path)
    if data_dirs and report.orphan_data_dirs:
        remove_orphan_data_dirs(report.orphan_data_dirs)
    return report
