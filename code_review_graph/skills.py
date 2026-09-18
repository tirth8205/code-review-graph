"""Claude Code skills and hooks auto-install.

Generates Claude Code agent skill files, hooks configuration, and
CLAUDE.md integration for seamless code-review-graph usage.
Also supports multi-platform MCP server installation and
Cursor hooks / OpenCode plugin generation.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import shlex
import shutil
import stat
import subprocess
import sys
from importlib import resources
from pathlib import Path
from typing import Any, Sequence

from . import jsonc
from ._legacy_instructions import LEGACY_INSTRUCTION_SECTIONS

logger = logging.getLogger(__name__)


# --- Multi-platform MCP install ---


def _zed_settings_path() -> Path:
    """Return the Zed settings.json path for the current OS."""
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Zed" / "settings.json"
    return Path.home() / ".config" / "zed" / "settings.json"


def _copilot_vscode_detected() -> bool:
    """Return whether a GitHub Copilot extension is installed for VS Code."""
    home = Path.home()
    extension_dirs = [
        home / ".vscode" / "extensions",
        home / ".vscode-insiders" / "extensions",
    ]

    system = platform.system()
    if system == "Darwin":
        for applications_dir in (Path("/Applications"), home / "Applications"):
            for app_name in (
                "Visual Studio Code.app",
                "Visual Studio Code - Insiders.app",
            ):
                extension_dirs.append(
                    applications_dir
                    / app_name
                    / "Contents"
                    / "Resources"
                    / "app"
                    / "extensions"
                )
    elif system == "Windows":
        for env_name in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"):
            if install_root := os.environ.get(env_name):
                for app_name in ("Microsoft VS Code", "Microsoft VS Code Insiders"):
                    extension_dirs.append(
                        Path(install_root)
                        / app_name
                        / "resources"
                        / "app"
                        / "extensions"
                    )
    elif system == "Linux":
        extension_dirs.extend(
            [
                Path("/usr/share/code/resources/app/extensions"),
                Path("/usr/share/code-insiders/resources/app/extensions"),
                Path("/usr/lib/code/resources/app/extensions"),
                Path("/opt/visual-studio-code/resources/app/extensions"),
                Path("/snap/code/current/usr/share/code/resources/app/extensions"),
            ]
        )

    for command in ("code", "code-insiders"):
        if executable := shutil.which(command):
            try:
                parents = Path(executable).resolve().parents[:6]
            except OSError:
                continue
            for parent in parents:
                extension_dirs.extend(
                    [
                        parent / "extensions",
                        parent / "resources" / "app" / "extensions",
                        parent / "Resources" / "app" / "extensions",
                    ]
                )

    for extensions_dir in dict.fromkeys(extension_dirs):
        try:
            extension_paths = list(extensions_dir.iterdir())
        except OSError:
            continue
        for extension_path in extension_paths:
            name = extension_path.name.lower()
            if name.startswith("github.copilot-"):
                return True
            if "copilot" not in name:
                continue
            try:
                manifest = json.loads(
                    (extension_path / "package.json").read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError):
                continue
            if (
                str(manifest.get("publisher", "")).lower() == "github"
                and str(manifest.get("name", "")).lower()
                in {"copilot", "copilot-chat"}
            ):
                return True
    return False


def _hermes_home() -> Path:
    """Return the Hermes Agent home directory.

    Mirrors Hermes' own resolution order: the ``HERMES_HOME`` environment
    variable wins, otherwise the platform-native default (``%LOCALAPPDATA%\\hermes``
    on Windows, ``~/.hermes`` elsewhere).
    """
    override = os.environ.get("HERMES_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    if platform.system() == "Windows":
        local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local_appdata) if local_appdata else Path.home() / "AppData" / "Local"
        return base / "hermes"
    return Path.home() / ".hermes"


def _hermes_config_path() -> Path:
    """Return the Hermes Agent config file (``config.yaml``)."""
    return _hermes_home() / "config.yaml"


def _opencode_config_path(repo_root: Path) -> Path:
    """Return OpenCode's existing project config, preferring JSONC."""
    for name in ("opencode.jsonc", "opencode.json"):
        path = repo_root / name
        if path.exists():
            return path
    return repo_root / "opencode.jsonc"


PLATFORMS: dict[str, dict[str, Any]] = {
    "codex": {
        "name": "Codex",
        "config_path": lambda root: Path.home() / ".codex" / "config.toml",
        "key": "mcp_servers",
        "detect": lambda: (Path.home() / ".codex").exists(),
        "format": "toml",
        "needs_type": True,
    },
    "claude": {
        "name": "Claude Code",
        "config_path": lambda root: root / ".mcp.json",
        "key": "mcpServers",
        "detect": lambda: True,
        "format": "object",
        "needs_type": True,
    },
    "cursor": {
        "name": "Cursor",
        "config_path": lambda root: root / ".cursor" / "mcp.json",
        "key": "mcpServers",
        "detect": lambda: (Path.home() / ".cursor").exists(),
        "format": "object",
        "needs_type": True,
    },
    "windsurf": {
        "name": "Windsurf",
        "config_path": lambda root: Path.home() / ".codeium" / "windsurf" / "mcp_config.json",
        "key": "mcpServers",
        "detect": lambda: (Path.home() / ".codeium" / "windsurf").exists(),
        "format": "object",
        "needs_type": False,
    },
    "zed": {
        "name": "Zed",
        "config_path": lambda root: _zed_settings_path(),
        "key": "context_servers",
        "detect": lambda: _zed_settings_path().parent.exists(),
        "format": "object",
        "needs_type": False,
    },
    "continue": {
        "name": "Continue",
        "config_path": lambda root: Path.home() / ".continue" / "config.json",
        "key": "mcpServers",
        "detect": lambda: (Path.home() / ".continue").exists(),
        "format": "array",
        "needs_type": True,
    },
    "opencode": {
        "name": "OpenCode",
        "config_path": _opencode_config_path,
        "key": "mcp",
        "detect": lambda: True,
        "format": "object",
        "needs_type": False,
    },
    "antigravity": {
        "name": "Antigravity",
        "config_path": lambda root: Path.home() / ".gemini" / "antigravity" / "mcp_config.json",
        "key": "mcpServers",
        "detect": lambda: (Path.home() / ".gemini" / "antigravity").exists(),
        "format": "object",
        "needs_type": False,
    },
    "gemini-cli": {
        "name": "Gemini CLI",
        "config_path": lambda root: root / ".gemini" / "settings.json",
        "key": "mcpServers",
        "detect": lambda: bool(shutil.which("gemini")) or (Path.home() / ".gemini").exists(),
        "format": "object",
        "needs_type": False,
    },
    "qwen": {
        "name": "Qwen Code",
        "config_path": lambda root: Path.home() / ".qwen" / "settings.json",
        "key": "mcpServers",
        "detect": lambda: (Path.home() / ".qwen").exists(),
        "format": "object",
        "needs_type": True,
    },
    "kiro": {
        "name": "Kiro",
        "config_path": lambda root: root / ".kiro" / "settings" / "mcp.json",
        "key": "mcpServers",
        "detect": lambda: (Path.home() / ".kiro").exists(),
        "format": "object",
        "needs_type": True,
    },
    "qoder": {
        "name": "Qoder",
        "config_path": lambda root: root / ".qoder" / "mcp.json",
        "key": "mcpServers",
        "detect": lambda: True,
        "format": "object",
        "needs_type": True,
    },
    "copilot": {
        "name": "GitHub Copilot",
        "config_path": lambda root: root / ".vscode" / "mcp.json",
        "key": "servers",
        "detect": _copilot_vscode_detected,
        "format": "object",
        "needs_type": True,
    },
    "copilot-cli": {
        "name": "GitHub Copilot CLI",
        "config_path": lambda root: Path.home() / ".copilot" / "mcp-config.json",
        # Copilot CLI reads "mcpServers"; releases before #616 wrote
        # "servers", which the client silently ignores.
        "key": "mcpServers",
        "legacy_keys": ("servers",),
        "detect": lambda: (Path.home() / ".copilot").exists(),
        "format": "object",
        "needs_type": True,
        # Validated with the released Copilot CLI in #658.
        "server_type": "local",
        "entry_fields": {"tools": ["*"]},
    },
    "hermes": {
        "name": "Hermes Agent",
        "config_path": lambda root: _hermes_config_path(),
        "key": "mcp_servers",
        "detect": lambda: _hermes_home().exists(),
        "format": "yaml",
        "needs_type": False,
    },
    "codebuddy": {
        "name": "CodeBuddy Code",
        "config_path": lambda root: root / ".mcp.json",
        "key": "mcpServers",
        "detect": lambda: True,
        "format": "object",
        "needs_type": True,
    },
}


def _in_poetry_project() -> bool:
    """Return True when the running interpreter is a Poetry-managed virtualenv.

    Two signals are checked so that **both** ``poetry shell`` and ``poetry run``
    are detected:

    * ``POETRY_ACTIVE=1`` — set by ``poetry shell`` when the user activates the
      virtual environment interactively.
    * ``VIRTUAL_ENV`` containing ``"pypoetry"`` — set by **both** ``poetry shell``
      and ``poetry run`` because Poetry stores its virtualenvs under a path that
      includes the string ``pypoetry`` (e.g.
      ``~/.cache/pypoetry/virtualenvs/<name>`` on Linux/macOS or
      ``%LOCALAPPDATA%\\pypoetry\\Cache\\virtualenvs\\<name>`` on Windows).

    Checking only ``POETRY_ACTIVE`` would miss the ``poetry run`` case, which is
    the primary scenario described in issue #256.
    """
    if os.environ.get("POETRY_ACTIVE") == "1":
        return True
    virtual_env = os.environ.get("VIRTUAL_ENV", "")
    return bool(virtual_env) and "pypoetry" in virtual_env.lower()


def _in_uv_project() -> bool:
    """Return True if ``sys.executable`` lives inside a uv-managed project.

    A project is considered uv-managed when a ``uv.lock`` file exists in any
    ancestor directory of the running Python interpreter (stopping at the home
    directory to avoid false positives on system-wide installations).
    """
    exe = Path(sys.executable).resolve()
    home = Path.home()
    for parent in exe.parents:
        if (parent / "uv.lock").exists():
            return True
        # Stop searching once we reach the home directory or filesystem root
        if parent == home or parent == parent.parent:
            break
    return False


def _detect_serve_command() -> tuple[str, list[str]]:
    """Return ``(command, args)`` that correctly launches ``code-review-graph serve``.

    Detection priority
    ------------------
    1. **Poetry** – ``POETRY_ACTIVE=1`` OR ``VIRTUAL_ENV`` contains ``"pypoetry"``
       (covers both ``poetry shell`` and ``poetry run``) and ``poetry`` is on PATH
       → ``poetry run code-review-graph serve``
    2. **uv project** – ``UV_PROJECT_ENVIRONMENT`` is set, or a ``uv.lock``
       ancestor is found alongside ``sys.executable``, and ``uv`` is on PATH
       → ``uv run code-review-graph serve``
    3. **uvx** – ``uvx`` is available on PATH (existing behaviour, unchanged)
       → ``uvx code-review-graph serve``
    4. **Fallback** – use the absolute path of the running Python interpreter
       → ``sys.executable -m code_review_graph serve``

    The fallback is always safe: ``sys.executable`` is the exact interpreter
    that is currently running, so it resolves correctly inside any virtual
    environment, conda env, or system installation.
    """
    # 1. Poetry (poetry shell or poetry run)
    if _in_poetry_project():
        poetry = shutil.which("poetry")
        if poetry:
            return ("poetry", ["run", "code-review-graph", "serve"])

    # 2. uv managed project environment
    if os.environ.get("UV_PROJECT_ENVIRONMENT") or _in_uv_project():
        uv = shutil.which("uv")
        if uv:
            return ("uv", ["run", "code-review-graph", "serve"])

    # 3. uvx global tool runner (existing behaviour, unchanged)
    if shutil.which("uvx"):
        return ("uvx", ["code-review-graph", "serve"])

    # 4. Absolute-path fallback using the running interpreter
    return (sys.executable, ["-m", "code_review_graph", "serve"])


def _build_server_entry(
    plat: dict[str, Any], key: str = "", repo_root: "Path | None" = None,
) -> dict[str, Any]:
    """Build the MCP server entry for a platform."""
    command, args = _detect_serve_command()
    if key == "opencode":
        opencode_command = [command, *args]
        if repo_root is not None:
            opencode_command.extend(("--repo", str(repo_root)))
        return {"type": "local", "command": opencode_command}

    entry: dict[str, Any] = {"command": command, "args": args}
    if key == "hermes":
        # No repo is pinned here, deliberately. Hermes Agent is a long-lived
        # assistant that moves between projects, and its stdio schema has no
        # ``cwd``, so a baked-in ``--repo`` would silently answer every
        # question about whichever repo happened to be installed from. Every
        # tool takes an explicit ``repo_root``; without a default, omitting it
        # fails loudly instead of returning another project's graph.
        return entry
    # Include cwd so the MCP server can find the graph database
    if repo_root is not None:
        entry["cwd"] = str(repo_root)
    if plat["needs_type"]:
        entry["type"] = plat.get("server_type", "stdio")
    entry.update(plat.get("entry_fields", {}))
    return entry


