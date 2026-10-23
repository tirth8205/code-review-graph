"""The installed OpenCode plugin must load and behave on OpenCode 2.x hosts.

Pins the plugin template that ``install --platform opencode`` writes to
``~/.config/opencode/plugins/crg-plugin.ts`` (PR #1039; issues #1038, #1114,
and the Bun ``$`` bug from #947).

The OpenCode 2.x loader (``packages/core/src/plugin/module.ts``) decodes the
module against ``{ default: { id: string, setup | effect: function } }`` and
rejects anything else with ``Plugin must export a default definition with an id
and an effect or setup function``. A promise plugin's ``setup(ctx)`` receives
``ctx.location``, ``ctx.event.subscribe({ signal })`` and
``ctx.tool.hook(name, callback)``; a rejected ``execute.before`` callback fails
the tool call it guards. The built-in tool names are ``shell``, ``edit``,
``write`` and ``patch``.

Two layers of checks:

* Static checks of the generated source, which always run.
* A behavioural harness that loads the generated TypeScript with Node's type
  stripping, hands ``setup`` a context shaped like the 2.x promise API, and
  drives every handler against a fake ``code-review-graph`` executable. It
  needs Node 22.6 or newer on a POSIX system and skips otherwise.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path

import pytest

from code_review_graph import skills, uninstall
from code_review_graph.skills import _opencode_plugin_content, install_opencode_plugin

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Static checks of the generated source
# ---------------------------------------------------------------------------


def _import_lines(source: str) -> list[str]:
    return [line.strip() for line in source.splitlines() if line.startswith("import ")]


def test_template_has_no_runtime_import_the_host_may_not_provide():
    """Only ``node:`` builtins may be imported at runtime.

    OpenCode 2.0.0 - 2.0.24 do not hand ``@opencode/plugin`` to plugins in the
    global plugins directory: a runtime ``import { Plugin } from
    "@opencode/plugin"`` fails there with ``Cannot find package
    '@opencode/plugin'`` (observed on 2.0.7, 2.0.12, 2.0.20, 2.0.22-2.0.24),
    while the same file with a type-only import loads. ``Plugin.define`` is
    the identity function, so the runtime import buys nothing.
    """
    content = _opencode_plugin_content()
    runtime = [
        line
        for line in _import_lines(content)
        if not line.startswith("import type ")
        and not re.search(r'from "node:[a-z_/]+"$', line)
    ]
    assert runtime == [], f"runtime imports a 2.x host may not resolve: {runtime}"


def test_template_never_uses_the_bun_shell_helper():
    """The 2.x context has no ``$``; ``$`${cmd}``` also escaped whole commands."""
    content = _opencode_plugin_content()
    assert "$`" not in content
    assert "ctx.$" not in content
    assert "{ $ }" not in content
    assert 'execFileAsync("code-review-graph", args' in content
    assert "shell: true" not in content


def test_every_cli_call_is_an_argv_array_with_a_bounded_timeout():
    content = _opencode_plugin_content()
    calls = re.findall(r"runQuiet\((\[[^\]]*\]),\s*([^,]+),\s*([\d_]+)\)", content)
    argvs = {argv for argv, _cwd, _timeout in calls}
    assert argvs == {'["status"]', '["update", "--skip-flows"]', '["detect-changes", "--brief"]'}
    for _argv, _cwd, timeout in calls:
        assert 0 < int(timeout.replace("_", "")) <= 60_000
    assert "timeout: timeoutMs" in content


def test_hooks_target_the_opencode_2x_builtin_tool_names():
    content = _opencode_plugin_content()
    after = content.split('ctx.tool.hook("execute.after"', 1)[1].split("ctx.tool.hook(", 1)[0]
    before = content.split('ctx.tool.hook("execute.before"', 1)[1]
    after_tools = set(re.findall(r'tool !== "([a-z]+)"', after))
    before_tools = set(re.findall(r'tool !== "([a-z]+)"', before))
    assert {"edit", "write", "patch"} <= after_tools
    assert "shell" in before_tools


def test_escape_sequences_survive_the_python_string():
    """``\\s`` and ``\\n`` must reach the TypeScript source as escapes."""
    content = _opencode_plugin_content()
    assert r"/^git\s+commit/i" in content
    assert r'"[code-review-graph] Pre-commit analysis:\n"' in content


def test_template_has_one_default_export_and_a_final_newline():
    content = _opencode_plugin_content()
    content.encode("utf-8")
    assert content.count("export default") == 1
    assert content.endswith("\n")


# ---------------------------------------------------------------------------
# Install, reinstall over an earlier release, uninstall
# ---------------------------------------------------------------------------

# The plugin v2.3.9 wrote (V1 ``app.on`` API), as far as its shape matters.
_RELEASED_V1_PLUGIN = """\
import type { Plugin } from "@opencode-ai/plugin"

