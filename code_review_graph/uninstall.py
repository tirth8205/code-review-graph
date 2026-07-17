"""Safely reverse artifacts created by :mod:`code_review_graph.skills`.

The original implementation was contributed by Stephen Cheng in PR #491.
This current-main replacement keeps that command/report design while deriving
the live MCP inventory from ``skills.PLATFORMS`` and treating every shared file
as user-owned data that may only be edited surgically.
"""

from __future__ import annotations

import copy
import importlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import skills

_tomllib: Any = importlib.import_module(
    "tomllib" if sys.version_info >= (3, 11) else "tomli"
)

_ENTRY_NAME = "code-review-graph"
_GIT_HOOK_MARKER = (
    "# Installed by code-review-graph. Remove this file to disable pre-commit graph checks."
)
_GITIGNORE_BANNER = "# Added by code-review-graph"


@dataclass
class UninstallReport:
    """Structured record of completed or planned uninstall actions."""

    removed_paths: list[str] = field(default_factory=list)
    edited_paths: list[str] = field(default_factory=list)
    skipped_paths: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def total_actions(self) -> int:
        return len(self.removed_paths) + len(self.edited_paths)


@dataclass(frozen=True)
class _Token:
    kind: str
    start: int
    end: int
    value: str | None = None


@dataclass(frozen=True)
class _Member:
    key: str
    key_index: int
    value_index: int
    value_end: int
    comma_index: int | None


@dataclass(frozen=True)
class _Element:
    value_index: int
    value_end: int
    comma_index: int | None


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def _is_lexical_child(path: Path, boundary: Path) -> bool:
    candidate = _absolute(path)
    root = _absolute(boundary)
    if candidate == root:
        return False
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _safe_path(
    path: Path,
    boundary: Path,
    report: UninstallReport,
    *,
    describe_skip: bool = True,
) -> bool:
    """Require lexical and resolved containment, with no symlink traversal."""
    candidate = _absolute(path)
    root = _absolute(boundary)
    if not _is_lexical_child(candidate, root):
        if describe_skip:
            report.skipped_paths.append(f"{candidate} (outside allowed boundary {root})")
        return False

    try:
        resolved_root = root.resolve(strict=False)
        resolved_candidate = candidate.resolve(strict=False)
        resolved_candidate.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError):
        if describe_skip:
            report.skipped_paths.append(f"{candidate} (resolved outside allowed boundary {root})")
        return False

    current = candidate
    while current != root:
        try:
            if current.is_symlink():
                if describe_skip:
                    report.skipped_paths.append(f"{candidate} (symlink path is not removed)")
                return False
        except OSError as exc:
            if describe_skip:
                report.skipped_paths.append(f"{candidate} (cannot inspect path: {exc})")
            return False
        current = current.parent
    return True


def _record_edit(report: UninstallReport, path: Path, detail: str, dry_run: bool) -> None:
    verb = f"would {detail}" if dry_run else detail
    report.edited_paths.append(f"{path} ({verb})")


def _record_remove(report: UninstallReport, path: Path, detail: str, dry_run: bool) -> None:
    verb = f"would {detail}" if dry_run else detail
    report.removed_paths.append(f"{path} ({verb})")


def _read_text(path: Path, report: UninstallReport) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        report.errors.append(f"{path}: read failed ({exc})")
        return None


def _write_text(
    path: Path,
    text: str,
    report: UninstallReport,
    *,
    detail: str,
    dry_run: bool,
) -> None:
    if dry_run:
        _record_edit(report, path, detail, True)
        return
    temporary: Path | None = None
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
        temporary = None
    except (OSError, UnicodeError) as exc:
        report.errors.append(f"{path}: write failed ({exc})")
        return
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as exc:
                report.errors.append(f"{temporary}: temporary cleanup failed ({exc})")
    _record_edit(report, path, detail, False)