# Fields an MCP entry written by this project has ever carried. An entry
# holding anything else (``url``, ``disabled``, ``autoApprove``, ...) was
# shaped by hand and is never rewritten.
_GENERATED_ENTRY_FIELDS = frozenset(
    {"name", "type", "command", "args", "cwd", "tools", "env"}
)

# ``type`` values this project writes. A user pointing the same name at an
# ``sse`` or ``http`` transport wrote that entry themselves.
_GENERATED_ENTRY_TYPES = frozenset({"stdio", "local"})

# Every argument vector a release of this project has written after the
# launcher, mapped to the launcher basenames allowed to carry it. Matching the
# whole vector is the boundary: a hand-tuned
# ``uv run --project <path> code-review-graph serve`` (the shape the
# troubleshooting guide asks people to write) is not in this table, so the
# ``--project`` flag someone added survives a reinstall.
_GENERATED_SERVE_ARGV: dict[tuple[str, ...], frozenset[str]] = {
    # ``uvx code-review-graph serve``
    ("code-review-graph", "serve"): frozenset({"uvx"}),
    # ``code-review-graph serve``
    ("serve",): frozenset({"code-review-graph"}),
    # ``poetry run ...`` / ``uv run ...``
    ("run", "code-review-graph", "serve"): frozenset({"poetry", "uv"}),
    # ``<interpreter> -m code_review_graph serve``
    ("-m", "code_review_graph", "serve"): frozenset({"python"}),
}

# ``python``, ``python3``, ``python3.12``, ``pythonw`` and nothing else.
_PYTHON_LAUNCHER_RE = re.compile(r"\Apythonw?[0-9]*(?:\.[0-9]+)*\Z")


def _launcher_basename(token: str) -> str:
    """Return the bare program name of a command token, without ``.exe``."""
    name = token.replace("\\", "/").rsplit("/", 1)[-1]
    if name.lower().endswith(".exe"):
        name = name[: -len(".exe")]
    return name


def _entry_command_tokens(entry: dict[str, Any]) -> list[str] | None:
    """Flatten an MCP entry's command and args into one token list."""
    command = entry.get("command")
    if isinstance(command, str):
        tokens = [command]
    elif isinstance(command, list) and all(isinstance(item, str) for item in command):
        tokens = list(command)
    else:
        return None
    args = entry.get("args", [])
    if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
        return None
    return tokens + args


def _is_generated_server_entry(entry: Any) -> bool:
    """Return True when ``entry`` is an MCP registration this project wrote.

    Recognition is by the exact shapes this project emits, not by equality with
    the entry the running version would write: an older release pinned an
    absolute interpreter path and a checkout that no longer exists, and exactly
    that entry has to be replaced rather than kept. Everything else under our
    name -- a field this project never writes, a transport it never uses, a
    command line someone tuned by hand -- counts as the user's own and is left
    alone. Mentioning ``code-review-graph`` somewhere on the command line is
    not enough; the whole argument vector has to be one this project wrote.
    """
    if not isinstance(entry, dict):
        return False
    if not set(entry) <= _GENERATED_ENTRY_FIELDS:
        return False
    if "type" in entry and entry["type"] not in _GENERATED_ENTRY_TYPES:
        return False
    if "tools" in entry and entry["tools"] != ["*"]:
        return False
    if "env" in entry and entry["env"] not in ([], {}):
        # Releases before #616 wrote ``env: []`` for OpenCode; anything else in
        # ``env`` is a value the user put there.
        return False
    tokens = _entry_command_tokens(entry)
    if not tokens:
        return False
    launcher, rest = tokens[0], tuple(tokens[1:])
    # The only tail any release has appended is the repo pin (OpenCode folds
    # the whole command line into ``command``).
    if len(rest) >= 2 and rest[-2] == "--repo":
        rest = rest[:-2]
    launchers = _GENERATED_SERVE_ARGV.get(rest)
    if launchers is None:
        return False
    name = _launcher_basename(launcher)
    if "python" in launchers:
        return bool(_PYTHON_LAUNCHER_RE.match(name))
    return name in launchers


def _report_user_owned_entry(config_path: Path, label: str = "") -> None:
    """Say that a hand-written entry under our name was deliberately kept."""
    prefix = f"  {label}: " if label else "  "
    print(
        f"{prefix}{config_path} holds a hand-written 'code-review-graph' entry "
        f"— leaving it as it is."
    )


def _warn_legacy_opencode_config(repo_root: Path) -> None:
    """Warn without modifying the obsolete Cursor-shaped OpenCode config."""
    legacy = repo_root / ".opencode.json"
    if not legacy.exists():
        return
    try:
        parsed = json.loads(
            _strip_jsonc(legacy.read_text(encoding="utf-8", errors="replace"))
        )
    except (json.JSONDecodeError, OSError):
        return
    if not isinstance(parsed, dict):
        return
    servers = parsed.get("mcpServers")
    if isinstance(servers, dict) and "code-review-graph" in servers:
        print(
            f"  OpenCode: legacy config found at {legacy}; leaving it unchanged. "
            "OpenCode now reads opencode.json or opencode.jsonc with a top-level "
            "'mcp' setting."
        )


def _load_tomllib() -> Any:
    """Return the TOML reader for this interpreter (``tomli`` before 3.11)."""
    import importlib

    return importlib.import_module("tomllib" if sys.version_info >= (3, 11) else "tomli")


def _format_toml_value(value: Any) -> str:
    """Format a primitive Python value as TOML."""
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return "[" + ", ".join(_format_toml_value(item) for item in value) + "]"
    raise TypeError(f"Unsupported TOML value: {type(value)!r}")


def _toml_header_matches(line: str, table_path: tuple[str, ...]) -> bool:
    """Return whether ``line`` opens the TOML table named by ``table_path``."""
    stripped = line.strip()
    if not stripped.startswith("[") or not stripped.endswith("]"):
        return False
    if stripped.startswith("[["):
        return False  # array of tables: never what this project writes
    parts = [part.strip().strip('"').strip("'") for part in stripped[1:-1].split(".")]
    return tuple(parts) == table_path


def _toml_table_span(lines: list[str], table_path: tuple[str, ...]) -> tuple[int, int] | None:
    """Return the ``(start, end)`` line span of a TOML table, or None.

    ``end`` stops just past the table's last key line. The trailing run of
    comment and blank lines before the next table header is deliberately left
    outside the span: in TOML a comment sitting above ``[next.table]`` belongs
    to that table, and swallowing it into the replaced region silently deletes
    something the user wrote (``# DO NOT REMOVE`` above an unrelated server,
    or a free-standing note at the end of the file). This project never writes
    a comment inside its own table, so nothing of ours is stranded by stopping
    early.
    """
    start = None
    for index, line in enumerate(lines):
        if _toml_header_matches(line, table_path):
            start = index
            break
    if start is None:
        return None
    end = start + 1
    last_content = end
    while end < len(lines):
        stripped = lines[end].strip()
        if stripped.startswith("["):
            break
        if stripped and not stripped.startswith("#"):
            last_content = end + 1
        end += 1
    return start, last_content


def _merge_toml_mcp_server(
    config_path: Path,
    server_name: str,
    server_entry: dict[str, Any],
    dry_run: bool = False,
) -> bool | None:
    """Write a Codex MCP server table without clobbering the rest of the file.

    An entry a previous release wrote is replaced in place rather than treated
    as up to date, so a stale absolute interpreter path or a dead ``cwd`` does
    not survive a reinstall. A hand-written entry is never rewritten.

    Returns True when the file was (or would be) modified, False when no edit
    is needed, and None when the edit was refused -- because it would lose
    data, or because the table belongs to the user and only they can change it.
    """
    table_path = ("mcp_servers", server_name)
    section_lines = [f"[mcp_servers.{server_name}]"]
    for key, value in server_entry.items():
        section_lines.append(f"{key} = {_format_toml_value(value)}")
    section = "\n".join(section_lines) + "\n"

    existing = ""
    if config_path.exists():
        existing = config_path.read_text(encoding="utf-8")

    lines = existing.splitlines(keepends=True)
    span = _toml_table_span(lines, table_path)
    if existing:
        tomllib = _load_tomllib()
        current: Any = None
        parsed_ok = False
        try:
            parsed = tomllib.loads(existing)
        except tomllib.TOMLDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            parsed_ok = True
            table = parsed.get("mcp_servers")
            if isinstance(table, dict):
                current = table.get(server_name)
        if not parsed_ok:
            # Unparseable TOML: only the header tells us anything, so do the
            # conservative thing and never edit a table we cannot read.
            if span is not None:
                return False
        elif current is not None:
            if current == server_entry:
                return False
            if not _is_generated_server_entry(current):
                # Not ours to rewrite, and not something to claim as installed.
                _report_user_owned_entry(config_path)
                return None
            if span is None:
                print(
                    f"  {config_path}: the existing [mcp_servers.{server_name}] table "
                    f"could not be located for replacement — left unchanged."
                )
                return None
            if dry_run:
                return True
            start, end = span
            config_path.write_text(
                "".join(lines[:start]) + section + "".join(lines[end:]), encoding="utf-8"
            )
            return True

    if dry_run:
        return True

    config_path.parent.mkdir(parents=True, exist_ok=True)
    prefix = ""
    if existing:
        prefix = existing if existing.endswith("\n") else existing + "\n"
        if not prefix.endswith("\n\n"):
            prefix += "\n"
    config_path.write_text(prefix + section, encoding="utf-8")
    return True


def _yaml_section_bounds(lines: list[str], key: str) -> tuple[int, int] | None:
    """Return ``(header_index, end_index)`` for a top-level block mapping ``key``.

    ``end_index`` is the index just past the last line belonging to the block
    (blank lines and comments trailing the block are excluded so an insertion
    lands inside it). Returns ``None`` when the key is absent or is not a
    block mapping (e.g. ``key: {}`` written in flow style).
    """
    header = None
    for index, line in enumerate(lines):
        if line.startswith((" ", "\t", "#")) or not line.strip():
            continue
        name, sep, value = line.partition(":")
        if sep and name.strip() == key:
            if value.strip() and not value.strip().startswith("#"):
                return None  # inline/flow value — refuse to edit
            header = index
            break
    if header is None:
        return None
    end = header + 1
    last_content = header + 1
    while end < len(lines):
        line = lines[end]
        if line.strip() and not line.startswith((" ", "\t")):
            break
        if line.strip() and not line.lstrip().startswith("#"):
            last_content = end + 1
        end += 1
    return header, last_content


def _yaml_block_indent(lines: list[str], start: int, end: int) -> int:
    """Return the child indentation used inside a block, defaulting to 2."""
    for line in lines[start:end]:
        stripped = line.lstrip()
        if stripped and not stripped.startswith("#"):
            return len(line) - len(stripped)
    return 2


def _yaml_child_bounds(
    lines: list[str], start: int, end: int, name: str
) -> tuple[int, int] | None:
    """Return the line span of the child mapping ``name`` inside a block.

    Only a child written at the block's own indentation is matched, and the
    span runs to the last line indented deeper than it, so replacing the span
    cannot swallow a sibling entry.
    """
    indent = _yaml_block_indent(lines, start, end)
    header = None
    for index in range(start, min(end, len(lines))):
        line = lines[index]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if len(line) - len(line.lstrip()) != indent:
            continue
        key, sep, _value = stripped.partition(":")
        if sep and key.strip().strip('"').strip("'") == name:
            header = index
            break
    if header is None:
        return None
    cursor = header + 1
    last_content = cursor
    while cursor < min(end, len(lines)):
        line = lines[cursor]
        if line.strip() and (len(line) - len(line.lstrip())) <= indent:
            break
        if line.strip() and not line.lstrip().startswith("#"):
            last_content = cursor + 1
        cursor += 1
    return header, last_content


