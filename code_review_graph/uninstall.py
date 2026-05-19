"""Reverses the artifacts installed by ``code-review-graph install``.

This module powers the ``code-review-graph uninstall`` CLI command. It walks
the full inventory of files that ``install`` creates and either deletes them
(when the file is wholly owned by code-review-graph) or surgically removes
our entries from shared configuration files (e.g. ``.mcp.json``,
``.codex/config.toml``, ``CLAUDE.md``) — preserving any other content the
user has added.

The design follows three principles:

1. **Conservative by default.** A dry-run summary is printed before anything
   is removed; the user must confirm (or pass ``--yes``).
2. **Surgical edits for shared configs.** We never delete a file that may
   contain non-CRG entries. Only the ``code-review-graph`` entry is
   removed; if removing it leaves the container empty we also tidy that up.
3. **Comprehensive inventory.** Every path that any ``install_*`` helper in
   :mod:`code_review_graph.skills` can write is enumerated here so a single
   ``uninstall`` cleans up the entire footprint.

The implementation is intentionally side-effect-free until ``run()`` is
called and the caller passes ``dry_run=False``. Each step records itself
in the returned :class:`UninstallReport` so the CLI can print a structured
summary.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Marker used by skills.py when appending the MCP tools section to
# CLAUDE.md and the other instruction files. The marker line and
# everything below it (to EOF) is what install writes; we strip the
# same span on uninstall.
_INSTRUCTION_MARKER = "<!-- code-review-graph MCP tools -->"

# Marker used in the git pre-commit hook so we can recognise our own
# entry and remove it without nuking user-defined hooks.
_GIT_HOOK_MARKER = "# Installed by code-review-graph."

# All instruction files install can touch, mirroring skills.py.
_INSTRUCTION_FILES: tuple[str, ...] = (
    "CLAUDE.md",
    "AGENTS.md",
    "GEMINI.md",
    ".cursorrules",
    ".windsurfrules",
    "QODER.md",
    ".kiro/steering/code-review-graph.md",
    ".github/code-review-graph.instruction.md",
)


@dataclass
class UninstallReport:
    """Structured record of what an uninstall did (or would do)."""

    removed_paths: list[str] = field(default_factory=list)
    edited_paths: list[str] = field(default_factory=list)
    skipped_paths: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def total_actions(self) -> int:
        return len(self.removed_paths) + len(self.edited_paths)


# ---------------------------------------------------------------------------
# Path discovery
# ---------------------------------------------------------------------------


def _zed_settings_path() -> Path:
    """Mirror :func:`code_review_graph.skills._zed_settings_path`."""
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Zed" / "settings.json"
    return Path.home() / ".config" / "zed" / "settings.json"


def _user_artifacts() -> list[tuple[Path, str]]:
    """Return ``[(path, kind)]`` for every user-level artifact install may write.

    ``kind`` is one of:

    * ``"dir"``         — directory to delete wholesale.
    * ``"file"``        — file to delete wholesale (we own the whole file).
    * ``"json_mcp"``    — JSON config; remove our entry under one of the
      standard MCP keys (``mcpServers`` / ``servers`` / ``context_servers``).
    * ``"toml_codex"``  — ``~/.codex/config.toml`` — remove ``[mcp_servers.code-review-graph]``.
    * ``"json_hooks"``  — generic JSON hooks file; remove entries whose
      ``command`` references ``code-review-graph``.
    """
    home = Path.home()
    return [
        # The user-level registry / daemon state / logs directory. This is
        # entirely ours, so it goes wholesale.
        (home / ".code-review-graph", "dir"),
        # MCP server configs (JSON).
        (home / ".cursor" / "mcp.json", "json_mcp"),
        (home / ".codeium" / "windsurf" / "mcp_config.json", "json_mcp"),
        (_zed_settings_path(), "json_mcp"),
        (home / ".continue" / "config.json", "json_mcp"),
        (home / ".gemini" / "antigravity" / "mcp_config.json", "json_mcp"),
        (home / ".qwen" / "settings.json", "json_mcp"),
        (home / ".copilot" / "mcp-config.json", "json_mcp"),
        # Codex MCP config (TOML).
        (home / ".codex" / "config.toml", "toml_codex"),
        # Codex / Cursor hooks (JSON).
        (home / ".codex" / "hooks.json", "json_hooks"),
        (home / ".cursor" / "hooks.json", "json_hooks"),
        # Cursor hook scripts directory — wholly ours (install always
        # writes the three scripts here).
        (home / ".cursor" / "hooks", "dir"),
        # OpenCode plugin file — wholly ours.
        (home / ".config" / "opencode" / "plugins" / "crg-plugin.ts", "file"),
    ]


def _per_repo_artifacts(repo_root: Path) -> list[tuple[Path, str]]:
    """Return ``[(path, kind)]`` for every per-repo artifact."""
    return [
        # Local graph database directory (the big one — usually the most
        # storage the user reclaims).
        (repo_root / ".code-review-graph", "dir"),
        # Legacy DB file from older versions.
        (repo_root / ".code-review-graph.db", "file"),
        (repo_root / ".code-review-graph.db-wal", "file"),
        (repo_root / ".code-review-graph.db-shm", "file"),
        # Workspace-level MCP configs.
        (repo_root / ".mcp.json", "json_mcp"),
        (repo_root / ".opencode.json", "json_mcp"),
        (repo_root / ".cursor" / "mcp.json", "json_mcp"),
        (repo_root / ".qoder" / "mcp.json", "json_mcp"),
        (repo_root / ".kiro" / "settings" / "mcp.json", "json_mcp"),
        (repo_root / ".vscode" / "mcp.json", "json_mcp"),
        # Hooks (workspace-level).
        (repo_root / ".claude" / "settings.json", "json_hooks"),
        (repo_root / ".qoder" / "settings.json", "json_hooks"),
        (repo_root / ".gemini" / "settings.json", "json_hooks"),
        # Generated skills directories — wholly ours.
        (repo_root / ".claude" / "skills" / "explore-codebase", "dir"),
        (repo_root / ".claude" / "skills" / "review-changes", "dir"),
        (repo_root / ".claude" / "skills" / "debug-issue", "dir"),
        (repo_root / ".claude" / "skills" / "refactor-safely", "dir"),
        (repo_root / ".qoder" / "skills", "dir"),
        (repo_root / ".gemini" / "skills" / "explore-codebase", "dir"),
        (repo_root / ".gemini" / "skills" / "review-changes", "dir"),
        (repo_root / ".gemini" / "skills" / "debug-issue", "dir"),
        (repo_root / ".gemini" / "skills" / "refactor-safely", "dir"),
        # Gemini CLI hook scripts.
        (repo_root / ".gemini" / "hooks" / "crg-session-start.sh", "file"),
        (repo_root / ".gemini" / "hooks" / "crg-update.sh", "file"),
        # Pre-commit hook (surgical — preserve user hooks).
        (repo_root / ".git" / "hooks" / "pre-commit", "git_hook"),
    ] + [
        (repo_root / name, "instruction") for name in _INSTRUCTION_FILES
    ]


# ---------------------------------------------------------------------------
# Surgical edits
# ---------------------------------------------------------------------------

_MCP_KEYS = ("mcpServers", "servers", "context_servers")
_ENTRY_NAME = "code-review-graph"


def _strip_jsonc(raw: str) -> str:
    """Strip ``//`` comments and trailing commas (Zed-style JSONC)."""
    stripped = re.sub(r"//.*?$", "", raw, flags=re.MULTILINE)
    stripped = re.sub(r",(\s*[}\]])", r"\1", stripped)
    return stripped