def _tokenize_jsonc(raw: str) -> list[_Token]:
    """Tokenize JSONC while retaining exact source offsets for safe splices."""
    tokens: list[_Token] = []
    i = 0
    while i < len(raw):
        char = raw[i]
        if char.isspace():
            i += 1
            continue
        if char == "/" and i + 1 < len(raw):
            if raw[i + 1] == "/":
                newline = raw.find("\n", i + 2)
                i = len(raw) if newline < 0 else newline
                continue
            if raw[i + 1] == "*":
                end = raw.find("*/", i + 2)
                i = len(raw) if end < 0 else end + 2
                continue
        if char == '"':
            start = i
            i += 1
            while i < len(raw):
                if raw[i] == "\\":
                    i += 2
                    continue
                if raw[i] == '"':
                    i += 1
                    break
                i += 1
            else:
                raise ValueError("unterminated JSON string")
            try:
                value = json.loads(raw[start:i])
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON string: {exc}") from exc
            tokens.append(_Token("string", start, i, value))
            continue
        if char in "{}[]:,":
            tokens.append(_Token(char, i, i + 1))
            i += 1
            continue
        start = i
        while i < len(raw):
            if raw[i].isspace() or raw[i] in "{}[]:,":
                break
            if raw[i] == "/" and i + 1 < len(raw) and raw[i + 1] in "/*":
                break
            i += 1
        if i == start:
            raise ValueError(f"unexpected JSONC character at offset {i}")
        tokens.append(_Token("literal", start, i, raw[start:i]))
    return tokens


def _skip_value(tokens: Sequence[_Token], index: int) -> int:
    """Return the token index immediately after one JSON value."""
    if index >= len(tokens):
        raise ValueError("missing JSON value")
    token = tokens[index]
    if token.kind not in ("{", "["):
        return index + 1
    closing = "}" if token.kind == "{" else "]"
    depth = 1
    index += 1
    while index < len(tokens):
        kind = tokens[index].kind
        if kind == token.kind:
            depth += 1
        elif kind == closing:
            depth -= 1
            if depth == 0:
                return index + 1
        elif kind in ("{", "["):
            index = _skip_value(tokens, index)
            continue
        index += 1
    raise ValueError("unterminated JSON container")


def _object_members(tokens: Sequence[_Token], index: int) -> list[_Member]:
    if index >= len(tokens) or tokens[index].kind != "{":
        raise ValueError("expected JSON object")
    members: list[_Member] = []
    cursor = index + 1
    while cursor < len(tokens) and tokens[cursor].kind != "}":
        if tokens[cursor].kind == ",":  # trailing comma
            cursor += 1
            continue
        key_token = tokens[cursor]
        if key_token.kind != "string" or not isinstance(key_token.value, str):
            raise ValueError("expected JSON object key")
        if cursor + 1 >= len(tokens) or tokens[cursor + 1].kind != ":":
            raise ValueError("expected colon after JSON object key")
        value_index = cursor + 2
        value_end = _skip_value(tokens, value_index)
        comma_index = value_end if (
            value_end < len(tokens) and tokens[value_end].kind == ","
        ) else None
        members.append(
            _Member(key_token.value, cursor, value_index, value_end, comma_index)
        )
        cursor = value_end + 1 if comma_index is not None else value_end
    return members


def _array_elements(tokens: Sequence[_Token], index: int) -> list[_Element]:
    if index >= len(tokens) or tokens[index].kind != "[":
        raise ValueError("expected JSON array")
    elements: list[_Element] = []
    cursor = index + 1
    while cursor < len(tokens) and tokens[cursor].kind != "]":
        if tokens[cursor].kind == ",":  # trailing comma
            cursor += 1
            continue
        value_end = _skip_value(tokens, cursor)
        comma_index = value_end if (
            value_end < len(tokens) and tokens[value_end].kind == ","
        ) else None
        elements.append(_Element(cursor, value_end, comma_index))
        cursor = value_end + 1 if comma_index is not None else value_end
    return elements


def _find_value(tokens: Sequence[_Token], path: Sequence[str | int]) -> int:
    if not tokens:
        raise ValueError("empty JSON document")
    current = 0
    for component in path:
        if isinstance(component, str):
            member = next(
                (item for item in _object_members(tokens, current) if item.key == component),
                None,
            )
            if member is None:
                raise KeyError(component)
            current = member.value_index
        else:
            elements = _array_elements(tokens, current)
            if component < 0 or component >= len(elements):
                raise IndexError(component)
            current = elements[component].value_index
    return current


