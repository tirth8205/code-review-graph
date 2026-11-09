"""Hold the file changes ``install`` makes until they have been shown (#173).

The installer reads and writes files only through the helpers here. With no
plan active they work on the disk directly, as they always did. Inside
:func:`planning` a write is kept in memory, later reads in the same run see
it, and nothing reaches the disk until :meth:`Plan.apply`. ``install
--dry-run`` prints the plan as diffs and drops it. A real install lists the
plan, asks its question, and only then writes, so what is written is what
was shown.
"""

from __future__ import annotations

import contextlib
import difflib
import errno
import io
import os
import shutil
import stat
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Union

PathLike = Union[str, "os.PathLike[str]"]

#: Diff lines shown for one file before the rest is cut with a note.
DIFF_LINE_LIMIT = 60
# A file only a few lines over the limit is shown whole.
_MIN_CUT = 5


@dataclass
class FileChange:
    """One file as it is on disk and as the plan leaves it.

    ``before`` and ``after`` are ``None`` when the file does not exist on that
    side. ``mode`` holds permission bits the plan sets, or ``None`` to leave
    them as they are. ``link`` is set when the plan removes a symlink itself
    rather than the file it points to, and holds where the link pointed.
    """

    path: Path
    before: bytes | None
    after: bytes | None
    before_mode: int | None
    mode: int | None
    link: str | None = None

    def is_change(self) -> bool:
        if self.link is not None or self.after != self.before:
            return True
        return self.after is not None and _mode_changes(self.before_mode, self.mode)


def _mode_changes(before: int | None, after: int | None) -> bool:
    # Windows keeps only a read-only flag, so a requested mode says nothing.
    if after is None or os.name == "nt":
        return False
    return before is None or (before & 0o777) != (after & 0o777)


def _key(path: PathLike) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _real_key(path: PathLike) -> Path:
    # One entry per file, so a write through one name (CLAUDE.md linked to
    # AGENTS.md) is seen through the other and written once.
    return Path(os.path.realpath(os.fspath(path)))


def _disk_state(path: Path) -> tuple[bytes | None, int | None]:
    """Return a regular file's bytes and permission bits, or ``(None, None)``."""
    try:
        info = os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None, None
    if not stat.S_ISREG(info.st_mode):
        return None, None
    return path.read_bytes(), stat.S_IMODE(info.st_mode)


def _encode_text(text: str, encoding: str) -> bytes:
    # The same bytes Path.write_text produces: "\n" becomes the platform's
    # line ending, as text mode does.
    if os.linesep != "\n":
        text = text.replace("\n", os.linesep)
    return text.encode(encoding)


def _decode_text(data: bytes, encoding: str, errors: str) -> str:
    # The same text Path.read_text returns, universal newlines included.
    with io.TextIOWrapper(io.BytesIO(data), encoding=encoding, errors=errors) as reader:
        return reader.read()