def _remove_mcp_entry(path: Path, report: UninstallReport, *, dry_run: bool) -> None:
    """Remove ``code-review-graph`` from a JSON MCP config file.

    Preserves all other servers and top-level keys. If the file becomes
    *empty of meaningful content* (i.e. only an empty ``mcpServers``
    object remains), we leave it alone — the user may want to keep their
    empty file.
    """
    if not path.exists():
        return
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
        data = json.loads(_strip_jsonc(raw))
    except (json.JSONDecodeError, OSError) as exc:
        report.skipped_paths.append(f"{path} (unparseable: {exc})")
        return

    changed = False
    for key in _MCP_KEYS:
        if key not in data:
            continue
        container = data[key]
        if isinstance(container, dict) and _ENTRY_NAME in container:
            del container[_ENTRY_NAME]
            changed = True
        elif isinstance(container, list):
            new_list = [
                s for s in container
                if not (isinstance(s, dict) and s.get("name") == _ENTRY_NAME)
            ]
            if len(new_list) != len(container):
                data[key] = new_list
                changed = True

    if not changed:
        return

    if dry_run:
        report.edited_paths.append(f"{path} (would remove '{_ENTRY_NAME}' MCP entry)")
        return

    try:
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        report.edited_paths.append(f"{path} (removed '{_ENTRY_NAME}' MCP entry)")
    except OSError as exc:
        report.errors.append(f"{path}: write failed ({exc})")


