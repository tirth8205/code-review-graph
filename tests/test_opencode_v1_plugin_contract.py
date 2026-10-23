"""Contract tests for the OpenCode 1.x plugin template."""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from code_review_graph.skills import _opencode_plugin_content


def test_v1_template_uses_returned_hooks_shape():
    content = _opencode_plugin_content(1)
    assert 'import type { Plugin } from "@opencode-ai/plugin"' in content
    assert "const CodeReviewGraphPlugin: Plugin = async" in content
    assert "event: async" in content
    assert '"tool.execute.before": async' in content
    assert "app.on(" not in content
    assert 'id: "code-review-graph"' not in content


def test_v1_template_uses_safe_bounded_process_calls():
    content = _opencode_plugin_content(1)
    calls = re.findall(r"runQuiet\((\[[^\]]*\]),\s*([^,]+),\s*([\d_]+)\)", content)
    argvs = {argv for argv, _cwd, _timeout in calls}
    assert argvs == {'["status"]', '["update", "--skip-flows"]', '["detect-changes", "--brief"]'}
    for _argv, _cwd, timeout in calls:
        assert 0 < int(timeout.replace("_", "")) <= 60_000
    assert 'execFileAsync("code-review-graph", args' in content
    assert "shell: true" not in content


def test_v1_template_avoids_the_subdirectory_repo_bug():
    content = _opencode_plugin_content(1)
    assert "const baseDir = worktree || directory" in content
    assert "--repo" not in content


def _node_with_type_stripping() -> str | None:
    node = shutil.which("node")
    if node is None:
        return None
    try:
        out = subprocess.run(
            [node, "--version"], capture_output=True, text=True, timeout=30
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.match(r"v(\d+)\.(\d+)", out)
    if match is None or (int(match.group(1)), int(match.group(2))) < (22, 6):
        return None
    return node


_DRIVER = r"""
import cp from "node:child_process"
import util from "node:util"
import { syncBuiltinESMExports } from "node:module"
import { pathToFileURL } from "node:url"

const [pluginPath, directory, worktree] = process.argv.slice(2)
const calls = []
function recorded(file, args, options, callback) {
  calls.push({ file, args: [...(args ?? [])], cwd: options?.cwd, timeout: options?.timeout })
  if (typeof callback === "function") callback(null, "Nodes: 1\n", "")
}
recorded[util.promisify.custom] = async (file, args, options) => {
  calls.push({ file, args: [...(args ?? [])], cwd: options?.cwd, timeout: options?.timeout })
  return { stdout: "Nodes: 1\n", stderr: "" }
}
cp.execFile = recorded
syncBuiltinESMExports()

const mod = await import(pathToFileURL(pluginPath).href)
const hooks = await mod.default({ directory, worktree })
await hooks.event({ event: { type: "session.created" } })
await hooks.event({ event: { type: "file.edited" } })
await hooks["tool.execute.before"](
  { tool: "bash" },
  { args: { command: "git commit -m test" } },
)
console.log(JSON.stringify({ exports: Object.keys(mod).sort(), calls }))
"""


def test_v1_hooks_execute_expected_commands_from_worktree(tmp_path):
    node = _node_with_type_stripping()
    if node is None:
        pytest.skip("needs Node >= 22.6 with TypeScript type stripping")

    plugin = tmp_path / "crg-plugin.ts"
    plugin.write_text(_opencode_plugin_content(1), encoding="utf-8")
    driver = tmp_path / "driver.mjs"
    driver.write_text(_DRIVER, encoding="utf-8")
    directory = tmp_path / "repo" / "sub"
    directory.mkdir(parents=True)
    worktree = directory.parent

    proc = subprocess.run(
        [
            node,
            "--experimental-strip-types",
            "--no-warnings",
            str(driver),
            str(plugin),
            str(directory),
            str(worktree),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout.splitlines()[-1])
    assert report["exports"] == ["default"]
    assert [call["args"] for call in report["calls"]] == [
        ["status"],
        ["update", "--skip-flows"],
        ["detect-changes", "--brief"],
    ]
    assert {call["cwd"] for call in report["calls"]} == {str(worktree)}
    assert [call["timeout"] for call in report["calls"]] == [15_000, 60_000, 30_000]
