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
    assert 'const baseDir = worktree && worktree !== "/" ? worktree : directory' in content
    assert "--repo" not in content


@pytest.mark.parametrize("major", [1, 2])
def test_updates_go_through_one_per_instance_updater(major):
    """A module-level queue would drop one repository's update for another's."""
    content = _opencode_plugin_content(major)
    assert content.count('runQuiet(["update", "--skip-flows"]') == 1
    assert "function makeUpdater(cwd: string)" in content
    assert content.count("makeUpdater(baseDir)") == 1
    # The running/pending flags live inside the factory, not at module level.
    assert "\nlet " not in content


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


_BEHAVIOUR_DRIVER = r"""
import cp from "node:child_process"
import util from "node:util"
import { syncBuiltinESMExports } from "node:module"
import { pathToFileURL } from "node:url"

const [pluginPath, directory] = process.argv.slice(2)
const calls = []
let inflight = 0
let maxInflight = 0
function recorded() { throw new Error("only the promisified form is used") }
recorded[util.promisify.custom] = async (file, args, options) => {
  calls.push({ args: [...args], cwd: options?.cwd })
  inflight += 1
  maxInflight = Math.max(maxInflight, inflight)
  await new Promise((resolve) => setTimeout(resolve, 100))
  inflight -= 1
  return { stdout: "", stderr: "" }
}
cp.execFile = recorded
syncBuiltinESMExports()
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

const mod = await import(pathToFileURL(pluginPath).href)
const report = {}

// OpenCode reports "/" as the worktree outside Git.
const hooks = await mod.default({ directory, worktree: "/" })
const edit = () => hooks.event({ event: { type: "file.edited" } })
await hooks.event({ event: { type: "session.created" } })
report.statusCwd = calls[0].cwd
calls.length = 0

// Edits during a run share one follow-up run; they never overlap.
const burst = [edit(), edit(), edit(), edit()]
await Promise.all(burst)
await sleep(350)
report.burst = { updates: calls.length, maxInflight, cwd: calls[0].cwd }

// Idle again: the next edit starts a new run straight away.
calls.length = 0
await edit()
report.afterIdle = calls.length

// A second instance has its own queue, so a busy repository cannot swallow
// another repository's update.
calls.length = 0
const other = await mod.default({ directory: directory + "-other", worktree: "/" })
await Promise.all([edit(), other.event({ event: { type: "file.edited" } })])
await sleep(150)
report.twoInstances = calls.map((call) => call.cwd).sort()
console.log(JSON.stringify(report))
"""


def test_v1_worktree_sentinel_and_update_serialisation(tmp_path):
    node = _node_with_type_stripping()
    if node is None:
        pytest.skip("needs Node >= 22.6 with TypeScript type stripping")

    plugin = tmp_path / "crg-plugin.ts"
    plugin.write_text(_opencode_plugin_content(1), encoding="utf-8")
    driver = tmp_path / "driver.mjs"
    driver.write_text(_BEHAVIOUR_DRIVER, encoding="utf-8")
    directory = tmp_path / "svn-project"
    directory.mkdir()

    proc = subprocess.run(
        [node, "--experimental-strip-types", "--no-warnings", str(driver), str(plugin),
         str(directory)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout.splitlines()[-1])
    assert report["statusCwd"] == str(directory)
    assert report["burst"] == {"updates": 2, "maxInflight": 1, "cwd": str(directory)}
    assert report["afterIdle"] == 1
    assert report["twoInstances"] == [str(directory), str(directory) + "-other"]


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