def _remove_codex_toml_entry(
    path: Path, report: UninstallReport, *, dry_run: bool,
) -> None:
    """Remove ``[mcp_servers.code-review-graph]`` (and its body) from ``config.toml``.

    The Codex config is hand-edited TOML with our own
    ``_merge_toml_mcp_server`` writer in :mod:`skills`. To stay
    dependency-light we do a textual removal of the exact section
    ``[mcp_servers.code-review-graph]`` and every following line until the
    next ``[section]`` header or EOF.
    """
    if not path.exists():
        return
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        report.errors.append(f"{path}: read failed ({exc})")
        return

    header = f"[mcp_servers.{_ENTRY_NAME}]"
    if header not in text:
        return

    lines = text.splitlines(keepends=True)
    out: list[str] = []
    dropping = False
    for line in lines:
        stripped = line.strip()
        if stripped == header:
            dropping = True
            continue
        if dropping and stripped.startswith("[") and stripped.endswith("]"):
            dropping = False
        if not dropping:
            out.append(line)

    new_text = "".join(out).rstrip() + "\n"
    if new_text == text:
        return

    if dry_run:
        report.edited_paths.append(f"{path} (would remove [mcp_servers.{_ENTRY_NAME}])")
        return

    try:
        path.write_text(new_text, encoding="utf-8")
        report.edited_paths.append(f"{path} (removed [mcp_servers.{_ENTRY_NAME}])")
    except OSError as exc:
        report.errors.append(f"{path}: write failed ({exc})")


def _hook_entry_is_ours(entry: Any) -> bool:
    """Return True if a hooks-config entry was installed by us.

    Hooks come in two shapes across the various platforms:

    * ``{"command": "...code-review-graph..."}``
    * ``{"matcher": "...", "hooks": [{"command": "...code-review-graph..."}]}``
    """
    if not isinstance(entry, dict):
        return False
    cmd = entry.get("command")
    if isinstance(cmd, str) and "code-review-graph" in cmd:
        return True
    nested = entry.get("hooks")
    if isinstance(nested, list):
        for h in nested:
            if isinstance(h, dict):
                hc = h.get("command", "")
                if isinstance(hc, str) and "code-review-graph" in hc:
                    return True
    return False


def _remove_hook_entries(
    path: Path, report: UninstallReport, *, dry_run: bool,
) -> None:
    """Remove our hook entries from a JSON hooks config (settings.json / hooks.json)."""
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError) as exc:
        report.skipped_paths.append(f"{path} (unparseable: {exc})")
        return

    hooks_obj = data.get("hooks")
    if not isinstance(hooks_obj, dict):
        return

    changed = False
    for event_name, arr in list(hooks_obj.items()):
        if not isinstance(arr, list):
            continue
        new_arr = [entry for entry in arr if not _hook_entry_is_ours(entry)]
        if len(new_arr) != len(arr):
            hooks_obj[event_name] = new_arr
            changed = True
        if not new_arr:
            del hooks_obj[event_name]
            changed = True

    if not changed:
        return

    # Tidy up an empty top-level "hooks" object so we don't leave
    # noise behind.
    if not hooks_obj:
        del data["hooks"]

    if dry_run:
        report.edited_paths.append(f"{path} (would remove code-review-graph hook entries)")
        return

    try:
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        report.edited_paths.append(f"{path} (removed code-review-graph hook entries)")
    except OSError as exc:
        report.errors.append(f"{path}: write failed ({exc})")


def _remove_instruction_section(
    path: Path, report: UninstallReport, *, dry_run: bool,
) -> None:
    """Remove the ``<!-- code-review-graph MCP tools -->`` section.

    ``install`` always appends the section to EOF and never inserts it in
    the middle, so we delete from the marker line to EOF. If after the
    edit the file is empty (or whitespace-only), we remove the file.
    """
    if not path.exists():
        return
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        report.errors.append(f"{path}: read failed ({exc})")
        return

    if _INSTRUCTION_MARKER not in text:
        return

    idx = text.find(_INSTRUCTION_MARKER)
    new_text = text[:idx].rstrip() + "\n"

    if dry_run:
        if new_text.strip():
            report.edited_paths.append(f"{path} (would remove MCP tools section)")
        else:
            report.removed_paths.append(f"{path} (would delete — only contained our section)")
        return

    try:
        if not new_text.strip():
            path.unlink()
            report.removed_paths.append(str(path))
        else:
            path.write_text(new_text, encoding="utf-8")
            report.edited_paths.append(f"{path} (removed MCP tools section)")
    except OSError as exc:
        report.errors.append(f"{path}: write failed ({exc})")


