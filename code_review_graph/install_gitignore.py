"""The opt-in ``.gitignore`` block for files ``install`` created (#471).

``install --gitignore`` adds one marked block to the repository's
``.gitignore``. The block lists the files and folders that run created and
that git does not track. A path that existed before the run is never listed,
so nothing a user wrote is ignored, and a tracked path is never listed. A
folder the run created is listed whole. Running install again merges into the
same block instead of adding a second one. ``uninstall`` removes the block and
nothing else.

The ``.code-review-graph/`` entry has its own handling in
:func:`incremental.ensure_repo_gitignore_excludes_crg` and is not touched here.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePath, PurePosixPath

from . import codex_skill, skills
from . import install_plan as planned_files

logger = logging.getLogger(__name__)

BLOCK_START = "# >>> code-review-graph install files >>>"
BLOCK_NOTE = "# Added by `code-review-graph install --gitignore`. `uninstall` removes this block."
BLOCK_END = "# <<< code-review-graph install files <<<"

# The only entry shape install writes: one path anchored to the folder of the
# .gitignore, made of plain characters, so no entry can act as a glob or a
# negation. Anything else inside the markers was written by hand.
_ENTRY = re.compile(r"/[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*/?")


@dataclass(frozen=True)
class Plan:
    """What was on disk before install wrote anything."""

    files: tuple[str, ...]
    existed: frozenset[str]


@dataclass
class Result:
    """What happened to the block.

    ``status`` is one of ``"written"``, ``"unchanged"``, ``"edited"`` (the
    block was changed by hand and is left alone), ``"not-git"`` or
    ``"error"``.
    """

    status: str
    added: list[str] = field(default_factory=list)
    entries: list[str] = field(default_factory=list)
    detail: str = ""


def _relative(repo_root: Path, path: Path) -> str | None:
    """Return ``path`` relative to the repository, or None when outside it."""
    try:
        relative = os.path.normpath(os.path.relpath(path, repo_root))
    except ValueError:  # another drive on Windows
        return None
    parts = PurePath(relative).parts
    if not parts or relative == "." or parts[0] in ("..", ".git") or os.path.isabs(relative):
        return None
    return PurePosixPath(*parts).as_posix()


def repo_files(
    repo_root: Path,
    target: str,
    *,
    with_skills: bool,
    with_hooks: bool,
    with_instructions: bool,
) -> list[str]:
    """Return the files ``install`` may write inside the repository.

    Follows the steps in ``cli._handle_init`` for the same options. Files
    outside the repository, such as user-level configs and ``.git/hooks``, are
    left out because no ``.gitignore`` in the repository applies to them.
    """
    everything = target == "all"
    files: list[Path] = [
        spec["config_path"](repo_root)
        for spec in skills.platforms_for_target(repo_root, target).values()
    ]
    # The MCP step runs first, so a settings file it writes is already there
    # when the hook step backs that file up.
    config_files = set(files)
    # Codex writes under CODEX_HOME, which can be a folder in the work tree.
    codex = target in ("codex", "all")
    codex_home = codex_skill.codex_home()

    if with_skills:
        slugs = [name.rsplit(".", 1)[0] for name in skills._SKILLS]
        for folder, owner in ((".claude", "claude"), (".gemini", "gemini-cli"),
                              (".codebuddy", "codebuddy")):
            if everything or target == owner:
                files.extend(repo_root / folder / "skills" / slug / "SKILL.md" for slug in slugs)
        if everything or target == "qoder":
            files.extend(
                repo_root / ".qoder" / "skills" / name / "SKILL.md"
                for name in skills.bundled_skill_names()
            )
        if target == "codex" or (everything and skills.PLATFORMS["codex"]["detect"]()):
            try:
                bundled = codex_skill._iter_resource_files(codex_skill.bundled_skill_dir())
            except OSError:  # install reports the missing bundle itself
                bundled = {}
            skill_dir = codex_skill.codex_skill_dir()
            files.extend(skill_dir / name for name in (*bundled, codex_skill.MANIFEST_NAME))

    if with_hooks:
        settings_folders = [
            folder
            for folder, owner in ((".claude", "claude"), (".qoder", "qoder"),
                                  (".codebuddy", "codebuddy"), (".gemini", "gemini-cli"))
            if everything or target == owner
        ]
        for folder in settings_folders:
            settings = repo_root / folder / "settings.json"
            files.append(settings)
            # A settings file that is already there is backed up first.
            if settings.exists() or settings in config_files:
                files.append(repo_root / folder / "settings.json.bak")
        if codex:
            codex_hooks = codex_home / "hooks.json"
            files.append(codex_hooks)
            if codex_hooks.exists():
                files.append(codex_home / "hooks.json.bak")
        if everything or target == "gemini-cli":
            files.extend(
                repo_root / ".gemini" / "hooks" / name
                for name in skills._GEMINI_CLI_HOOK_FILENAMES
            )
        if target in ("all", "claude", "qoder", "codex"):
            # Usually .git/hooks, which is skipped below, but core.hooksPath
            # can point at a folder inside the work tree.
            hooks_dir = skills._git_hooks_dir(repo_root)
            if hooks_dir is not None:
                files.extend(
                    hooks_dir / name for name in ("pre-commit", *skills._GIT_SYNC_HOOK_NAMES)
                )

    if with_instructions:
        if target in ("claude", "all"):
            files.append(repo_root / "CLAUDE.md")
        for filename, owners in skills._PLATFORM_INSTRUCTION_FILES.items():
            if everything or target in owners:
                files.append(repo_root / filename)

    relative = (_relative(repo_root, path) for path in files)
    return sorted({path for path in relative if path is not None})


def _with_folders(files: Iterable[str]) -> dict[str, bool]:
    """Map every file and every folder above it to whether it is a folder."""
    paths: dict[str, bool] = {}
    for file in files:
        paths.setdefault(file, False)
        for parent in PurePosixPath(file).parents:
            if parent.as_posix() == ".":
                break
            paths[parent.as_posix()] = True
    return paths


def plan(
    repo_root: Path,
    target: str,
    *,
    with_skills: bool,
    with_hooks: bool,
    with_instructions: bool,
) -> Plan:
    """Record which of install's paths exist. Call it before install writes."""
    files = repo_files(
        repo_root,
        target,
        with_skills=with_skills,
        with_hooks=with_hooks,
        with_instructions=with_instructions,
    )
    existed = frozenset(
        path for path in _with_folders(files) if os.path.lexists(repo_root / path)
    )
    return Plan(files=tuple(files), existed=existed)


