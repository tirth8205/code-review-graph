"""End-to-end lifecycle checks for every platform installer.

CONTRIBUTING.md makes every supported AI tool permanent maintenance surface:
its config path, schema, install merge, uninstall and tests all have to keep
working on every release. These tests drive the real ``install`` and
``uninstall`` code paths against a scratch git repository inside a throwaway
HOME, once per platform, through the five steps that a user actually performs:

1. first install writes the exact config the platform reads;
2. a second install is idempotent, byte for byte;
3. installing over a pre-existing config preserves unrelated user settings,
   including another MCP server;
4. uninstall removes every trace it created and leaves the rest alone;
5. reinstalling over an older release's entry replaces it rather than leaving
   a stale entry beside the new one.

The suite is slow on purpose (it shells out to ``git`` and writes real files),
so it is gated behind the ``platform_lifecycle`` marker:

    uv run --python 3.13 python -m pytest tests/test_platform_lifecycle.py \
        -m platform_lifecycle -q

Known-broken combinations are marked ``xfail(strict=True)`` with the bug they
record, so the suite stays green while a regression in the opposite direction
(a bug quietly fixed, or a passing case quietly breaking) still fails loudly.
"""

from __future__ import annotations

import json
import os
import platform as platform_module
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from code_review_graph import cli, skills, uninstall

pytestmark = pytest.mark.platform_lifecycle

ENTRY_NAME = "code-review-graph"
FOREIGN_SERVER = "unrelated-mcp-server"
FOREIGN_SETTING = "unrelatedUserSetting"
FOREIGN_VALUE = "do-not-touch-me"
STALE_ABSOLUTE = "/opt/previous-release/bin/python3.9"
STALE_CWD = "/previous/checkout/somewhere/else"


# ---------------------------------------------------------------------------
# Canary bookkeeping.
#
# A check that passes because it silently did nothing is worse than no check.
# Every step records the concrete comparisons it made; ``test_zz_canary``
# refuses to pass unless each step really executed against each platform and
# really compared something.
# ---------------------------------------------------------------------------

_LEDGER: dict[str, dict[str, int]] = {}


def _record(step: str, platform_key: str, comparisons: int) -> None:
    assert comparisons > 0, f"{step}[{platform_key}] compared nothing"
    _LEDGER.setdefault(step, {})[platform_key] = comparisons


# ---------------------------------------------------------------------------
# What each platform is expected to write.
#
# Restated here on purpose rather than read back out of ``skills.PLATFORMS``:
# a test that derives its expectations from the code under test cannot notice
# that code changing. A moved config path or a renamed settings key fails here.
# ---------------------------------------------------------------------------


def _zed_relative() -> str:
    if platform_module.system() == "Darwin":
        return "Library/Application Support/Zed/settings.json"
    return ".config/zed/settings.json"


@dataclass(frozen=True)
class PlatformExpectation:
    """The contract one platform's installer is held to."""

    key: str
    scope: str  # "repo", "home" or "hermes"
    relative_config: str
    server_key: str
    fmt: str  # object | array | toml | yaml
    entry_type: str | None  # expected value of the entry's "type" field
    expects_cwd: bool
    extra_fields: dict[str, Any] = field(default_factory=dict)
    command_is_list: bool = False  # OpenCode folds command+args into one list

    def config_path(self, repo: Path, home: Path, hermes_home: Path) -> Path:
        base = {"repo": repo, "home": home, "hermes": hermes_home}[self.scope]
        return base / self.relative_config