class Plan:
    """File writes held in memory, in the order they were made."""

    def __init__(self) -> None:
        self._files: dict[Path, FileChange] = {}

    # -- reads ---------------------------------------------------------------

    def _link_entry(self, path: PathLike) -> FileChange | None:
        entry = self._files.get(_key(path))
        return entry if entry is not None and entry.link is not None else None

    def _planned(self, path: PathLike) -> FileChange | None:
        return self._link_entry(path) or self._files.get(_real_key(path))

    def exists(self, path: PathLike) -> bool:
        planned = self._planned(path)
        if planned is not None:
            return planned.after is not None
        return Path(path).exists()

    def is_file(self, path: PathLike) -> bool:
        planned = self._planned(path)
        if planned is not None:
            return planned.after is not None
        return Path(path).is_file()

    def read_bytes(self, path: PathLike) -> bytes:
        planned = self._planned(path)
        if planned is None:
            return Path(path).read_bytes()
        if planned.after is None:
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(path))
        return planned.after

    def lexists(self, path: PathLike) -> bool:
        """Return whether ``path`` will exist, as a file or as a folder of one."""
        planned = self._planned(path)
        if planned is not None:
            return planned.after is not None
        key = _real_key(path)
        for entry in self._files.values():
            if entry.after is not None and key in entry.path.parents:
                return True
        return os.path.lexists(path)

    def mode(self, path: PathLike) -> int | None:
        """Return the permission bits ``path`` will have, if known."""
        planned = self._planned(path)
        if planned is None:
            return _disk_state(_real_key(path))[1]
        if planned.after is None:
            return None
        return planned.mode if planned.mode is not None else planned.before_mode

    # -- writes --------------------------------------------------------------

    def _entry(self, path: PathLike) -> FileChange:
        link_entry = self._link_entry(path)
        if link_entry is not None:
            return link_entry
        key = _real_key(path)
        entry = self._files.get(key)
        if entry is None:
            before, before_mode = _disk_state(key)
            entry = FileChange(key, before, before, before_mode, None)
            self._files[key] = entry
        return entry

    def _check_can_create(self, key: Path) -> None:
        """Raise the error a real write would, so a dry run reports it too."""
        planned = self._files.get(key)
        if planned is not None and planned.after is not None:
            return
        if key.is_dir():
            raise IsADirectoryError(errno.EISDIR, os.strerror(errno.EISDIR), str(key))
        for parent in key.parents:
            planned = self._files.get(parent)
            if planned is not None and planned.after is not None:
                raise NotADirectoryError(errno.ENOTDIR, os.strerror(errno.ENOTDIR), str(parent))
            if parent.is_dir():
                return
            if os.path.lexists(parent):
                raise NotADirectoryError(errno.ENOTDIR, os.strerror(errno.ENOTDIR), str(parent))

    def write_bytes(self, path: PathLike, data: bytes, mode: int | None = None) -> None:
        self._check_can_create(_real_key(path))
        entry = self._entry(path)
        entry.after = data
        if mode is not None:
            entry.mode = mode

    def chmod(self, path: PathLike, mode: int) -> None:
        if not self.is_file(path):
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(path))
        self._entry(path).mode = mode

    def unlink(self, path: PathLike) -> None:
        if not self.is_file(path):
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(path))
        key = _key(path)
        if self._link_entry(path) is None and os.path.islink(key):
            # Removing a symlink removes the link, never the file it points to.
            before, before_mode = _disk_state(key)
            self._files[key] = FileChange(
                key, before, before, before_mode, None, link=os.readlink(key)
            )
        entry = self._entry(path)
        entry.after = None
        entry.mode = None

    # -- result --------------------------------------------------------------

    def changes(self) -> list[FileChange]:
        """Return every file the plan changes, in the order first touched."""
        return [entry for entry in self._files.values() if entry.is_change()]

    def checkpoint(self) -> dict[Path, tuple[bytes | None, int | None]]:
        """Return the plan as it stands, for :meth:`changes_since` and :meth:`rollback`."""
        return {key: (entry.after, entry.mode) for key, entry in self._files.items()}

    def changes_since(
        self, saved: dict[Path, tuple[bytes | None, int | None]]
    ) -> list[FileChange]:
        """Return the changes made to the plan after ``saved`` was taken."""
        return [
            entry
            for key, entry in self._files.items()
            if entry.is_change() and saved.get(key) != (entry.after, entry.mode)
        ]

    def rollback(self, saved: dict[Path, tuple[bytes | None, int | None]]) -> None:
        """Drop every change made to the plan after ``saved`` was taken."""
        for key in list(self._files):
            if key in saved:
                self._files[key].after, self._files[key].mode = saved[key]
            else:
                del self._files[key]

    def apply(self) -> list[tuple[Path, str]]:
        """Write the plan to disk and return ``(path, reason)`` for each failure.

        A file whose content or permissions changed on disk after the plan was
        made is left alone, so an edit made while the user read the preview is
        never overwritten.
        """
        failures: list[tuple[Path, str]] = []
        for change in self.changes():
            try:
                if not _unchanged_on_disk(change):
                    failures.append(
                        (change.path, "it changed after the preview, so it was left alone")
                    )
                    continue
                if change.link is not None:
                    os.unlink(change.path)
                elif change.after is None:
                    change.path.unlink()
                if change.after is None:
                    continue
                if change.after != change.before or change.link is not None:
                    change.path.parent.mkdir(parents=True, exist_ok=True)
                    change.path.write_bytes(change.after)
                if change.mode is not None:
                    os.chmod(change.path, change.mode)
            except OSError as exc:
                failures.append((change.path, str(exc)))
        return failures


def _unchanged_on_disk(change: FileChange) -> bool:
    if change.link is not None:
        return os.path.islink(change.path) and os.readlink(change.path) == change.link
    return _disk_state(change.path) == (change.before, change.before_mode)


_ACTIVE: ContextVar[Plan | None] = ContextVar("code_review_graph_install_plan", default=None)


def active() -> Plan | None:
    """Return the plan writes go to, or ``None`` when they go to disk."""
    return _ACTIVE.get()


@contextlib.contextmanager
def planning() -> Iterator[Plan]:
    """Hold every write made through this module until ``Plan.apply``."""
    plan = Plan()
    token = _ACTIVE.set(plan)
    try:
        yield plan
    finally:
        _ACTIVE.reset(token)


# -- helpers the installer calls ---------------------------------------------


def exists(path: PathLike) -> bool:
    plan = active()
    return plan.exists(path) if plan is not None else Path(path).exists()


def is_file(path: PathLike) -> bool:
    plan = active()
    return plan.is_file(path) if plan is not None else Path(path).is_file()


def lexists(path: PathLike) -> bool:
    """Like ``os.path.lexists``, counting files and folders the plan creates."""
    plan = active()
    return plan.lexists(path) if plan is not None else os.path.lexists(path)