def _merge_yaml_mcp_server(
    config_path: Path,
    server_key: str,
    server_name: str,
    server_entry: dict[str, Any],
    dry_run: bool = False,
) -> bool | None:
    """Add an MCP server to a YAML config without reformatting the rest.

    The file is edited as text rather than round-tripped through a YAML
    dumper: Hermes' ``config.yaml`` is hand-edited and full of comments,
    anchors, and deliberate ordering that a dump would destroy. The parsed
    document is only used to decide *whether* an edit is needed, and the
    result is re-parsed before writing so a malformed edit is never saved.

    Returns True when the file was (or would be) modified, False when the
    entry is already present, and None when the edit was refused -- to avoid
    data loss, or because the entry belongs to the user (the reason is
    printed).
    """
    import yaml  # type: ignore[import-untyped]

    raw = ""
    replacing = False
    if config_path.exists():
        raw = config_path.read_text(encoding="utf-8", errors="replace")
        try:
            parsed = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            print(
                f"  {config_path} contains unparseable YAML ({exc.__class__.__name__})"
                f" — skipping to avoid data loss. Please add the MCP config manually."
            )
            return None
        if parsed is not None and not isinstance(parsed, dict):
            print(
                f"  {config_path} is valid YAML but not a top-level mapping "
                f"({type(parsed).__name__}) — skipping to avoid data loss. "
                f"Please add the MCP config manually."
            )
            return None
        existing_servers = (parsed or {}).get(server_key)
        if existing_servers is not None and not isinstance(existing_servers, dict):
            print(
                f"  {config_path} setting {server_key!r} is "
                f"{type(existing_servers).__name__}; expected a mapping — "
                f"skipping to avoid data loss."
            )
            return None
        if isinstance(existing_servers, dict) and server_name in existing_servers:
            current = existing_servers[server_name]
            if current == server_entry:
                return False
            if not _is_generated_server_entry(current):
                # Not ours to rewrite, and not something to claim as installed.
                _report_user_owned_entry(config_path)
                return None
            replacing = True

    lines = raw.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"

    bounds = _yaml_section_bounds(lines, server_key)
    if bounds is None and raw and server_key in (yaml.safe_load(raw) or {}):
        # The key exists but is not an editable block mapping (flow style).
        print(
            f"  {config_path} setting {server_key!r} is not a block mapping — "
            f"skipping to avoid data loss. Please add the MCP config manually."
        )
        return None

    body = yaml.safe_dump(
        {server_name: server_entry},
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
    )
    if bounds is None:
        indent = 2
        prefix = "" if not lines or lines[-1].strip() == "" else "\n"
        block = prefix + f"{server_key}:\n" + _indent_block(body, indent)
        new_lines = lines + [block]
    else:
        header, insert_at = bounds
        indent = _yaml_block_indent(lines, header + 1, insert_at)
        child = (
            _yaml_child_bounds(lines, header + 1, insert_at, server_name)
            if replacing
            else None
        )
        if replacing and child is None:
            print(
                f"  {config_path}: the existing {server_name!r} entry could not be "
                f"located for replacement — left unchanged."
            )
            return None
        if child is None:
            new_lines = lines[:insert_at] + [_indent_block(body, indent)] + lines[insert_at:]
        else:
            child_start, child_end = child
            new_lines = (
                lines[:child_start] + [_indent_block(body, indent)] + lines[child_end:]
            )

    rewritten = "".join(new_lines)
    try:
        reparsed = yaml.safe_load(rewritten)
    except yaml.YAMLError as exc:  # pragma: no cover - defensive validation
        print(
            f"  {config_path}: safe YAML edit failed "
            f"({exc.__class__.__name__}) — left unchanged."
        )
        return None
    written = (reparsed.get(server_key) or {}) if isinstance(reparsed, dict) else {}
    if not isinstance(written, dict) or written.get(server_name) != server_entry:
        print(f"  {config_path}: safe YAML edit did not take effect — left unchanged.")
        return None

    if not dry_run:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(rewritten, encoding="utf-8")
    return True


def _indent_block(text: str, indent: int) -> str:
    """Indent every non-empty line of ``text`` by ``indent`` spaces."""
    pad = " " * indent
    return "".join(
        f"{pad}{line}" if line.strip() else line
        for line in text.splitlines(keepends=True)
    )


def _strip_jsonc(text: str) -> str:
    """Strip JSONC comments and trailing commas without corrupting string values.

    Editors like Zed accept non-standard JSON (``//`` and ``/* */`` comments,
    trailing commas). To merge such a config we must reduce it to strict JSON
    first. A naive regex pass cannot tell structure from data: it would delete a
    comma inside ``"foo, bar"`` or truncate a ``"https://..."`` URL at the
    ``//``. This walks the text character by character, tracking whether we are
    inside a double-quoted string (respecting ``\\`` escapes), and only removes
    comments and trailing commas that appear in structural position. Content
    inside string values is preserved verbatim. (GH #553)
    """

    def _skip_comment(s: str, idx: int) -> int | None:
        """If a comment starts at ``idx``, return the index just past it."""
        if s[idx] != "/" or idx + 1 >= len(s):
            return None
        nxt = s[idx + 1]
        if nxt == "/":
            idx += 2
            while idx < len(s) and s[idx] != "\n":
                idx += 1
            return idx
        if nxt == "*":
            idx += 2
            while idx + 1 < len(s) and not (s[idx] == "*" and s[idx + 1] == "/"):
                idx += 1
            return idx + 2  # consume the closing */ (or run off the end if unterminated)
        return None

    out: list[str] = []
    i = 0
    n = len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])  # escaped char is data, never a delimiter
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        # Outside a string.
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        past = _skip_comment(text, i)
        if past is not None:
            i = past
            continue
        if ch == ",":
            # Trailing comma if the next significant char (skipping whitespace
            # and comments) closes an object or array.
            j = i + 1
            while j < n:
                if text[j] in " \t\r\n":
                    j += 1
                    continue
                past = _skip_comment(text, j)
                if past is not None:
                    j = past
                    continue
                break
            if j < n and text[j] in "}]":
                i += 1  # drop the trailing comma
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _splice_commented_config(
    raw: str,
    server_key: str,
    server_entry: dict[str, Any] | None,
    removals: list[tuple[str, ...]],
    *,
    array_format: bool,
    document: dict[str, Any],
) -> str | None:
    """Apply this install's edits to a JSONC file without losing its comments.

    Parsing a commented config, mutating the dict and dumping it back deletes
    every comment and every hand-made formatting choice in the file, which is a
    silent loss of something the user wrote. Only the members this install
    actually touches are spliced into the original text; everything else is
    copied through byte for byte.

    Returns None when the edit cannot be expressed as a splice, so the caller
    can leave the file alone instead of flattening it. Array-shaped configs
    (Continue) are refused for the same reason: this project has no positional
    splice for them.
    """
    try:
        if not jsonc.tokenize(raw):
            # Comments but no JSON document yet (#344). A JSONC file may open
            # with comments, so keep them above the config we write.
            body = json.dumps(document, indent=2, ensure_ascii=False)
            return raw.rstrip("\n") + "\n" + body + "\n"
        if array_format:
            return None
        text = jsonc.remove_paths(raw, removals) if removals else raw
        if server_entry is None:
            return text
        try:
            return jsonc.set_member(text, (server_key, "code-review-graph"), server_entry)
        except KeyError:
            return jsonc.set_member(text, (server_key,), {"code-review-graph": server_entry})
    except (ValueError, KeyError, IndexError, RecursionError):
        return None