export default (app: any) => {
  app.on("file.edited", async ({ $ }: { $: any }) => {
    try {
      await $`code-review-graph update --skip-flows`.quiet()
    } catch {
    }
  })
}
"""


def _released_plugin(tag: str) -> str:
    """Return the plugin source a release wrote, or the frozen copy."""
    if shutil.which("git") is None:
        return _RELEASED_V1_PLUGIN
    result = subprocess.run(
        ["git", "show", f"{tag}:code_review_graph/skills.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        return _RELEASED_V1_PLUGIN
    source = result.stdout
    start = source.find("def _opencode_plugin_content() -> str:")
    opener = '    return """\\\n'
    body_start = source.find(opener, start) if start >= 0 else -1
    body_end = source.find('"""', body_start + len(opener)) if body_start >= 0 else -1
    if body_end < 0:
        return _RELEASED_V1_PLUGIN
    # The released body is a plain string literal with Python escapes.
    body = source[body_start + len(opener):body_end]
    return str(ast.literal_eval('"""' + body + '"""'))


def test_reinstall_replaces_the_plugin_released_in_2_3_9(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    plugin = tmp_path / ".config" / "opencode" / "plugins" / "crg-plugin.ts"
    plugin.parent.mkdir(parents=True)
    released = _released_plugin("v2.3.9")
    assert "app.on(" in released
    plugin.write_text(released, encoding="utf-8")

    install_opencode_plugin(opencode_major=2)

    content = plugin.read_text(encoding="utf-8")
    assert content == _opencode_plugin_content()
    assert "app.on(" not in content
    assert 'id: "code-review-graph"' in content
    assert "setup(ctx" in content


def test_install_twice_is_byte_identical(tmp_path, monkeypatch):
    """A rewrite with identical bytes does not make the 2.x watcher reload."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    first = install_opencode_plugin(opencode_major=2).read_bytes()
    second = install_opencode_plugin(opencode_major=2).read_bytes()
    assert first == second


def test_uninstall_removes_the_plugin_and_keeps_other_plugins(tmp_path, monkeypatch):
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    (repo / ".git" / "hooks").mkdir(parents=True)
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("HERMES_HOME", str(home / ".hermes"))
    plugin = install_opencode_plugin(opencode_major=2)
    other = plugin.parent / "other-plugin.ts"
    other.write_text("export default { id: 'other', setup() {} }\n", encoding="utf-8")

    uninstall.run(repo=repo, keep_data=True)

    assert not plugin.exists()
    assert other.read_text(encoding="utf-8") == "export default { id: 'other', setup() {} }\n"


def test_opencode_platform_still_installs_the_plugin_via_the_cli_path(tmp_path, monkeypatch):
    """``install --platform opencode`` reaches ``install_opencode_plugin``."""
    assert skills.PLATFORMS["opencode"]["detect"]()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(skills, "_detect_opencode_major_version", lambda: 2)
    from code_review_graph import cli

    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setattr(sys, "argv", [
        "code-review-graph", "install", "--platform", "opencode", "--repo", str(repo),
        "--no-instructions", "--no-skills",
    ])
    try:
        cli.main()
    except SystemExit as exc:  # argparse exits 0 on success paths in some versions
        assert exc.code in (0, None)
    plugin = tmp_path / ".config" / "opencode" / "plugins" / "crg-plugin.ts"
    assert plugin.read_text(encoding="utf-8") == _opencode_plugin_content()


# ---------------------------------------------------------------------------
# CHANGELOG
# ---------------------------------------------------------------------------


def _changelog_sections(text: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        heading = re.match(r"^## \[([^\]]+)\]", line)
        if heading:
            current = heading.group(1)
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    return {name: "\n".join(lines) for name, lines in sections.items()}


def test_changelog_credits_version_aware_template_only_to_releases_that_ship_it():
    """A released section must not claim a fix its tag does not contain.

    v2.3.9 shipped the V1 ``app.on`` template (#1114 reports it failing on
    2.3.9), so the OpenCode 2.x entry belongs under ``[Unreleased]``.
    """
    if shutil.which("git") is None:
        pytest.skip("git is not available")
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    sections = _changelog_sections(changelog)
    marker = "OpenCode installer now detects the installed OpenCode major version"
    assert marker in sections["Unreleased"]
    for version, body in sections.items():
        if version == "Unreleased" or marker not in body:
            continue
        shipped = subprocess.run(
            ["git", "show", f"v{version}:code_review_graph/skills.py"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if shipped.returncode != 0:
            continue  # tag not fetched in this checkout
        assert "_detect_opencode_major_version" in shipped.stdout, (
            f"CHANGELOG [{version}] lists version-aware OpenCode plugin generation, "
            f"but v{version} does not ship it"
        )


# ---------------------------------------------------------------------------
# Behavioural harness (Node type stripping)
# ---------------------------------------------------------------------------

_FAKE_CLI = """\
#!/bin/sh
printf '%s\\t%s\\n' "$(pwd -P)" "$*" >> "$FAKE_CRG_LOG"
if [ -n "$FAKE_CRG_SLEEP" ]; then sleep "$FAKE_CRG_SLEEP"; fi
if [ -n "$FAKE_CRG_STDOUT" ]; then printf '%s\\n' "$FAKE_CRG_STDOUT"; fi
exit "${FAKE_CRG_EXIT:-0}"
"""

_DRIVER = r"""
import cp from "node:child_process"
import util from "node:util"
import { syncBuiltinESMExports } from "node:module"
import { pathToFileURL } from "node:url"

const [pluginPath, baseDir, otherDir, binDir] = process.argv.slice(2)
const basePath = process.env.PATH
const withCli = `${binDir}:${basePath}`

// Record every subprocess the plugin starts, synchronously, then run it.
const calls = []
const original = cp.execFile
const originalPromise = original[util.promisify.custom]
function recorded(file, args, options, callback) {
  calls.push({ file, args: [...(args ?? [])], cwd: options?.cwd, timeout: options?.timeout })
  return original.apply(this, arguments)
}
recorded[util.promisify.custom] = (file, args, options) => {
  calls.push({ file, args: [...(args ?? [])], cwd: options?.cwd, timeout: options?.timeout })
  return originalPromise(file, args, options)
}
cp.execFile = recorded
syncBuiltinESMExports()

const logs = []
const print = console.log
console.log = (...parts) => logs.push(parts.map(String).join(" "))
const unhandled = []
process.on("unhandledRejection", (error) => unhandled.push(String(error)))

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
async function waitFor(predicate, ms = 10000) {
  const end = Date.now() + ms
  while (!predicate()) {
    if (Date.now() > end) return false
    await sleep(10)
  }
  return true
}
function env(changes) {
  for (const key of ["FAKE_CRG_EXIT", "FAKE_CRG_SLEEP", "FAKE_CRG_STDOUT"]) delete process.env[key]
  process.env.PATH = withCli
  Object.assign(process.env, changes)
}

const report = {}
const mod = await import(pathToFileURL(pluginPath).href)
const def = mod.default
report.contract = {
  defaultType: def === null ? "null" : typeof def,
  id: def?.id ?? null,
  setupType: typeof def?.setup,
  effectType: typeof def?.effect,
  exports: Object.keys(mod).sort(),
}
const loadable =
  def !== null && typeof def === "object" && typeof def.id === "string" &&
  (typeof def.setup === "function" || typeof def.effect === "function")
if (!loadable || typeof def.setup !== "function") {
  print(JSON.stringify(report))
  process.exit(0)
}

const queue = []
let wake = null
let subscribeOptions = null
let iteratorClosed = false
const push = (event) => {
  queue.push(event)
  if (wake) { const w = wake; wake = null; w() }
}
const hooks = {}
const hookResults = []
const ctx = {
  location: { directory: baseDir, project: { id: "prj", directory: baseDir, canonical: baseDir } },
  options: {},
  event: {
    subscribe(options) {
      subscribeOptions = { hasSignal: options?.signal instanceof AbortSignal }
      const signal = options?.signal
      return {
        [Symbol.asyncIterator]() {
          return {
            async next() {
              for (;;) {
                if (signal?.aborted) {
                  iteratorClosed = true
                  return { done: true, value: undefined }
                }
                if (queue.length) return { done: false, value: queue.shift() }
                await new Promise((resolve) => {
                  wake = resolve
                  signal?.addEventListener("abort", () => resolve(), { once: true })
                })
              }
            },
            async return() { iteratorClosed = true; return { done: true, value: undefined } },
          }
        },
      }
    },
  },
  tool: {
    hook(name, callback) {
      hooks[name] = callback
      const registration = { dispose: async () => {} }
      hookResults.push(name)
      return Promise.resolve(registration)
    },
  },
}

env({})
const cleanup = await def.setup(ctx)
report.setup = {
  cleanupType: typeof cleanup,
  hooks: Object.keys(hooks).sort(),
  subscribe: subscribeOptions,
}

const since = (mark) => calls.slice(mark)
async function settle(callback) {
  const started = Date.now()
  try {
    await callback()
    return { threw: false, ms: Date.now() - started }
  } catch (error) {
    return { threw: true, error: String(error), ms: Date.now() - started }
  }
}

// --- session.created ------------------------------------------------------
let mark = calls.length
push({ type: "session.updated", location: { directory: baseDir } })
push({ type: "session.created", location: { directory: otherDir } })
push({ type: "session.created", location: { directory: baseDir } })
await waitFor(() => calls.length > mark)
report.sessionEvents = since(mark)

mark = calls.length
push({ type: "session.created", data: {} })
await waitFor(() => calls.length > mark)
report.sessionWithoutLocation = since(mark)

mark = calls.length
env({ FAKE_CRG_EXIT: "3", FAKE_CRG_STDOUT: "should not be logged" })
push({ type: "session.created", location: { directory: baseDir } })
await waitFor(() => calls.length > mark)
await sleep(300)
env({ PATH: basePath })
push({ type: "session.created", location: { directory: baseDir } })
await waitFor(() => calls.length > mark + 1)
await sleep(300)
env({ FAKE_CRG_STDOUT: "Nodes: 5" })
push({ type: "session.created", location: { directory: baseDir } })
await waitFor(() => calls.length > mark + 2)
await waitFor(() => logs.some((line) => line.includes("Nodes: 5")), 5000)
report.sessionSurvivesFailures = { calls: since(mark), logs: [...logs] }
logs.length = 0

// --- execute.after ----------------------------------------------------------
env({})
const after = hooks["execute.after"]
mark = calls.length
const ignoredAfter = []
for (const event of [
  { tool: "read", status: "completed", input: {}, result: {} },
  { tool: "edit", status: "error", input: {}, error: {} },
  { tool: "shell", status: "completed", input: {}, result: {} },
  { status: "completed", input: {}, result: {} },
]) {
  ignoredAfter.push(await settle(() => after(event)))
}
report.afterIgnored = { calls: since(mark), results: ignoredAfter }

report.afterUpdates = {}
for (const tool of ["edit", "write", "patch", "Edit"]) {
  mark = calls.length
  const result = await settle(() => after({ tool, status: "completed", input: {}, result: {} }))
  report.afterUpdates[tool] = { calls: since(mark), result }
}

env({ FAKE_CRG_SLEEP: "2" })
mark = calls.length
report.afterDoesNotWait = await settle(() =>
  after({ tool: "write", status: "completed", input: {}, result: {} }))
report.afterDoesNotWait.calls = since(mark)

env({ PATH: basePath })
mark = calls.length
report.afterMissingCli = await settle(() =>
  after({ tool: "edit", status: "completed", input: {}, result: {} }))
report.afterMissingCli.calls = since(mark)

// --- execute.before ---------------------------------------------------------
env({})
const before = hooks["execute.before"]
mark = calls.length
const ignoredBefore = []
for (const event of [
  { tool: "shell", input: { command: "git status" } },
  { tool: "shell", input: { command: "echo git commit" } },
  { tool: "read", input: { command: "git commit -m x" } },
  { tool: "edit", input: { command: "git commit -m x" } },
  { tool: "shell", input: { command: ["git", "commit"] } },
  { tool: "shell", input: null },
  { tool: "shell" },
  { input: { command: "git commit" } },
]) {
  ignoredBefore.push(await settle(() => before(event)))
}
report.beforeIgnored = { calls: since(mark), results: ignoredBefore }

report.beforeCommits = {}
for (const [name, event] of [
  ["shell", { tool: "shell", input: { command: "git commit -m 'msg with spaces'" } }],
  ["bash", { tool: "bash", input: { command: "git commit" } }],
  ["caseAndSpacing", { tool: "Shell", input: { command: "GIT   COMMIT --amend" } }],
  ["cmdField", { tool: "shell", input: { cmd: "git commit -a" } }],
]) {
  mark = calls.length
  const result = await settle(() => before(event))
  report.beforeCommits[name] = { calls: since(mark), result }
}

env({ FAKE_CRG_SLEEP: "1", FAKE_CRG_STDOUT: "risk: low" })
mark = calls.length
logs.length = 0
report.beforeWaitsAndLogs = await settle(() =>
  before({ tool: "shell", input: { command: "git commit -m x" } }))
report.beforeWaitsAndLogs.calls = since(mark)
report.beforeWaitsAndLogs.logs = [...logs]

env({ FAKE_CRG_EXIT: "1", FAKE_CRG_STDOUT: "boom" })
logs.length = 0
report.beforeFailingCli = await settle(() =>
  before({ tool: "shell", input: { command: "git commit -m x" } }))
report.beforeFailingCli.logs = [...logs]

env({ PATH: basePath })
report.beforeMissingCli = await settle(() =>
  before({ tool: "shell", input: { command: "git commit -m x" } }))

// --- cleanup ----------------------------------------------------------------
env({})
mark = calls.length
if (typeof cleanup === "function") await cleanup()
await waitFor(() => iteratorClosed, 2000)
push({ type: "session.created", location: { directory: baseDir } })
await sleep(300)
report.cleanup = { iteratorClosed, calls: since(mark) }

report.unhandled = unhandled
print(JSON.stringify(report))
"""


def _node_with_type_stripping() -> str | None:
    if sys.platform == "win32":
        return None
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


def _nfc(path: str) -> str:
    return unicodedata.normalize("NFC", path)


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    node = _node_with_type_stripping()
    if node is None:
        pytest.skip("needs Node >= 22.6 (TypeScript type stripping) on a POSIX system")

    root = tmp_path_factory.mktemp("opencode-plugin")
    host = root / "host"
    # Older 2.x hosts do not provide @opencode/plugin at runtime.
    host.mkdir()
    (host / "package.json").write_text('{"type": "module"}\n', encoding="utf-8")
    plugin = host / "crg-plugin.ts"
    plugin.write_text(_opencode_plugin_content(), encoding="utf-8")
    driver = host / "driver.mjs"
    driver.write_text(_DRIVER, encoding="utf-8")

    bin_dir = root / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "code-review-graph"
    fake.write_text(_FAKE_CLI, encoding="utf-8")
    fake.chmod(0o755)

    base_dir = (root / "repo dir é").resolve()
    base_dir.mkdir()
    other_dir = (root / "other-repo").resolve()
    other_dir.mkdir()
    cli_log = root / "cli.log"

    child_env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(root),
        "FAKE_CRG_LOG": str(cli_log),
    }
    proc = subprocess.run(
        [node, "--experimental-strip-types", "--no-warnings", str(driver),
         str(plugin), str(base_dir), str(other_dir), str(bin_dir)],
        cwd=host,
        env=child_env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-4000:]
    lines = [line for line in proc.stdout.splitlines() if line.startswith("{")]
    assert lines, proc.stdout[-2000:] + proc.stderr[-2000:]
    report = json.loads(lines[-1])
    cli_lines = (
        cli_log.read_text(encoding="utf-8").splitlines() if cli_log.exists() else []
    )
    return {
        "report": report,
        "base": str(base_dir),
        "other": str(other_dir),
        "cli": [tuple(line.split("\t", 1)) for line in cli_lines],
    }


def _argv(calls: list[dict]) -> list[list[str]]:
    return [call["args"] for call in calls]


def test_default_export_satisfies_the_2x_loader_schema(harness):
    contract = harness["report"]["contract"]
    assert contract["defaultType"] == "object"
    assert contract["id"] == "code-review-graph"
    assert contract["setupType"] == "function"
    assert contract["exports"] == ["default"]


def test_setup_registers_both_tool_hooks_and_an_abortable_subscription(harness):
    report = harness["report"]
    setup = report["setup"]
    assert setup["hooks"] == ["execute.after", "execute.before"]
    assert setup["subscribe"] == {"hasSignal": True}
    assert setup["cleanupType"] == "function"
    assert report["unhandled"] == []


def test_session_created_runs_status_only_for_this_location(harness):
    report = harness["report"]
    events = report["sessionEvents"]
    assert _argv(events) == [["status"]]
    assert events[0]["cwd"] == harness["base"]
    assert events[0]["file"] == "code-review-graph"
    assert events[0]["timeout"] == 15_000
    no_location = report["sessionWithoutLocation"]
    assert _argv(no_location) == [["status"]]
    assert no_location[0]["cwd"] == harness["base"]


def test_session_status_failures_never_stop_the_subscription(harness):
    survived = harness["report"]["sessionSurvivesFailures"]
    assert _argv(survived["calls"]) == [["status"], ["status"], ["status"]]
    assert survived["logs"] == ["[code-review-graph] Nodes: 5"]


def test_successful_edit_write_patch_update_the_graph(harness):
    updates = harness["report"]["afterUpdates"]
    for tool in ("edit", "write", "patch", "Edit"):
        entry = updates[tool]
        assert entry["result"]["threw"] is False
        assert _argv(entry["calls"]) == [["update", "--skip-flows"]], tool
        assert entry["calls"][0]["cwd"] == harness["base"]
        assert entry["calls"][0]["timeout"] == 60_000


def test_failed_or_unrelated_tool_calls_do_not_update(harness):
    ignored = harness["report"]["afterIgnored"]
    assert ignored["calls"] == []
    assert all(result["threw"] is False for result in ignored["results"])


def test_update_after_an_edit_does_not_block_the_tool_call(harness):
    """The fake CLI sleeps 2s; the hook must return long before that."""
    result = harness["report"]["afterDoesNotWait"]
    assert result["threw"] is False
    assert _argv(result["calls"]) == [["update", "--skip-flows"]]
    assert result["ms"] < 1500


def test_git_commit_through_shell_runs_detect_changes_first(harness):
    commits = harness["report"]["beforeCommits"]
    for name, entry in commits.items():
        assert entry["result"]["threw"] is False, name
        assert _argv(entry["calls"]) == [["detect-changes", "--brief"]], name
        assert entry["calls"][0]["cwd"] == harness["base"]
        assert entry["calls"][0]["timeout"] == 30_000
    waited = harness["report"]["beforeWaitsAndLogs"]
    assert waited["threw"] is False
    assert waited["ms"] >= 900  # the analysis finishes before the commit runs
    assert waited["logs"] == ["[code-review-graph] Pre-commit analysis:\nrisk: low"]


def test_non_commit_or_malformed_tool_input_is_ignored(harness):
    ignored = harness["report"]["beforeIgnored"]
    assert ignored["calls"] == []
    assert all(result["threw"] is False for result in ignored["results"])


def test_a_failing_or_missing_cli_never_rejects_a_hook(harness):
    """A rejected ``execute.before`` fails the guarded commit on 2.x."""
    report = harness["report"]
    assert report["beforeFailingCli"]["threw"] is False
    assert report["beforeFailingCli"]["logs"] == []
    assert report["beforeMissingCli"]["threw"] is False
    assert report["afterMissingCli"]["threw"] is False
    assert _argv(report["afterMissingCli"]["calls"]) == [["update", "--skip-flows"]]
    assert report["unhandled"] == []


def test_cleanup_ends_the_event_subscription(harness):
    cleanup = harness["report"]["cleanup"]
    assert cleanup["iteratorClosed"] is True
    assert cleanup["calls"] == []


def test_cli_runs_inside_a_path_with_spaces_and_unicode(harness):
    """The real subprocess lands in the plugin's directory, byte for byte."""
    cwds = {_nfc(cwd) for cwd, _args in harness["cli"]}
    assert cwds == {_nfc(harness["base"])}
    args = {args for _cwd, args in harness["cli"]}
    assert {"status", "update --skip-flows", "detect-changes --brief"} <= args
    assert os.path.basename(harness["base"]) == "repo dir é"