def read_bytes(path: PathLike) -> bytes:
    plan = active()
    return plan.read_bytes(path) if plan is not None else Path(path).read_bytes()


def read_text(path: PathLike, encoding: str = "utf-8", errors: str = "strict") -> str:
    plan = active()
    if plan is None or plan._planned(path) is None:
        return Path(path).read_text(encoding=encoding, errors=errors)
    return _decode_text(plan.read_bytes(path), encoding, errors)


def write_text(path: PathLike, text: str, encoding: str = "utf-8") -> None:
    """Write ``text`` as ``Path.write_text`` would, creating parent folders."""
    plan = active()
    if plan is not None:
        plan.write_bytes(path, _encode_text(text, encoding))
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding=encoding)


def write_bytes(path: PathLike, data: bytes) -> None:
    """Write ``data`` as it is, with no line ending change, creating parent folders."""
    plan = active()
    if plan is not None:
        plan.write_bytes(path, data)
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def copy_file(source: PathLike, destination: PathLike) -> None:
    """Copy content and permission bits, as ``shutil.copy2`` does."""
    plan = active()
    if plan is None:
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        return
    plan.write_bytes(destination, plan.read_bytes(source), mode=plan.mode(source))


def chmod(path: PathLike, mode: int) -> None:
    plan = active()
    if plan is not None:
        plan.chmod(path, mode)
    else:
        os.chmod(path, mode)


def unlink(path: PathLike) -> None:
    plan = active()
    if plan is not None:
        plan.unlink(path)
    else:
        Path(path).unlink()


# -- showing a plan ----------------------------------------------------------


def display_path(path: PathLike, repo_root: PathLike | None = None) -> str:
    """Show ``path`` relative to the repository or home folder when it is inside."""
    target = _key(path)
    bases: list[tuple[str, str]] = []
    if repo_root is not None:
        for base in {os.path.abspath(repo_root), os.path.realpath(repo_root)}:
            bases.append((base, ""))
    home = str(Path.home())
    for base in {os.path.abspath(home), os.path.realpath(home)}:
        bases.append((base, "~/"))
    for candidate in (str(target), os.path.realpath(target)):
        for base, prefix in bases:
            try:
                relative = Path(candidate).relative_to(base)
            except ValueError:
                continue
            if relative.parts:
                return prefix + relative.as_posix()
    return target.as_posix()


def diff_label(shown: str, side: str) -> str:
    """Name a file in a diff header: ``a/`` or ``b/`` only for repository paths."""
    if shown.startswith("~/") or Path(shown).is_absolute():
        return shown
    return f"{side}/{shown}"


def describe(change: FileChange) -> str:
    """Say in a word or two what the plan does to the file."""
    if change.before is None:
        return "new"
    if change.after is None:
        return "removed"
    if change.after == change.before:
        return "permissions"
    return "update"


def _lines(data: bytes | None) -> list[str] | None:
    if data is None:
        return []
    if b"\0" in data:
        return None
    return data.decode("utf-8", errors="replace").splitlines()


def render(
    changes: Iterable[FileChange],
    repo_root: PathLike | None = None,
    limit: int = DIFF_LINE_LIMIT,
) -> str:
    """Return each change as a short unified diff, cut at ``limit`` lines a file."""
    blocks: list[str] = []
    for change in changes:
        shown = display_path(change.path, repo_root)
        out: list[str] = []
        if change.before is not None and change.after is not None and _mode_changes(
            change.before_mode, change.mode
        ):
            out.append(
                f"mode {change.before_mode or 0:o} -> {change.mode or 0:o}: {shown}"
            )
        elif change.before is None and change.mode is not None and change.mode & 0o111:
            out.append(f"new file, executable: {shown}")

        if change.after != change.before:
            before, after = _lines(change.before), _lines(change.after)
            if before is None or after is None:
                size = len(change.after or b"")
                out.append(f"binary file: {shown} ({size} bytes)")
            else:
                diff = list(
                    difflib.unified_diff(
                        before,
                        after,
                        fromfile=(
                            "/dev/null" if change.before is None else diff_label(shown, "a")
                        ),
                        tofile="/dev/null" if change.after is None else diff_label(shown, "b"),
                        lineterm="",
                    )
                )
                if not diff:
                    # Same lines: only an empty new file or line endings differ.
                    what = "new empty file" if change.before is None else "line endings change"
                    out.append(f"{what}: {shown}")
                head, body = diff[:2], diff[2:]
                out.extend(head)
                # Cutting only a few lines saves nothing, so show those in full.
                if len(body) <= limit + _MIN_CUT:
                    out.extend(body)
                else:
                    out.extend(body[:limit])
                    out.append(f"... {len(body) - limit} more lines not shown")
        blocks.append("\n".join(out))
    return "\n\n".join(blocks) + ("\n" if blocks else "")