def _removal_ranges(tokens: Sequence[_Token], path: Sequence[str | int]) -> list[tuple[int, int]]:
    if not path:
        raise ValueError("refusing to remove the JSON document root")
    parent_index = _find_value(tokens, path[:-1])
    component = path[-1]
    if isinstance(component, str):
        members = _object_members(tokens, parent_index)
        sibling_index = next(
            (index for index, member in enumerate(members) if member.key == component),
            None,
        )
        if sibling_index is None:
            raise KeyError(component)
        item = members[sibling_index]
        start = tokens[item.key_index].start
        end = tokens[item.value_end - 1].end
        if item.comma_index is not None:
            return [(start, tokens[item.comma_index].end)]
        if sibling_index > 0:
            previous = members[sibling_index - 1]
            if previous.comma_index is not None:
                return [
                    (tokens[previous.comma_index].start, tokens[previous.comma_index].end),
                    (start, end),
                ]
        return [(start, end)]

    elements = _array_elements(tokens, parent_index)
    if component < 0 or component >= len(elements):
        raise IndexError(component)
    element = elements[component]
    start = tokens[element.value_index].start
    end = tokens[element.value_end - 1].end
    if element.comma_index is not None:
        return [(start, tokens[element.comma_index].end)]
    if component > 0:
        previous_element = elements[component - 1]
        if previous_element.comma_index is not None:
            return [
                (
                    tokens[previous_element.comma_index].start,
                    tokens[previous_element.comma_index].end,
                ),
                (start, end),
            ]
    return [(start, end)]


def _remove_jsonc_paths(raw: str, paths: Iterable[Sequence[str | int]]) -> str:
    """Remove paths against one token snapshot, then merge overlapping spans."""
    tokens = _tokenize_jsonc(raw)
    ranges = [
        source_range
        for path in paths
        for source_range in _removal_ranges(tokens, tuple(path))
    ]
    merged: list[tuple[int, int]] = []
    for start, end in sorted(set(ranges)):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    for start, end in reversed(merged):
        raw = raw[:start] + raw[end:]
    return raw


def _parse_jsonc(path: Path, raw: str, report: UninstallReport) -> dict[str, Any] | None:
    try:
        parsed = json.loads(skills._strip_jsonc(raw))
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        report.skipped_paths.append(f"{path} (parse failed; left unchanged: {exc})")
        return None
    if not isinstance(parsed, dict):
        report.skipped_paths.append(
            f"{path} (top level is {type(parsed).__name__}, not an object; left unchanged)"
        )
        return None
    return parsed


def _remove_mcp_entry(
    path: Path,
    *,
    key: str,
    format_name: str,
    boundary: Path,
    report: UninstallReport,
    dry_run: bool,
) -> None:
    if not path.exists():
        return
    if not _safe_path(path, boundary, report):
        return
    raw = _read_text(path, report)
    if raw is None:
        return
    data = _parse_jsonc(path, raw, report)
    if data is None or key not in data:
        return

    expected_type = list if format_name == "array" else dict
    container = data[key]
    if not isinstance(container, expected_type):
        expected = "array" if expected_type is list else "object"
        report.skipped_paths.append(
            f"{path} ({key!r} is {type(container).__name__}, not {expected}; left unchanged)"
        )
        return

    if format_name == "array":
        indices = [
            index
            for index, entry in enumerate(container)
            if isinstance(entry, dict) and entry.get("name") == _ENTRY_NAME
        ]
        paths: list[tuple[str | int, ...]] = [(key, index) for index in indices]
        expected_data = copy.deepcopy(data)
        expected_data[key] = [
            entry
            for entry in container
            if not (isinstance(entry, dict) and entry.get("name") == _ENTRY_NAME)
        ]
    else:
        if _ENTRY_NAME not in container:
            return
        paths = [(key, _ENTRY_NAME)]
        expected_data = copy.deepcopy(data)
        del expected_data[key][_ENTRY_NAME]
    if not paths:
        return

    try:
        rewritten = _remove_jsonc_paths(raw, paths)
        reparsed = json.loads(skills._strip_jsonc(rewritten))
    except (IndexError, KeyError, RecursionError, ValueError, json.JSONDecodeError) as exc:
        report.skipped_paths.append(f"{path} (safe JSONC edit failed; left unchanged: {exc})")
        return
    if reparsed != expected_data:
        report.skipped_paths.append(f"{path} (safe JSONC edit did not validate; left unchanged)")
        return
    _write_text(
        path,
        rewritten,
        report,
        detail=f"removed {_ENTRY_NAME!r} from {key!r}",
        dry_run=dry_run,
    )


