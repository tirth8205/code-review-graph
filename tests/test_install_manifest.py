"""install records the files it writes in the repository; uninstall drops them.

``.code-review-graph/install-manifest.json`` lets build and change discovery
leave out install's own files, so a first review is not of ``.mcp.json`` and
``.claude/settings.json``. The record only ever grows on install, and shrinks
as uninstall removes or cleans each file.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

import pytest

from code_review_graph import cli, install_manifest, install_plan, skills, uninstall


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("HERMES_HOME", str(home / ".hermes"))
    monkeypatch.setenv("DSH_HOME", str(home / ".dsh"))
    return root


def _install(repo: Path, platform: str = "claude", **overrides: object) -> None:
    values: dict[str, object] = {
        "repo": str(repo),
        "dry_run": False,
        "platform": platform,
        "yes": True,
        "no_instructions": False,
        "no_skills": False,
        "no_hooks": False,
    }
    values.update(overrides)
    cli._handle_init(argparse.Namespace(**values))


def _record(repo: Path) -> dict:
    return json.loads(install_manifest.manifest_path(repo).read_text(encoding="utf-8"))


def test_install_records_each_file_it_writes_in_the_repository(repo: Path) -> None:
    _install(repo)

    record = _record(repo)
    assert record["version"] == 1
    files = record["files"]
    assert files == sorted(files)
    assert {".mcp.json", ".claude/settings.json", "CLAUDE.md", ".gitignore"} <= set(files)
    assert ".claude/skills/review-changes/SKILL.md" in files
    # Not in the repository's own files: git's hooks folder, the data folder.
    assert not [path for path in files if path.startswith((".git/", ".code-review-graph/"))]
    assert all(not os.path.isabs(path) and "\\" not in path for path in files)
    # The data folder it creates keeps the record out of commits.
    assert (repo / ".code-review-graph" / ".gitignore").read_text(encoding="utf-8").endswith(
        "*\n"
    )


def test_a_dry_run_shows_the_record_and_writes_nothing(repo: Path, capsys) -> None:
    _install(repo, dry_run=True)

    assert not (repo / ".code-review-graph").exists()
    assert "+++ b/.code-review-graph/install-manifest.json" in capsys.readouterr().out


def test_reinstall_keeps_every_recorded_file(repo: Path) -> None:
    _install(repo)
    path = install_manifest.manifest_path(repo)
    data = _record(repo)
    data["files"] = sorted([*data["files"], "tools/kept-from-an-earlier-install.json"])
    path.write_text(json.dumps(data), encoding="utf-8")

    _install(repo, platform="gemini-cli")

    files = _record(repo)["files"]
    assert "tools/kept-from-an-earlier-install.json" in files
    assert ".mcp.json" in files
    assert ".gemini/settings.json" in files


def test_reinstall_with_nothing_new_leaves_the_record_alone(repo: Path) -> None:
    _install(repo)
    # The second run backs up the settings file it merges into, and records
    # the backup; after that there is nothing new.
    _install(repo)
    assert ".claude/settings.json.bak" in _record(repo)["files"]
    path = install_manifest.manifest_path(repo)
    before = path.read_bytes()
    os.utime(path, (1, 1))

    _install(repo)

    assert path.read_bytes() == before
    assert path.stat().st_mtime == 1


def test_declined_instructions_are_not_recorded(repo: Path, monkeypatch) -> None:
    monkeypatch.setattr(cli, "_confirm_yes_no", lambda *args, **kwargs: False)

    _install(repo, yes=False)

    files = _record(repo)["files"]
    assert ".mcp.json" in files
    assert "CLAUDE.md" not in files
    assert not (repo / "CLAUDE.md").exists()


def test_a_damaged_record_is_replaced_by_a_good_one(repo: Path) -> None:
    path = install_manifest.manifest_path(repo)
    path.parent.mkdir()
    path.write_text("{not json", encoding="utf-8")

    _install(repo)

    assert ".mcp.json" in _record(repo)["files"]


def test_uninstall_drops_what_it_removed_and_then_the_record(repo: Path) -> None:
    _install(repo)
    assert install_manifest.manifest_path(repo).is_file()

    uninstall.run(repo=repo, keep_data=True)

    assert not install_manifest.manifest_path(repo).exists()


def test_uninstall_keeps_a_recorded_file_it_did_not_touch(repo: Path) -> None:
    _install(repo)
    path = install_manifest.manifest_path(repo)
    data = _record(repo)
    data["files"] = sorted([*data["files"], "notes/mine.md"])
    path.write_text(json.dumps(data), encoding="utf-8")

    uninstall.run(repo=repo, keep_data=True)

    assert _record(repo)["files"] == ["notes/mine.md"]


def test_unbinding_one_platform_drops_only_its_entry_file(repo: Path) -> None:
    _install(repo)

    uninstall.run(repo=repo, keep_data=True, platforms=["claude"])

    files = _record(repo)["files"]
    assert ".mcp.json" not in files
    assert ".claude/settings.json" in files


def test_an_uninstall_dry_run_leaves_the_record_alone(repo: Path) -> None:
    _install(repo)
    before = install_manifest.manifest_path(repo).read_bytes()

    uninstall.run(repo=repo, keep_data=True, dry_run=True)

    assert install_manifest.manifest_path(repo).read_bytes() == before


def test_relative_paths_keep_only_the_repositorys_own_files(tmp_path: Path) -> None:
    real = tmp_path / "real repo"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)

    paths = install_manifest.relative_paths(
        linked,
        [
            real / ".mcp.json",
            linked / ".claude" / "settings.json",
            real / ".git" / "hooks" / "pre-commit",
            real / ".code-review-graph" / "install-manifest.json",
            tmp_path / "elsewhere.json",
            real,
        ],
    )

    assert paths == [".claude/settings.json", ".mcp.json"]


def test_plan_written_lists_unchanged_writes_too(tmp_path: Path) -> None:
    same = tmp_path / "same.txt"
    same.write_text("x\n", encoding="utf-8")
    with install_plan.planning() as plan:
        install_plan.write_text(same, "x\n")
        install_plan.write_text(tmp_path / "new.txt", "y\n")
        install_plan.unlink(same)
        install_plan.write_text(same, "x\n")

    assert [change.path.name for change in plan.changes()] == ["new.txt"]
    assert sorted(path.name for path in plan.written()) == ["new.txt", "same.txt"]


def test_the_record_skips_a_hook_folder_inside_the_work_tree(repo: Path) -> None:
    """core.hooksPath can point inside the work tree; the hooks there are recorded."""
    subprocess.run(["git", "config", "core.hooksPath", ".githooks"], cwd=repo, check=True)
    assert skills._git_hooks_dir(repo) == repo / ".githooks"

    _install(repo)

    assert ".githooks/pre-commit" in _record(repo)["files"]
