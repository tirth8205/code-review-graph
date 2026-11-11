"""``code-review-graph doctor`` names what is wrong with a setup, and changes nothing.

From the gstack developer-experience review (item 16). The case that started
it: pip installed 2.4.0 in a virtual environment, the MCP entry ran
``uvx code-review-graph serve``, uvx ran an older uv tool install, and that
copy refused the graph the new one wrote. Nothing said which copy was which.

The copies here are small shell scripts that print a version, so these tests
run real programs from a real PATH. They need a POSIX shell.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from code_review_graph import __version__, cli, doctor
from code_review_graph.graph import GraphStore
from code_review_graph.migrations import LATEST_VERSION
from code_review_graph.parser import NodeInfo

pytestmark = pytest.mark.skipif(os.name == "nt", reason="fake copies are POSIX shell scripts")

OLD = "2.3.9"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _program(folder: Path, name: str, body: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _copy(folder: Path, version: str) -> Path:
    return _program(folder, "code-review-graph", f'echo "code-review-graph {version}"\n')


@pytest.fixture()
def env(tmp_path: Path, monkeypatch) -> dict[str, Path]:
    """A PATH that holds git and nothing else, and a throwaway home folder."""
    tools = tmp_path / "tools"
    tools.mkdir()
    git = shutil.which("git")
    assert git, "these tests need git"
    (tools / "git").symlink_to(git)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(tools))
    return {"tools": tools, "home": home}


def _set_path(monkeypatch, env: dict[str, Path], *folders: Path) -> None:
    monkeypatch.setenv("PATH", os.pathsep.join([*map(str, folders), str(env["tools"])]))


@pytest.fixture()
def repo(tmp_path: Path, env) -> Path:
    """A git repository with one commit and a graph built at that commit."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "doctor@example.invalid")
    _git(root, "config", "user.name", "Doctor Test")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    with GraphStore(root / ".code-review-graph" / "graph.db") as store:
        store.upsert_node(NodeInfo(
            kind="File", name="app.py", file_path=str(root / "app.py"),
            line_start=1, line_end=2, language="python",
        ), file_hash="h")
        store.set_metadata("git_head_sha", _git(root, "rev-parse", "HEAD"))
        store.set_metadata("last_updated", "2026-11-11T10:00:00")
        store.commit()
    return root


def _mcp(root: Path, command: str, args: list[str]) -> None:
    (root / ".mcp.json").write_text(json.dumps({
        "mcpServers": {"code-review-graph": {
            "command": command, "args": args, "type": "stdio", "cwd": str(root),
        }},
    }), encoding="utf-8")


def _statuses(report: doctor.Report) -> list[tuple[str, str]]:
    return [(f.status, f.message) for f in report.findings]


def _with(report: doctor.Report, status: str, text: str) -> list[doctor.Finding]:
    return [f for f in report.findings if f.status == status and text in f.message]


# ---------------------------------------------------------------------------
# Copies on PATH
# ---------------------------------------------------------------------------


def test_healthy_setup_exits_zero(repo, env, tmp_path, monkeypatch, capsys):
    _set_path(monkeypatch, env, _copy(tmp_path / "bin", __version__).parent)

    assert doctor.run_doctor(repo) == 0

    out = capsys.readouterr().out
    assert out.rstrip().endswith("All good.")
    assert "Hooks and your shell run this one." in out
    assert f"built at HEAD" in out


def test_an_older_copy_first_on_path_fails_and_says_which(repo, env, tmp_path, monkeypatch):
    old = _copy(tmp_path / "old", OLD)
    _set_path(monkeypatch, env, old.parent)

    report = doctor.diagnose(repo)

    assert not report.healthy
    [failed] = _with(report, doctor.FAIL, f"On PATH: {old} {OLD}")
    assert f"Upgrade the copy at {old}" in failed.fix
    assert [c.version for c in report.path_copies] == [OLD]


def test_a_hidden_older_copy_is_only_a_warning(repo, env, tmp_path, monkeypatch):
    new = _copy(tmp_path / "new", __version__)
    old = _copy(tmp_path / "old", OLD)
    _set_path(monkeypatch, env, new.parent, old.parent)

    report = doctor.diagnose(repo)

    assert report.healthy, _statuses(report)
    assert [c.path for c in report.path_copies] == [str(new), str(old)]
    assert _with(report, doctor.WARN, f"{old} {OLD}. Hidden behind the one above.")


def test_the_same_file_twice_on_path_is_listed_once(repo, env, tmp_path, monkeypatch):
    new = _copy(tmp_path / "new", __version__)
    link = tmp_path / "link"
    link.mkdir()
    (link / "code-review-graph").symlink_to(new)
    _set_path(monkeypatch, env, new.parent, link, new.parent)

    assert [c.path for c in doctor.diagnose(repo).path_copies] == [str(new)]


# ---------------------------------------------------------------------------
# MCP entries
# ---------------------------------------------------------------------------