def _remove_git_hook(
    path: Path, report: UninstallReport, *, dry_run: bool,
) -> None:
    """Remove our pre-commit block from ``.git/hooks/pre-commit``.

    The hook installer either creates a fresh file (whose first line is
    a shebang followed by our marker block) or appends our block to an
    existing hook. We recognise our block by the ``_GIT_HOOK_MARKER``
    comment line; we drop that line and every subsequent line until we
    leave the contiguous block we added.
    """
    if not path.exists():
        return
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        report.errors.append(f"{path}: read failed ({exc})")
        return

    if _GIT_HOOK_MARKER not in text:
        return

    lines = text.splitlines(keepends=True)
    out: list[str] = []
    dropping = False
    for line in lines:
        if _GIT_HOOK_MARKER in line:
            dropping = True
            continue
        if dropping:
            # Our block runs from the marker through the next blank line
            # (or EOF). The installed snippet is:
            #
            #   # Installed by code-review-graph. ...
            #   if command -v code-review-graph >/dev/null 2>&1; then
            #       code-review-graph update || true
            #       code-review-graph detect-changes --brief || true
            #   fi
            #
            # so we stop dropping after the ``fi`` line.
            if line.strip() == "fi":
                dropping = False
                continue
            continue
        out.append(line)

    new_text = "".join(out).rstrip()
    # If only the shebang remains (or nothing), delete the file: a hook
    # with only ``#!/bin/sh`` is functionally empty and creates a
    # confusing artifact.
    meaningful = "\n".join(
        ln for ln in new_text.splitlines() if ln.strip() and not ln.strip().startswith("#!")
    ).strip()

    if dry_run:
        if meaningful:
            report.edited_paths.append(f"{path} (would remove code-review-graph block)")
        else:
            report.removed_paths.append(f"{path} (would delete — only contained our block)")
        return

    try:
        if not meaningful:
            path.unlink()
            report.removed_paths.append(str(path))
        else:
            path.write_text(new_text + "\n", encoding="utf-8")
            report.edited_paths.append(f"{path} (removed code-review-graph block)")
    except OSError as exc:
        report.errors.append(f"{path}: write failed ({exc})")


def _remove_gitignore_entry(
    repo_root: Path, report: UninstallReport, *, dry_run: bool,
) -> None:
    """Remove the ``.code-review-graph/`` line (and its banner) from .gitignore.

    Mirrors :func:`code_review_graph.incremental.ensure_repo_gitignore_excludes_crg`,
    which inserts ``# Added by code-review-graph`` followed by
    ``.code-review-graph/``.
    """
    gi = repo_root / ".gitignore"
    if not gi.exists():
        return
    try:
        text = gi.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        report.errors.append(f"{gi}: read failed ({exc})")
        return

    lines = text.splitlines()
    new_lines: list[str] = []
    skip_next = False
    changed = False
    for line in lines:
        if skip_next:
            skip_next = False
            if line.strip() in (".code-review-graph", ".code-review-graph/"):
                changed = True
                continue
            new_lines.append(line)
            continue
        if line.strip() == "# Added by code-review-graph":
            # The very next line is the entry we wrote; drop both.
            skip_next = True
            changed = True
            continue
        if line.strip() in (".code-review-graph", ".code-review-graph/"):
            changed = True
            continue
        new_lines.append(line)

    if not changed:
        return

    new_text = "\n".join(new_lines).rstrip() + "\n"

    if dry_run:
        if new_text.strip():
            report.edited_paths.append(f"{gi} (would remove .code-review-graph/ entry)")
        else:
            report.removed_paths.append(f"{gi} (would delete — file would be empty)")
        return

    try:
        if not new_text.strip():
            gi.unlink()
            report.removed_paths.append(str(gi))
        else:
            gi.write_text(new_text, encoding="utf-8")
            report.edited_paths.append(f"{gi} (removed .code-review-graph/ entry)")
    except OSError as exc:
        report.errors.append(f"{gi}: write failed ({exc})")


# ---------------------------------------------------------------------------
# Whole-file/dir removal
# ---------------------------------------------------------------------------