EXPECTATIONS: dict[str, PlatformExpectation] = {
    "codex": PlatformExpectation(
        key="codex",
        scope="home",
        relative_config=".codex/config.toml",
        server_key="mcp_servers",
        fmt="toml",
        entry_type="stdio",
        expects_cwd=True,
    ),
    "claude": PlatformExpectation(
        key="claude",
        scope="repo",
        relative_config=".mcp.json",
        server_key="mcpServers",
        fmt="object",
        entry_type="stdio",
        expects_cwd=True,
    ),
    "cursor": PlatformExpectation(
        key="cursor",
        scope="repo",
        relative_config=".cursor/mcp.json",
        server_key="mcpServers",
        fmt="object",
        entry_type="stdio",
        expects_cwd=True,
    ),
    "windsurf": PlatformExpectation(
        key="windsurf",
        scope="home",
        relative_config=".codeium/windsurf/mcp_config.json",
        server_key="mcpServers",
        fmt="object",
        entry_type=None,
        expects_cwd=True,
    ),
    "zed": PlatformExpectation(
        key="zed",
        scope="home",
        relative_config=_zed_relative(),
        server_key="context_servers",
        fmt="object",
        entry_type=None,
        expects_cwd=True,
    ),
    "continue": PlatformExpectation(
        key="continue",
        scope="home",
        relative_config=".continue/config.json",
        server_key="mcpServers",
        fmt="array",
        entry_type="stdio",
        expects_cwd=True,
    ),
    "opencode": PlatformExpectation(
        key="opencode",
        scope="repo",
        relative_config="opencode.jsonc",
        server_key="mcp",
        fmt="object",
        entry_type="local",
        expects_cwd=False,
        command_is_list=True,
    ),
    "antigravity": PlatformExpectation(
        key="antigravity",
        scope="home",
        relative_config=".gemini/antigravity/mcp_config.json",
        server_key="mcpServers",
        fmt="object",
        entry_type=None,
        expects_cwd=True,
    ),
    "gemini-cli": PlatformExpectation(
        key="gemini-cli",
        scope="repo",
        relative_config=".gemini/settings.json",
        server_key="mcpServers",
        fmt="object",
        entry_type=None,
        expects_cwd=True,
    ),
    "qwen": PlatformExpectation(
        key="qwen",
        scope="home",
        relative_config=".qwen/settings.json",
        server_key="mcpServers",
        fmt="object",
        entry_type="stdio",
        expects_cwd=True,
    ),
    "kiro": PlatformExpectation(
        key="kiro",
        scope="repo",
        relative_config=".kiro/settings/mcp.json",
        server_key="mcpServers",
        fmt="object",
        entry_type="stdio",
        expects_cwd=True,
    ),
    "qoder": PlatformExpectation(
        key="qoder",
        scope="repo",
        relative_config=".qoder/mcp.json",
        server_key="mcpServers",
        fmt="object",
        entry_type="stdio",
        expects_cwd=True,
    ),
    "copilot": PlatformExpectation(
        key="copilot",
        scope="repo",
        relative_config=".vscode/mcp.json",
        server_key="servers",
        fmt="object",
        entry_type="stdio",
        expects_cwd=True,
    ),
    "copilot-cli": PlatformExpectation(
        key="copilot-cli",
        scope="home",
        relative_config=".copilot/mcp-config.json",
        server_key="mcpServers",
        fmt="object",
        entry_type="local",
        expects_cwd=True,
        extra_fields={"tools": ["*"]},
    ),
    "hermes": PlatformExpectation(
        key="hermes",
        scope="hermes",
        relative_config="config.yaml",
        server_key="mcp_servers",
        fmt="yaml",
        entry_type=None,
        expects_cwd=False,
    ),
    "codebuddy": PlatformExpectation(
        key="codebuddy",
        scope="repo",
        relative_config=".mcp.json",
        server_key="mcpServers",
        fmt="object",
        entry_type="stdio",
        expects_cwd=True,
    ),
}

PLATFORM_KEYS = sorted(EXPECTATIONS)

# Instruction files each single-platform install is expected to write, as a
# cross-check on ``skills._PLATFORM_INSTRUCTION_FILES``.
EXPECTED_INSTRUCTION_FILES: dict[str, tuple[str, ...]] = {
    "codex": ("AGENTS.md",),
    "claude": ("CLAUDE.md",),
    "cursor": ("AGENTS.md", ".cursorrules"),
    "windsurf": (".windsurfrules",),
    "zed": (),
    "continue": (),
    "opencode": ("AGENTS.md",),
    "antigravity": ("AGENTS.md", "GEMINI.md"),
    "gemini-cli": ("GEMINI.md",),
    "qwen": (),
    "kiro": (".kiro/steering/code-review-graph.md",),
    "qoder": ("QODER.md",),
    "copilot": (".github/instructions/code-review-graph.instructions.md",),
    "copilot-cli": (".github/instructions/code-review-graph.instructions.md",),
    "hermes": ("AGENTS.md",),
    "codebuddy": ("CODEBUDDY.md",),
}

# Files a single-platform install is expected to create for hooks and skills,
# relative to the repo root ("repo:") or the throwaway home ("home:").
EXPECTED_EXTRA_ARTIFACTS: dict[str, tuple[str, ...]] = {
    "codex": ("home:.codex/hooks.json", "repo:.git/hooks/pre-commit"),
    "claude": (
        "repo:.claude/settings.json",
        "repo:.claude/skills/explore-codebase/SKILL.md",
        "repo:.git/hooks/pre-commit",
    ),
    "cursor": ("home:.cursor/hooks.json", "home:.cursor/hooks/crg-update.sh"),
    "windsurf": (),
    "zed": (),
    "continue": (),
    "opencode": ("home:.config/opencode/plugins/crg-plugin.ts",),
    "antigravity": (),
    "gemini-cli": (
        "repo:.gemini/skills/explore-codebase/SKILL.md",
        "repo:.gemini/hooks/crg-update.sh",
    ),
    "qwen": (),
    "kiro": (),
    "qoder": (
        "repo:.qoder/settings.json",
        "repo:.qoder/skills/explore-codebase/SKILL.md",
        "repo:.git/hooks/pre-commit",
    ),
    "copilot": (),
    "copilot-cli": (),
    "hermes": ("hermes:skills/code-review-graph/explore-codebase/SKILL.md",),
    "codebuddy": (
        "repo:.codebuddy/settings.json",
        "repo:.codebuddy/skills/explore-codebase/SKILL.md",
    ),
}


# ---------------------------------------------------------------------------
# Isolated environment
# ---------------------------------------------------------------------------


HOME_SUBDIRS = (
    ".codex",
    ".cursor",
    ".codeium/windsurf",
    ".continue",
    ".gemini",
    ".gemini/antigravity",
    ".qwen",
    ".kiro",
    ".copilot",
    ".config/opencode",
    str(Path(_zed_relative()).parent),
)


@dataclass
class Sandbox:
    repo: Path
    home: Path
    hermes_home: Path

    def config_path(self, key: str) -> Path:
        return EXPECTATIONS[key].config_path(self.repo, self.home, self.hermes_home)

    def resolve(self, marker: str) -> Path:
        scope, _, rel = marker.partition(":")
        base = {"repo": self.repo, "home": self.home, "hermes": self.hermes_home}[scope]
        return base / rel

    def roots(self) -> tuple[Path, ...]:
        return (self.repo, self.home, self.hermes_home)