def test_unpinned_uvx_entry_reads_the_uv_tool_install(repo, env, tmp_path, monkeypatch):
    """The reviewer's case: uvx ran the older uv tool install."""
    marker = tmp_path / "uvx-ran"
    bin_dir = tmp_path / "bin"
    _copy(bin_dir, __version__)
    _program(bin_dir, "uv", (
        'if [ "$1 $2" = "tool list" ]; then\n'
        f'  echo "code-review-graph v{OLD}"\n'
        '  echo "- code-review-graph"\n'
        "fi\n"
    ))
    _program(bin_dir, "uvx", f'touch "{marker}"\n')
    _set_path(monkeypatch, env, bin_dir)
    _mcp(repo, "uvx", ["code-review-graph", "serve"])

    report = doctor.diagnose(repo)

    [failed] = _with(report, doctor.FAIL, "MCP entry in .mcp.json")
    assert f"runs {OLD}, not {__version__}" in failed.message
    assert "uv tool upgrade code-review-graph" in failed.fix
    assert report.mcp_entries[0].version == OLD
    assert not marker.exists(), "doctor ran uvx, which can start a download"


def test_unpinned_uvx_without_a_tool_install_is_unknown_and_never_run(
    repo, env, tmp_path, monkeypatch,
):
    marker = tmp_path / "uvx-ran"
    bin_dir = tmp_path / "bin"
    _copy(bin_dir, __version__)
    _program(bin_dir, "uv", "echo 'No tools installed'\n")
    _program(bin_dir, "uvx", f'touch "{marker}"\necho "code-review-graph 9.9.9"\n')
    _set_path(monkeypatch, env, bin_dir)
    _mcp(repo, "uvx", ["code-review-graph", "serve"])

    report = doctor.diagnose(repo)

    assert report.healthy, _statuses(report)
    [unknown] = _with(report, doctor.WARN, "Version unknown")
    assert "uvx code-review-graph --version" in unknown.message
    assert not marker.exists()


def test_pinned_uvx_entry_reads_the_pin(repo, env, tmp_path, monkeypatch):
    _set_path(monkeypatch, env, _copy(tmp_path / "bin", __version__).parent)
    _mcp(repo, "uvx", [f"code-review-graph=={OLD}", "serve"])

    report = doctor.diagnose(repo)

    [failed] = _with(report, doctor.FAIL, "MCP entry in .mcp.json")
    assert "pinned in the entry" in failed.message
    assert "install again" in failed.fix


def test_python_module_entry_asks_that_python(repo, env, tmp_path, monkeypatch):
    _set_path(monkeypatch, env, _copy(tmp_path / "bin", __version__).parent)
    _mcp(repo, sys.executable, ["-m", "code_review_graph", "serve"])

    report = doctor.diagnose(repo)

    assert report.mcp_entries[0].version == __version__
    assert _with(report, doctor.OK, f"MCP entry in .mcp.json (Claude Code, CodeBuddy Code) "
                                    f"runs {__version__}")


def test_absolute_program_entry_asks_that_program(repo, env, tmp_path, monkeypatch):
    _set_path(monkeypatch, env, _copy(tmp_path / "bin", __version__).parent)
    old = _copy(tmp_path / "venv" / "bin", OLD)
    _mcp(repo, str(old), ["serve"])

    report = doctor.diagnose(repo)

    [failed] = _with(report, doctor.FAIL, "MCP entry in .mcp.json")
    assert f"the copy at {old}" in failed.message


# ---------------------------------------------------------------------------
# The graph
# ---------------------------------------------------------------------------


def test_no_graph_fails_with_the_build_fix(tmp_path, env, monkeypatch):
    root = tmp_path / "bare"
    root.mkdir()
    _git(root, "init", "-q")
    _set_path(monkeypatch, env, _copy(tmp_path / "bin", __version__).parent)

    report = doctor.diagnose(root)

    [failed] = _with(report, doctor.FAIL, "No graph at")
    assert failed.fix == "Run: code-review-graph build"
    assert not (root / ".code-review-graph").exists(), "doctor created the data folder"