def _remove_path(
    path: Path, kind: str, report: UninstallReport, *, dry_run: bool,
) -> None:
    """Delete a path that is wholly owned by code-review-graph."""
    if not path.exists() and not path.is_symlink():
        return

    if dry_run:
        report.removed_paths.append(f"{path} ({kind})")
        return

    try:
        if kind == "dir":
            shutil.rmtree(path)
        else:
            path.unlink()
        report.removed_paths.append(str(path))
    except OSError as exc:
        report.errors.append(f"{path}: remove failed ({exc})")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _process_repo(
    repo_root: Path,
    report: UninstallReport,
    *,
    dry_run: bool,
    keep_data: bool,
) -> None:
    """Walk per-repo artifacts and remove or edit each one."""
    for path, kind in _per_repo_artifacts(repo_root):
        if keep_data and kind == "dir" and path.name == ".code-review-graph":
            report.skipped_paths.append(f"{path} (kept: --keep-data)")
            continue
        if kind in ("dir", "file"):
            _remove_path(path, kind, report, dry_run=dry_run)
        elif kind == "json_mcp":
            _remove_mcp_entry(path, report, dry_run=dry_run)
        elif kind == "json_hooks":
            _remove_hook_entries(path, report, dry_run=dry_run)
        elif kind == "instruction":
            _remove_instruction_section(path, report, dry_run=dry_run)
        elif kind == "git_hook":
            _remove_git_hook(path, report, dry_run=dry_run)
    _remove_gitignore_entry(repo_root, report, dry_run=dry_run)


def _process_user(
    report: UninstallReport,
    *,
    dry_run: bool,
    keep_data: bool,
) -> None:
    """Walk user-level artifacts and remove or edit each one."""
    for path, kind in _user_artifacts():
        if keep_data and kind == "dir" and path.name == ".code-review-graph":
            report.skipped_paths.append(f"{path} (kept: --keep-data)")
            continue
        if kind in ("dir", "file"):
            _remove_path(path, kind, report, dry_run=dry_run)
        elif kind == "json_mcp":
            _remove_mcp_entry(path, report, dry_run=dry_run)
        elif kind == "json_hooks":
            _remove_hook_entries(path, report, dry_run=dry_run)
        elif kind == "toml_codex":
            _remove_codex_toml_entry(path, report, dry_run=dry_run)


def collect_repo_roots(
    explicit_repo: Path | None,
    *,
    all_repos: bool,
) -> list[Path]:
    """Determine which repository roots to clean up.

    Resolution rules:

    * ``explicit_repo`` takes precedence and is always returned (alone).
    * Otherwise we always include the current working directory.
    * With ``all_repos=True`` we additionally include every path
      registered in ``~/.code-review-graph/registry.json``.
    """
    if explicit_repo is not None:
        return [explicit_repo.resolve()]

    roots: list[Path] = [Path(os.getcwd()).resolve()]
    if all_repos:
        # Lazy import — keeps uninstall importable in environments where
        # the registry module's deps aren't available.
        try:
            from .registry import Registry

            registry = Registry()
            for entry in registry.list_repos():
                p = Path(entry["path"]).resolve()
                if p not in roots:
                    roots.append(p)
                # Also clean up any external data_dir the user pointed
                # the repo at.
                data_dir = entry.get("data_dir")
                if data_dir:
                    dp = Path(data_dir).resolve()
                    if dp.exists() and dp not in roots:
                        roots.append(dp)
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("Could not load registry: %s", exc)
    return roots


def run(
    *,
    repo: Path | None = None,
    all_repos: bool = False,
    keep_data: bool = False,
    keep_user_configs: bool = False,
    dry_run: bool = False,
) -> UninstallReport:
    """Perform the uninstall and return a structured report.

    Args:
        repo: Specific repository root to clean. When ``None`` we use
            the current working directory.
        all_repos: When True, also sweep every repo in the registry
            (in addition to ``repo``/CWD).
        keep_data: Skip removal of ``.code-review-graph/`` directories
            (per-repo and user-level). Useful when the user only wants
            to disable the tool but keep the graph cached.
        keep_user_configs: Skip the user-level pass entirely. Useful
            when only cleaning a single project.
        dry_run: Report planned actions without making changes.

    Returns:
        :class:`UninstallReport` describing the actions taken (or
        planned).
    """
    report = UninstallReport()

    for root in collect_repo_roots(repo, all_repos=all_repos):
        if not root.exists():
            report.skipped_paths.append(f"{root} (missing)")
            continue
        _process_repo(root, report, dry_run=dry_run, keep_data=keep_data)

    if not keep_user_configs:
        _process_user(report, dry_run=dry_run, keep_data=keep_data)

    return report