@pytest.fixture
def sandbox(tmp_path_factory, monkeypatch) -> Sandbox:
    """A scratch git repository plus a throwaway HOME and HERMES_HOME."""
    base = tmp_path_factory.mktemp("lifecycle").resolve()
    home = base / "home"
    repo = base / "repo"
    hermes_home = home / ".hermes"
    for directory in (home, repo):
        directory.mkdir(parents=True, exist_ok=True)
    for relative in HOME_SUBDIRS:
        (home / relative).mkdir(parents=True, exist_ok=True)
    hermes_home.mkdir(parents=True, exist_ok=True)

    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Lifecycle Test"], cwd=repo, check=True)
    (repo / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("CRG_HOME", str(home / ".code-review-graph"))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(Path, "home", lambda: home)

    return Sandbox(repo=repo, home=home, hermes_home=hermes_home)


def run_install(sandbox: Sandbox, key: str) -> None:
    """Drive the real ``code-review-graph install`` handler for one platform."""
    args = _Namespace(
        repo=str(sandbox.repo),
        platform=key,
        dry_run=False,
        yes=True,
        no_instructions=False,
        no_skills=False,
        no_hooks=False,
    )
    cli._handle_init(args)


class _Namespace:
    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)


def run_uninstall(sandbox: Sandbox, **kwargs: Any) -> uninstall.UninstallReport:
    return uninstall.run(repo=sandbox.repo, **kwargs)


# ---------------------------------------------------------------------------
# Config readers, one per on-disk format.
# ---------------------------------------------------------------------------


def read_entry(path: Path, expect: PlatformExpectation) -> dict[str, Any] | None:
    """Return the code-review-graph entry from a config, or None if absent."""
    if not path.exists():
        return None
    raw = path.read_text(encoding="utf-8")
    if expect.fmt == "toml":
        import tomllib

        data = tomllib.loads(raw)
        return data.get(expect.server_key, {}).get(ENTRY_NAME)
    if expect.fmt == "yaml":
        import yaml

        data = yaml.safe_load(raw) or {}
        return (data.get(expect.server_key) or {}).get(ENTRY_NAME)
    data = json.loads(skills._strip_jsonc(raw))
    bucket = data.get(expect.server_key)
    if expect.fmt == "array":
        if not isinstance(bucket, list):
            return None
        matches = [e for e in bucket if isinstance(e, dict) and e.get("name") == ENTRY_NAME]
        return matches[0] if matches else None
    if not isinstance(bucket, dict):
        return None
    return bucket.get(ENTRY_NAME)


def count_entries(path: Path, expect: PlatformExpectation) -> int:
    """Count how many code-review-graph registrations the config holds."""
    if not path.exists():
        return 0
    raw = path.read_text(encoding="utf-8")
    if expect.fmt == "toml":
        return len(re.findall(rf"^\[{re.escape(expect.server_key)}\.{ENTRY_NAME}\]", raw, re.M))
    if expect.fmt == "yaml":
        return len(re.findall(rf"^\s+{ENTRY_NAME}:", raw, re.M))
    data = json.loads(skills._strip_jsonc(raw))
    bucket = data.get(expect.server_key)
    if expect.fmt == "array":
        if not isinstance(bucket, list):
            return 0
        return sum(1 for e in bucket if isinstance(e, dict) and e.get("name") == ENTRY_NAME)
    if not isinstance(bucket, dict):
        return 0
    return 1 if ENTRY_NAME in bucket else 0


def read_foreign(path: Path, expect: PlatformExpectation) -> tuple[Any, Any]:
    """Return ``(unrelated top-level setting, unrelated MCP server entry)``."""
    raw = path.read_text(encoding="utf-8")
    if expect.fmt == "toml":
        import tomllib

        data = tomllib.loads(raw)
        return data.get(FOREIGN_SETTING), data.get(expect.server_key, {}).get(FOREIGN_SERVER)
    if expect.fmt == "yaml":
        import yaml

        data = yaml.safe_load(raw) or {}
        return data.get(FOREIGN_SETTING), (data.get(expect.server_key) or {}).get(FOREIGN_SERVER)
    data = json.loads(skills._strip_jsonc(raw))
    bucket = data.get(expect.server_key)
    if expect.fmt == "array":
        matches = [
            e for e in (bucket or []) if isinstance(e, dict) and e.get("name") == FOREIGN_SERVER
        ]
        return data.get(FOREIGN_SETTING), (matches[0] if matches else None)
    return data.get(FOREIGN_SETTING), (bucket or {}).get(FOREIGN_SERVER)


FOREIGN_ENTRY = {"command": "other-tool", "args": ["--serve"]}


