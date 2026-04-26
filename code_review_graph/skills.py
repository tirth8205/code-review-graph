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
import shutil
import stat
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# --- Multi-platform MCP install ---


def _zed_settings_path() -> Path:
    """Return the Zed settings.json path for the current OS."""
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Zed" / "settings.json"
    return Path.home() / ".config" / "zed" / "settings.json"


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
        "config_path": lambda root: root / ".opencode.json",
        "key": "mcpServers",
        "detect": lambda: True,
        "format": "object",
        "needs_type": True,
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


def _build_server_entry(plat: dict[str, Any], key: str = "") -> dict[str, Any]:
    """Build the MCP server entry for a platform."""
    command, args = _detect_serve_command()
    entry: dict[str, Any] = {"command": command, "args": args}
    if plat["needs_type"]:
        entry["type"] = "stdio"
    if key == "opencode":
        entry["env"] = []
    return entry


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


def _merge_toml_mcp_server(
    config_path: Path,
    server_name: str,
    server_entry: dict[str, Any],
    dry_run: bool = False,
) -> bool:
    """Append a Codex MCP server section without clobbering the rest of the file."""
    section_header = f"[mcp_servers.{server_name}]"
    existing = ""
    if config_path.exists():
        existing = config_path.read_text(encoding="utf-8")
        if section_header in existing:
            return False

    section_lines = [section_header]
    for key, value in server_entry.items():
        section_lines.append(f"{key} = {_format_toml_value(value)}")
    section = "\n".join(section_lines) + "\n"

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
    if target == "all":
        platforms_to_install = {k: v for k, v in PLATFORMS.items() if v["detect"]()}
        # Workspace-level Kiro detection
        if "kiro" not in platforms_to_install and (repo_root / ".kiro").is_dir():
            platforms_to_install["kiro"] = PLATFORMS["kiro"]
    else:
        if target not in PLATFORMS:
            logger.error("Unknown platform: %s", target)
            return []
        platforms_to_install = {target: PLATFORMS[target]}

    configured: list[str] = []

    for key, plat in platforms_to_install.items():
        config_path: Path = plat["config_path"](repo_root)
        server_key = plat["key"]
        server_entry = _build_server_entry(plat, key=key)

        if plat["format"] == "toml":
            changed = _merge_toml_mcp_server(
                config_path,
                "code-review-graph",
                server_entry,
                dry_run=dry_run,
            )
            if not changed:
                print(f"  {plat['name']}: already configured in {config_path}")
                configured.append(plat["name"])
                continue
            if dry_run:
                print(f"  [dry-run] {plat['name']}: would write {config_path}")
            else:
                print(f"  {plat['name']}: configured {config_path}")
            configured.append(plat["name"])
            continue

        # Read existing config
        existing: dict[str, Any] = {}
        if config_path.exists():
            try:
                existing = json.loads(config_path.read_text(encoding="utf-8", errors="replace"))
            except (json.JSONDecodeError, OSError):
                logger.warning("Invalid JSON in %s, will overwrite.", config_path)
                existing = {}

        if plat["format"] == "array":
            arr = existing.get(server_key, [])
            if not isinstance(arr, list):
                arr = []
            # Check if already present
            if any(isinstance(s, dict) and s.get("name") == "code-review-graph" for s in arr):
                print(f"  {plat['name']}: already configured in {config_path}")
                configured.append(plat["name"])
                continue
            arr_entry = {"name": "code-review-graph", **server_entry}
            arr.append(arr_entry)
            existing[server_key] = arr
        else:
            servers = existing.get(server_key, {})
            if not isinstance(servers, dict):
                servers = {}
            if "code-review-graph" in servers:
                print(f"  {plat['name']}: already configured in {config_path}")
                configured.append(plat["name"])
                continue
            servers["code-review-graph"] = server_entry
            existing[server_key] = servers

        if dry_run:
            print(f"  [dry-run] {plat['name']}: would write {config_path}")
        else:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
            print(f"  {plat['name']}: configured {config_path}")

        configured.append(plat["name"])

    return configured


# --- Skill file contents ---

