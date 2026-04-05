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
                existing = json.loads(config_path.read_text())
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
            config_path.write_text(json.dumps(existing, indent=2) + "\n")
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
            "- Use `find_large_functions` to identify complex code."
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
            "- Overall merge recommendation"
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
            "- Recent changes are the most common source of new issues."
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
            "- Run `find_large_functions` to identify decomposition targets."
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
        path.write_text(content)
        logger.info("Wrote skill: %s", path)

    return skills_dir


def generate_hooks_config() -> dict[str, Any]:
    """Generate Claude Code settings configuration.

    Returns a settings dict with permissions (auto-allow MCP tools) and
    hooks (PostToolUse, SessionStart, PreToolUse) for automatic graph
    updates and search enrichment.

    Returns:
        Dict suitable for merging into .claude/settings.json.
    """
    return {
        "permissions": {
            "allow": [
                "mcp__code-review-graph__*",
            ],
        },
        "hooks": {
            "PostToolUse": [
                {
                    "matcher": "Edit|Write|Bash",
                    "command": "code-review-graph update --quiet",
                    "timeout": 5000,
                },
            ],
            "SessionStart": [
                {
                    "command": "code-review-graph status --json",
                    "timeout": 3000,
                },
            ],
            "PreCommit": [
                {
                    "command": "code-review-graph detect-changes --brief",
                    "timeout": 10000,
                },
                {
                    "matcher": "Grep|Glob|Bash|Read",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "code-review-graph enrich",
                            "timeout": 5000,
                        }
                    ],
                },
            ],
        }
    }


def install_hooks(repo_root: Path) -> None:
    """Write hooks and permissions config to .claude/settings.json.

    Merges with existing settings, preserving user's own permission
    rules and non-hook configuration.

    Args:
        repo_root: Repository root directory.
    """
    settings_dir = repo_root / ".claude"
    settings_dir.mkdir(parents=True, exist_ok=True)
    settings_path = settings_dir / "settings.json"

    existing: dict[str, Any] = {}
    if settings_path.exists():
        try:
            existing = json.loads(settings_path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read existing %s: %s", settings_path, exc)

    config = generate_hooks_config()

    # Deep-merge permissions.allow (don't clobber user's existing rules)
    if "permissions" in config:
        existing_perms = existing.setdefault("permissions", {})
        existing_allow = existing_perms.setdefault("allow", [])
        for rule in config["permissions"]["allow"]:
            if rule not in existing_allow:
                existing_allow.append(rule)
        del config["permissions"]

    existing.update(config)

    settings_path.write_text(json.dumps(existing, indent=2) + "\n")
    logger.info("Wrote settings config: %s", settings_path)


_CLAUDE_MD_SECTION_MARKER = "<!-- code-review-graph MCP tools -->"

_CLAUDE_MD_SECTION = f"""{_CLAUDE_MD_SECTION_MARKER}
## MCP Tools: code-review-graph

This project has a structural knowledge graph that auto-updates on file changes.
Routine Grep/Glob/Read results are automatically enriched with callers, callees,
flows, and test coverage (via hooks -- no action needed).

Use these tools for **deep analysis** that enrichment doesn't cover:

| Tool | Use when |
|------|----------|
| `detect_changes` | Reviewing code changes -- risk-scored analysis |
| `get_review_context` | Token-efficient source snippets for review |
| `get_impact_radius` | Understanding blast radius of a change |
| `get_affected_flows` | Finding which execution paths are impacted |
| `get_architecture_overview` | High-level codebase structure |
| `refactor_tool` | Planning renames, finding dead code |
"""

_PLATFORM_SECTION_MARKER = "<!-- code-review-graph MCP tools -->"

_PLATFORM_SECTION = f"""{_PLATFORM_SECTION_MARKER}
## MCP Tools: code-review-graph

This project has a structural knowledge graph. Prefer these MCP tools over
Grep/Glob/Read for code exploration -- they give you structural context
(callers, dependents, test coverage) that file scanning cannot.

| Tool | Use when |
|------|----------|
| `semantic_search_nodes` | Finding functions/classes by name or keyword |
| `query_graph` | Tracing callers, callees, imports, tests, dependencies |
| `detect_changes` | Reviewing code changes -- risk-scored analysis |
| `get_review_context` | Token-efficient source snippets for review |
| `get_impact_radius` | Understanding blast radius of a change |
| `get_affected_flows` | Finding which execution paths are impacted |
| `get_architecture_overview` | High-level codebase structure |
| `refactor_tool` | Planning renames, finding dead code |

The graph auto-updates. Use `detect_changes` for code review,
`get_affected_flows` for impact, `query_graph` pattern="tests_for" for coverage.
"""


def _inject_instructions(file_path: Path, marker: str, section: str) -> bool:
    """Append an instruction section to a file if not already present.

    Idempotent: checks if the marker is already present before appending.
    Creates the file if it doesn't exist.

    Returns True if the file was modified.
    """
    existing = ""
    if file_path.exists():
        existing = file_path.read_text()

    if marker in existing:
        logger.info("%s already contains instructions, skipping.", file_path.name)
        return False

    separator = "\n" if existing and not existing.endswith("\n") else ""
    extra_newline = "\n" if existing else ""
    file_path.write_text(existing + separator + extra_newline + section)
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
    """Inject 'prefer graph tools' instructions into non-hook platform files.

    Generates AGENTS.md, GEMINI.md, .cursorrules, and .windsurfrules
    with stronger instructions since these platforms lack PreToolUse
    hooks for passive enrichment.

    Returns list of files that were created or updated.
    """
    updated: list[str] = []
    for label, filename in _PLATFORM_INSTRUCTION_FILES.items():
        path = repo_root / filename
        if _inject_instructions(path, _PLATFORM_SECTION_MARKER, _PLATFORM_SECTION):
            updated.append(label)
    return updated
