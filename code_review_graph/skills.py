"""Claude Code skills and hooks auto-install.

Generates Claude Code agent skill files, hooks configuration, and
CLAUDE.md integration for seamless code-review-graph usage.
Also supports multi-platform MCP server installation.
"""

from __future__ import annotations

import json
import logging
import platform
import shutil
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
}


def _build_server_entry(plat: dict[str, Any], key: str = "") -> dict[str, Any]:
    """Build the MCP server entry for a platform."""
    if shutil.which("uvx"):
        entry: dict[str, Any] = {
            "command": "uvx",
            "args": ["code-review-graph", "serve"],
        }
    else:
        entry = {
            "command": "code-review-graph",
            "args": ["serve"],
        }
    if plat["needs_type"]:
        entry["type"] = "stdio"
    if key == "opencode":
        entry["env"] = []
    return entry


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
        platforms_to_install = {
            k: v for k, v in PLATFORMS.items() if v["detect"]()
        }
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

        # Read existing config
        existing: dict[str, Any] = {}
        if config_path.exists():
            try:
                existing = json.loads(config_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                logger.warning("Invalid JSON in %s, will overwrite.", config_path)
                existing = {}

        if plat["format"] == "array":
            arr = existing.get(server_key, [])
            if not isinstance(arr, list):
                arr = []
            # Check if already present
            if any(
                isinstance(s, dict) and s.get("name") == "code-review-graph"
                for s in arr
            ):
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
            "- ALWAYS start with `get_minimal_context(task=\"<your task>\")` "
            "before any other graph tool.\n"
            "- Use `detail_level=\"minimal\"` on all calls. Only escalate to "
            "\"standard\" when minimal is insufficient.\n"
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
            "pattern=\"tests_for\" to check test coverage.\n"
            "4. Run `get_impact_radius` to understand the blast radius.\n"
            "5. For any untested changes, suggest specific test cases.\n\n"
            "### Output Format\n\n"
            "Provide findings grouped by risk level (high/medium/low) with:\n"
            "- What changed and why it matters\n"
            "- Test coverage status\n"
            "- Suggested improvements\n"
            "- Overall merge recommendation\n\n"
            "## Token Efficiency Rules\n"
            "- ALWAYS start with `get_minimal_context(task=\"<your task>\")` "
            "before any other graph tool.\n"
            "- Use `detail_level=\"minimal\"` on all calls. Only escalate to "
            "\"standard\" when minimal is insufficient.\n"
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
            "- ALWAYS start with `get_minimal_context(task=\"<your task>\")` "
            "before any other graph tool.\n"
            "- Use `detail_level=\"minimal\"` on all calls. Only escalate to "
            "\"standard\" when minimal is insufficient.\n"
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
            "1. Use `refactor_tool` with mode=\"suggest\" for community-driven "
            "refactoring suggestions.\n"
            "2. Use `refactor_tool` with mode=\"dead_code\" to find unreferenced code.\n"
            "3. For renames, use `refactor_tool` with mode=\"rename\" to preview all "
            "affected locations.\n"
            "4. Use `apply_refactor_tool` with the refactor_id to apply renames.\n"
            "5. After changes, run `detect_changes` to verify the refactoring impact.\n\n"
            "### Safety Checks\n\n"
            "- Always preview before applying (rename mode gives you an edit list).\n"
            "- Check `get_impact_radius` before major refactors.\n"
            "- Use `get_affected_flows` to ensure no critical paths are broken.\n"
            "- Run `find_large_functions` to identify decomposition targets.\n\n"
            "## Token Efficiency Rules\n"
            "- ALWAYS start with `get_minimal_context(task=\"<your task>\")` "
            "before any other graph tool.\n"
            "- Use `detail_level=\"minimal\"` on all calls. Only escalate to "
            "\"standard\" when minimal is insufficient.\n"
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


def generate_hooks_config() -> dict[str, Any]:
    """Generate Claude Code hooks configuration.

    Returns a hooks config dict with PostToolUse, SessionStart, and
    PreCommit hooks for automatic graph updates.

    Returns:
        Dict with hooks configuration suitable for .claude/settings.json.
    """
    return {
        "hooks": {
            "PostToolUse": [
                {
                    "matcher": "Edit|Write|Bash",
                    "command": "code-review-graph update --skip-flows",
                    "timeout": 5000,
                },
            ],
            "SessionStart": [
                {
                    "command": "code-review-graph status",
                    "timeout": 3000,
                },
            ],
            "PreCommit": [
                {
                    "command": "code-review-graph detect-changes --brief",
                    "timeout": 10000,
                },
            ],
        }
    }


def install_hooks(repo_root: Path) -> None:
    """Write hooks config to .claude/settings.json.

    Merges with existing settings if present, preserving non-hook
    configuration.

    Args:
        repo_root: Repository root directory.
    """
    settings_dir = repo_root / ".claude"
    settings_dir.mkdir(parents=True, exist_ok=True)
    settings_path = settings_dir / "settings.json"

    existing: dict[str, Any] = {}
    if settings_path.exists():
        try:
            existing = json.loads(settings_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read existing %s: %s", settings_path, exc)

    hooks_config = generate_hooks_config()
    existing.update(hooks_config)

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
|------|----------|
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
        existing = file_path.read_text(encoding="utf-8")

    if marker in existing:
        logger.info("%s already contains instructions, skipping.", file_path.name)
        return False

    separator = "\n" if existing and not existing.endswith("\n") else ""
    extra_newline = "\n" if existing else ""
    file_path.write_text(existing + separator + extra_newline + section, encoding="utf-8")
    logger.info("Appended MCP tools section to %s", file_path)
    return True


def inject_claude_md(repo_root: Path) -> None:
    """Append MCP tools section to CLAUDE.md."""
    _inject_instructions(
        repo_root / "CLAUDE.md", _CLAUDE_MD_SECTION_MARKER, _CLAUDE_MD_SECTION,
    )


# Cross-platform instruction files so every AI coding tool uses the graph.
_PLATFORM_INSTRUCTION_FILES = {
    "AGENTS.md": "AGENTS.md",       # Cursor, OpenCode, Antigravity
    "GEMINI.md": "GEMINI.md",       # Antigravity / Gemini CLI
    ".cursorrules": ".cursorrules",  # Cursor (legacy, widely used)
    ".windsurfrules": ".windsurfrules",  # Windsurf
}


def inject_platform_instructions(repo_root: Path) -> list[str]:
    """Inject 'use graph first' instructions into all platform rule files.

    Generates AGENTS.md, GEMINI.md, .cursorrules, and .windsurfrules
    with instructions to prefer code-review-graph MCP tools over
    manual file scanning.

    Returns list of files that were created or updated.
    """
    updated: list[str] = []
    for label, filename in _PLATFORM_INSTRUCTION_FILES.items():
        path = repo_root / filename
        if _inject_instructions(path, _CLAUDE_MD_SECTION_MARKER, _CLAUDE_MD_SECTION):
            updated.append(label)
    return updated