_SKILLS: dict[str, dict[str, str]] = {
    "explore-codebase.md": {
        "name": "Explore Codebase",
        "description": "Navigate and understand codebase structure using the knowledge graph",
        "body": (
            "## Explore Codebase\n\n"
            "Use the code-review-graph MCP tools to explore and understand the codebase.\n\n"
            "### Steps\n\n"
            "1. Run `list_graph_stats` to see overall codebase metrics.\n"
            "2. Run `get_architecture_overview` for high-level community structure.\n"
            "3. Use `list_communities` to find major modules, then `get_community` "
            "for details.\n"
            "4. Use `semantic_search_nodes` to find specific functions or classes.\n"
            "5. Use `query_graph` with patterns like `callers_of`, `callees_of`, "
            "`imports_of` to trace relationships.\n"
            "6. Use `list_flows` and `get_flow` to understand execution paths.\n\n"
            "### Tips\n\n"
            "- Start broad (stats, architecture) then narrow down to specific areas.\n"
            "- Use `children_of` on a file to see all its functions and classes.\n"
            "- Use `find_large_functions` to identify complex code.\n\n"
            "## Token Efficiency Rules\n"
            '- ALWAYS start with `get_minimal_context(task="<your task>")` '
            "before any other graph tool.\n"
            '- Use `detail_level="minimal"` on all calls. Only escalate to '
            '"standard" when minimal is insufficient.\n'
            "- Target: complete any review/debug/refactor task in ≤5 tool calls "
            "and ≤800 total output tokens."
        ),
    },
    "review-changes.md": {
        "name": "Review Changes",
        "description": "Perform a structured code review using change detection and impact",
        "body": (
            "## Review Changes\n\n"
            "Perform a thorough, risk-aware code review using the knowledge graph.\n\n"
            "### Steps\n\n"
            "1. Run `detect_changes` to get risk-scored change analysis.\n"
            "2. Run `get_affected_flows` to find impacted execution paths.\n"
            "3. For each high-risk function, run `query_graph` with "
            'pattern="tests_for" to check test coverage.\n'
            "4. Run `get_impact_radius` to understand the blast radius.\n"
            "5. For any untested changes, suggest specific test cases.\n\n"
            "### Output Format\n\n"
            "Provide findings grouped by risk level (high/medium/low) with:\n"
            "- What changed and why it matters\n"
            "- Test coverage status\n"
            "- Suggested improvements\n"
            "- Overall merge recommendation\n\n"
            "## Token Efficiency Rules\n"
            '- ALWAYS start with `get_minimal_context(task="<your task>")` '
            "before any other graph tool.\n"
            '- Use `detail_level="minimal"` on all calls. Only escalate to '
            '"standard" when minimal is insufficient.\n'
            "- Target: complete any review/debug/refactor task in ≤5 tool calls "
            "and ≤800 total output tokens."
        ),
    },
    "debug-issue.md": {
        "name": "Debug Issue",
        "description": "Systematically debug issues using graph-powered code navigation",
        "body": (
            "## Debug Issue\n\n"
            "Use the knowledge graph to systematically trace and debug issues.\n\n"
            "### Steps\n\n"
            "1. Use `semantic_search_nodes` to find code related to the issue.\n"
            "2. Use `query_graph` with `callers_of` and `callees_of` to trace "
            "call chains.\n"
            "3. Use `get_flow` to see full execution paths through suspected areas.\n"
            "4. Run `detect_changes` to check if recent changes caused the issue.\n"
            "5. Use `get_impact_radius` on suspected files to see what else is affected.\n\n"
            "### Tips\n\n"
            "- Check both callers and callees to understand the full context.\n"
            "- Look at affected flows to find the entry point that triggers the bug.\n"
            "- Recent changes are the most common source of new issues.\n\n"
            "## Token Efficiency Rules\n"
            '- ALWAYS start with `get_minimal_context(task="<your task>")` '
            "before any other graph tool.\n"
            '- Use `detail_level="minimal"` on all calls. Only escalate to '
            '"standard" when minimal is insufficient.\n'
            "- Target: complete any review/debug/refactor task in ≤5 tool calls "
            "and ≤800 total output tokens."
        ),
    },
    "refactor-safely.md": {
        "name": "Refactor Safely",
        "description": "Plan and execute safe refactoring using dependency analysis",
        "body": (
            "## Refactor Safely\n\n"
            "Use the knowledge graph to plan and execute refactoring with confidence.\n\n"
            "### Steps\n\n"
            '1. Use `refactor_tool` with mode="suggest" for community-driven '
            "refactoring suggestions.\n"
            '2. Use `refactor_tool` with mode="dead_code" to find unreferenced code.\n'
            '3. For renames, use `refactor_tool` with mode="rename" to preview all '
            "affected locations.\n"
            "4. Use `apply_refactor_tool` with the refactor_id to apply renames.\n"
            "5. After changes, run `detect_changes` to verify the refactoring impact.\n\n"
            "### Safety Checks\n\n"
            "- Always preview before applying (rename mode gives you an edit list).\n"
            "- Check `get_impact_radius` before major refactors.\n"
            "- Use `get_affected_flows` to ensure no critical paths are broken.\n"
            "- Run `find_large_functions` to identify decomposition targets.\n\n"
            "## Token Efficiency Rules\n"
            '- ALWAYS start with `get_minimal_context(task="<your task>")` '
            "before any other graph tool.\n"
            '- Use `detail_level="minimal"` on all calls. Only escalate to '
            '"standard" when minimal is insufficient.\n'
            "- Target: complete any review/debug/refactor task in ≤5 tool calls "
            "and ≤800 total output tokens."
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
        path = skills_dir / filename
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
    """
    repo_arg = json.dumps(repo_root.resolve().as_posix())
    return {
        "hooks": {
            "PostToolUse": [
                {
                    "matcher": "Edit|Write|Bash",
                    "hooks": [
                        {
                            "type": "command",
                            "command": (
                                "git rev-parse --git-dir >/dev/null 2>&1"
                                f" && code-review-graph update --skip-flows"
                                f" --repo {repo_arg}"
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
                                "git rev-parse --git-dir >/dev/null 2>&1"
                                f" && code-review-graph status --repo {repo_arg}"
                                " || echo 'Not a git repo, skipping'"
                            ),
                            "timeout": 10,
                        },
                    ],
                },
            ],
        }
    }


def install_git_hook(repo_root: Path) -> Path | None:
    """Install a git pre-commit hook that prints a risk summary before each commit.

    Called automatically by ``code-review-graph install``
    Creates ``.git/hooks/pre-commit`` if it doesn't exist, or appends to an
    existing one — preserving any hooks already there. Returns None when no
    ``.git`` directory is found.
    """
    script = """\
#!/bin/sh
# Installed by code-review-graph. Remove this file to disable pre-commit graph checks.
if command -v code-review-graph >/dev/null 2>&1; then
    code-review-graph update || true
    code-review-graph detect-changes --brief || true
fi
"""
    marker = "code-review-graph detect-changes"

    git_dir = repo_root / ".git"
    if not git_dir.is_dir():
        logger.warning("No .git directory found at %s — skipping git hook install.", repo_root)
        return None

    hook_path = git_dir / "hooks" / "pre-commit"
    hook_path.parent.mkdir(exist_ok=True)

    if hook_path.exists():
        existing = hook_path.read_text(encoding="utf-8")
        if marker in existing:
            return hook_path
        hook_path.write_text(existing.rstrip("\n") + "\n" + script, encoding="utf-8")
    else:
        hook_path.write_text(script, encoding="utf-8")

    hook_path.chmod(0o755)
    logger.info("Wrote git pre-commit hook: %s", hook_path)
    return hook_path


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

    hooks_config = generate_hooks_config(repo_root)
    existing_hooks = existing.get("hooks", {})
    if not isinstance(existing_hooks, dict):
        logger.warning("Existing hooks config is not a dict; replacing with defaults")
        existing_hooks = {}

    merged_hooks = dict(existing_hooks)
    for hook_name, hook_entries in hooks_config.get("hooks", {}).items():
        if isinstance(merged_hooks.get(hook_name), list):
            merged_list = list(merged_hooks[hook_name])
            for entry in hook_entries:
                if entry not in merged_list:
                    merged_list.append(entry)
            merged_hooks[hook_name] = merged_list
        else:
            merged_hooks[hook_name] = hook_entries

    existing["hooks"] = merged_hooks

    settings_path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    logger.info("Wrote hooks config: %s", settings_path)


_CLAUDE_MD_SECTION_MARKER = "<!-- code-review-graph MCP tools -->"

_CLAUDE_MD_SECTION = f"""{_CLAUDE_MD_SECTION_MARKER}
## MCP Tools: code-review-graph

**IMPORTANT: This project has a knowledge graph. ALWAYS use the
code-review-graph MCP tools BEFORE using Grep/Glob/Read to explore
the codebase.** The graph is faster, cheaper (fewer tokens), and gives
you structural context (callers, dependents, test coverage) that file
scanning cannot.

### When to use graph tools FIRST

- **Exploring code**: `semantic_search_nodes` or `query_graph` instead of Grep
- **Understanding impact**: `get_impact_radius` instead of manually tracing imports
- **Code review**: `detect_changes` + `get_review_context` instead of reading entire files
- **Finding relationships**: `query_graph` with callers_of/callees_of/imports_of/tests_for
- **Architecture questions**: `get_architecture_overview` + `list_communities`

Fall back to Grep/Glob/Read **only** when the graph doesn't cover what you need.

### Key Tools

| Tool | Use when |
| ------ | ---------- |
| `detect_changes` | Reviewing code changes — gives risk-scored analysis |
| `get_review_context` | Need source snippets for review — token-efficient |
| `get_impact_radius` | Understanding blast radius of a change |
| `get_affected_flows` | Finding which execution paths are impacted |
| `query_graph` | Tracing callers, callees, imports, tests, dependencies |
| `semantic_search_nodes` | Finding functions/classes by name or keyword |
| `get_architecture_overview` | Understanding high-level codebase structure |
| `refactor_tool` | Planning renames, finding dead code |

### Workflow

1. The graph auto-updates on file changes (via hooks).
2. Use `detect_changes` for code review.
3. Use `get_affected_flows` to understand impact.
4. Use `query_graph` pattern=\"tests_for\" to check coverage.
"""


def _inject_instructions(file_path: Path, marker: str, section: str) -> bool:
    """Append an instruction section to a file if not already present.

    Idempotent: checks if the marker is already present before appending.
    Creates the file if it doesn't exist.

    Returns True if the file was modified.
    """
    existing = ""
    if file_path.exists():
        existing = file_path.read_text(encoding="utf-8", errors="replace")

    if marker in existing:
        logger.info("%s already contains instructions, skipping.", file_path.name)
        return False

    separator = "\n" if existing and not existing.endswith("\n") else ""
    extra_newline = "\n" if existing else ""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(existing + separator + extra_newline + section, encoding="utf-8")
    logger.info("Appended MCP tools section to %s", file_path)
    return True


def inject_claude_md(repo_root: Path) -> None:
    """Append MCP tools section to CLAUDE.md."""
    _inject_instructions(
        repo_root / "CLAUDE.md",
        _CLAUDE_MD_SECTION_MARKER,
        _CLAUDE_MD_SECTION,
    )


# Cross-platform instruction files and which platforms own each one.
# Used to filter writes when the user passes --platform <X>: only files
# whose owner set includes the target (or "all") are written.
_PLATFORM_INSTRUCTION_FILES: dict[str, tuple[str, ...]] = {
    "AGENTS.md": ("cursor", "opencode", "antigravity"),
    "GEMINI.md": ("antigravity", "gemini-cli"),
    ".cursorrules": ("cursor",),
    ".windsurfrules": ("windsurf",),
    "QODER.md": ("qoder",),
    ".kiro/steering/code-review-graph.md": ("kiro",),
}


# --- Gemini CLI hooks + skills (workspace-level: .gemini/) ---


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

CRG_MSG="$msg" python3 -c 'import json, os; print(json.dumps({"systemMessage": os.environ.get("CRG_MSG",""), "suppressOutput": True}))' 2>/dev/null || echo '{"suppressOutput": true}'
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

    session_start_path = hooks_dir / "crg-session-start.sh"
    session_start_path.write_text(session_start_script, encoding="utf-8")
    session_start_path.chmod(0o755)

    update_path = hooks_dir / "crg-update.sh"
    update_path.write_text(update_script, encoding="utf-8")
    update_path.chmod(0o755)

    hooks_obj = existing.get("hooks", {})
    if not isinstance(hooks_obj, dict):
        hooks_obj = {}

    def _ensure_group(event_name: str, matcher: str, hook_command: str, name: str, timeout: int) -> None:
        arr = hooks_obj.get(event_name, [])
        if not isinstance(arr, list):
            arr = []

        # De-duplicate by command (and type) inside nested hooks list.
        def _group_has_command(group: Any) -> bool:
            if not isinstance(group, dict):
                return False
            nested = group.get("hooks", [])
            if not isinstance(nested, list):
                return False
            for h in nested:
                if isinstance(h, dict) and h.get("type") == "command" and h.get("command") == hook_command:
                    return True
            return False

        if any(_group_has_command(g) for g in arr):
            hooks_obj[event_name] = arr
            return

        arr.append(
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
        )
        hooks_obj[event_name] = arr

    _ensure_group(
        event_name="SessionStart",
        matcher="",
        hook_command="bash .gemini/hooks/crg-session-start.sh",
        name="code-review-graph status",
        timeout=10_000,
    )
    _ensure_group(
        event_name="AfterTool",
        matcher="write_file|replace",
        hook_command="bash .gemini/hooks/crg-update.sh",
        name="code-review-graph update",
        timeout=30_000,
    )

    existing["hooks"] = hooks_obj
    settings_path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
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


def inject_platform_instructions(repo_root: Path, target: str = "all") -> list[str]:
    """Inject 'use graph first' instructions into platform rule files.

    Writes AGENTS.md, GEMINI.md, .cursorrules, and/or .windsurfrules
    depending on ``target``:

    - ``"all"`` (default): writes every file — matches pre-filter behavior.
    - ``"claude"``: writes nothing (CLAUDE.md is handled by ``inject_claude_md``).
    - any other platform key (``cursor``, ``windsurf``, ``antigravity``,
      ``opencode``): writes only the files associated with that platform.

    Returns list of filenames that were created or updated.
    """
    updated: list[str] = []
    for filename, owners in _PLATFORM_INSTRUCTION_FILES.items():
        if target != "all" and target not in owners:
            continue
        path = repo_root / filename
        if _inject_instructions(path, _CLAUDE_MD_SECTION_MARKER, _CLAUDE_MD_SECTION):
            updated.append(filename)
    return updated


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
        event_hooks = existing_hooks.get(event, [])
        if not isinstance(event_hooks, list):
            event_hooks = []
        # De-duplicate: skip if a hook with the same command already exists
        existing_commands = {h.get("command", "") for h in event_hooks if isinstance(h, dict)}
        for entry in entries:
            if entry["command"] not in existing_commands:
                event_hooks.append(entry)
        existing_hooks[event] = event_hooks

    existing["hooks"] = existing_hooks

    cursor_dir.mkdir(parents=True, exist_ok=True)
    hooks_json_path.write_text(
        json.dumps(existing, indent=2) + "\n",
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
    This function copies the project's skills/ directory contents to that location.

    Args:
        repo_root: Repository root directory (where the skills/ folder is located).

    Returns:
        Path to the Qoder skills directory, or None if installation failed.
    """
    # Qoder skills directory (project-level)
    qoder_skills_dir = repo_root / ".qoder" / "skills"
    qoder_skills_dir.mkdir(parents=True, exist_ok=True)

    # Source skills directory in the project
    source_skills_dir = repo_root / "skills"
    if not source_skills_dir.exists():
        logger.warning("No skills/ directory found in %s", repo_root)
        return None

    installed_count = 0
    for skill_dir in source_skills_dir.iterdir():
        if skill_dir.is_dir():
            skill_file = skill_dir / "SKILL.md"
            if skill_file.exists():
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