def install_platform_configs(
    repo_root: Path,
    target: str = "all",
    dry_run: bool = False,
) -> list[str]:
    """Install MCP config for one or all detected platforms.

    Args:
        repo_root: Project root directory.
        target: Platform key or "all".
        dry_run: If True, print what would be done without writing.

    Returns:
        List of platform names that were configured.
    """
    shared_aliases: dict[str, tuple[str, ...]] = {}
    if target == "all":
        platforms_to_install = {k: v for k, v in PLATFORMS.items() if v["detect"]()}
        # Workspace-level Kiro detection
        if "kiro" not in platforms_to_install and (repo_root / ".kiro").is_dir():
            platforms_to_install["kiro"] = PLATFORMS["kiro"]

        # Claude Code and CodeBuddy intentionally share the official project
        # .mcp.json/mcpServers contract. Process that exact pair once, but do
        # not deduplicate arbitrary clients merely because a config path
        # happens to match: their keys or entry schemas may differ.
        claude = platforms_to_install.get("claude")
        codebuddy = platforms_to_install.get("codebuddy")
        if claude is not None and codebuddy is not None:
            same_contract = (
                claude["config_path"](repo_root) == codebuddy["config_path"](repo_root)
                and all(
                    claude[field] == codebuddy[field]
                    for field in ("key", "format", "needs_type")
                )
            )
            if same_contract:
                shared_aliases["claude"] = ("codebuddy",)
                del platforms_to_install["codebuddy"]
    else:
        if target not in PLATFORMS:
            logger.error("Unknown platform: %s", target)
            return []
        platforms_to_install = {target: PLATFORMS[target]}

    configured: list[str] = []

    def _record_configured(key: str, plat: dict[str, Any]) -> None:
        configured.append(plat["name"])
        configured.extend(PLATFORMS[alias]["name"] for alias in shared_aliases.get(key, ()))

    for key, plat in platforms_to_install.items():
        if key == "opencode":
            _warn_legacy_opencode_config(repo_root)
        config_path: Path = plat["config_path"](repo_root)
        server_key = plat["key"]
        server_entry = _build_server_entry(plat, key=key, repo_root=repo_root)

        if plat["format"] in ("toml", "yaml"):
            if plat["format"] == "toml":
                changed = _merge_toml_mcp_server(
                    config_path,
                    "code-review-graph",
                    server_entry,
                    dry_run=dry_run,
                )
            else:
                changed = _merge_yaml_mcp_server(
                    config_path,
                    server_key,
                    "code-review-graph",
                    server_entry,
                    dry_run=dry_run,
                )
            if changed is None:
                # Refused to avoid data loss; the reason was already printed.
                continue
            if not changed:
                print(f"  {plat['name']}: already configured in {config_path}")
                _record_configured(key, plat)
                continue
            if dry_run:
                print(f"  [dry-run] {plat['name']}: would write {config_path}")
            else:
                print(f"  {plat['name']}: configured {config_path}")
            _record_configured(key, plat)
            continue

        # Read existing config
        existing: dict[str, Any] = {}
        raw = ""
        if config_path.exists():
            raw = config_path.read_text(encoding="utf-8", errors="replace")
            # Strip comments and trailing commas (JSONC compat for editors like
            # Zed that allow non-standard JSON) without corrupting string values.
            stripped = _strip_jsonc(raw)
            if not stripped.strip():
                # An empty (or comment-only) file is a valid empty config,
                # not a parse failure — proceed and write a fresh one rather
                # than mis-flagging it "unparseable" and skipping. See #344.
                existing = {}
            else:
                try:
                    parsed = json.loads(stripped)
                except (json.JSONDecodeError, OSError):
                    print(f"  {plat['name']}: {config_path} contains "
                          f"unparseable JSON — skipping to avoid data loss. "
                          f"Please add the MCP config manually.")
                    continue
                if not isinstance(parsed, dict):
                    # Valid JSON, but the top level is a list/scalar rather
                    # than an object. Writing our server object would clobber
                    # the user's data, and the ``.get()`` calls below would
                    # raise AttributeError. Refuse and skip. See #344.
                    print(f"  {plat['name']}: {config_path} is valid JSON but "
                          f"not a top-level object "
                          f"({type(parsed).__name__}) — skipping to avoid "
                          f"data loss. Please add the MCP config manually.")
                    continue
                existing = parsed

        expected_container = list if plat["format"] == "array" else dict
        if server_key in existing and not isinstance(
            existing[server_key], expected_container
        ):
            expected_name = "array" if expected_container is list else "object"
            actual_name = type(existing[server_key]).__name__
            print(
                f"  {plat['name']}: {config_path} setting {server_key!r} "
                f"is {actual_name}; expected a JSON {expected_name} — "
                f"skipping to avoid data loss. Please repair that setting "
                f"or add the MCP config manually."
            )
            continue

        # Paths this write removes, recorded so a commented file can be
        # spliced instead of re-serialised.
        removals: list[tuple[str, ...]] = []
        wrote_entry = False

        if plat["format"] == "array":
            arr = existing.get(server_key, [])
            arr_entry = {"name": "code-review-graph", **server_entry}
            ours = [
                index
                for index, item in enumerate(arr)
                if isinstance(item, dict) and item.get("name") == "code-review-graph"
            ]
            if ours:
                current = arr[ours[0]]
                if not _is_generated_server_entry(current):
                    # Not ours to rewrite, so not ours to claim as installed.
                    _report_user_owned_entry(config_path, plat["name"])
                    continue
                if len(ours) == 1 and current == arr_entry:
                    print(f"  {plat['name']}: already configured in {config_path}")
                    _record_configured(key, plat)
                    continue
                # Replace the first registration and drop any duplicates an
                # older release stacked up beside it.
                duplicates = set(ours[1:])
                arr = [
                    arr_entry if index == ours[0] else item
                    for index, item in enumerate(arr)
                    if index not in duplicates
                ]
            else:
                arr = [*arr, arr_entry]
            existing[server_key] = arr
            wrote_entry = True
        else:
            # Remove entries written under keys the client never read, then
            # install the validated entry under the current key.
            migrated = False
            for legacy_key in plat.get("legacy_keys", ()):
                legacy = existing.get(legacy_key)
                if (
                    isinstance(legacy, dict)
                    and "code-review-graph" in legacy
                ):
                    del legacy["code-review-graph"]
                    removals.append((legacy_key, "code-review-graph"))
                    if not legacy:
                        del existing[legacy_key]
                        removals[-1] = (legacy_key,)
                    migrated = True
            servers = existing.get(server_key, {})
            current = servers.get("code-review-graph")
            user_owned = current is not None and not _is_generated_server_entry(current)
            if user_owned:
                # Someone wrote this entry themselves; it is not ours to rewrite.
                _report_user_owned_entry(config_path, plat["name"])
                if not migrated:
                    continue
            elif current == server_entry and not migrated:
                print(f"  {plat['name']}: already configured in {config_path}")
                _record_configured(key, plat)
                continue
            else:
                servers["code-review-graph"] = server_entry
                existing[server_key] = servers
                wrote_entry = True

        # Re-serialising a commented config deletes every comment in it, so a
        # commented file is spliced instead. Computed before the dry-run branch
        # so a dry run reports the same refusal a real run would.
        spliced: str | None = None
        if jsonc.has_comments(raw):
            spliced = _splice_commented_config(
                raw,
                server_key,
                server_entry if wrote_entry else None,
                removals,
                array_format=plat["format"] == "array",
                document=existing,
            )
            if spliced is None:
                print(
                    f"  {plat['name']}: {config_path} keeps comments that this "
                    f"installer cannot preserve through an edit — left "
                    f"unchanged. Please add the MCP config manually."
                )
                continue

        if dry_run:
            print(f"  [dry-run] {plat['name']}: would write {config_path}")
        else:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(
                spliced
                if spliced is not None
                else json.dumps(existing, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            print(f"  {plat['name']}: configured {config_path}")

        if wrote_entry:
            _record_configured(key, plat)

    return configured


# --- Skill file contents ---

_SKILLS: dict[str, dict[str, str]] = {
    "explore-codebase.md": {
        "name": "explore-codebase",
        "description": "Navigate and understand codebase structure using the knowledge graph",
        "body": (
            "## Explore Codebase\n\n"
            "Use the code-review-graph MCP tools to find your way around the codebase.\n\n"
            "### Steps\n\n"
            "1. Call `get_architecture_overview_tool` for the community structure. Call "
            "`list_communities_tool`, then `get_community_tool`, only for the modules you need.\n"
            "2. Call `semantic_search_nodes_tool` to find a function or class by name or keyword.\n"
            "3. Call `query_graph_tool` with `callers_of`, `callees_of` or `imports_of` to trace "
            "relationships. `children_of` on a file lists its functions and classes.\n"
            "4. Call `list_flows_tool`, then `get_flow_tool` for one flow, to follow an execution "
            "path.\n"
            "5. Call `find_large_functions_tool` to find oversized functions.\n"
            "6. Call `list_graph_stats_tool` only when you need node, edge and language counts.\n\n"
            "## Token Efficiency Rules\n"
            '- Call `get_minimal_context_tool(task="<your task>")` before any other graph tool.\n'
            '- Pass `detail_level="minimal"` wherever a tool accepts it. Use "standard" only when '
            "minimal is not enough.\n"
            "- Prefer a targeted `query_graph_tool` call over a broad listing call.\n"
            "- Budget: about five tool calls and 800 tokens of graph output per task.\n"
            "- Read the implementation and its tests before changing code. The graph narrows "
            "scope; it does not replace the source."
        ),
    },
    "review-changes.md": {
        "name": "review-changes",
        "description": "Perform a structured code review using change detection and impact",
        "body": (
            "## Review Changes\n\n"
            "Review a change set with risk scores and blast radius from the knowledge graph.\n\n"
            "### Steps\n\n"
            "1. Call `detect_changes_tool` for risk-scored changed functions, test gaps and "
            "affected flows.\n"
            "2. Call `get_affected_flows_tool` only when you need the steps of an affected flow.\n"
            '3. For each high-risk function, call `query_graph_tool` with `pattern="tests_for"` '
            "to check test coverage.\n"
            "4. Call `get_impact_radius_tool` when the blast radius is not clear from step 1.\n"
            "5. Suggest specific test cases for untested changes.\n\n"
            "### Output Format\n\n"
            "Group findings by risk level (high, medium, low). For each finding give what changed "
            "and why it matters, its test coverage, and the suggested fix. End with a merge "
            "recommendation.\n\n"
            "## Token Efficiency Rules\n"
            '- Call `get_minimal_context_tool(task="<your task>")` before any other graph tool.\n'
            '- Pass `detail_level="minimal"` wherever a tool accepts it. Use "standard" only when '
            "minimal is not enough.\n"
            "- Prefer a targeted `query_graph_tool` call over a broad listing call.\n"
            "- Budget: about five tool calls and 800 tokens of graph output per task.\n"
            "- Read the implementation and its tests before changing code. The graph narrows "
            "scope; it does not replace the source."
        ),
    },
    "debug-issue.md": {
        "name": "debug-issue",
        "description": "Systematically debug issues using graph-powered code navigation",
        "body": (
            "## Debug Issue\n\n"
            "Trace a bug through the knowledge graph before reading source.\n\n"
            "### Steps\n\n"
            "1. Call `semantic_search_nodes_tool` to find code related to the issue.\n"
            "2. Call `query_graph_tool` with `callers_of` and `callees_of` to trace the call "
            "chain in both directions.\n"
            "3. Call `get_flow_tool` for the execution path that reaches the suspect code. Its "
            "entry point is where the bug is triggered.\n"
            "4. Call `detect_changes_tool` to check whether a recent change caused the issue.\n"
            "5. Call `get_impact_radius_tool` on the suspect files to see what a fix would "
            "affect.\n\n"
            "## Token Efficiency Rules\n"
            '- Call `get_minimal_context_tool(task="<your task>")` before any other graph tool.\n'
            '- Pass `detail_level="minimal"` wherever a tool accepts it. Use "standard" only when '
            "minimal is not enough.\n"
            "- Prefer a targeted `query_graph_tool` call over a broad listing call.\n"
            "- Budget: about five tool calls and 800 tokens of graph output per task.\n"
            "- Read the implementation and its tests before changing code. The graph narrows "
            "scope; it does not replace the source."
        ),
    },
    "refactor-safely.md": {
        "name": "refactor-safely",
        "description": "Plan and execute safe refactoring using dependency analysis",
        "body": (
            "## Refactor Safely\n\n"
            "Plan a refactor from the dependency graph and apply renames from a preview.\n\n"
            "### Steps\n\n"
            '1. Call `refactor_tool` with `mode="suggest"` for refactoring candidates, or '
            '`mode="dead_code"` for unreferenced code.\n'
            '2. For a rename, call `refactor_tool` with `mode="rename"`, `old_name` and '
            "`new_name`. Check the returned edit list before applying.\n"
            "3. Call `apply_refactor_tool` with the returned `refactor_id` to apply the rename.\n"
            "4. Before a large refactor, call `get_impact_radius_tool` and "
            "`get_affected_flows_tool` to see the dependents and critical paths involved.\n"
            "5. Call `find_large_functions_tool` to find functions worth splitting.\n"
            "6. After the change, call `detect_changes_tool` to confirm the impact matches the "
            "plan.\n\n"
            "## Token Efficiency Rules\n"
            '- Call `get_minimal_context_tool(task="<your task>")` before any other graph tool.\n'
            '- Pass `detail_level="minimal"` wherever a tool accepts it. Use "standard" only when '
            "minimal is not enough.\n"
            "- Prefer a targeted `query_graph_tool` call over a broad listing call.\n"
            "- Budget: about five tool calls and 800 tokens of graph output per task.\n"
            "- Read the implementation and its tests before changing code. The graph narrows "
            "scope; it does not replace the source."
        ),
    },
}


def generate_skills(repo_root: Path, skills_dir: Path | None = None) -> Path:
    """Generate Claude Code skill files.

    Creates `.claude/skills/` directory with 4 skill markdown files,
    each containing frontmatter and instructions.

    Args:
        repo_root: Repository root directory.
        skills_dir: Custom skills directory. Defaults to repo_root/.claude/skills.

    Returns:
        Path to the skills directory.
    """
    if skills_dir is None:
        skills_dir = repo_root / ".claude" / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)

    for filename, skill in _SKILLS.items():
        # Claude Code expects skills at .claude/skills/<name>/SKILL.md
        skill_name = filename.removesuffix(".md")
        skill_subdir = skills_dir / skill_name
        skill_subdir.mkdir(parents=True, exist_ok=True)
        path = skill_subdir / "SKILL.md"
        content = (
            "---\n"
            f"name: {skill['name']}\n"
            f"description: {skill['description']}\n"
            "---\n\n"
            f"{skill['body']}\n"
        )
        path.write_text(content, encoding="utf-8")
        logger.info("Wrote skill: %s", path)

    return skills_dir


def generate_hooks_config(repo_root: Path) -> dict[str, Any]:
    """Generate Claude Code hooks configuration.

    Hooks use the v1.x+ schema: each entry needs a ``matcher`` and a nested
    ``hooks`` array. Timeouts are in seconds. ``PreCommit`` is not a valid
    Claude Code event — pre-commit checks are handled by ``install_git_hook``.

    The ``repo_root`` parameter is retained for backward compatibility but is
    not embedded in hook commands. Instead, the repo root is resolved at
    runtime via ``git rev-parse --show-toplevel`` so that ``settings.json``
    is shareable across collaborators with different checkout paths.
    A PATH guard ensures the hook exits silently when the binary is not on
    ``$PATH`` (e.g. installed in a project venv).
    """
    return {
        "hooks": {
            "PostToolUse": [
                {
                    "matcher": "Edit|Write",
                    "hooks": [
                        {
                            "type": "command",
                            "command": (
                                "cat >/dev/null || true; "
                                "command -v code-review-graph >/dev/null 2>&1 || exit 0; "
                                "git rev-parse --git-dir >/dev/null 2>&1"
                                " && code-review-graph update --skip-flows"
                                " --repo \"$(git rev-parse --show-toplevel 2>/dev/null)\""
                                " || true"
                            ),
                            "timeout": 30,
                        },
                    ],
                },
            ],
            "SessionStart": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": (
                                "cat >/dev/null || true; "
                                "command -v code-review-graph >/dev/null 2>&1 || exit 0; "
                                "git rev-parse --git-dir >/dev/null 2>&1"
                                " && code-review-graph status"
                                " --repo \"$(git rev-parse --show-toplevel 2>/dev/null)\""
                                " || echo 'Not a git repo, skipping'"
                            ),
                            "timeout": 10,
                        },
                    ],
                },
            ],
        }
    }


def generate_codex_hooks_config(repo_root: Path) -> dict[str, Any]:
    """Generate native Codex hooks configuration for ~/.codex/hooks.json."""
    return {
        "hooks": {
            "PostToolUse": [
                {
                    "matcher": "Write|Edit|Bash",
                    "hooks": [
                        {
                            "type": "command",
                            "command": (
                                "cat >/dev/null || true; "
                                "git rev-parse --git-dir >/dev/null 2>&1"
                                " && code-review-graph update --skip-flows"
                                " || true"
                            ),
                            "timeout": 30,
                            "statusMessage": "Updating code-review-graph",
                        },
                    ],
                },
            ],
            "SessionStart": [
                {
                    "matcher": "startup|resume",
                    "hooks": [
                        {
                            "type": "command",
                            "command": (
                                "cat >/dev/null || true; "
                                "git rev-parse --git-dir >/dev/null 2>&1"
                                " && code-review-graph status"
                                " || echo 'Not a git repo, skipping'"
                            ),
                            "timeout": 10,
                            "statusMessage": "Checking code-review-graph status",
                        },
                    ],
                },
            ],
        }
    }


# --- Git pre-commit hook ---------------------------------------------------
#
# The generated block is delimited by explicit begin/end markers, the same way
# the managed instruction block is, so uninstall can cut out exactly what this
# project wrote. Nothing may infer the block's extent from its contents: the
# body nests an ``if``/``elif``/``else`` inside an outer ``if``, so any rule
# based on shell keywords (for example "stop at the first ``fi``") leaves half
# a block behind and turns the user's pre-commit hook into a syntax error.

_GIT_HOOK_BEGIN_MARKER = "# >>> code-review-graph pre-commit hook >>>"
_GIT_HOOK_END_MARKER = "# <<< code-review-graph pre-commit hook <<<"

# The human-readable line every release of the block has carried. Kept because
# it is the only thing an unmarked block written by an older release has in
# common with the current one.
_GIT_HOOK_NOTE = (
    "# Installed by code-review-graph. Remove this file to disable pre-commit graph checks."
)

_GIT_HOOK_SHEBANG = "#!/bin/sh\n"

_GIT_HOOK_BODY = """\
if command -v code-review-graph >/dev/null 2>&1; then
    crg_hook_git_dir=$(git rev-parse --absolute-git-dir 2>/dev/null) || crg_hook_git_dir=""
    crg_hook_root=$(git rev-parse --show-toplevel 2>/dev/null) || crg_hook_root=""
    if [ -z "$crg_hook_git_dir" ] || [ -z "$crg_hook_root" ]; then
        echo "code-review-graph: skipping automatic checks; cannot determine the Git worktree." >&2
    elif [ -f "$crg_hook_git_dir/commondir" ] && [ "$CRG_HOOK_WORKTREES" != "1" ]; then
        # Only a linked worktree's git dir carries a commondir file (Git 2.5+),
        # so this needs no rev-parse options newer than --absolute-git-dir.
        echo "code-review-graph: skipping automatic checks in a linked worktree;" \\
            "set CRG_HOOK_WORKTREES=1 to keep a graph for this worktree too." >&2
    else
        code-review-graph update --repo "$crg_hook_root" || true
        code-review-graph detect-changes --brief --repo "$crg_hook_root" || true
    fi
fi
"""