def _remove_toml_entry(
    path: Path,
    key: str,
    boundary: Path,
    report: UninstallReport,
    *,
    dry_run: bool,
) -> None:
    if not path.exists() or not _safe_path(path, boundary, report):
        return
    raw = _read_text(path, report)
    if raw is None:
        return
    try:
        _tomllib.loads(raw)
    except _tomllib.TOMLDecodeError as exc:
        report.skipped_paths.append(f"{path} (TOML parse failed; left unchanged: {exc})")
        return
    header = f"[{key}.{_ENTRY_NAME}]"
    lines = raw.splitlines(keepends=True)
    start = next((index for index, line in enumerate(lines) if line.strip() == header), None)
    if start is None:
        return
    end = start + 1
    while end < len(lines):
        stripped = lines[end].strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            break
        end += 1
    rewritten = "".join(lines[:start] + lines[end:])
    try:
        _tomllib.loads(rewritten)
    except _tomllib.TOMLDecodeError as exc:  # pragma: no cover - defensive validation
        report.skipped_paths.append(f"{path} (safe TOML edit failed; left unchanged: {exc})")
        return
    _write_text(
        path,
        rewritten,
        report,
        detail=f"removed [{key}.{_ENTRY_NAME}]",
        dry_run=dry_run,
    )