def test_a_graph_built_at_another_commit_fails_with_the_update_fix(
    repo, env, tmp_path, monkeypatch,
):
    _set_path(monkeypatch, env, _copy(tmp_path / "bin", __version__).parent)
    (repo / "b.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "second")

    report = doctor.diagnose(repo)

    [failed] = _with(report, doctor.FAIL, "but HEAD is")
    assert failed.fix == "Run: code-review-graph update"
    assert report.graph["stale"] is True


def test_a_graph_from_a_newer_copy_fails(repo, env, tmp_path, monkeypatch):
    _set_path(monkeypatch, env, _copy(tmp_path / "bin", __version__).parent)
    conn = sqlite3.connect(repo / ".code-review-graph" / "graph.db")
    conn.execute(
        "UPDATE metadata SET value = ? WHERE key = 'schema_version'",
        (str(LATEST_VERSION + 1),),
    )
    conn.commit()
    conn.close()

    report = doctor.diagnose(repo)

    assert _with(report, doctor.FAIL, f"schema v{LATEST_VERSION + 1}, written by a newer copy")


def test_an_unfinished_build_fails(repo, env, tmp_path, monkeypatch):
    _set_path(monkeypatch, env, _copy(tmp_path / "bin", __version__).parent)
    conn = sqlite3.connect(repo / ".code-review-graph" / "graph.db")
    conn.execute(
        "INSERT OR REPLACE INTO metadata (key, value) VALUES ('build_state', 'in-progress')"
    )
    conn.commit()
    conn.close()

    report = doctor.diagnose(repo)

    assert _with(report, doctor.FAIL, "The last build did not finish")


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------


def _released_git_hook_blocks() -> list[str]:
    from code_review_graph import skills

    return [
        *skills._LEGACY_GIT_HOOK_BLOCKS,
        skills._GIT_HOOK_BLOCK,
        *(skills._git_sync_hook_block(name) for name in skills._GIT_SYNC_HOOK_NAMES),
    ]


@pytest.mark.parametrize("block", _released_git_hook_blocks())
def test_every_released_git_hook_shape_is_seen(block, repo, env, tmp_path, monkeypatch):
    _set_path(monkeypatch, env, _copy(tmp_path / "bin", __version__).parent)
    hooks = Path(_git(repo, "rev-parse", "--absolute-git-dir")) / "hooks"
    hooks.mkdir(exist_ok=True)
    (hooks / "pre-commit").write_text("#!/bin/sh\n" + block, encoding="utf-8")

    report = doctor.diagnose(repo)

    assert report.hooks == [{"where": "git", "file": str(hooks), "events": ["pre-commit"]}]
    assert report.healthy, _statuses(report)


def test_editor_hooks_are_listed_by_event(repo, env, tmp_path, monkeypatch):
    from code_review_graph.skills import generate_hooks_config

    _set_path(monkeypatch, env, _copy(tmp_path / "bin", __version__).parent)
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.json").write_text(
        json.dumps(generate_hooks_config(repo)), encoding="utf-8",
    )

    report = doctor.diagnose(repo)

    assert _with(report, doctor.OK, "Hooks: Claude Code PostToolUse, SessionStart.")


def test_hooks_with_nothing_on_path_are_a_warning(repo, env, monkeypatch):
    hooks = Path(_git(repo, "rev-parse", "--absolute-git-dir")) / "hooks"
    hooks.mkdir(exist_ok=True)
    (hooks / "pre-commit").write_text(
        "#!/bin/sh\ncode-review-graph update || true\n", encoding="utf-8",
    )

    report = doctor.diagnose(repo)

    assert _with(report, doctor.WARN, "there is none on PATH here, so they do nothing")


def test_a_hook_that_names_an_older_copy_by_path_fails(repo, env, tmp_path, monkeypatch):
    _set_path(monkeypatch, env, _copy(tmp_path / "bin", __version__).parent)
    old = _copy(tmp_path / "pinned", OLD)
    hooks = Path(_git(repo, "rev-parse", "--absolute-git-dir")) / "hooks"
    hooks.mkdir(exist_ok=True)
    (hooks / "pre-commit").write_text(
        f'#!/bin/sh\n"{old}" update --quiet || true\n', encoding="utf-8",
    )

    report = doctor.diagnose(repo)

    assert _with(report, doctor.FAIL, f"The hooks run {old}, which is {OLD}")


# ---------------------------------------------------------------------------
# Read-only, exit codes and JSON
# ---------------------------------------------------------------------------


def _snapshot(root: Path) -> dict[str, tuple[int, int]]:
    return {
        str(path.relative_to(root)): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in root.rglob("*")
        if path.is_file()
    }


def test_doctor_writes_nothing(repo, env, tmp_path, monkeypatch):
    _set_path(monkeypatch, env, _copy(tmp_path / "bin", __version__).parent)
    _mcp(repo, sys.executable, ["-m", "code_review_graph", "serve"])
    before = _snapshot(repo)

    doctor.diagnose(repo)

    assert _snapshot(repo) == before


def _cli(monkeypatch, *argv: str) -> int:
    monkeypatch.setattr(sys, "argv", ["code-review-graph", *argv])
    try:
        cli.main()
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


def test_cli_json_is_one_object(repo, env, tmp_path, monkeypatch, capsys):
    _set_path(monkeypatch, env, _copy(tmp_path / "bin", __version__).parent)

    code = _cli(monkeypatch, "doctor", "--json", "--repo", str(repo))

    data = json.loads(capsys.readouterr().out)
    assert code == 0
    assert data["healthy"] is True
    assert data["this_copy"]["version"] == __version__
    assert data["graph"]["schema_version"] == LATEST_VERSION
    assert {check["status"] for check in data["checks"]} <= {"ok", "info", "warn"}


def test_cli_exits_one_when_a_check_fails(repo, env, tmp_path, monkeypatch, capsys):
    _set_path(monkeypatch, env, _copy(tmp_path / "old", OLD).parent)

    code = _cli(monkeypatch, "doctor", "--repo", str(repo))

    out = capsys.readouterr().out
    assert code == 1
    assert out.rstrip().endswith("1 problem found.")
    assert "Fix: Upgrade the copy at" in out