_GIT_HOOK_BLOCK = (
    f"{_GIT_HOOK_BEGIN_MARKER}\n{_GIT_HOOK_NOTE}\n{_GIT_HOOK_BODY}{_GIT_HOOK_END_MARKER}\n"
)

# Verbatim blocks shipped before the markers existed. Append-only, for the same
# reason ``_legacy_instructions`` is: an entry dropped here is a block that can
# no longer be upgraded or removed cleanly. Each is matched by its full text,
# which is the only boundary an unmarked block has.
_LEGACY_GIT_HOOK_BLOCKS: tuple[str, ...] = (
    # v2.2.3 - v2.3.2: detect-changes only, before the hook also ran an update.
    f"{_GIT_HOOK_NOTE}\n"
    "if command -v code-review-graph >/dev/null 2>&1; then\n"
    "    code-review-graph detect-changes --brief || true\n"
    "fi\n",
    # v2.3.3 - v2.3.8: update plus detect-changes, before the linked-worktree
    # guard (#313).
    f"{_GIT_HOOK_NOTE}\n"
    "if command -v code-review-graph >/dev/null 2>&1; then\n"
    "    code-review-graph update || true\n"
    "    code-review-graph detect-changes --brief || true\n"
    "fi\n",
    # The worktree-aware hook, shipped with no begin/end markers.
    f"{_GIT_HOOK_NOTE}\n{_GIT_HOOK_BODY}",
)


def _known_git_hook_blocks() -> tuple[str, ...]:
    """Every unmarked hook block this project has generated, longest first.

    Longest first matters: a shorter variant contained in a longer one must
    never win the match and strand the tail.
    """
    return tuple(sorted(set(_LEGACY_GIT_HOOK_BLOCKS), key=len, reverse=True))


def _git_hook_markers_balanced(text: str) -> bool:
    """Return whether every begin marker in ``text`` has its own end marker.

    A begin marker with no end marker after it, or a second begin marker
    opening before the first one closes, means the block was hand-edited. There
    is then no trustworthy boundary, so callers must refuse rather than guess
    where the block stops.
    """
    begins = text.count(_GIT_HOOK_BEGIN_MARKER)
    if begins != text.count(_GIT_HOOK_END_MARKER):
        return False
    cursor = 0
    for _ in range(begins):
        begin = text.find(_GIT_HOOK_BEGIN_MARKER, cursor)
        closing = text.find(_GIT_HOOK_END_MARKER, begin)
        if closing < 0:
            return False
        nested = text.find(_GIT_HOOK_BEGIN_MARKER, begin + len(_GIT_HOOK_BEGIN_MARKER))
        if 0 <= nested < closing:
            return False
        cursor = closing + len(_GIT_HOOK_END_MARKER)
    return True


def _git_hook_block_span(text: str, start: int = 0) -> tuple[int, int] | None:
    """Return ``(begin, end)`` offsets of one marked block, or None.

    ``end`` is just past the block's trailing newline, so slicing the span out
    leaves no blank line behind.
    """
    begin = text.find(_GIT_HOOK_BEGIN_MARKER, start)
    if begin < 0:
        return None
    closing = text.find(_GIT_HOOK_END_MARKER, begin)
    if closing < 0:
        # Someone deleted the closing marker; guessing where the block stops is
        # how user content gets eaten, so refuse.
        return None
    end = closing + len(_GIT_HOOK_END_MARKER)
    if end < len(text) and text[end] == "\n":
        end += 1
    return begin, end


def _upgrade_git_hook_block(existing: str) -> str | None:
    """Return ``existing`` with a block we wrote replaced by the current one.

    Returns None when the hook carries our marker but no block this project
    recognises, meaning it was hand-edited and must be left alone. An
    unbalanced marker pair is one such case: falling through to the unmarked
    matcher there would strip the body and leave the orphan marker line
    sitting in the user's hook.
    """
    if _GIT_HOOK_BEGIN_MARKER in existing and not _git_hook_markers_balanced(existing):
        return None
    span = _git_hook_block_span(existing)
    if span is not None:
        begin, end = span
        return existing[:begin] + _GIT_HOOK_BLOCK + existing[end:]
    for block in _known_git_hook_blocks():
        if block in existing:
            return existing.replace(block, _GIT_HOOK_BLOCK, 1)
    return None


def install_git_hook(repo_root: Path) -> Path | None:
    """Install a git pre-commit hook that prints a risk summary before each commit.

    Called automatically by ``code-review-graph install``.
    The hooks directory is resolved via ``git rev-parse --git-path hooks`` so
    the hook lands where git actually runs it — including linked worktrees
    and submodules (where ``.git`` is a file, not a directory) and repos with
    ``core.hooksPath`` set (issue #313). ``core.hooksPath`` users with their
    own hook manager (husky, pre-commit) may prefer integrating the
    ``code-review-graph`` commands into that manager manually instead.

    Creates ``pre-commit`` if it doesn't exist, or appends to an existing
    one — the hook is appended, not overwritten, preserving any hooks
    already there. Falls back to the legacy ``.git/hooks`` resolution when
    git itself is unavailable. Returns None when no hooks directory can be
    determined. A block written by any past release is upgraded in place, and
    a hand-edited one is left alone. The installed hook skips automatic checks
    in linked worktrees, where an implicit update could build a duplicate graph
    for a different branch; ``CRG_HOOK_WORKTREES=1`` opts a worktree back in.
    Detection relies only on ``git rev-parse --absolute-git-dir`` (Git 2.13),
    not on newer options.
    """
    hooks_dir: Path | None = None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-path", "hooks"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=str(repo_root),
            timeout=10,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode == 0 and result.stdout.strip():
            # Output is relative to repo_root (".git/hooks", a core.hooksPath
            # value such as ".husky") or absolute (linked worktrees).
            hooks_dir = repo_root / result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("git unavailable (%s); falling back to .git/hooks resolution.", exc)

    if hooks_dir is None:
        git_dir = repo_root / ".git"
        if not git_dir.is_dir():
            logger.warning(
                "No git hooks directory found at %s — skipping git hook install.", repo_root
            )
            return None
        hooks_dir = git_dir / "hooks"

    hook_path = hooks_dir / "pre-commit"
    hook_path.parent.mkdir(parents=True, exist_ok=True)

    if hook_path.exists():
        existing = hook_path.read_text(encoding="utf-8")
        if _GIT_HOOK_BEGIN_MARKER in existing and not _git_hook_markers_balanced(existing):
            logger.warning(
                "%s has a code-review-graph begin marker with no matching end "
                "marker; leaving it alone.",
                hook_path,
            )
            return hook_path
        if _GIT_HOOK_BEGIN_MARKER in existing or _GIT_HOOK_NOTE in existing:
            # Upgrade only a block this project generated; custom hook logic
            # and surrounding user commands remain intact.
            upgraded = _upgrade_git_hook_block(existing)
            if upgraded is None:
                logger.warning(
                    "%s has a hand-edited code-review-graph block; leaving it alone.",
                    hook_path,
                )
                return hook_path
            if upgraded == existing:
                logger.info("%s already holds the current hook block.", hook_path)
                return hook_path
            hook_path.write_text(upgraded, encoding="utf-8")
        else:
            hook_path.write_text(
                existing.rstrip("\n") + "\n" + _GIT_HOOK_BLOCK, encoding="utf-8"
            )
    else:
        hook_path.write_text(_GIT_HOOK_SHEBANG + _GIT_HOOK_BLOCK, encoding="utf-8")

    hook_path.chmod(0o755)
    logger.info("Wrote git pre-commit hook: %s", hook_path)
    return hook_path


# --- Hook ownership --------------------------------------------------------
#
# A hook entry is this project's to replace only when the command is one this
# project writes. Ownership must never be claimed by an unbounded substring
# search: ``bash /opt/acrg-tools/run.sh`` contains ``crg-`` inside a directory
# name and has nothing to do with this project, and a user's own
# ``code-review-graph build --repo /srv/mono && notify-team`` is their hook,
# not ours. Both survived before this file started merging by command, and both
# have to keep surviving. Where a command cannot be recognised with certainty
# it is kept, and the user is told.

# Shell scripts this project installs. Ownership of a script command is decided
# by the script's own file name, never by a substring of the path around it.
_GENERATED_HOOK_SCRIPTS = frozenset(
    {"crg-update.sh", "crg-session-start.sh", "crg-pre-commit.sh"}
)

_HOOK_SCRIPT_RUNNERS = frozenset({"bash", "sh"})

# Exactly the subcommand-and-flag combinations a released hook has run.
# Anything else after ``code-review-graph`` -- another subcommand, another
# flag, no flag at all -- was written by someone else.
_GENERATED_HOOK_BODIES = (
    "update --quiet --skip-flows",
    "update --quiet",
    "update --skip-flows",
    "status --json",
    "status",
    "detect-changes --brief",
)


def _hook_body_pattern() -> str:
    """Alternate the released bodies, longest first, spaces relaxed."""
    return "|".join(
        r"\s+".join(re.escape(token) for token in body.split())
        for body in sorted(_GENERATED_HOOK_BODIES, key=len, reverse=True)
    )


# The CLI one-liners every release has written, as one anchored pattern. Each
# optional piece is a prelude some release added around the body.
_GENERATED_HOOK_COMMAND_RE = re.compile(
    r"\A"
    r"(?:cat\s*>\s*/dev/null\s*\|\|\s*true;\s*)?"
    r"(?:command\s+-v\s+code-review-graph\s*>/dev/null\s+2>&1\s*\|\|\s*exit\s+0;\s*)?"
    r"(?:git\s+rev-parse\s+--git-dir\s*>/dev/null\s+2>&1\s*&&\s*)?"
    r"code-review-graph\s+(?:" + _hook_body_pattern() + r")"
    r"(?:\s+--repo\s+(?:\"[^\"]*\"|'[^']*'|[^\s\"'|&;<>]+))?"
    r"(?:\s*\|\|\s*(?:true|echo\s+'Not a git repo, skipping'))?"
    r"\Z"
)

# Loose spellings that merely *mention* this project. Used only to tell the
# user about a hook that was kept because it could not be recognised; never to
# claim one.
_HOOK_MENTION_MARKERS = ("code-review-graph", "code_review_graph", "crg-")

# Matchers this project has written, per hook event. A group filed under any
# other matcher belongs to whoever wrote it, even when a command inside it
# resembles ours -- a user's PostToolUse hook on matcher ``Write`` is theirs.
_GENERATED_HOOK_MATCHERS: dict[str, frozenset[str | None]] = {
    "PostToolUse": frozenset({"Edit|Write", "Edit|Write|Bash", "Write|Edit|Bash"}),
    "SessionStart": frozenset({"", None, "startup|resume"}),
    "AfterTool": frozenset({"write_file|replace"}),
}


# Characters that begin a second command or a redirection. A word carrying one
# of these unquoted is not part of a path, so the line is a compound command
# somebody wrote, not a plain call to one of our scripts.
_SHELL_OPERATOR_RE = re.compile(r"[|&;<>`]|\$\(")

# A word that opens a fresh argument -- an option, an absolute POSIX path, a
# Windows drive or UNC path -- and so cannot be the continuation of the
# previous word's path.
_NEW_ARGUMENT_RE = re.compile(r"\A(?:[-/\\]|[A-Za-z]:[\\/])")


def _shell_words(command: str) -> list[tuple[str, str]] | None:
    """Split ``command`` into shell words as ``(raw, unquoted)`` pairs.

    ``str.split`` is the wrong tool: it cannot see through ``"..."`` and it
    breaks ``/Users/jo smith/hooks/crg-update.sh`` -- one path on a home
    directory with a space in it -- into two words. ``shlex`` understands
    quoting, and non-POSIX mode leaves backslashes alone so a Windows path
    survives the trip. Returns None for a line no shell could parse, such as
    one with an unterminated quote; nothing about such a line is certain
    enough to act on.
    """
    lexer = shlex.shlex(command, posix=False, punctuation_chars=False)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        raw_words = list(lexer)
    except ValueError:
        return None
    return [(word, _unquote_word(word)) for word in raw_words]


def _unquote_word(word: str) -> str:
    """Strip one layer of matching surrounding quotes from a shell word."""
    for quote in ('"', "'"):
        if len(word) >= 2 and word.startswith(quote) and word.endswith(quote):
            return word[1:-1]
    return word


