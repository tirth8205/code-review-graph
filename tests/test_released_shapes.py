"""Every shape a released version wrote must still be recognised today.

Install upgrades a previous release's artifact in place, and uninstall removes
it. Both decide what belongs to this project from a recorded set of shapes, and
a shape missing from that set is a silent failure for everyone who installed
from the release that wrote it: uninstall refuses and leaves the block behind,
install stacks a second hook beside the first.

The recorded sets are therefore checked against the releases themselves. The
tags are read out of the repository when they are available, so a shape added
to the code without being recorded fails here; the frozen snapshots below keep
the same check meaningful in a shallow checkout that has no tags.
"""

from __future__ import annotations

import ast
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

import pytest

from code_review_graph import skills, uninstall

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_PATH = "code_review_graph/skills.py"

# Rendered from the parameter a released ``_ensure_group`` passes through; the
# real values are captured from that call's ``hook_command`` keyword instead.
_PASSTHROUGH = "{hook_command}"

# Stand-ins for values a released source interpolated at runtime.
# ``test_every_released_hook_command_is_recognised`` fails on any interpolation
# that is not listed here, so a new one cannot slip past this file unnoticed.
PLACEHOLDERS = {
    "repo_arg": "/repo/checkout",
    "hooks_dir": "/home/u/.cursor/hooks",
    "_GEMINI_CLI_HOOK_FILENAMES[0]": "crg-session-start.sh",
    "_GEMINI_CLI_HOOK_FILENAMES[1]": "crg-update.sh",
    "sys.executable": "/opt/previous-release/bin/python3.9",
    "hook_command": _PASSTHROUGH,
}

# Frozen from the tags below, so a checkout without tags still checks
# something. ``test_recorded_hook_blocks_match_the_tags`` keeps it honest.
RELEASED_GIT_HOOK_BODIES = (
    # v2.2.3 - v2.3.2
    "# Installed by code-review-graph. Remove this file to disable pre-commit"
    " graph checks.\n"
    "if command -v code-review-graph >/dev/null 2>&1; then\n"
    "    code-review-graph detect-changes --brief || true\n"
    "fi\n",
    # v2.3.3 - v2.3.8
    "# Installed by code-review-graph. Remove this file to disable pre-commit"
    " graph checks.\n"
    "if command -v code-review-graph >/dev/null 2>&1; then\n"
    "    code-review-graph update || true\n"
    "    code-review-graph detect-changes --brief || true\n"
    "fi\n",
)