def write_preexisting_config(path: Path, expect: PlatformExpectation) -> None:
    """Seed a user's own config: an unrelated setting and another MCP server."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if expect.fmt == "toml":
        path.write_text(
            f'{FOREIGN_SETTING} = "{FOREIGN_VALUE}"\n\n'
            f"[{expect.server_key}.{FOREIGN_SERVER}]\n"
            'command = "other-tool"\n'
            'args = ["--serve"]\n',
            encoding="utf-8",
        )
        return
    if expect.fmt == "yaml":
        path.write_text(
            f"{FOREIGN_SETTING}: {FOREIGN_VALUE}\n"
            f"{expect.server_key}:\n"
            f"  {FOREIGN_SERVER}:\n"
            "    command: other-tool\n"
            "    args:\n"
            "      - --serve\n",
            encoding="utf-8",
        )
        return
    if expect.fmt == "array":
        bucket: Any = [{"name": FOREIGN_SERVER, **FOREIGN_ENTRY}]
    else:
        bucket = {FOREIGN_SERVER: dict(FOREIGN_ENTRY)}
    path.write_text(
        json.dumps({FOREIGN_SETTING: FOREIGN_VALUE, expect.server_key: bucket}, indent=2) + "\n",
        encoding="utf-8",
    )


def write_stale_config(path: Path, expect: PlatformExpectation) -> None:
    """Seed the config an older release would have written.

    The old entry names an absolute interpreter path and a checkout that no
    longer exists, which is exactly the shape that has to be replaced rather
    than kept.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if expect.fmt == "toml":
        path.write_text(
            f"[{expect.server_key}.{ENTRY_NAME}]\n"
            f'command = "{STALE_ABSOLUTE}"\n'
            'args = ["-m", "code_review_graph", "serve"]\n'
            f'cwd = "{STALE_CWD}"\n',
            encoding="utf-8",
        )
        return
    if expect.fmt == "yaml":
        path.write_text(
            f"{expect.server_key}:\n"
            f"  {ENTRY_NAME}:\n"
            f"    command: {STALE_ABSOLUTE}\n"
            "    args:\n"
            "      - -m\n"
            "      - code_review_graph\n"
            "      - serve\n",
            encoding="utf-8",
        )
        return
    stale = {
        "command": STALE_ABSOLUTE,
        "args": ["-m", "code_review_graph", "serve"],
        "cwd": STALE_CWD,
    }
    if expect.fmt == "array":
        bucket: Any = [{"name": ENTRY_NAME, **stale}]
    else:
        bucket = {ENTRY_NAME: stale}
    path.write_text(
        json.dumps({expect.server_key: bucket}, indent=2) + "\n", encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Filesystem snapshots, used to prove uninstall really removed what it made.
# ---------------------------------------------------------------------------


def snapshot(sandbox: Sandbox) -> dict[Path, bytes]:
    """Map every relevant file under the sandbox to its bytes.

    Git's own object store churns on every command and says nothing about the
    installer, so ``.git`` is skipped apart from ``.git/hooks`` — where the
    installed pre-commit hook lives and therefore has to be accounted for.
    """
    files: dict[Path, bytes] = {}
    seen: set[Path] = set()
    for root in sandbox.roots():
        if root in seen or not root.exists():
            continue
        seen.add(root)
        for dirpath, dirnames, filenames in os.walk(root):
            current = Path(dirpath)
            if current.name == ".git" and current.parent == sandbox.repo:
                dirnames[:] = ["hooks"]
            for name in filenames:
                path = current / name
                try:
                    files[path] = path.read_bytes()
                except OSError:
                    continue
    return files


def leftovers(before: dict[Path, bytes], after_install: dict[Path, bytes],
              after_uninstall: dict[Path, bytes]) -> list[str]:
    """Files the installer created that uninstall failed to remove."""
    created = set(after_install) - set(before)
    return sorted(str(p) for p in created if p in after_uninstall)


def content_traces(before: dict[Path, bytes],
                   after_uninstall: dict[Path, bytes]) -> list[str]:
    """Pre-existing files that still mention us after uninstall."""
    stragglers = []
    for path, data in after_uninstall.items():
        original = before.get(path)
        if original is None:
            continue
        if ENTRY_NAME.encode() in data and ENTRY_NAME.encode() not in original:
            stragglers.append(str(path))
    return sorted(stragglers)


# ---------------------------------------------------------------------------
# Step 1: first install writes the exact config the platform reads.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", PLATFORM_KEYS)
def test_step1_install_writes_the_expected_config(sandbox: Sandbox, key: str) -> None:
    expect = EXPECTATIONS[key]
    config = sandbox.config_path(key)
    assert not config.exists(), f"{config} existed before install; sandbox is not clean"

    run_install(sandbox, key)

    checks = 0
    assert config.exists(), f"{key}: install wrote no config at {config}"
    checks += 1

    entry = read_entry(config, expect)
    assert entry is not None, f"{key}: no {ENTRY_NAME!r} entry under {expect.server_key!r}"
    checks += 1
    assert count_entries(config, expect) == 1
    checks += 1

    if expect.command_is_list:
        command = entry["command"]
        assert isinstance(command, list) and command, f"{key}: command must be a list"
        assert "serve" in command, f"{key}: command does not launch the server: {command}"
        assert str(sandbox.repo) in command, f"{key}: repo not pinned in {command}"
        checks += 3
    else:
        assert isinstance(entry["command"], str) and entry["command"]
        assert entry["args"][-1] == "serve", f"{key}: args do not end in serve: {entry['args']}"
        checks += 2

    if expect.entry_type is None:
        assert "type" not in entry, f"{key}: unexpected type field {entry.get('type')!r}"
    else:
        assert entry.get("type") == expect.entry_type, (
            f"{key}: type is {entry.get('type')!r}, expected {expect.entry_type!r}"
        )
    checks += 1

    if expect.expects_cwd:
        assert entry.get("cwd") == str(sandbox.repo), (
            f"{key}: cwd is {entry.get('cwd')!r}, expected {str(sandbox.repo)!r}"
        )
    else:
        assert "cwd" not in entry, f"{key}: cwd must not be pinned ({entry.get('cwd')!r})"
    checks += 1

    for name, value in expect.extra_fields.items():
        assert entry.get(name) == value, f"{key}: {name} is {entry.get(name)!r}"
        checks += 1

    for relative in EXPECTED_INSTRUCTION_FILES[key]:
        path = sandbox.repo / relative
        assert path.exists(), f"{key}: instruction file {relative} was not written"
        body = path.read_text(encoding="utf-8")
        assert skills._CLAUDE_MD_SECTION_MARKER in body, (
            f"{key}: {relative} has no managed instruction marker"
        )
        checks += 2

    for marker in EXPECTED_EXTRA_ARTIFACTS[key]:
        path = sandbox.resolve(marker)
        assert path.exists(), f"{key}: expected artifact missing: {marker}"
        checks += 1

    _record("step1_install", key, checks)


# ---------------------------------------------------------------------------
# Step 2: a second install changes nothing.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", PLATFORM_KEYS)
def test_step2_second_install_is_byte_identical(sandbox: Sandbox, key: str) -> None:
    expect = EXPECTATIONS[key]
    config = sandbox.config_path(key)

    run_install(sandbox, key)
    first = snapshot(sandbox)
    first_config = config.read_bytes()
    assert ENTRY_NAME.encode() in first_config, f"{key}: first install wrote no entry"

    run_install(sandbox, key)
    second = snapshot(sandbox)

    checks = 0
    assert config.read_bytes() == first_config, (
        f"{key}: {config} changed on the second install"
    )
    checks += 1
    assert count_entries(config, expect) == 1, (
        f"{key}: the second install duplicated the server entry in {config}"
    )
    checks += 1

    changed = sorted(
        str(path)
        for path, data in first.items()
        if not path.name.endswith(".bak") and second.get(path) != data
    )
    assert not changed, f"{key}: the second install rewrote {changed}"
    checks += len(first)

    added = sorted(
        str(path)
        for path in set(second) - set(first)
        if not path.name.endswith(".bak")
    )
    assert not added, f"{key}: the second install created {added}"
    checks += 1

    for relative in EXPECTED_INSTRUCTION_FILES[key]:
        body = (sandbox.repo / relative).read_text(encoding="utf-8")
        assert body.count(skills._CLAUDE_MD_SECTION_MARKER) == 1, (
            f"{key}: {relative} holds a duplicated instruction block"
        )
        checks += 1

    _record("step2_idempotent", key, checks)


# ---------------------------------------------------------------------------
# Step 3: installing over a user's own config keeps their settings.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", PLATFORM_KEYS)
def test_step3_preexisting_user_config_survives(sandbox: Sandbox, key: str) -> None:
    expect = EXPECTATIONS[key]
    config = sandbox.config_path(key)
    write_preexisting_config(config, expect)
    original_setting, original_server = read_foreign(config, expect)
    assert original_setting == FOREIGN_VALUE
    assert original_server is not None, "the fixture itself is broken"

    run_install(sandbox, key)

    checks = 0
    assert read_entry(config, expect) is not None, (
        f"{key}: install did not add its entry to the pre-existing {config}"
    )
    checks += 1

    setting, server = read_foreign(config, expect)
    assert setting == FOREIGN_VALUE, (
        f"{key}: the unrelated top-level setting was destroyed ({setting!r})"
    )
    checks += 1
    assert server is not None, f"{key}: the unrelated MCP server was removed from {config}"
    checks += 1
    assert server.get("command") == FOREIGN_ENTRY["command"], (
        f"{key}: the unrelated MCP server's command was rewritten ({server!r})"
    )
    assert list(server.get("args", [])) == FOREIGN_ENTRY["args"], (
        f"{key}: the unrelated MCP server's args were rewritten ({server!r})"
    )
    checks += 2

    _record("step3_preserves_user_config", key, checks)


# ---------------------------------------------------------------------------
# Step 4: uninstall removes every trace and leaves the rest alone.
# ---------------------------------------------------------------------------


CONTENT_LEFTOVER_XFAIL = {
    "qoder": (
        "uninstall._process_repo removes .qoder/skills/<name>/SKILL.md only for "
        "names found in the TARGET repo's own skills/ directory, while install "
        "copies them out of the installed package, so every Qoder skill file "
        "survives uninstall in a repo that has no skills/ directory of its own"
    ),
    "gemini-cli": (
        "install_gemini_cli_hooks copies .gemini/settings.json to "
        "settings.json.bak, and uninstall never removes the backup, so a full "
        "copy of the code-review-graph MCP registration stays on disk"
    ),
}

HUSK_XFAIL = (
    "uninstall strips the entry but leaves behind the config file the "
    "installer itself created, as an empty stub (for example "
    "'{\"mcpServers\": {}}'), so a repo that never had one before uninstall "
    "is left with committed litter"
)

GIT_HOOK_PLATFORMS = ("claude", "codex", "qoder")


@pytest.mark.parametrize("key", PLATFORM_KEYS)
def test_step4a_uninstall_removes_the_registration_and_keeps_user_settings(
    sandbox: Sandbox, key: str
) -> None:
    """Uninstall must unregister us and must not touch the user's own settings."""
    expect = EXPECTATIONS[key]
    config = sandbox.config_path(key)
    write_preexisting_config(config, expect)
    before = snapshot(sandbox)

    run_install(sandbox, key)
    assert read_entry(config, expect) is not None, f"{key}: nothing was installed to remove"
    assert set(snapshot(sandbox)) - set(before), f"{key}: install created no files at all"

    report = run_uninstall(sandbox)
    after_uninstall = snapshot(sandbox)

    checks = 0
    assert read_entry(config, expect) is None, (
        f"{key}: the MCP entry survived uninstall in {config}"
    )
    checks += 1
    assert count_entries(config, expect) == 0, (
        f"{key}: a code-review-graph registration is still readable in {config}"
    )
    checks += 1

    stale_content = content_traces(before, after_uninstall)
    assert not stale_content, (
        f"{key}: uninstall left code-review-graph text in pre-existing {stale_content}"
    )
    checks += 1

    setting, server = read_foreign(config, expect)
    assert setting == FOREIGN_VALUE, f"{key}: uninstall destroyed the unrelated setting"
    assert server is not None, f"{key}: uninstall removed the unrelated MCP server"
    assert server.get("command") == FOREIGN_ENTRY["command"], (
        f"{key}: uninstall rewrote the unrelated MCP server ({server!r})"
    )
    checks += 3

    assert not report.errors, f"{key}: uninstall reported errors {report.errors}"
    checks += 1

    _record("step4a_uninstall", key, checks)


@pytest.mark.parametrize(
    "key",
    [
        pytest.param(
            k,
            marks=(
                [pytest.mark.xfail(strict=True, reason=CONTENT_LEFTOVER_XFAIL[k])]
                if k in CONTENT_LEFTOVER_XFAIL
                else []
            ),
        )
        for k in PLATFORM_KEYS
    ],
)
def test_step4b_uninstall_leaves_no_file_still_holding_our_content(
    sandbox: Sandbox, key: str
) -> None:
    """No file the installer created may survive still mentioning us."""
    before = snapshot(sandbox)
    run_install(sandbox, key)
    after_install = snapshot(sandbox)
    created = set(after_install) - set(before)
    assert created, f"{key}: install created no files at all"

    run_uninstall(sandbox)
    after_uninstall = snapshot(sandbox)

    live = sorted(
        str(path)
        for path in created
        if path in after_uninstall and ENTRY_NAME.encode() in after_uninstall[path]
    )
    assert not live, f"{key}: uninstall left live code-review-graph content in {live}"

    _record("step4b_content_leftovers", key, len(created))


@pytest.mark.parametrize(
    "key",
    [pytest.param(k, marks=[pytest.mark.xfail(strict=True, reason=HUSK_XFAIL)])
     for k in PLATFORM_KEYS],
)
def test_step4c_uninstall_leaves_no_empty_config_husk(sandbox: Sandbox, key: str) -> None:
    """A config file the installer created should not outlive the uninstall."""
    before = snapshot(sandbox)
    run_install(sandbox, key)
    after_install = snapshot(sandbox)
    created = set(after_install) - set(before)
    assert created, f"{key}: install created no files at all"

    run_uninstall(sandbox)
    after_uninstall = snapshot(sandbox)

    husks = sorted(str(path) for path in created if path in after_uninstall)
    assert not husks, f"{key}: uninstall left {husks} behind"

    _record("step4c_husks", key, len(created))


@pytest.mark.parametrize("key", GIT_HOOK_PLATFORMS)
def test_step4d_uninstall_keeps_git_commit_working(sandbox: Sandbox, key: str) -> None:
    """Uninstall must not leave a pre-commit hook that breaks every commit.

    Guards the release blocker this suite recorded: ``_remove_git_hook`` used
    to stop dropping at the first line that strips to ``fi``, and the installed
    hook nests an if/elif/else inside its outer ``if command -v``. The inner
    ``fi`` ended the drop, the outer one was written back, and the hook became
    ``#!/bin/sh\\nfi`` -- a syntax error that failed every later ``git commit``.
    The block now carries explicit begin/end markers and is cut out whole.
    """
    hook = sandbox.repo / ".git" / "hooks" / "pre-commit"
    run_install(sandbox, key)
    assert hook.exists(), f"{key}: no pre-commit hook was installed to begin with"
    assert ENTRY_NAME in hook.read_text(encoding="utf-8"), (
        f"{key}: the installed hook does not mention us; the fixture is wrong"
    )

    run_uninstall(sandbox)

    if hook.exists():
        body = hook.read_text(encoding="utf-8")
        syntax = subprocess.run(
            ["sh", "-n", str(hook)], capture_output=True, text=True, check=False
        )
        assert syntax.returncode == 0, (
            f"{key}: uninstall left an unparseable pre-commit hook "
            f"({body!r}): {syntax.stderr.strip()}"
        )
    committed = subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "after uninstall"],
        cwd=sandbox.repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert committed.returncode == 0, (
        f"{key}: git commit fails after uninstall: {committed.stderr.strip()}"
    )

    _record("step4d_git_hook", key, 3)