def _is_generated_hook_script(command: str) -> bool:
    """Return whether ``command`` just runs a hook script this project writes.

    Ownership is the script's own file name, so the answer must not depend on
    how the path leading to it is spelled: quoted or bare, absolute or
    relative, ``/`` or ``\\``, with a space in the home directory, with a
    trailing argument. What still disqualifies a command is a second program:
    a chained or redirected command, or one of our scripts passed as an
    argument to somebody else's.
    """
    words = _shell_words(command)
    if not words:
        return False
    # A quoted word's contents are literal, so an operator character inside
    # one is part of the path and not a chained command.
    if any(raw == value and _SHELL_OPERATOR_RE.search(value) for raw, value in words):
        return False
    values = [value for _raw, value in words]
    start = (
        1
        if len(values) > 1 and _launcher_basename(values[0]) in _HOOK_SCRIPT_RUNNERS
        else 0
    )
    # An unquoted path with a space in it arrives as several words, so grow the
    # candidate one word at a time. Stop as soon as the next word opens a new
    # argument, or the previous one already ended in a script of its own,
    # because then the words are two arguments rather than one path.
    for end in range(start + 1, len(values) + 1):
        if end > start + 1:
            if _NEW_ARGUMENT_RE.match(values[end - 1]):
                break
            if values[end - 2].lower().endswith(".sh"):
                break
        if _launcher_basename(" ".join(values[start:end])) in _GENERATED_HOOK_SCRIPTS:
            return True
    return False


def _is_generated_hook_command(command: Any) -> bool:
    """Return whether ``command`` is a hook command this project writes.

    Only the exact command shapes released versions emit count. Anything else
    that merely mentions this project -- a different subcommand, extra flags, a
    second command chained on with ``&&``, an unrelated path that happens to
    contain ``crg-`` -- belongs to the user and is left in place.
    """
    if not isinstance(command, str):
        return False
    stripped = command.strip()
    if not stripped:
        return False
    return bool(_GENERATED_HOOK_COMMAND_RE.match(stripped)) or _is_generated_hook_script(
        stripped
    )


def _report_kept_hook(command: Any) -> None:
    """Say that a hook mentioning this project was deliberately left alone.

    Certainty is the condition for touching someone's configuration. When a
    command mentions this project but is not a shape it writes, the hook stays
    and the user is told, rather than being deleted on a guess.
    """
    if not isinstance(command, str):
        return
    if not any(marker in command for marker in _HOOK_MENTION_MARKERS):
        return
    safe = "".join(char for char in command if char.isprintable())[:160]
    print(f"  kept a hook command this installer did not write: {safe}")


def _allowed_hook_matchers(event_name: str, new_entries: Sequence[Any]) -> frozenset[Any]:
    """Return the matchers under which a group may be claimed for this event."""
    allowed: set[Any] = set(_GENERATED_HOOK_MATCHERS.get(event_name, frozenset()))
    for entry in new_entries:
        if isinstance(entry, dict):
            matcher = entry.get("matcher")
            if isinstance(matcher, str) or matcher is None:
                allowed.add(matcher)
    return frozenset(allowed)


def _merge_hook_entries(
    existing: Any, new_entries: list[Any], event_name: str = ""
) -> list[Any]:
    """Return ``existing`` with our own hook groups replaced by ``new_entries``.

    Comparing whole entries (or exact command strings) only ever recognises the
    hook the running version would write, so any change to the command left the
    previous release's hook in place and appended a second one beside it, and
    the repository then ran two code-review-graph hooks on one event.

    Ownership is therefore decided by the command, bounded twice over: the
    command has to be one of the shapes this project emits, and the group has
    to sit under a matcher this project has written for this event. A group
    that also holds a hook someone else wrote keeps that hook, and the
    replacement lands where the old group sat rather than at the end, so a hook
    that ran first goes on running first.
    """
    allowed = _allowed_hook_matchers(event_name, new_entries)
    kept: list[Any] = []
    insert_at: int | None = None
    for group in existing if isinstance(existing, list) else []:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            kept.append(group)
            continue
        matcher = group.get("matcher")
        if not (isinstance(matcher, str) or matcher is None) or matcher not in allowed:
            kept.append(group)
            for hook in group["hooks"]:
                if isinstance(hook, dict):
                    _report_kept_hook(hook.get("command"))
            continue
        nested = group["hooks"]
        survivors = [
            hook
            for hook in nested
            if not (
                isinstance(hook, dict) and _is_generated_hook_command(hook.get("command"))
            )
        ]
        for hook in survivors:
            if isinstance(hook, dict):
                _report_kept_hook(hook.get("command"))
        if len(survivors) == len(nested):
            kept.append(group)
            continue
        if not survivors:
            if insert_at is None:
                insert_at = len(kept)  # the whole group was ours
            continue
        kept.append({**group, "hooks": survivors})
        if insert_at is None:
            insert_at = len(kept)
    if insert_at is None:
        insert_at = len(kept)
    return kept[:insert_at] + list(new_entries) + kept[insert_at:]


def _merge_flat_hook_entries(existing: Any, new_entries: list[Any]) -> list[Any]:
    """The same replacement rule for a flat, one-level hook list (Cursor)."""
    kept: list[Any] = []
    insert_at: int | None = None
    for hook in existing if isinstance(existing, list) else []:
        if isinstance(hook, dict) and _is_generated_hook_command(hook.get("command")):
            if insert_at is None:
                insert_at = len(kept)
            continue
        if isinstance(hook, dict):
            _report_kept_hook(hook.get("command"))
        kept.append(hook)
    if insert_at is None:
        insert_at = len(kept)
    return kept[:insert_at] + list(new_entries) + kept[insert_at:]