def _commands(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        command = value.get("command")
        if isinstance(command, str):
            found.add(command)
        for child in value.values():
            found.update(_commands(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_commands(child))
    return found


def _legacy_repo_hook_commands(repo_root: Path) -> set[str]:
    """Exact project hook commands written by the source #491-era installer."""
    repo_arg = json.dumps(repo_root.resolve().as_posix())
    return {
        (
            "git rev-parse --git-dir >/dev/null 2>&1"
            " && code-review-graph update --skip-flows"
            f" --repo {repo_arg}"
            " || true"
        ),
        (
            "git rev-parse --git-dir >/dev/null 2>&1"
            f" && code-review-graph status --repo {repo_arg}"
            " || echo 'Not a git repo, skipping'"
        ),
    }


def _legacy_codex_hook_commands() -> set[str]:
    """Exact user hook commands written before the current stdin guard."""
    return {
        (
            "git rev-parse --git-dir >/dev/null 2>&1"
            " && code-review-graph update --skip-flows"
            " || true"
        ),
        (
            "git rev-parse --git-dir >/dev/null 2>&1"
            " && code-review-graph status"
            " || echo 'Not a git repo, skipping'"
        ),
    }


def _clean_hook_data(
    data: dict[str, Any],
    owned_commands: set[str],
) -> tuple[dict[str, Any], list[tuple[str | int, ...]]]:
    expected = copy.deepcopy(data)
    hooks_obj = data.get("hooks")
    if not isinstance(hooks_obj, dict):
        return expected, []

    paths: list[tuple[str | int, ...]] = []
    expected_hooks = expected["hooks"]
    for event, entries in hooks_obj.items():
        if not isinstance(entries, list):
            continue
        new_entries: list[Any] = []
        entry_paths: list[tuple[str | int, ...]] = []
        for entry_index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                new_entries.append(copy.deepcopy(entry))
                continue
            direct_command = entry.get("command")
            if isinstance(direct_command, str) and direct_command in owned_commands:
                entry_paths.append(("hooks", event, entry_index))
                continue

            nested = entry.get("hooks")
            if not isinstance(nested, list):
                new_entries.append(copy.deepcopy(entry))
                continue
            kept_nested: list[Any] = []
            nested_paths: list[tuple[str | int, ...]] = []
            for nested_index, hook in enumerate(nested):
                command = hook.get("command") if isinstance(hook, dict) else None
                if isinstance(command, str) and command in owned_commands:
                    nested_paths.append(("hooks", event, entry_index, "hooks", nested_index))
                else:
                    kept_nested.append(copy.deepcopy(hook))
            if not kept_nested and nested_paths:
                entry_paths.append(("hooks", event, entry_index))
                continue
            new_entry = copy.deepcopy(entry)
            if nested_paths:
                new_entry["hooks"] = kept_nested
                entry_paths.extend(nested_paths)
            new_entries.append(new_entry)

        if not new_entries and entry_paths:
            expected_hooks.pop(event, None)
            paths.append(("hooks", event))
        elif entry_paths:
            expected_hooks[event] = new_entries
            paths.extend(entry_paths)

    if paths and not expected_hooks:
        expected.pop("hooks", None)
        paths = [("hooks",)]
    return expected, paths


def _remove_hooks(
    path: Path,
    owned_commands: set[str],
    boundary: Path,
    report: UninstallReport,
    *,
    dry_run: bool,
) -> None:
    if not path.exists() or not _safe_path(path, boundary, report):
        return
    raw = _read_text(path, report)
    if raw is None:
        return
    data = _parse_jsonc(path, raw, report)
    if data is None:
        return
    expected, paths = _clean_hook_data(data, owned_commands)
    if not paths:
        return
    try:
        rewritten = _remove_jsonc_paths(raw, paths)
        reparsed = json.loads(skills._strip_jsonc(rewritten))
    except (IndexError, KeyError, RecursionError, ValueError, json.JSONDecodeError) as exc:
        report.skipped_paths.append(f"{path} (safe hook edit failed; left unchanged: {exc})")
        return
    if reparsed != expected:
        report.skipped_paths.append(f"{path} (safe hook edit did not validate; left unchanged)")
        return
    _write_text(
        path,
        rewritten,
        report,
        detail="removed code-review-graph hook entries",
        dry_run=dry_run,
    )


def _remove_file(
    path: Path,
    boundary: Path,
    report: UninstallReport,
    *,
    dry_run: bool,
    detail: str = "removed owned file",
) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if not _safe_path(path, boundary, report):
        return
    if dry_run:
        _record_remove(report, path, detail, True)
        return
    try:
        path.unlink()
    except OSError as exc:
        report.errors.append(f"{path}: remove failed ({exc})")
        return
    _record_remove(report, path, detail, False)


def _remove_tree(
    path: Path,
    boundary: Path,
    report: UninstallReport,
    *,
    dry_run: bool,
) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if not _safe_path(path, boundary, report):
        return
    if not path.is_dir():
        report.skipped_paths.append(f"{path} (expected an owned directory; left unchanged)")
        return
    if dry_run:
        _record_remove(report, path, "remove owned directory", True)
        return
    try:
        shutil.rmtree(path)
    except OSError as exc:
        report.errors.append(f"{path}: remove failed ({exc})")
        return
    _record_remove(report, path, "removed owned directory", False)


def _prune_empty_directory(path: Path, boundary: Path) -> None:
    if not path.is_dir() or path.is_symlink() or not _is_lexical_child(path, boundary):
        return
    try:
        path.rmdir()
    except OSError:
        return


def _remove_skill_file(
    path: Path,
    boundary: Path,
    report: UninstallReport,
    *,
    dry_run: bool,
) -> None:
    existed = path.exists()
    _remove_file(path, boundary, report, dry_run=dry_run, detail="remove generated skill")
    if existed and not dry_run and not path.exists():
        _prune_empty_directory(path.parent, boundary)


def _remove_instruction(
    path: Path,
    section: str,
    boundary: Path,
    report: UninstallReport,
    *,
    dry_run: bool,
) -> None:
    if not path.exists() or not _safe_path(path, boundary, report):
        return
    raw = _read_text(path, report)
    if raw is None:
        return
    exact_index = raw.find(section)
    if exact_index >= 0:
        start = exact_index
        end = exact_index + len(section)
        rewritten = raw[:start] + raw[end:]
    else:
        marker_index = raw.find(skills._CLAUDE_MD_SECTION_MARKER)
        if marker_index < 0:
            return
        report.skipped_paths.append(
            f"{path} (marked instruction section differs from a known installed section; "
            "left unchanged)"
        )
        return
    rewritten = rewritten.rstrip() + ("\n" if rewritten.strip() else "")
    if rewritten:
        _write_text(
            path,
            rewritten,
            report,
            detail="removed code-review-graph instruction section",
            dry_run=dry_run,
        )
    else:
        _remove_file(
            path,
            boundary,
            report,
            dry_run=dry_run,
            detail="remove generated instruction file",
        )


def _resolve_git_hook(repo_root: Path) -> Path:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-path", "hooks"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        result = None
    if result is not None and result.returncode == 0 and result.stdout.strip():
        return repo_root / result.stdout.strip() / "pre-commit"
    return repo_root / ".git" / "hooks" / "pre-commit"


def _remove_git_hook(
    repo_root: Path,
    report: UninstallReport,
    *,
    dry_run: bool,
) -> None:
    path = _resolve_git_hook(repo_root)
    if not path.exists() or not _safe_path(path, repo_root, report):
        return
    raw = _read_text(path, report)
    if raw is None or _GIT_HOOK_MARKER not in raw:
        return
    lines = raw.splitlines(keepends=True)
    rewritten: list[str] = []
    dropping = False
    for line in lines:
        if _GIT_HOOK_MARKER in line:
            dropping = True
            continue
        if dropping:
            if line.strip() == "fi":
                dropping = False
            continue
        rewritten.append(line)
    new_text = "".join(rewritten).rstrip() + "\n"
    meaningful = [
        line
        for line in new_text.splitlines()
        if line.strip() and not line.strip().startswith("#!")
    ]
    if meaningful:
        _write_text(
            path,
            new_text,
            report,
            detail="removed code-review-graph hook block",
            dry_run=dry_run,
        )
    else:
        _remove_file(
            path,
            repo_root,
            report,
            dry_run=dry_run,
            detail="remove hook containing only code-review-graph",
        )


def _remove_gitignore(
    repo_root: Path,
    report: UninstallReport,
    *,
    dry_run: bool,
) -> None:
    path = repo_root / ".gitignore"
    if not path.exists() or not _safe_path(path, repo_root, report):
        return
    raw = _read_text(path, report)
    if raw is None:
        return
    lines = raw.splitlines(keepends=True)
    rewritten: list[str] = []
    changed = False
    index = 0
    while index < len(lines):
        if (
            lines[index].strip() == _GITIGNORE_BANNER
            and index + 1 < len(lines)
            and lines[index + 1].strip() in {".code-review-graph", ".code-review-graph/"}
        ):
            changed = True
            index += 2
            continue
        rewritten.append(lines[index])
        index += 1
    if not changed:
        return
    new_text = "".join(rewritten)
    if new_text.strip():
        _write_text(
            path,
            new_text,
            report,
            detail="removed installer-owned ignore block",
            dry_run=dry_run,
        )
    else:
        _remove_file(
            path,
            repo_root,
            report,
            dry_run=dry_run,
            detail="remove generated .gitignore",
        )


def _scope_for_config(path: Path, repo_root: Path, home: Path) -> tuple[str, Path] | None:
    if _is_lexical_child(path, repo_root):
        return "repo", repo_root
    if _is_lexical_child(path, home):
        return "user", home
    return None


def _process_platform_configs(
    repo_root: Path,
    home: Path,
    report: UninstallReport,
    *,
    scope: str,
    dry_run: bool,
) -> None:
    seen: set[tuple[Path, str, str]] = set()
    for platform_name, spec in skills.PLATFORMS.items():
        try:
            path = _absolute(spec["config_path"](repo_root))
            key = str(spec["key"])
            format_name = str(spec["format"])
        except (KeyError, OSError, TypeError, ValueError) as exc:
            report.skipped_paths.append(
                f"{platform_name} config (invalid platform specification: {exc})"
            )
            continue
        destination = _scope_for_config(path, repo_root, home)
        if destination is None:
            if path.exists():
                report.skipped_paths.append(
                    f"{path} ({platform_name} config is outside home/repo boundary)"
                )
            continue
        config_scope, boundary = destination
        if config_scope != scope:
            continue
        identity = (path, key, format_name)
        if identity in seen:
            continue
        seen.add(identity)
        if format_name == "toml":
            _remove_toml_entry(path, key, boundary, report, dry_run=dry_run)
        elif format_name in {"object", "array"}:
            _remove_mcp_entry(
                path,
                key=key,
                format_name=format_name,
                boundary=boundary,
                report=report,
                dry_run=dry_run,
            )
        else:
            report.skipped_paths.append(
                f"{path} ({platform_name} has unsupported config format {format_name!r})"
            )


def _generated_skill_slugs() -> list[str]:
    return [filename.rsplit(".", 1)[0] for filename in skills._SKILLS]


def _remove_legacy_mcp_configs(
    repo_root: Path,
    home: Path,
    report: UninstallReport,
    *,
    scope: str,
    dry_run: bool,
) -> None:
    """Clean only historical paths from #491 that are absent from live specs."""
    artifacts = {
        "repo": (repo_root / ".opencode.json", repo_root),
        "user": (home / ".cursor" / "mcp.json", home),
    }
    path, boundary = artifacts[scope]
    _remove_mcp_entry(
        path,
        key="mcpServers",
        format_name="object",
        boundary=boundary,
        report=report,
        dry_run=dry_run,
    )


def _process_repo(
    repo_root: Path,
    home: Path,
    report: UninstallReport,
    *,
    keep_data: bool,
    dry_run: bool,
) -> None:
    _process_platform_configs(repo_root, home, report, scope="repo", dry_run=dry_run)
    _remove_legacy_mcp_configs(
        repo_root,
        home,
        report,
        scope="repo",
        dry_run=dry_run,
    )

    data_paths = [
        (repo_root / ".code-review-graph", "tree"),
        (repo_root / ".code-review-graph.db", "file"),
        (repo_root / ".code-review-graph.db-wal", "file"),
        (repo_root / ".code-review-graph.db-shm", "file"),
    ]
    for path, kind in data_paths:
        if keep_data:
            if path.exists() or path.is_symlink():
                report.skipped_paths.append(f"{path} (kept by --keep-data)")
            continue
        if kind == "tree":
            _remove_tree(path, repo_root, report, dry_run=dry_run)
        else:
            _remove_file(path, repo_root, report, dry_run=dry_run)

    hook_commands = _commands(skills.generate_hooks_config(repo_root))
    hook_commands.update(_legacy_repo_hook_commands(repo_root))
    _remove_hooks(
        repo_root / ".claude" / "settings.json",
        hook_commands,
        repo_root,
        report,
        dry_run=dry_run,
    )
    _remove_hooks(
        repo_root / ".qoder" / "settings.json",
        hook_commands,
        repo_root,
        report,
        dry_run=dry_run,
    )
    _remove_hooks(
        repo_root / ".codebuddy" / "settings.json",
        hook_commands,
        repo_root,
        report,
        dry_run=dry_run,
    )

    gemini_script_names = skills._GEMINI_CLI_HOOK_FILENAMES
    gemini_commands = {f"bash .gemini/hooks/{name}" for name in gemini_script_names}
    _remove_hooks(
        repo_root / ".gemini" / "settings.json",
        gemini_commands,
        repo_root,
        report,
        dry_run=dry_run,
    )
    for name in gemini_script_names:
        _remove_file(
            repo_root / ".gemini" / "hooks" / name,
            repo_root,
            report,
            dry_run=dry_run,
        )

    for root_name in (".claude", ".gemini", ".codebuddy"):
        for slug in _generated_skill_slugs():
            _remove_skill_file(
                repo_root / root_name / "skills" / slug / "SKILL.md",
                repo_root,
                report,
                dry_run=dry_run,
            )
    source_skills = repo_root / "skills"
    if source_skills.is_dir() and not source_skills.is_symlink():
        try:
            candidates = list(source_skills.iterdir())
        except OSError as exc:
            report.errors.append(f"{source_skills}: list failed ({exc})")
        else:
            for candidate in candidates:
                if candidate.is_dir() and (candidate / "SKILL.md").is_file():
                    _remove_skill_file(
                        repo_root / ".qoder" / "skills" / candidate.name / "SKILL.md",
                        repo_root,
                        report,
                        dry_run=dry_run,
                    )

    instruction_sections = {
        "CLAUDE.md": skills._CLAUDE_MD_SECTION,
        **{
            relative: skills._PLATFORM_INSTRUCTION_CUSTOM_SECTIONS.get(
                relative,
                (skills._CLAUDE_MD_SECTION_MARKER, skills._CLAUDE_MD_SECTION),
            )[1]
            for relative in skills._PLATFORM_INSTRUCTION_FILES
        },
    }
    for relative, section in instruction_sections.items():
        _remove_instruction(
            repo_root / relative,
            section,
            repo_root,
            report,
            dry_run=dry_run,
        )

    _remove_git_hook(repo_root, report, dry_run=dry_run)
    _remove_gitignore(repo_root, report, dry_run=dry_run)


def _process_user(
    reference_repo: Path,
    home: Path,
    report: UninstallReport,
    *,
    keep_data: bool,
    dry_run: bool,
) -> None:
    _process_platform_configs(reference_repo, home, report, scope="user", dry_run=dry_run)
    _remove_legacy_mcp_configs(
        reference_repo,
        home,
        report,
        scope="user",
        dry_run=dry_run,
    )

    user_data = home / ".code-review-graph"
    if keep_data:
        if user_data.exists() or user_data.is_symlink():
            report.skipped_paths.append(f"{user_data} (kept by --keep-data)")
    else:
        _remove_tree(user_data, home, report, dry_run=dry_run)

    _remove_hooks(
        home / ".codex" / "hooks.json",
        _commands(skills.generate_codex_hooks_config(reference_repo))
        | _legacy_codex_hook_commands(),
        home,
        report,
        dry_run=dry_run,
    )
    _remove_hooks(
        home / ".cursor" / "hooks.json",
        _commands(skills.generate_cursor_hooks_config()),
        home,
        report,
        dry_run=dry_run,
    )
    for filename in skills._cursor_hook_scripts():
        _remove_file(
            home / ".cursor" / "hooks" / filename,
            home,
            report,
            dry_run=dry_run,
        )
    _remove_file(
        home / ".config" / "opencode" / "plugins" / "crg-plugin.ts",
        home,
        report,
        dry_run=dry_run,
    )


def _registry_repo_paths(home: Path, report: UninstallReport) -> list[Path]:
    path = home / ".code-review-graph" / "registry.json"
    if not path.exists() or not _safe_path(path, home, report):
        return []
    raw = _read_text(path, report)
    if raw is None:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, RecursionError) as exc:
        report.skipped_paths.append(f"{path} (registry parse failed: {exc})")
        return []
    repos = data.get("repos") if isinstance(data, dict) else None
    if not isinstance(repos, list):
        report.skipped_paths.append(f"{path} (registry has no valid repos array)")
        return []
    paths: list[Path] = []
    for entry in repos:
        value = entry.get("path") if isinstance(entry, dict) else None
        if isinstance(value, str) and value:
            paths.append(Path(value).expanduser())
        data_dir = entry.get("data_dir") if isinstance(entry, dict) else None
        if isinstance(data_dir, str) and data_dir:
            report.skipped_paths.append(
                f"{Path(data_dir).expanduser()} (external data directory retained for safety)"
            )
    return paths


def _normalise_repo(path: Path, home: Path, report: UninstallReport) -> Path | None:
    lexical = _absolute(path)
    try:
        resolved = lexical.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        report.skipped_paths.append(f"{lexical} (cannot resolve repository: {exc})")
        return None
    filesystem_root = Path(resolved.anchor)
    if resolved in {filesystem_root, home.resolve(strict=False)}:
        report.skipped_paths.append(f"{resolved} (refusing unsafe repository boundary)")
        return None
    if not resolved.is_dir():
        report.skipped_paths.append(f"{resolved} (repository directory is missing)")
        return None

    from .incremental import find_repo_root

    repository_root = find_repo_root(resolved)
    if repository_root is None:
        report.skipped_paths.append(f"{resolved} (not inside a Git or SVN repository)")
        return None
    try:
        repository_root = repository_root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        report.skipped_paths.append(f"{resolved} (cannot resolve repository root: {exc})")
        return None
    if repository_root in {Path(repository_root.anchor), home.resolve(strict=False)}:
        report.skipped_paths.append(
            f"{repository_root} (refusing unsafe repository boundary)"
        )
        return None
    return repository_root


def run(
    *,
    repo: Path | None = None,
    all_repos: bool = False,
    keep_data: bool = False,
    keep_user_configs: bool = False,
    dry_run: bool = False,
) -> UninstallReport:
    """Uninstall CRG artifacts and return a precise action report."""
    report = UninstallReport()
    home = _absolute(Path.home())
    requested = [repo if repo is not None else Path.cwd()]
    if all_repos:
        requested.extend(_registry_repo_paths(home, report))

    roots: list[Path] = []
    for candidate in requested:
        normalised = _normalise_repo(Path(candidate), home, report)
        if normalised is not None and normalised not in roots:
            roots.append(normalised)

    for root in roots:
        _process_repo(
            root,
            home,
            report,
            keep_data=keep_data,
            dry_run=dry_run,
        )

    if not keep_user_configs:
        reference = roots[0] if roots else home / ".crg-uninstall-repo-sentinel"
        _process_user(
            reference,
            home,
            report,
            keep_data=keep_data,
            dry_run=dry_run,
        )
    return report