@pytest.mark.parametrize("key", PLATFORM_KEYS)
def test_step4e_platform_scoped_unbind_covers_every_platform(
    sandbox: Sandbox, key: str
) -> None:
    """``uninstall --platform <key>`` has to know about every installed platform."""
    expect = EXPECTATIONS[key]
    config = sandbox.config_path(key)
    run_install(sandbox, key)
    assert read_entry(config, expect) is not None, f"{key}: nothing installed"

    report = run_uninstall(sandbox, platforms=[key])

    checks = 0
    assert read_entry(config, expect) is None, (
        f"{key}: the unbind sweep does not cover this platform; "
        f"{config} still holds the entry"
    )
    checks += 1
    touched = " ".join(report.edited_paths + report.removed_paths)
    assert str(config) in touched, (
        f"{key}: the unbind report never mentions {config}: {touched!r}"
    )
    checks += 1

    _record("step4e_unbind_sweep", key, checks)


# ---------------------------------------------------------------------------
# Step 5: reinstalling over an older release's entry replaces it.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", PLATFORM_KEYS)
def test_step5_stale_config_entry_is_replaced(sandbox: Sandbox, key: str) -> None:
    """An entry a previous release wrote is replaced, not treated as current.

    Guards GH #558: ``install_platform_configs`` (and the TOML and YAML merges)
    used to treat any existing ``code-review-graph`` entry as up to date and
    return early, so an older release's absolute interpreter path and dead
    ``cwd`` survived every reinstall.
    """
    expect = EXPECTATIONS[key]
    config = sandbox.config_path(key)
    write_stale_config(config, expect)
    planted = config.read_text(encoding="utf-8")
    assert STALE_ABSOLUTE in planted, "the stale fixture itself is broken"

    run_install(sandbox, key)

    checks = 0
    entry = read_entry(config, expect)
    assert entry is not None, f"{key}: reinstall dropped the entry entirely"
    checks += 1
    assert count_entries(config, expect) == 1, (
        f"{key}: reinstall left the stale entry beside the new one"
    )
    checks += 1
    assert STALE_ABSOLUTE not in config.read_text(encoding="utf-8"), (
        f"{key}: the older release's absolute interpreter path survives in {config}"
    )
    checks += 1
    # The default must not be STALE_CWD itself: OpenCode and Hermes pin no cwd
    # at all (step 1 asserts exactly that), and an absent cwd is the opposite
    # of a surviving stale one.
    assert entry.get("cwd", "") != STALE_CWD, (
        f"{key}: the older release's dead cwd survives in {config}"
    )
    checks += 1

    _record("step5_stale_config", key, checks)