def _git(repo_root: Path, args: list[str], stdin: str | None = None) -> str | None:
    try:
        # Always feed stdin, even empty, so git never reads the caller's stdin.
        result = subprocess.run(
            ["git", *args],
            cwd=str(repo_root),
            input=stdin or "",
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("git %s failed in %s: %s", args[0], repo_root, exc)
        return None
    # check-ignore exits 1 when nothing is ignored; that is not a failure.
    if result.returncode not in (0, 1):
        return None
    return result.stdout


def _in_git_work_tree(repo_root: Path) -> bool:
    out = _git(repo_root, ["rev-parse", "--is-inside-work-tree"])
    return out is not None and out.strip() == "true"


class _GitError(Exception):
    """git could not say which files it tracks."""


def _tracked(repo_root: Path, paths: Iterable[str]) -> set[str]:
    """Return the tracked files at or under any of ``paths``.

    Raises :class:`_GitError` rather than guess: an empty answer would let a
    tracked file into the block.
    """
    names = [path.rstrip("/") for path in paths]
    if not names:
        return set()
    out = _git(repo_root, ["--literal-pathspecs", "ls-files", "-z", "--", *names])
    if out is None:
        raise _GitError("git ls-files failed")
    return {name for name in out.split("\0") if name}


def _holds_tracked(path: str, tracked: set[str]) -> bool:
    stem = path.rstrip("/")
    return any(name == stem or name.startswith(stem + "/") for name in tracked)


def _ignored(repo_root: Path, entries: list[str]) -> set[str]:
    """Return the entries some ignore rule already covers.

    A folder entry keeps its trailing slash, which tells git it is a folder
    even before it exists.
    """
    if not entries:
        return set()
    out = _git(repo_root, ["check-ignore", "-z", "--stdin"], stdin="\0".join(entries) + "\0")
    return {name for name in (out or "").split("\0") if name}


def _created_entries(repo_root: Path, install_plan: Plan, dry_run: bool) -> list[str]:
    """Return the top-most paths this run created, folders ending with ``/``."""
    paths = _with_folders(install_plan.files)
    created = {
        path
        for path in paths
        if path not in install_plan.existed
        and (dry_run or planned_files.lexists(repo_root / path))
    }
    tops = sorted(path for path in created if PurePosixPath(path).parent.as_posix() not in created)
    tracked = _tracked(repo_root, tops)

    entries: list[str] = []

    def visit(path: str) -> None:
        if not paths[path]:
            if path not in tracked:
                entries.append(path)
            return
        if not _holds_tracked(path, tracked):
            entries.append(path + "/")
            return
        # Tracked files were deleted from this folder; list only the new parts.
        for child in sorted(c for c in created if PurePosixPath(c).parent.as_posix() == path):
            visit(child)

    for top in tops:
        visit(top)
    # A name the entry format cannot spell (a space, a glob character) is left
    # out rather than escaped, so every line in the block stays recognisable.
    entries = [entry for entry in entries if _ENTRY.fullmatch(f"/{entry}")]
    ignored = _ignored(repo_root, entries)
    return [entry for entry in entries if entry not in ignored]


def _find_block(lines: list[str]) -> tuple[int, int] | str | None:
    """Return the block's first and last line index, ``"edited"`` or None."""
    bare = [line.rstrip("\r\n") for line in lines]
    starts = [index for index, line in enumerate(bare) if line == BLOCK_START]
    ends = [index for index, line in enumerate(bare) if line == BLOCK_END]
    if not starts and not ends:
        return None
    if len(starts) != 1 or len(ends) != 1 or ends[0] < starts[0]:
        return "edited"
    start, end = starts[0], ends[0]
    for line in bare[start + 1 : end]:
        if line != BLOCK_NOTE and not _ENTRY.fullmatch(line):
            return "edited"
    return start, end


def _block_entries(lines: list[str], start: int, end: int) -> list[str]:
    return [
        line.rstrip("\r\n")[1:]
        for line in lines[start + 1 : end]
        if line.rstrip("\r\n") != BLOCK_NOTE
    ]


def _render(entries: list[str], newline: str) -> str:
    body = [BLOCK_START, BLOCK_NOTE, *(f"/{entry}" for entry in entries), BLOCK_END]
    return newline.join(body) + newline


def _replace_block(
    lines: list[str], start: int, end: int, entries: list[str], newline: str
) -> str:
    """Return the text with the block at ``start``..``end`` holding ``entries``.

    No entries removes the block. A block added to a file with no final
    newline brought the newline that ends the line before it, and leaves its
    own last line unended to say so. Removing such a block takes that newline
    back, so the file ends as it did before.
    """
    before = "".join(lines[:start])
    owns_newline = start > 0 and end == len(lines) - 1 and not lines[end].endswith("\n")
    if not owns_newline:
        block = _render(entries, newline) if entries else ""
        return before + block + "".join(lines[end + 1 :])
    if entries:
        return before + _render(entries, newline).removesuffix(newline)
    previous = lines[start - 1]
    return before[: -2 if previous.endswith("\r\n") else -1]


def remove_block(text: str) -> tuple[str, str]:
    """Remove the block from ``text``, used by ``uninstall``.

    Returns the new text and ``"removed"``, ``"absent"`` or ``"edited"``. The
    block's own lines, and the newline it added before itself, are the only
    bytes removed, so the text around it comes back exactly as it was. A
    block that was edited by hand is left alone.
    """
    lines = text.splitlines(keepends=True)
    found = _find_block(lines)
    if found is None:
        return text, "absent"
    if isinstance(found, str):
        return text, found
    start, end = found
    return _replace_block(lines, start, end, [], "\n"), "removed"


def apply(repo_root: Path, install_plan: Plan, *, dry_run: bool) -> Result:
    """Merge what this run created into the block.

    With ``dry_run`` nothing is written: every planned path that does not
    exist yet is taken as created, and the result says what would change.
    ``install`` itself calls this inside its plan (#173), where the files the
    plan creates count as created and the write is held with the rest.
    """
    if not _in_git_work_tree(repo_root):
        return Result("not-git")
    gitignore = repo_root / ".gitignore"
    if gitignore.is_symlink():
        # uninstall never edits through a symlink, so install does not either.
        return Result("error", detail=f"{gitignore} is a symlink; left alone")
    try:
        # Bytes, so the file's own line endings survive the round trip.
        text = (
            planned_files.read_bytes(gitignore).decode("utf-8")
            if planned_files.exists(gitignore)
            else ""
        )
    except (OSError, UnicodeError) as exc:
        logger.warning("Could not read %s: %s", gitignore, exc)
        return Result("error", detail=str(exc))

    lines = text.splitlines(keepends=True)
    found = _find_block(lines)
    if isinstance(found, str):
        return Result("edited")

    old: list[str] = []
    try:
        if found is not None:
            old = _block_entries(lines, *found)
            # Keep an earlier entry while its path is still there and untracked.
            # A dry run writes nothing, so a planned path counts as there:
            # the real run writes it again.
            planned = _with_folders(install_plan.files) if dry_run else {}
            tracked = _tracked(repo_root, old)
            old = [
                entry
                for entry in old
                if (
                    planned_files.lexists(repo_root / entry.rstrip("/"))
                    or entry.rstrip("/") in planned
                )
                and not _holds_tracked(entry, tracked)
            ]
        created = _created_entries(repo_root, install_plan, dry_run)
    except _GitError as exc:
        return Result("error", detail=str(exc))
    entries = sorted(set(old) | set(created))
    added = [entry for entry in entries if entry not in old]

    newline = "\r\n" if lines and lines[0].endswith("\r\n") else "\n"
    if found is not None:
        new_text = _replace_block(lines, *found, entries, newline)
    elif entries and text and not text.endswith("\n"):
        # A last line without a newline has to be ended first. The block then
        # leaves its own last line unended, so uninstall knows to take that
        # one newline back.
        new_text = text + newline + _render(entries, newline).removesuffix(newline)
    elif entries:
        new_text = text + _render(entries, newline)
    else:
        new_text = text

    if new_text == text:
        return Result("unchanged", entries=entries)
    if not dry_run:
        try:
            planned_files.write_bytes(gitignore, new_text.encode("utf-8"))
        except OSError as exc:
            logger.warning("Could not write %s: %s", gitignore, exc)
            return Result("error", detail=str(exc))
    return Result("written", added=added, entries=entries)