def _merge_hooks_into_settings(
    settings_dir: Path,
    hooks_config: dict[str, Any],
) -> Path:
    """Merge hook entries into a project settings file without clobbering users."""
    settings_dir.mkdir(parents=True, exist_ok=True)
    settings_path = settings_dir / "settings.json"

    existing: dict[str, Any] = {}
    if settings_path.exists():
        try:
            existing = json.loads(settings_path.read_text(encoding="utf-8", errors="replace"))
            backup_path = settings_dir / "settings.json.bak"
            shutil.copy2(settings_path, backup_path)
            logger.info("Backed up existing settings to %s", backup_path)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read existing %s: %s", settings_path, exc)

    existing_hooks = existing.get("hooks", {})
    if not isinstance(existing_hooks, dict):
        logger.warning("Existing hooks config is not a dict; replacing with defaults")
        existing_hooks = {}

    merged_hooks = dict(existing_hooks)
    for hook_name, hook_entries in hooks_config.get("hooks", {}).items():
        if isinstance(merged_hooks.get(hook_name), list):
            merged_hooks[hook_name] = _merge_hook_entries(
                merged_hooks[hook_name], hook_entries, hook_name
            )
        else:
            merged_hooks[hook_name] = hook_entries

    existing["hooks"] = merged_hooks

    settings_path.write_text(
        json.dumps(existing, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    logger.info("Wrote hooks config: %s", settings_path)
    return settings_path


def install_hooks(repo_root: Path, platform: str = "claude") -> None:
    """Write hooks config to platform-specific settings.json.

    Merges new hook entries into existing settings, preserving both
    non-hook configuration and user-defined hooks.  A backup of the
    original file is created before any modifications.

    Args:
        repo_root: Repository root directory.
        platform: Target platform ("claude" or "qoder").
    """
    if platform == "qoder":
        settings_dir = repo_root / ".qoder"
    else:
        settings_dir = repo_root / ".claude"
    _merge_hooks_into_settings(settings_dir, generate_hooks_config(repo_root))


def install_codebuddy_hooks(repo_root: Path) -> Path:
    """Install runtime-resolved POSIX hooks in .codebuddy/settings.json.

    CodeBuddy uses the same nested hook schema and POSIX-shell execution
    model as the existing project hooks. The shared generator deliberately
    resolves the checkout at hook runtime instead of embedding the installer's
    absolute path, so committed settings work for every collaborator.
    """
    hooks_config = generate_hooks_config(repo_root)
    # CodeBuddy's Bash tool can create or rewrite files without going through
    # Edit/Write, so its PostToolUse contract also observes Bash. The command
    # itself still resolves the repository dynamically at hook runtime.
    hooks_config["hooks"]["PostToolUse"][0]["matcher"] = "Edit|Write|Bash"
    return _merge_hooks_into_settings(
        repo_root / ".codebuddy",
        hooks_config,
    )


def install_codex_hooks(repo_root: Path) -> Path:
    """Write native Codex hooks config to ~/.codex/hooks.json.

    Merges code-review-graph hook entries into any existing hooks.json,
    preserving user-defined hook entries and other top-level settings.
    A backup of the original file is created before modifications.
    """
    codex_dir = Path.home() / ".codex"
    codex_dir.mkdir(parents=True, exist_ok=True)
    hooks_path = codex_dir / "hooks.json"

    existing: dict[str, Any] = {}
    if hooks_path.exists():
        try:
            existing = json.loads(hooks_path.read_text(encoding="utf-8", errors="replace"))
            backup_path = codex_dir / "hooks.json.bak"
            shutil.copy2(hooks_path, backup_path)
            logger.info("Backed up existing Codex hooks to %s", backup_path)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read existing %s: %s", hooks_path, exc)

    hooks_config = generate_codex_hooks_config(repo_root)
    existing_hooks = existing.get("hooks", {})
    if not isinstance(existing_hooks, dict):
        logger.warning("Existing Codex hooks config is not a dict; replacing with defaults")
        existing_hooks = {}

    merged_hooks = dict(existing_hooks)
    for hook_name, hook_entries in hooks_config.get("hooks", {}).items():
        if isinstance(merged_hooks.get(hook_name), list):
            merged_hooks[hook_name] = _merge_hook_entries(
                merged_hooks[hook_name], hook_entries, hook_name
            )
        else:
            merged_hooks[hook_name] = hook_entries

    existing["hooks"] = merged_hooks
    hooks_path.write_text(
        json.dumps(existing, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    logger.info("Wrote Codex hooks config: %s", hooks_path)
    return hooks_path


_CLAUDE_MD_SECTION_MARKER = "<!-- code-review-graph MCP tools -->"

# Closes the managed block so reinstall can replace it without guessing where it
# ends. Releases before this shipped only the opening marker; those blocks are
# matched by their full text instead, see _legacy_instructions.
_CLAUDE_MD_SECTION_END_MARKER = "<!-- /code-review-graph MCP tools -->"

# Shared across every platform instruction file so the wording stays identical.
_INSTRUCTION_INTRO = """**This project has a knowledge graph. Start with the code-review-graph
MCP tools to narrow scope, then read the source.** The graph is cheaper than scanning files and
gives you structural context (callers, dependents, test coverage) that file search cannot."""

_INSTRUCTION_GUARDRAILS = """### Verify in the source

- Narrow scope with the graph, then read the source. Do not change code from graph output alone.
- For any non-trivial change, read the implementation and the relevant tests before concluding.
- Verify the exact source when touching behavior, database logic, migrations, retries, fallbacks,
  recovery, or compatibility code.
- When the graph and the source disagree, the source wins. The graph may be stale or may not
  model that relationship.
- An empty graph result can mean "not indexed" or "not statically visible", not "does not exist"."""

_CLAUDE_MD_SECTION = f"""{_CLAUDE_MD_SECTION_MARKER}
## MCP Tools: code-review-graph

{_INSTRUCTION_INTRO}

### When to use graph tools FIRST

- **Exploring code**: `semantic_search_nodes_tool` or `query_graph_tool` instead of Grep
- **Understanding impact**: `get_impact_radius_tool` instead of manually tracing imports
- **Code review**: `detect_changes_tool` + `get_review_context_tool` instead of reading entire files
- **Finding relationships**: `query_graph_tool` with callers_of/callees_of/imports_of/tests_for
- **Architecture questions**: `get_architecture_overview_tool` + `list_communities_tool`

{_INSTRUCTION_GUARDRAILS}

### Key Tools

| Tool | Use when |
| ------ | ---------- |
| `detect_changes_tool` | Reviewing code changes — gives risk-scored analysis |
| `get_review_context_tool` | Need source snippets for review — token-efficient |
| `get_impact_radius_tool` | Understanding blast radius of a change |
| `get_affected_flows_tool` | Finding which execution paths are impacted |
| `query_graph_tool` | Tracing callers, callees, imports, tests, dependencies |
| `semantic_search_nodes_tool` | Finding functions/classes by name or keyword |
| `get_architecture_overview_tool` | Understanding high-level codebase structure |
| `refactor_tool` | Planning renames, finding dead code |

### Workflow

1. The graph auto-updates on file changes (via hooks).
2. Use `detect_changes_tool` for code review.
3. Use `get_affected_flows_tool` to understand impact.
4. Use `query_graph_tool` pattern=\"tests_for\" to check coverage.
{_CLAUDE_MD_SECTION_END_MARKER}
"""

# Copilot-specific instruction file content: uses VS Code tool references and
# includes YAML front matter so Copilot Chat applies it across the workspace.
_COPILOT_SECTION = f"""---
applyTo: '**'
description: >-
  Use code-review-graph MCP tools for token-efficient
  codebase exploration and code review.
---

{_CLAUDE_MD_SECTION_MARKER}
## MCP Tools: code-review-graph

{_INSTRUCTION_INTRO}

### When to use graph tools FIRST

- **Exploring code**: `semantic_search_nodes_tool` or `query_graph_tool`
- **Understanding impact**: `get_impact_radius_tool`
- **Code review**: `detect_changes_tool` + `get_review_context_tool`
- **Finding relationships**: `query_graph_tool` callers_of/callees_of
- **Architecture questions**: `get_architecture_overview_tool`

{_INSTRUCTION_GUARDRAILS}

### Key Tools

| Tool | Use when |
| ------ | ---------- |
| `detect_changes_tool` | Risk-scored change analysis |
| `get_review_context_tool` | Token-efficient source snippets |
| `get_impact_radius_tool` | Blast radius of a change |
| `get_affected_flows_tool` | Impacted execution paths |
| `query_graph_tool` | Trace callers, callees, imports, tests |
| `semantic_search_nodes_tool` | Find functions/classes by keyword |
| `get_architecture_overview_tool` | High-level structure |
| `refactor_tool` | Rename planning, dead code |

### Workflow

1. The graph auto-updates on file changes (via hooks).
2. Use `detect_changes_tool` for code review.
3. Use `get_affected_flows_tool` to understand impact.
4. Use `query_graph_tool` pattern=\"tests_for\" to check coverage.
{_CLAUDE_MD_SECTION_END_MARKER}
"""

# Maps instruction file path → (marker, section) for files that need content
# different from the default _CLAUDE_MD_SECTION. Legacy paths remain here so
# uninstall can identify sections written by older releases.
_PLATFORM_INSTRUCTION_CUSTOM_SECTIONS: dict[str, tuple[str, str]] = {
    ".github/instructions/code-review-graph.instructions.md": (
        _CLAUDE_MD_SECTION_MARKER,
        _COPILOT_SECTION,
    ),
    ".github/code-review-graph.instruction.md": (_CLAUDE_MD_SECTION_MARKER, _COPILOT_SECTION),
}


def _known_instruction_sections() -> tuple[str, ...]:
    """Every block text this project has ever generated, longest first.

    Longest first matters: a shorter variant that happens to be contained in a
    longer one must never win the match and leave the tail behind.
    """
    current = (_CLAUDE_MD_SECTION, _COPILOT_SECTION)
    return tuple(sorted({*current, *LEGACY_INSTRUCTION_SECTIONS}, key=len, reverse=True))


def _upgrade_managed_block(existing: str, section: str) -> str | None:
    """Replace a previously generated block with ``section``.

    Only text that exactly equals a known generated block is ever rewritten, so
    anything the user wrote around it survives byte for byte. Blocks predating
    the end marker have no closing boundary, which is why nothing here searches
    for one; guessing where such a block stops would eat user content.

    Returns the new file content, or None when the marker is present but no
    known block is, meaning someone edited the block by hand.
    """
    stale = [
        block
        for block in _known_instruction_sections()
        if block != section and block in existing
    ]
    if not stale:
        return None
    # Anchor on the longest match, then drop any duplicate blocks an older
    # release left behind. Splitting around the anchor keeps the cleanup away
    # from the text being written in, which a plain str.replace would not.
    head = stale[0]
    index = existing.index(head)
    before, after = existing[:index], existing[index + len(head) :]
    for block in stale[1:]:
        before = before.replace(block, "")
        after = after.replace(block, "")
    if section in before or section in after:
        # The current block is already there; the stale ones were duplicates.
        return before + after
    return before + section + after


def _inject_instructions(file_path: Path, marker: str, section: str) -> str:
    """Create, or upgrade in place, the managed instruction block in a file.

    Returns one of:

    - ``"created"``: the block was written for the first time, creating the
      file or appending to one that had no block.
    - ``"updated"``: an older generated block was replaced with the current one.
    - ``"unchanged"``: the file already holds the current block, byte for byte.
      Nothing is written, so repeated installs do not touch the file.
    - ``"conflict"``: the marker is present but the block matches nothing this
      project generated, so it was hand-edited. The file is left alone and the
      caller is expected to tell the user about it.
    """
    existing = ""
    if file_path.exists():
        existing = file_path.read_text(encoding="utf-8", errors="replace")

    if marker in existing:
        upgraded = _upgrade_managed_block(existing, section)
        if upgraded is None:
            if section in existing:
                logger.info("%s already holds the current instructions.", file_path.name)
                return "unchanged"
            logger.warning(
                "%s has a hand-edited code-review-graph section; leaving it alone.",
                file_path,
            )
            return "conflict"
        file_path.write_text(upgraded, encoding="utf-8")
        logger.info("Updated the MCP tools section in %s", file_path)
        return "updated"

    separator = "\n" if existing and not existing.endswith("\n") else ""
    extra_newline = "\n" if existing else ""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(existing + separator + extra_newline + section, encoding="utf-8")
    logger.info("Appended MCP tools section to %s", file_path)
    return "created"


def inject_claude_md(repo_root: Path) -> str:
    """Create or upgrade the MCP tools section in CLAUDE.md.

    Returns the outcome string documented on ``_inject_instructions``.
    """
    return _inject_instructions(
        repo_root / "CLAUDE.md",
        _CLAUDE_MD_SECTION_MARKER,
        _CLAUDE_MD_SECTION,
    )


# Cross-platform instruction files and which platforms own each one.
# Used to filter writes when the user passes --platform <X>: only files
# whose owner set includes the target (or "all") are written.
_PLATFORM_INSTRUCTION_FILES: dict[str, tuple[str, ...]] = {
    "AGENTS.md": ("cursor", "opencode", "antigravity", "codex", "hermes"),
    "GEMINI.md": ("antigravity", "gemini-cli"),
    ".cursorrules": ("cursor",),
    ".windsurfrules": ("windsurf",),
    "QODER.md": ("qoder",),
    ".kiro/steering/code-review-graph.md": ("kiro",),
    ".github/instructions/code-review-graph.instructions.md": ("copilot", "copilot-cli"),
    "CODEBUDDY.md": ("codebuddy",),
}

# Superseded paths written by older releases. Reinstall removes only the exact
# generated section and leaves any user-authored content intact.
_LEGACY_PLATFORM_INSTRUCTION_FILES: dict[str, tuple[str, ...]] = {
    ".github/code-review-graph.instruction.md": ("copilot", "copilot-cli"),
}


def _remove_legacy_instruction_file(path: Path) -> None:
    """Strip an exact generated section from a superseded instruction file."""
    if not path.exists():
        return
    content = path.read_text(encoding="utf-8", errors="replace")
    if _CLAUDE_MD_SECTION_MARKER not in content:
        return
    # Longest first, so removing a long block cannot leave the tail of a shorter
    # variant it contains. Anything not generated by this project is left alone.
    for section in _known_instruction_sections():
        content = content.replace(section, "")
    if _CLAUDE_MD_SECTION_MARKER in content:
        return
    if content.strip():
        path.write_text(content.rstrip() + "\n", encoding="utf-8")
        logger.info("Removed legacy instruction section from %s", path)
    else:
        path.unlink()
        logger.info("Removed legacy instruction file %s", path)


# --- Gemini CLI hooks + skills (workspace-level: .gemini/) ---

_GEMINI_CLI_HOOK_FILENAMES = ("crg-session-start.sh", "crg-update.sh")


def install_gemini_cli_hooks(repo_root: Path) -> Path:
    """Install Gemini CLI hooks in .gemini/settings.json and write hook scripts.

    Hooks schema reference:
    - https://geminicli.com/docs/hooks/reference/

    This is workspace-scoped (project) configuration: .gemini/settings.json
    """
    settings_dir = repo_root / ".gemini"
    settings_dir.mkdir(parents=True, exist_ok=True)
    settings_path = settings_dir / "settings.json"

    existing: dict[str, Any] = {}
    if settings_path.exists():
        try:
            existing = json.loads(settings_path.read_text(encoding="utf-8", errors="replace"))
            backup_path = settings_dir / "settings.json.bak"
            shutil.copy2(settings_path, backup_path)
            logger.info("Backed up existing Gemini CLI settings to %s", backup_path)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read existing %s: %s", settings_path, exc)

    hooks_dir = settings_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    repo_arg = repo_root.resolve().as_posix()
    session_start_script = """\
#!/usr/bin/env bash
# code-review-graph: session start status (Gemini CLI hook)
# Must output ONLY JSON on stdout. Logs go to stderr. Never blocks the session.
set -euo pipefail

cat > /dev/null || true

msg="$(code-review-graph status --repo "__CRG_REPO__" 2>&1 | head -n 1 || true)"

CRG_MSG="$msg" python3 -c '
import json,os
m=os.environ.get("CRG_MSG","")
print(json.dumps({"systemMessage":m,"suppressOutput":True}))
' 2>/dev/null || echo '{"suppressOutput": true}'
exit 0
"""
    session_start_script = session_start_script.replace("__CRG_REPO__", repo_arg)

    update_script = """\
#!/usr/bin/env bash
# code-review-graph: incremental update after write/replace (Gemini CLI hook)
# Must output ONLY JSON on stdout. Low-noise: no systemMessage.
set -euo pipefail

cat > /dev/null || true

code-review-graph update --skip-flows --repo "__CRG_REPO__" >/dev/null 2>&1 || true
echo '{"suppressOutput": true}'
exit 0
"""
    update_script = update_script.replace("__CRG_REPO__", repo_arg)

    session_start_path = hooks_dir / _GEMINI_CLI_HOOK_FILENAMES[0]
    session_start_path.write_text(session_start_script, encoding="utf-8")
    session_start_path.chmod(0o755)

    update_path = hooks_dir / _GEMINI_CLI_HOOK_FILENAMES[1]
    update_path.write_text(update_script, encoding="utf-8")
    update_path.chmod(0o755)

    hooks_obj = existing.get("hooks", {})
    if not isinstance(hooks_obj, dict):
        hooks_obj = {}

    def _ensure_group(
        event_name: str, matcher: str, hook_command: str, name: str, timeout: int,
    ) -> None:
        # Replace whatever shape a previous release wrote for this event rather
        # than appending a second code-review-graph hook beside it.
        arr = hooks_obj.get(event_name, [])
        hooks_obj[event_name] = _merge_hook_entries(
            arr,
            [
                {
                    "matcher": matcher,
                    "hooks": [
                        {
                            "type": "command",
                            "command": hook_command,
                            "name": name,
                            "timeout": timeout,
                        }
                    ],
                }
            ],
            event_name,
        )

    _ensure_group(
        event_name="SessionStart",
        matcher="",
        hook_command=f"bash .gemini/hooks/{_GEMINI_CLI_HOOK_FILENAMES[0]}",
        name="code-review-graph status",
        timeout=10_000,
    )
    _ensure_group(
        event_name="AfterTool",
        matcher="write_file|replace",
        hook_command=f"bash .gemini/hooks/{_GEMINI_CLI_HOOK_FILENAMES[1]}",
        name="code-review-graph update",
        timeout=30_000,
    )

    existing["hooks"] = hooks_obj
    settings_path.write_text(
        json.dumps(existing, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    logger.info("Wrote Gemini CLI hooks config: %s", settings_path)
    return settings_path


def install_gemini_cli_skills(repo_root: Path) -> Path:
    """Install Gemini CLI Agent Skills in .gemini/skills/<skill>/SKILL.md."""
    skills_root = repo_root / ".gemini" / "skills"
    skills_root.mkdir(parents=True, exist_ok=True)

    for filename, skill in _SKILLS.items():
        slug = filename.rsplit(".", 1)[0]
        skill_dir = skills_root / slug
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_path = skill_dir / "SKILL.md"
        content = (
            "---\n"
            f"name: {slug}\n"
            f"description: {skill['description']}\n"
            "---\n\n"
            f"{skill['body']}\n"
        )
        skill_path.write_text(content, encoding="utf-8")
        logger.info("Wrote Gemini CLI skill: %s", skill_path)

    return skills_root


def install_codebuddy_skills(repo_root: Path) -> Path:
    """Install project skills in .codebuddy/skills/<name>/SKILL.md."""
    skills_root = repo_root / ".codebuddy" / "skills"
    skills_root.mkdir(parents=True, exist_ok=True)

    for filename, skill in _SKILLS.items():
        slug = filename.rsplit(".", 1)[0]
        skill_dir = skills_root / slug
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_path = skill_dir / "SKILL.md"
        content = (
            "---\n"
            f"name: {slug}\n"
            f"description: {skill['description']}\n"
            "---\n\n"
            f"{skill['body']}\n"
        )
        skill_path.write_text(content, encoding="utf-8")
        logger.info("Wrote CodeBuddy skill: %s", skill_path)

    return skills_root


def inject_platform_instructions(repo_root: Path, target: str = "all") -> list[str]:
    """Inject 'use graph first' instructions into platform rule files.

    Writes AGENTS.md, GEMINI.md, .cursorrules, and/or .windsurfrules
    depending on ``target``:

    - ``"all"`` (default): writes every file — matches pre-filter behavior.
    - ``"claude"``: writes nothing (CLAUDE.md is handled by ``inject_claude_md``).
    - any other platform key (``cursor``, ``windsurf``, ``antigravity``,
      ``opencode``, ``codex``): writes only the files associated with that platform.

    Returns list of filenames that were created or updated. Use
    ``inject_instruction_files`` when the caller also needs to know which files
    were left alone because someone edited the block by hand.
    """
    outcomes = inject_instruction_files(repo_root, target=target, include_claude_md=False)
    return [name for name, outcome in outcomes.items() if outcome in ("created", "updated")]


def inject_instruction_files(
    repo_root: Path,
    target: str = "all",
    *,
    include_claude_md: bool = True,
) -> dict[str, str]:
    """Write every instruction file for ``target`` and report what happened.

    Maps each filename to ``"created"``, ``"updated"``, ``"unchanged"`` or
    ``"conflict"``, as documented on ``_inject_instructions``. This is the entry
    point the install command uses so it can tell the user which files it
    upgraded and which ones need manual attention.
    """
    outcomes: dict[str, str] = {}
    if include_claude_md and target in ("claude", "all"):
        outcomes["CLAUDE.md"] = inject_claude_md(repo_root)
    for filename, owners in _PLATFORM_INSTRUCTION_FILES.items():
        if target != "all" and target not in owners:
            continue
        path = repo_root / filename
        if filename in _PLATFORM_INSTRUCTION_CUSTOM_SECTIONS:
            marker, section = _PLATFORM_INSTRUCTION_CUSTOM_SECTIONS[filename]
        else:
            marker, section = _CLAUDE_MD_SECTION_MARKER, _CLAUDE_MD_SECTION
        outcomes[filename] = _inject_instructions(path, marker, section)
    for filename, owners in _LEGACY_PLATFORM_INSTRUCTION_FILES.items():
        if target != "all" and target not in owners:
            continue
        _remove_legacy_instruction_file(repo_root / filename)
    return outcomes


# --- Cursor hooks ---


def generate_cursor_hooks_config() -> dict[str, Any]:
    """Generate Cursor hooks.json configuration.

    Returns a dict conforming to the Cursor hooks schema (version 1) with
    hooks for afterFileEdit, sessionStart, and beforeShellExecution.
    Each hook points to a shell script in ~/.cursor/hooks/.

    Returns:
        Dict suitable for writing as ~/.cursor/hooks.json.
    """
    hooks_dir = str(Path.home() / ".cursor" / "hooks")
    return {
        "version": 1,
        "hooks": {
            "afterFileEdit": [
                {
                    "command": f"{hooks_dir}/crg-update.sh",
                    "timeout": 5,
                },
            ],
            "sessionStart": [
                {
                    "command": f"{hooks_dir}/crg-session-start.sh",
                    "timeout": 5,
                },
            ],
            "beforeShellExecution": [
                {
                    "matcher": "^git\\s+commit",
                    "command": f"{hooks_dir}/crg-pre-commit.sh",
                    "timeout": 10,
                },
            ],
        },
    }


def _cursor_hook_scripts() -> dict[str, str]:
    """Return a mapping of filename -> shell script content for Cursor hooks.

    Three scripts are generated:
    - crg-update.sh: runs ``code-review-graph update --skip-flows`` after file edits
    - crg-session-start.sh: runs ``code-review-graph status`` on session start
    - crg-pre-commit.sh: runs ``code-review-graph detect-changes --brief`` before
      git commit commands

    All scripts:
    - Read stdin (Cursor passes JSON context) and discard it
    - Fail gracefully (exit 0) so they never block the editor
    - Emit valid JSON on stdout per the Cursor hooks protocol
    """
    update_script = """\
#!/usr/bin/env bash
# code-review-graph: auto-update graph after file edits (Cursor hook)
# Fails gracefully — never blocks the editor.
set -euo pipefail

# Consume stdin (Cursor sends JSON context)
cat > /dev/null

# Run update; swallow errors so the hook always succeeds.
output=$(code-review-graph update --skip-flows 2>&1) || true

# Emit valid JSON on stdout per Cursor hooks protocol.
python3 -c "
import json, sys
print(json.dumps({'message': 'graph updated', 'passed': True}))
" 2>/dev/null || echo '{"passed":true}'

exit 0
"""

    session_start_script = """\
#!/usr/bin/env bash
# code-review-graph: show graph status on session start (Cursor hook)
# Fails gracefully — never blocks the editor.
set -euo pipefail

# Consume stdin
cat > /dev/null

# Capture status output
output=$(code-review-graph status 2>&1) || output="graph not built yet"

# Emit valid JSON on stdout
python3 -c "
import json, sys
msg = sys.stdin.read()
print(json.dumps({'message': msg, 'passed': True}))
" <<< "$output" 2>/dev/null || echo '{"passed":true}'

exit 0
"""

    pre_commit_script = """\
#!/usr/bin/env bash
# code-review-graph: detect changes before git commit (Cursor hook)
# Fails gracefully — never blocks the editor.
set -euo pipefail

# Consume stdin
cat > /dev/null

# Run detect-changes; swallow errors
output=$(code-review-graph detect-changes --brief 2>&1) || output=""

# Emit valid JSON on stdout
python3 -c "
import json, sys
msg = sys.stdin.read()
print(json.dumps({'message': msg, 'passed': True}))
" <<< "$output" 2>/dev/null || echo '{"passed":true}'

exit 0
"""

    return {
        "crg-update.sh": update_script,
        "crg-session-start.sh": session_start_script,
        "crg-pre-commit.sh": pre_commit_script,
    }


def install_cursor_hooks() -> Path:
    """Install Cursor hooks configuration and scripts at user level.

    Writes ``~/.cursor/hooks.json`` (merging code-review-graph hooks
    into any existing configuration) and creates executable shell scripts
    in ``~/.cursor/hooks/``.

    Returns:
        Path to the hooks.json file that was written.
    """
    cursor_dir = Path.home() / ".cursor"
    hooks_json_path = cursor_dir / "hooks.json"
    hooks_script_dir = cursor_dir / "hooks"

    # --- Merge hooks.json ---
    existing: dict[str, Any] = {}
    if hooks_json_path.exists():
        try:
            existing = json.loads(hooks_json_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read existing %s: %s", hooks_json_path, exc)

    new_config = generate_cursor_hooks_config()

    # Preserve version (use ours if absent)
    existing.setdefault("version", new_config["version"])

    # Merge hook arrays per event type
    existing_hooks = existing.get("hooks", {})
    if not isinstance(existing_hooks, dict):
        existing_hooks = {}

    for event, entries in new_config["hooks"].items():
        # Cursor's schema is one flat list per event. Replace our own hooks
        # instead of keeping a previous release's command beside the new one.
        existing_hooks[event] = _merge_flat_hook_entries(
            existing_hooks.get(event, []), entries
        )

    existing["hooks"] = existing_hooks

    cursor_dir.mkdir(parents=True, exist_ok=True)
    hooks_json_path.write_text(
        json.dumps(existing, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    logger.info("Wrote Cursor hooks config: %s", hooks_json_path)

    # --- Write hook scripts ---
    hooks_script_dir.mkdir(parents=True, exist_ok=True)
    scripts = _cursor_hook_scripts()

    for filename, content in scripts.items():
        script_path = hooks_script_dir / filename
        script_path.write_text(content, encoding="utf-8")
        # Make executable (owner rwx, group rx, other rx)
        script_path.chmod(stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
        logger.info("Wrote Cursor hook script: %s", script_path)

    return hooks_json_path


def install_qoder_skills(repo_root: Path) -> Path | None:
    """Install skills to Qoder's project-level skills directory.

    Qoder expects skills in .qoder/skills/{skillName}/SKILL.md format within the project.
    Loads the shipped skills from package resources. Source checkouts use their
    own top-level skills/ directory when wheel resources are not present.

    Args:
        repo_root: Target repository root directory.

    Returns:
        Path to the Qoder skills directory, or None if installation failed.
    """
    # Qoder skills directory (project-level)
    qoder_skills_dir = repo_root / ".qoder" / "skills"
    qoder_skills_dir.mkdir(parents=True, exist_ok=True)

    source_skills_dir = resources.files("code_review_graph").joinpath("_bundled_skills")
    if not source_skills_dir.is_dir():
        # Editable installs keep the same files beside the source package. Never
        # treat the target project's unrelated skills as CRG's bundled workflows.
        source_skills_dir = Path(__file__).resolve().parent.parent / "skills"
    if not source_skills_dir.is_dir():
        logger.warning("Bundled code-review-graph skills are unavailable.")
        return None

    installed_count = 0
    for skill_dir in source_skills_dir.iterdir():
        if skill_dir.is_dir():
            skill_file = skill_dir / "SKILL.md"
            if skill_file.is_file():
                target_dir = qoder_skills_dir / skill_dir.name
                target_dir.mkdir(parents=True, exist_ok=True)
                target_file = target_dir / "SKILL.md"
                target_file.write_text(skill_file.read_text(encoding="utf-8"), encoding="utf-8")
                logger.info("Installed Qoder skill: %s", skill_dir.name)
                installed_count += 1

    if installed_count > 0:
        logger.info("Installed %d skill(s) to %s", installed_count, qoder_skills_dir)
        return qoder_skills_dir
    return None


def install_hermes_skills(repo_root: Path) -> Path:
    """Install skills into Hermes Agent's user-level skills directory.

    Hermes discovers skills at ``<HERMES_HOME>/skills/<category>/<name>/SKILL.md``
    and uses the same ``name``/``description`` frontmatter as Claude Code, so
    :func:`generate_skills` writes them directly under a ``code-review-graph``
    category.
    """
    return generate_skills(repo_root, skills_dir=_hermes_home() / "skills" / "code-review-graph")


# --- OpenCode plugin ---


def _opencode_plugin_content() -> str:
    """Return TypeScript source for the OpenCode user-level plugin.

    The plugin hooks into three OpenCode events to mirror the Claude Code
    hook behaviors:

    1. ``file.edited`` — runs ``code-review-graph update --skip-flows``
    2. ``session.created`` — runs ``code-review-graph status``
    3. ``tool.execute.before`` — when the tool is a shell command starting
       with ``git commit``, runs ``code-review-graph detect-changes --brief``

    All handlers use try/catch so errors never break the editor session.
    The plugin uses Bun's ``$`` shell API (provided by OpenCode's plugin
    context) for subprocess execution.
    """
    return """\
import type { Plugin } from "@opencode-ai/plugin"

/**
 * code-review-graph plugin for OpenCode.
 *
 * Keeps the knowledge graph up-to-date and surfaces status
 * information automatically during coding sessions.
 *
 * Installed by: code-review-graph install --platform opencode
 */

// Helper: run a shell command quietly, swallowing errors.
async function run($: any, cmd: string): Promise<string> {
  try {
    const result = await $`${cmd}`.quiet()
    return result.stdout?.toString().trim() ?? ""
  } catch {
    return ""
  }
}

export default (app: any) => {
  // 1. Auto-update graph after file edits
  app.on("file.edited", async ({ $ }: { $: any }) => {
    try {
      await $`code-review-graph update --skip-flows`.quiet()
    } catch {
      // Swallow — graph may not be built yet for this project.
    }
  })

  // 2. Show graph status when a new session starts
  app.on("session.created", async ({ $ }: { $: any }) => {
    try {
      const result = await $`code-review-graph status`.quiet()
      const output = result.stdout?.toString().trim()
      if (output) {
        console.log("[code-review-graph]", output)
      }
    } catch {
      // Swallow — not every project has a graph.
    }
  })

  // 3. Detect changes before git commit commands
  app.on("tool.execute.before", async (ctx: any) => {
    try {
      const input = ctx?.input ?? ctx?.params ?? {}
      const cmd =
        input.command ?? input.cmd ?? input.content ?? ""
      if (typeof cmd === "string" && /^git\\s+commit/i.test(cmd)) {
        const result =
          await ctx.$`code-review-graph detect-changes --brief`.quiet()
        const output = result.stdout?.toString().trim()
        if (output) {
          console.log("[code-review-graph] Pre-commit analysis:\\n" + output)
        }
      }
    } catch {
      // Swallow — never block a commit.
    }
  })
}
"""


def install_opencode_plugin() -> Path:
    """Install the OpenCode user-level plugin for code-review-graph.

    Writes ``~/.config/opencode/plugins/crg-plugin.ts``.  Creates the
    directories if they don't exist.  If the file already exists it is
    overwritten (the plugin is self-contained and idempotent).

    Returns:
        Path to the plugin file that was written.
    """
    plugins_dir = Path.home() / ".config" / "opencode" / "plugins"
    plugin_path = plugins_dir / "crg-plugin.ts"

    plugins_dir.mkdir(parents=True, exist_ok=True)
    plugin_path.write_text(_opencode_plugin_content(), encoding="utf-8")
    logger.info("Wrote OpenCode plugin: %s", plugin_path)

    return plugin_path