# Platforms whose install writes a hooks file, mapped to the file, the event,
# the matcher an older release filed its group under, and the hook command that
# release wrote. Every shape below is a real one, read back out of this
# project's released tags: v2.2.0 wrote ``--quiet --skip-flows`` under matcher
# ``Edit|Write|Bash``, v2.3.3 wrote the Codex one-liner without the ``cat``
# prelude, and Gemini CLI's hook has always pointed at a ``crg-*.sh`` script
# (here under a checkout that no longer exists).
_OLD_SETTINGS_HOOK = "code-review-graph update --quiet --skip-flows"
_OLD_CODEX_HOOK = (
    "git rev-parse --git-dir >/dev/null 2>&1"
    " && code-review-graph update --skip-flows || true"
)
HOOK_PLATFORMS: dict[str, tuple[str, str, str, str]] = {
    "claude": (
        "repo:.claude/settings.json", "PostToolUse", "Edit|Write|Bash", _OLD_SETTINGS_HOOK,
    ),
    "qoder": (
        "repo:.qoder/settings.json", "PostToolUse", "Edit|Write|Bash", _OLD_SETTINGS_HOOK,
    ),
    "codebuddy": (
        "repo:.codebuddy/settings.json", "PostToolUse", "Edit|Write|Bash",
        _OLD_SETTINGS_HOOK,
    ),
    "codex": (
        "home:.codex/hooks.json", "PostToolUse", "Write|Edit|Bash", _OLD_CODEX_HOOK,
    ),
    "gemini-cli": (
        "repo:.gemini/settings.json",
        "AfterTool",
        "write_file|replace",
        f"bash {STALE_CWD}/.gemini/hooks/crg-update.sh",
    ),
}

