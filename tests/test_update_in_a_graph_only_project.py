"""``update`` works in a project that has a graph but no Git, and failures show.

The OpenCode plugin runs ``update`` in the project folder with no ``--repo``.
Outside Git that stopped with "Not in a git repository", and the plugin threw
the error away, so the graph went stale with no sign why. ``update`` now uses
the project folder when it holds a graph, and the plugin logs a failed
command. Issue crg-s30.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from code_review_graph import skills


def _crg(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_CEILING_DIRECTORIES": str(cwd.parent)}
    env.pop("CRG_REPO_ROOT", None)
    return subprocess.run(
        [sys.executable, "-m", "code_review_graph", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )


def test_update_without_repo_updates_a_graph_only_project(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    assert _crg("build", "--repo", str(project), cwd=project).returncode == 0
    (project / "a.py").write_text(
        "def a():\n    return 1\n\n\ndef added():\n    return 2\n", encoding="utf-8"
    )

    result = _crg("update", cwd=project)

    assert result.returncode == 0, result.stderr
    assert "Not in a git repository" not in result.stderr
    status = _crg("query", "file_summary", "a.py", cwd=project)
    assert "added" in status.stdout, status.stdout + status.stderr


def test_update_without_repo_still_builds_nothing_outside_git(tmp_path):
    folder = tmp_path / "folder"
    folder.mkdir()
    (folder / "a.py").write_text("x = 1\n", encoding="utf-8")

    result = _crg("update", cwd=folder)

    assert result.returncode == 1
    assert "Not in a git repository" in result.stderr
    assert not (folder / ".code-review-graph").exists()


# --- The plugin logs a failed command --------------------------------------


def _node() -> str | None:
    if sys.platform == "win32":
        return None
    node = shutil.which("node")
    if node is None:
        return None
    out = subprocess.run([node, "--version"], capture_output=True, text=True).stdout
    match = re.match(r"v(\d+)\.(\d+)", out.strip())
    if match is None or (int(match.group(1)), int(match.group(2))) < (22, 6):
        return None
    return node


_DRIVER = r"""
import { pathToFileURL } from "node:url"

const [pluginPath, directory] = process.argv.slice(2)
const mod = await import(pathToFileURL(pluginPath).href)
const hooks = await mod.default({ directory, worktree: "/" })
await hooks.event({ event: { type: "file.edited" } })
console.log(JSON.stringify({ done: true }))
"""


@pytest.mark.parametrize("pinned", [False, True], ids=["path-copy", "pinned"])
@pytest.mark.parametrize("cli", ["fails", "missing"])
def test_plugin_logs_a_failed_command(tmp_path, pinned, cli):
    node = _node()
    if node is None:
        pytest.skip("needs Node >= 22.6 with TypeScript type stripping on POSIX")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if cli == "fails":
        fake = bin_dir / ("python" if pinned else "code-review-graph")
        fake.write_text("#!/bin/sh\necho 'ERROR: boom here' >&2\nexit 1\n", encoding="utf-8")
        fake.chmod(0o755)
    launcher = [str(bin_dir / "python"), "-m", "code_review_graph"] if pinned else None
    content = (
        skills._opencode_plugin_content(1, launcher)
        if launcher
        else skills._opencode_plugin_content(1)
    )
    plugin = tmp_path / "crg-plugin.ts"
    plugin.write_text(content, encoding="utf-8")
    driver = tmp_path / "driver.mjs"
    driver.write_text(_DRIVER, encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()

    proc = subprocess.run(
        [node, "--experimental-strip-types", "--no-warnings", str(driver), str(plugin),
         str(project)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin", "HOME": str(tmp_path)},
    )

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.splitlines()[-1]) == {"done": True}
    if cli == "fails":
        assert "[code-review-graph] update failed: ERROR: boom here" in proc.stderr
    else:
        # No CLI at all is a normal state, not a failure to report.
        assert "[code-review-graph]" not in proc.stderr


@pytest.mark.parametrize("major", [1, 2])
def test_every_template_logs_through_one_helper(major):
    for launcher in (None, ["/venv/bin/python", "-m", "code_review_graph"]):
        content = (
            skills._opencode_plugin_content(major, launcher)
            if launcher
            else skills._opencode_plugin_content(major)
        )
        assert content.count("function logFailure(") == 1
        assert "logFailure(args, error)" in content
        assert "} catch {\n    return \"\"" not in content