# ---------------------------------------------------------------------------
# Reading the releases
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _version_tags() -> tuple[str, ...]:
    if shutil.which("git") is None:
        return ()
    try:
        result = subprocess.run(
            ["git", "tag", "--list", "v*"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return ()
    if result.returncode != 0:
        return ()
    return tuple(sorted(tag for tag in result.stdout.split() if tag.startswith("v")))


@lru_cache(maxsize=None)
def _released_source(tag: str) -> str:
    try:
        result = subprocess.run(
            ["git", "show", f"{tag}:{SKILLS_PATH}"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout if result.returncode == 0 else ""


def _released_trees() -> list[tuple[str, ast.Module]]:
    trees = []
    for tag in _version_tags():
        source = _released_source(tag)
        if not source:
            continue
        try:
            trees.append((tag, ast.parse(source)))
        except SyntaxError:  # pragma: no cover - a tag we cannot read teaches nothing
            continue
    return trees


def _function(tree: ast.Module, name: str) -> ast.FunctionDef | None:
    return next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == name
        ),
        None,
    )


def _render(node: ast.expr) -> str | None:
    """Render a string expression, or None when it cannot be resolved."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.JoinedStr):
        parts = [_render(value) for value in node.values]
        return None if any(part is None for part in parts) else "".join(parts)  # type: ignore[arg-type]
    if isinstance(node, ast.FormattedValue):
        return PLACEHOLDERS.get(ast.unparse(node.value))
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _render(node.left), _render(node.right)
        return None if left is None or right is None else left + right
    if isinstance(node, (ast.Name, ast.Attribute, ast.Subscript)):
        return PLACEHOLDERS.get(ast.unparse(node))
    return None


_HOOK_GENERATORS = (
    "generate_hooks_config",
    "generate_codex_hooks_config",
    "generate_cursor_hooks_config",
    "install_gemini_cli_hooks",
)


def _released_hook_commands() -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Return ``(commands, unresolved)``, both mapped to the tags involved.

    ``unresolved`` holds command expressions this file could not render, so an
    interpolation nobody taught it about is reported rather than skipped.
    """
    found: dict[str, set[str]] = {}
    unresolved: dict[str, set[str]] = {}

    def _record(node: ast.expr, tag: str) -> None:
        rendered = _render(node)
        if rendered is None:
            unresolved.setdefault(ast.unparse(node), set()).add(tag)
        elif rendered != _PASSTHROUGH:
            found.setdefault(rendered, set()).add(tag)

    for tag, tree in _released_trees():
        for name in _HOOK_GENERATORS:
            function = _function(tree, name)
            if function is None:
                continue
            for node in ast.walk(function):
                if isinstance(node, ast.Dict):
                    for key, value in zip(node.keys, node.values):
                        if isinstance(key, ast.Constant) and key.value == "command":
                            _record(value, tag)
                if isinstance(node, ast.Call):
                    for keyword in node.keywords:
                        if keyword.arg == "hook_command":
                            _record(keyword.value, tag)
    return found, unresolved


def _released_hook_matchers() -> dict[str, set[str]]:
    """Map every released ``event -> matcher`` pair to the tags that wrote it."""
    found: dict[str, set[str]] = {}
    for tag, tree in _released_trees():
        for name in _HOOK_GENERATORS:
            function = _function(tree, name)
            if function is None:
                continue
            for node in ast.walk(function):
                if isinstance(node, ast.Dict):
                    for key, value in zip(node.keys, node.values):
                        if not isinstance(key, ast.Constant) or not isinstance(
                            key.value, str
                        ):
                            continue
                        if key.value not in skills._GENERATED_HOOK_MATCHERS:
                            continue
                        try:
                            groups = ast.literal_eval(value)
                        except (ValueError, TypeError, SyntaxError):
                            continue
                        if not isinstance(groups, list):
                            continue
                        for group in groups:
                            if isinstance(group, dict):
                                found.setdefault(
                                    f"{key.value}:{group.get('matcher')!r}", set()
                                ).add(tag)
                if isinstance(node, ast.Call):
                    keywords = {kw.arg: kw.value for kw in node.keywords}
                    if "matcher" not in keywords or "event_name" not in keywords:
                        continue
                    try:
                        event = ast.literal_eval(keywords["event_name"])
                        matcher = ast.literal_eval(keywords["matcher"])
                    except (ValueError, TypeError, SyntaxError):
                        continue
                    found.setdefault(f"{event}:{matcher!r}", set()).add(tag)
    return found


def _released_hook_bodies() -> dict[str, set[str]]:
    """Map every released pre-commit hook block to the tags that wrote it."""
    found: dict[str, set[str]] = {}
    for tag, tree in _released_trees():
        function = _function(tree, "install_git_hook")
        if function is None:
            continue
        for node in ast.walk(function):
            if not isinstance(node, ast.Assign):
                continue
            value = node.value
            if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
                continue
            text = value.value
            if skills._GIT_HOOK_NOTE not in text:
                continue
            body = text.removeprefix("#!/bin/sh\n")
            found.setdefault(body, set()).add(tag)
    return found


def _released_server_entries() -> dict[str, set[str]]:
    """Map every released MCP entry shape (as repr) to the tags that wrote it."""
    found: dict[str, set[str]] = {}
    for tag, tree in _released_trees():
        for name in ("_detect_serve_command", "_build_server_entry"):
            function = _function(tree, name)
            if function is None:
                continue
            for node in ast.walk(function):
                # Releases up to v2.3.2 built the entry into a local and
                # returned the name; later ones return the literal.
                if isinstance(node, ast.Return) and node.value is not None:
                    values: list[ast.expr] = [node.value]
                elif isinstance(node, ast.AnnAssign) and node.value is not None:
                    values = [node.value]
                elif isinstance(node, ast.Assign):
                    values = [node.value]
                else:
                    continue
                for value in values:
                    for entry in _entries_from_return(value):
                        found.setdefault(repr(sorted(entry.items())), set()).add(tag)
    return found


def _entries_from_return(value: ast.expr) -> list[dict]:
    """Turn one ``return`` in a released builder into concrete MCP entries."""
    if isinstance(value, ast.Tuple) and len(value.elts) == 2:
        command = _render(value.elts[0])
        try:
            args = ast.literal_eval(value.elts[1])
        except (ValueError, TypeError, SyntaxError):
            return []
        if command is None or not isinstance(args, list):
            return []
        return [
            {"command": command, "args": args},
            {"command": command, "args": args, "type": "stdio", "cwd": "/repo"},
            # OpenCode folds the whole command line into ``command``.
            {"type": "local", "command": [command, *args, "--repo", "/repo"]},
        ]
    if isinstance(value, ast.Dict):
        try:
            entry = ast.literal_eval(value)
        except (ValueError, TypeError, SyntaxError):
            return []
        if not isinstance(entry, dict) or "command" not in entry:
            return []
        return [entry, {**entry, "type": "stdio"}, {**entry, "env": []}]
    return []


def _require_tags() -> tuple[str, ...]:
    tags = _version_tags()
    if not tags:
        pytest.skip("no version tags in this checkout; cannot derive released shapes")
    return tags


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------


def test_recorded_hook_blocks_match_the_tags() -> None:
    """The frozen snapshot really is what the released tags contain."""
    _require_tags()
    derived = _released_hook_bodies()
    assert derived, "no pre-commit hook body could be read from any tag"
    assert set(derived) == set(RELEASED_GIT_HOOK_BODIES), (
        "the released pre-commit hook bodies have changed; update "
        "RELEASED_GIT_HOOK_BODIES and skills._LEGACY_GIT_HOOK_BLOCKS together"
    )


@pytest.mark.parametrize("body", RELEASED_GIT_HOOK_BODIES)
def test_every_released_hook_body_is_still_recognised(body: str) -> None:
    """Install upgrades it and uninstall removes it, for every release."""
    assert body in skills._known_git_hook_blocks(), (
        "a released pre-commit hook body is not recorded, so uninstall refuses "
        "to remove it and install cannot upgrade it"
    )
    hook = f"#!/bin/sh\necho user-hook\n{body}echo trailing\n"

    upgraded = skills._upgrade_git_hook_block(hook)
    assert upgraded is not None
    assert body not in upgraded
    assert skills._GIT_HOOK_BLOCK in upgraded
    assert "echo user-hook" in upgraded and "echo trailing" in upgraded

    stripped, removed = uninstall._strip_git_hook_blocks(hook)
    assert removed is True
    assert skills._GIT_HOOK_NOTE not in stripped
    assert "echo user-hook" in stripped and "echo trailing" in stripped


def test_every_released_hook_command_is_recognised() -> None:
    _require_tags()
    commands, unresolved = _released_hook_commands()
    assert commands, "no hook command could be read from any tag"
    assert unresolved == {}, (
        "a released hook command interpolates something this file cannot "
        "render; add it to PLACEHOLDERS rather than letting it go unchecked"
    )
    unrecognised = {
        command: sorted(tags)
        for command, tags in commands.items()
        if not skills._is_generated_hook_command(command)
    }
    assert unrecognised == {}, (
        "a hook command a release wrote is no longer recognised, so reinstalling "
        "would leave it in place and add a second hook beside it"
    )


def test_every_released_hook_matcher_is_recorded() -> None:
    _require_tags()
    matchers = _released_hook_matchers()
    assert matchers, "no hook matcher could be read from any tag"
    missing = {
        pair: sorted(tags)
        for pair, tags in matchers.items()
        if ast.literal_eval(pair.split(":", 1)[1])
        not in skills._GENERATED_HOOK_MATCHERS.get(pair.split(":", 1)[0], frozenset())
    }
    assert missing == {}, (
        "a matcher a release filed its own hook group under is not recorded, so "
        "that group would never be replaced"
    )


def test_every_released_server_entry_is_recognised() -> None:
    _require_tags()
    entries = _released_server_entries()
    assert entries, "no MCP entry shape could be read from any tag"
    unrecognised = {
        shape: sorted(tags)
        for shape, tags in entries.items()
        if not skills._is_generated_server_entry(dict(ast.literal_eval(shape)))
    }
    assert unrecognised == {}, (
        "an MCP entry shape a release wrote is no longer recognised, so a stale "
        "interpreter path or dead checkout would survive a reinstall"
    )