def _is_crg_hook_command(command: str) -> bool:
    """Both spellings this project has ever put in a hook command."""
    return ENTRY_NAME in command or "crg-" in command


@pytest.mark.parametrize("key", sorted(HOOK_PLATFORMS))
def test_step5b_stale_hook_shape_is_replaced(sandbox: Sandbox, key: str) -> None:
    """Reinstalling replaces our hook rather than adding a second one.

    The hook merges used to compare whole entries (or exact command strings),
    so an older release's hook shape was kept and the current one appended
    beside it, and the repository then ran two code-review-graph hooks on the
    same event.
    """
    marker, event, stale_matcher, stale_command = HOOK_PLATFORMS[key]
    settings_path = sandbox.resolve(marker)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps(
            {
                "hooks": {
                    event: [
                        {
                            "matcher": stale_matcher,
                            "hooks": [
                                {"type": "command", "command": stale_command, "timeout": 30}
                            ],
                        }
                    ]
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    assert stale_command in settings_path.read_text(encoding="utf-8")

    run_install(sandbox, key)

    body = settings_path.read_text(encoding="utf-8")
    data = json.loads(body)
    groups = data["hooks"][event]
    commands = [
        hook.get("command", "") for group in groups for hook in group.get("hooks", [])
    ]
    crg_hooks = [command for command in commands if _is_crg_hook_command(command)]

    checks = 0
    assert crg_hooks, f"{key}: reinstall wrote no hook at all into {settings_path}"
    checks += 1
    # Compared exactly, not by substring: one released Codex command is a
    # prefix of the one that replaced it.
    assert stale_command not in commands, (
        f"{key}: the older release's hook command survives in {settings_path}"
    )
    checks += 1
    assert len(crg_hooks) == 1, (
        f"{key}: {len(crg_hooks)} code-review-graph hooks now run on {event} "
        f"in {settings_path}"
    )
    checks += 1

    _record("step5b_stale_hook", key, checks)


# ---------------------------------------------------------------------------
# The platform list itself must not drift.
# ---------------------------------------------------------------------------


def _readme_platform_keys(readme: Path) -> set[str]:
    """Parse the ``--platform`` list the README documents."""
    text = readme.read_text(encoding="utf-8")
    match = re.search(r"pass `--platform` with one of ([^:]+):", text)
    assert match, "README no longer documents a --platform list in the expected shape"
    found = set(re.findall(r"`([a-z0-9-]+)`", match.group(1)))
    assert found, "README --platform sentence parsed to nothing"
    return {"claude" if name == "claude-code" else name for name in found}


def _cli_platform_keys() -> set[str]:
    return {
        "claude" if name == "claude-code" else name
        for name in cli._PLATFORM_CHOICES
        if name != "all"
    }


def test_platform_list_does_not_drift() -> None:
    """The installer, the README and the uninstall sweep must agree."""
    repo_root = Path(__file__).resolve().parent.parent
    installer = set(skills.PLATFORMS)
    documented = _readme_platform_keys(repo_root / "README.md")
    cli_surface = _cli_platform_keys()
    expected = set(EXPECTATIONS)

    assert len(installer) == 16, (
        f"the installer now supports {len(installer)} platforms, not 16: "
        f"{sorted(installer)}"
    )

    missing_from_tests = sorted(installer - expected)
    extra_in_tests = sorted(expected - installer)
    assert not missing_from_tests and not extra_in_tests, (
        "this lifecycle suite does not match the installer: "
        f"untested={missing_from_tests} stale={extra_in_tests}"
    )

    undocumented = sorted(installer - documented)
    overdocumented = sorted(documented - installer)
    assert not undocumented and not overdocumented, (
        "README --platform list has drifted from skills.PLATFORMS: "
        f"installed-but-undocumented={undocumented} "
        f"documented-but-unsupported={overdocumented}"
    )

    cli_missing = sorted(installer - cli_surface)
    cli_extra = sorted(cli_surface - installer)
    assert not cli_missing and not cli_extra, (
        "cli._PLATFORM_CHOICES has drifted from skills.PLATFORMS: "
        f"unreachable-from-cli={cli_missing} accepted-but-unsupported={cli_extra}"
    )

    # The uninstall sweep iterates skills.PLATFORMS, so drift can only appear as
    # a format it cannot handle. Name any such platform explicitly.
    handled_formats = {"toml", "yaml", "object", "array"}
    unsweepable = sorted(
        name
        for name, spec in skills.PLATFORMS.items()
        if str(spec["format"]) not in handled_formats
    )
    assert not unsweepable, (
        "uninstall._process_platform_configs cannot clean these platforms: "
        f"{unsweepable}"
    )


def test_every_platform_has_a_reachable_config_path(sandbox: Sandbox) -> None:
    """Each platform's config must land inside the repo, the home, or HERMES_HOME.

    ``uninstall`` refuses to touch anything outside those boundaries, so a
    config written elsewhere would be installable but never removable.
    """
    offenders = []
    for name, spec in skills.PLATFORMS.items():
        path = Path(os.path.abspath(spec["config_path"](sandbox.repo)))
        scope = uninstall._scope_for_config(path, sandbox.repo, sandbox.home)
        if scope is None:
            offenders.append(f"{name} -> {path}")
    assert not offenders, f"configs outside every uninstall boundary: {offenders}"


def test_zz_canary_every_step_ran_against_every_platform() -> None:
    """Refuse to report success if the lifecycle steps quietly did nothing.

    Each step above records how many concrete comparisons it made. This runs
    last and fails when a step never executed, or executed without comparing
    anything, for any platform it claims to cover.
    """
    if not _LEDGER:
        pytest.skip("lifecycle steps were deselected; nothing to verify")

    expected_coverage = {
        "step1_install": set(PLATFORM_KEYS),
        "step2_idempotent": set(PLATFORM_KEYS),
        "step3_preserves_user_config": set(PLATFORM_KEYS),
        "step4a_uninstall": set(PLATFORM_KEYS),
        "step4e_unbind_sweep": set(PLATFORM_KEYS),
    }
    problems = []
    for step, platforms in expected_coverage.items():
        recorded = _LEDGER.get(step, {})
        if not recorded:
            problems.append(f"{step}: never ran")
            continue
        missing = sorted(platforms - set(recorded))
        if missing:
            problems.append(f"{step}: no result for {missing}")
        empty = sorted(name for name, count in recorded.items() if count <= 0)
        if empty:
            problems.append(f"{step}: compared nothing for {empty}")
    assert not problems, "; ".join(problems)

    total = sum(sum(counts.values()) for counts in _LEDGER.values())
    assert total > 300, f"only {total} comparisons were made across the lifecycle"


def test_zz_canary_git_and_yaml_are_actually_available() -> None:
    """The sandbox depends on real tools; a missing one must fail, not skip."""
    assert shutil.which("git"), "git is required to drive the real installer"

    import tomllib  # noqa: F401
    import yaml  # noqa: F401
