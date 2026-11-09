"""Change impact analysis for code review.

Maps git/svn diffs to affected functions, flows, communities, and test coverage
gaps. Produces risk-scored, priority-ordered review guidance.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .constants import GIT_TIMEOUT as _GIT_TIMEOUT
from .constants import SECURITY_KEYWORDS as _SECURITY_KEYWORDS
from .constants import env_float, env_int
from .errors import ChangeDiscoveryError
from .flows import _trace_single_flow, detect_entry_points, get_affected_flows
from .graph import (
    GraphEdge,
    GraphNode,
    GraphStore,
    _sanitize_name,
    node_to_dict,
    qualified_name_of,
)
from .parser import CodeParser, NodeInfo, _repo_relative_path, is_test_file, normalize_file_path

logger = logging.getLogger(__name__)


# Lifecycle and construction methods are exercised implicitly by every test
# that touches their class, so listing them as test gaps is noise that
# inflates the gap count and the Untested summary (#850). Names cover
# PHPUnit/xUnit and Python unittest conventions.
_TEST_GAP_EXEMPT_NAMES = frozenset({
    "setUp", "tearDown", "setUpBeforeClass", "tearDownAfterClass",
    "setup_method", "teardown_method", "setUpClass", "tearDownClass",
    "__construct", "__init__", "__destruct",
})


# ``up`` and ``down`` are too common to exempt everywhere, so they are exempt
# only in a migration file: one in a ``migrations`` folder (Laravel, Phinx,
# Doctrine, Knex) or a ``migrate`` folder (Rails), inside the repository (#850).
_MIGRATION_METHOD_NAMES = frozenset({"up", "down"})
_MIGRATION_DIRS = frozenset({"migrations", "migrate"})


def _is_migration_file(file_path: str, repo_root: str | Path | None) -> bool:
    """Whether *file_path* sits in a migration folder inside the repository.

    Read from the path relative to the root, as test folders are: a checkout
    under a directory named ``migrations`` must not exempt the whole repository.
    """
    relative = _repo_relative_path(normalize_file_path(file_path), repo_root)
    if relative is None:
        return False
    return any(part in _MIGRATION_DIRS for part in relative.split("/")[:-1])


def is_test_gap_exempt(node: GraphNode, repo_root: str | Path | None = None) -> bool:
    """Return whether a lifecycle node is exempt from test-gap reporting."""
    if node.kind != "Function":
        return False
    if node.name in _TEST_GAP_EXEMPT_NAMES:
        return True
    return node.name in _MIGRATION_METHOD_NAMES and _is_migration_file(
        node.file_path, repo_root,
    )


def _is_test_lifecycle(node: GraphNode, store: GraphStore) -> bool:
    """Return whether *node* is a lifecycle method of real test code.

    The exemption matches on a name, and production code uses these names too:
    an untested ``__init__`` can hold real logic, so it keeps its coverage risk
    even though it is left out of the gap list.
    """
    return is_test_gap_exempt(node) and (
        node.is_test or is_test_file(node.file_path, store.get_repo_root())
    )


# Being reached by a tested caller buys no risk discount at all. It was worth
# 0.03 briefly and that was wrong twice over. Evidentially: a static CALLS path
# shows the symbol is reachable, not that any test runs it -- about 6% of the
# symbols the walk reaches are never executed by this repository's own suite.
# Mechanically: every other term here moves in multiples of 0.05 and the scores
# quantize hard (one real delta produced 15 distinct scores over 49 symbols,
# with 25 tied at 0.05), so a 0.03 nudge could never leave a symbol tied -- it
# dropped it below its whole tie class, and `review_priorities` is
# `sorted(...)[:10]`. On the delta of #1047 that evicted the one genuinely
# untested symbol from the table the Action renders. The classification is
# reported in its own field instead, where it informs without displacing.
_SAFE_GIT_REF = re.compile(r"^[A-Za-z0-9_.~^/@{}\-]+$")
_SAFE_SVN_REV = re.compile(r"^r?\d+(:r?\d+|:HEAD|:BASE|:COMMITTED)?$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# 1. parse_git_diff_ranges / parse_svn_diff_ranges
# ---------------------------------------------------------------------------


def _vcs_unavailable(tool: str, exc: BaseException) -> ChangeDiscoveryError:
    """Describe a VCS command that could not be run at all.

    Mirrors :func:`code_review_graph.incremental._vcs_unavailable`; a missing
    binary and a timeout say nothing about the working tree, so no caller may
    read them as "no lines changed".
    """
    if isinstance(exc, subprocess.TimeoutExpired):
        return ChangeDiscoveryError(
            f"could not determine the changed lines: {tool} timed out after "
            f"{_GIT_TIMEOUT}s. Raise CRG_GIT_TIMEOUT, or re-run when the "
            "repository is not busy."
        )
    return ChangeDiscoveryError(
        f"could not determine the changed lines: {tool} could not be run "
        f"({exc}). Install {tool} and make sure it is on PATH."
    )


def parse_git_diff_ranges(
    repo_root: str,
    base: str = "HEAD~1",
    *,
    require_vcs: bool = False,
) -> dict[str, list[tuple[int, int]]]:
    """Run ``git diff --unified=0`` and extract changed line ranges per file.

    Args:
        repo_root: Absolute path to the repository root.
        base: Git ref to diff against (default: ``HEAD~1``).
        require_vcs: Raise
            :class:`~code_review_graph.errors.ChangeDiscoveryError` when git
            cannot be run at all, instead of returning an empty mapping that
            a caller would read as "no lines changed".

    Returns:
        Mapping of file paths to lists of ``(start_line, end_line)`` tuples.
        Returns an empty dict on error.
    """
    if not _SAFE_GIT_REF.match(base):
        logger.warning("Invalid git ref rejected: %s", base)
        return {}
    try:
        result = subprocess.run(
            ["git", "diff", "--unified=0", base, "--"],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=repo_root,
            timeout=_GIT_TIMEOUT,
        )
        if result.returncode != 0:
            logger.warning("git diff failed (rc=%d): %s", result.returncode, result.stderr[:200])
            return {}
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("git diff error: %s", exc)
        if require_vcs:
            raise _vcs_unavailable("git", exc) from exc
        return {}

    return _parse_unified_diff(result.stdout)


def parse_svn_diff_ranges(
    repo_root: str,
    rev_range: str | None = None,
    *,
    require_vcs: bool = False,
) -> dict[str, list[tuple[int, int]]]:
    """Run ``svn diff`` and extract changed line ranges per file.

    Args:
        repo_root: Absolute path to the SVN working copy root.
        rev_range: Optional SVN revision range in ``rXXX:HEAD`` format.
            When *None*, diffs the working copy against BASE (local changes).

    Returns:
        Mapping of file paths to lists of ``(start_line, end_line)`` tuples.
        Returns an empty dict on error.
    """
    cmd = ["svn", "diff", "--non-interactive"]
    if rev_range:
        if not _SAFE_SVN_REV.match(rev_range):
            logger.warning("Invalid SVN revision range rejected: %s", rev_range)
            return {}
        cmd.extend(["-r", rev_range])
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=repo_root,
            timeout=_GIT_TIMEOUT,
        )
        if result.returncode != 0:
            logger.warning("svn diff failed (rc=%d): %s", result.returncode, result.stderr[:200])
            return {}
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("svn diff error: %s", exc)
        if require_vcs:
            raise _vcs_unavailable("svn", exc) from exc
        return {}

    return _parse_unified_diff(result.stdout)


def parse_diff_ranges(
    repo_root: str,
    base: str = "HEAD~1",
    *,
    require_vcs: bool = False,
) -> dict[str, list[tuple[int, int]]]:
    """Auto-detect VCS and return changed line ranges per file.

    Dispatches to :func:`parse_git_diff_ranges` for Git repositories and
    :func:`parse_svn_diff_ranges` for SVN working copies.

    Args:
        repo_root: Absolute path to the repository/working-copy root.
        base: For Git: the ref to diff against (default ``HEAD~1``).
              For SVN: an optional revision range (e.g. ``"r100:HEAD"``);
              when *base* is not a valid SVN revision, working-copy changes
              (``svn diff``) are used instead.
        require_vcs: Raise
            :class:`~code_review_graph.errors.ChangeDiscoveryError` when the
            VCS binary is missing or times out, instead of returning ``{}``.
    """
    root_path = Path(repo_root)
    if (root_path / ".svn").exists():
        rev_range = base if _SAFE_SVN_REV.match(base) else None
        return parse_svn_diff_ranges(repo_root, rev_range, require_vcs=require_vcs)
    return parse_git_diff_ranges(repo_root, base, require_vcs=require_vcs)


_C_QUOTE_ESCAPES = {
    "a": "\a", "b": "\b", "f": "\f", "n": "\n",
    "r": "\r", "t": "\t", "v": "\v", "\\": "\\", '"': '"',
}


def _unquote_c_path(quoted: str) -> str:
    """Decode git's C-style quoted path back to text.

    With ``core.quotePath`` (the default) git writes a path containing a
    non-ASCII or control byte as ``"src/caf\\303\\251.py"``: double-quoted,
    with backslash escapes and three-digit octal escapes for raw bytes. The
    octal escapes are UTF-8 bytes, so they are reassembled before decoding.
    """
    raw = bytearray()
    index, end = 1, len(quoted) - 1
    while index < end:
        char = quoted[index]
        if char != "\\":
            raw.extend(char.encode("utf-8"))
            index += 1
            continue
        nxt = quoted[index + 1] if index + 1 < end else ""
        if nxt in _C_QUOTE_ESCAPES:
            raw.extend(_C_QUOTE_ESCAPES[nxt].encode("utf-8"))
            index += 2
            continue
        octal = ""
        while len(octal) < 3 and index + 1 + len(octal) < end:
            digit = quoted[index + 1 + len(octal)]
            if digit not in "01234567":
                break
            octal += digit
        if octal:
            raw.append(int(octal, 8) & 0xFF)
            index += 1 + len(octal)
            continue
        raw.extend(b"\\")
        index += 1
    return raw.decode("utf-8", "replace")


def _diff_header_path(rest: str, prefix: str = "b/") -> str | None:
    """Extract the path from the text after ``+++ `` (or ``--- ``).

    Git writes the path three ways, and only the plainest one is a bare
    ``b/path``: it appends a TAB when the path contains a space, and
    C-quotes the whole ``"b/path"`` when it contains a non-ASCII or control
    byte. Returns None for ``/dev/null``, the post-image of a deleted file
    (and the pre-image of a new one). *prefix* is ``"a/"`` for ``--- ``.
    """
    rest = rest.rstrip("\r")
    if rest.startswith('"'):
        closing = rest.rfind('"')
        path = _unquote_c_path(rest[: closing + 1])
    else:
        # The trailing TAB is a separator, never part of the path: git emits
        # one only when the path itself contains a space.
        path = rest.split("\t", 1)[0]
    if path.startswith(prefix):
        path = path[len(prefix):]
        return path or None
    return None


# The post-image header, in every spelling git writes it: a bare ``b/path``,
# the same with the TAB git appends when the path contains a space, the
# C-quoted ``"b/path"`` it uses when the path contains a non-ASCII or
# control byte, and ``/dev/null`` for a deleted file. Anchored on those
# three shapes so an added line that merely starts with "+++ " is not read
# as a header.
_POST_IMAGE_HEADER = re.compile(
    r'^\+\+\+ (/dev/null|"b/(?:[^"\\]|\\.)*"|b/.*)$'
)
# The pre-image header, in the same spellings. A deleted line whose text
# starts with "-- a/" also reads "--- a/...", so this one only counts when the
# next line is a post-image header, which is how git always writes the pair.
_PRE_IMAGE_HEADER = re.compile(
    r'^--- (/dev/null|"a/(?:[^"\\]|\\.)*"|a/.*)$'
)
_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass(frozen=True)
class PureDeletion:
    """One hunk that removed lines and added none.

    ``old_start``/``old_end`` are the removed lines in the old file.
    ``anchor`` is the new-side line the hole now follows (0 at the top of
    the file): the code on either side of it is code the hunk never touched.
    """

    old_start: int
    old_end: int
    anchor: int


@dataclass
class FileDeletions:
    """The pure-deletion hunks of one file, and where to read its old side."""

    old_path: str
    hunks: list[PureDeletion] = field(default_factory=list)


class DiffRanges(dict):
    """``{path: [(start, end), ...]}`` of new-side lines, plus pure deletions.

    It is the plain mapping every caller already reads, unchanged: a hunk
    that only deletes still appears as its zero-width ``(anchor, anchor)``
    position, which is where a source snippet should look. That position
    must not be used to say which code changed -- the lines on either side of
    a hole are lines the diff left alone -- so ``deletions`` keeps the old
    side of those hunks for :func:`analyze_changes`, and
    :meth:`edit_ranges` gives the mapping without them.

    A deleted file has no new side, and is left out of both, as before: once
    it is gone its importers' calls lose their target, so the graph can no
    longer say who used what was in it.
    """

    def __init__(
        self,
        ranges: dict[str, list[tuple[int, int]]] | None = None,
        deletions: dict[str, FileDeletions] | None = None,
    ) -> None:
        super().__init__(ranges or {})
        self.deletions: dict[str, FileDeletions] = deletions or {}

    def edit_ranges(self) -> dict[str, list[tuple[int, int]]]:
        """The ranges of hunks that added lines, pure deletions taken out."""
        edits: dict[str, list[tuple[int, int]]] = {}
        for path, ranges in self.items():
            kept = list(ranges)
            held = self.deletions.get(path)
            for hunk in held.hunks if held else ():
                # One position per pure deletion, and only one: a real
                # one-line edit can share the spelling.
                position = (hunk.anchor, hunk.anchor)
                if position in kept:
                    kept.remove(position)
            if kept:
                edits[path] = kept
        return edits

    def remapped(self, root: str | Path) -> DiffRanges:
        """Re-key on the graph's absolute paths, keeping the old-side paths.

        Diff keys are forward-slash paths relative to the repository root,
        while the graph stores absolute normalized paths. ``old_path`` stays
        relative: it is what ``git show`` is asked for.
        """
        root_path = Path(root)
        return DiffRanges(
            {normalize_file_path(root_path / k): v for k, v in self.items()},
            {
                normalize_file_path(root_path / k): v
                for k, v in self.deletions.items()
            },
        )


def _parse_unified_diff(diff_text: str) -> DiffRanges:
    """Parse unified diff output into file -> line-range mappings.

    Handles the ``@@ -old,count +new,count @@`` hunk header format. The
    result is a plain mapping of new-side ranges that also carries the old
    side of every pure-deletion hunk; see :class:`DiffRanges`.
    """
    ranges: dict[str, list[tuple[int, int]]] = {}
    deletions: dict[str, FileDeletions] = {}
    current_file: str | None = None
    old_file: str | None = None

    lines = diff_text.splitlines()
    for index, line in enumerate(lines):
        file_match = _POST_IMAGE_HEADER.match(line)
        if file_match:
            current_file = _diff_header_path(file_match.group(1))
            if index == 0 or not _PRE_IMAGE_HEADER.match(lines[index - 1]):
                # No pre-image header for this file: its old side has the
                # same path, not the previous file's.
                old_file = current_file
            continue
        pre_match = _PRE_IMAGE_HEADER.match(line)
        if pre_match and index + 1 < len(lines) and _POST_IMAGE_HEADER.match(
            lines[index + 1]
        ):
            old_file = _diff_header_path(pre_match.group(1), "a/")
            continue

        hunk_match = _HUNK_HEADER.match(line)
        if not hunk_match or current_file is None:
            continue
        old_start = int(hunk_match.group(1))
        old_count = int(hunk_match.group(2)) if hunk_match.group(2) else 1
        start = int(hunk_match.group(3))
        count = int(hunk_match.group(4)) if hunk_match.group(4) else 1
        if count == 0:
            # Pure deletion: nothing was added, so the new side is only the
            # line the hole follows. Still note that position, for the
            # callers that show source, and keep the removed lines.
            end = start
            held = deletions.get(current_file)
            if held is None:
                held = deletions[current_file] = FileDeletions(
                    old_path=old_file or current_file,
                )
            held.hunks.append(
                PureDeletion(old_start, old_start + old_count - 1, start)
            )
        else:
            end = start + count - 1
        ranges.setdefault(current_file, []).append((start, end))

    return DiffRanges(ranges, deletions)


# ---------------------------------------------------------------------------
# 2. compute_file_churn
# ---------------------------------------------------------------------------

_CHURN_SATURATION = 10.0
_CHURN_WEIGHT = 0.15
_NUMSTAT_COUNT = re.compile(r"^(?:\d+|-)$")

# ``git log --since --numstat`` now runs inside MCP tool calls, where an
# agent is blocked on the result, so it gets its own budget rather than the
# 30-second diff timeout: the history walk is capped at a commit count, and a
# slow repository degrades to the pre-churn behaviour (an empty mapping, and
# therefore a zero change-frequency term) instead of hanging the call.
# Read through the shared helpers (#912): a typo in either variable falls
# back to the documented default and warns by name, rather than aborting the
# import of every command with a bare ValueError.
_CHURN_TIMEOUT = env_float("CRG_CHURN_TIMEOUT", 5.0)
_CHURN_MAX_COMMITS = env_int("CRG_CHURN_MAX_COMMITS", 2000)

# Churn counts commits, so the answer only changes when HEAD does: successful
# results are keyed by (repo, commit, window).
_CHURN_CACHE_MAX_ENTRIES = 32
_CHURN_CACHE_LOCK = threading.Lock()
_CHURN_CACHE: dict[tuple[str, str, int], dict[str, int]] = {}

# Failures cannot be keyed by commit, because the commit is the first thing a
# slow or broken repository fails to tell us: ``git rev-parse`` runs under the
# same budget as the walk, so on the repositories the timeout exists for it
# times out too, and a commit-keyed failure cache never gets a key to store
# anything under. That is the whole "pays the timeout once" claim gone -- a
# 5-second rev-parse plus a 5-second walk, on every tool call, for the rest of
# the session.
#
# Churn being unavailable is therefore recorded per (repo, window) for the
# life of the process, ahead of any git call. It is deliberately sticky: the
# condition it records is a property of the repository (no Git, no commits, a
# history too slow to walk inside a tool call), not a transient. Callers that
# outlive a repository's state -- a long-lived MCP server, a test suite --
# clear it with ``clear_churn_cache``.
_CHURN_UNAVAILABLE: set[tuple[str, int]] = set()

# Status values reported alongside the counts, so a degraded review says so
# rather than quietly scoring the change-frequency term at zero.
CHURN_OK = "ok"
CHURN_UNAVAILABLE = "unavailable"
CHURN_OFF = "off"


def clear_churn_cache() -> None:
    """Drop every memoised churn result, successful and failed alike."""
    with _CHURN_CACHE_LOCK:
        _CHURN_CACHE.clear()
        _CHURN_UNAVAILABLE.clear()


def _churn_is_unavailable(fail_key: tuple[str, int]) -> bool:
    with _CHURN_CACHE_LOCK:
        return fail_key in _CHURN_UNAVAILABLE


def _mark_churn_unavailable(fail_key: tuple[str, int]) -> None:
    with _CHURN_CACHE_LOCK:
        if len(_CHURN_UNAVAILABLE) >= _CHURN_CACHE_MAX_ENTRIES:
            _CHURN_UNAVAILABLE.clear()
        _CHURN_UNAVAILABLE.add(fail_key)
    logger.warning(
        "Change-frequency risk disabled for %s: git history could not be "
        "read within %.3gs. Risk scores exclude the churn term.",
        fail_key[0], _CHURN_TIMEOUT,
    )


def _churn_cache_key(repo_root: str, window_days: int) -> tuple[str, str, int] | None:
    """Identify the commit churn was computed at, or None when unknown.

    An unknown commit (no Git, a repository without commits, a ``rev-parse``
    that did not answer in time) is not a transient: it is the same condition
    that stops the walk below from answering, so the caller records churn as
    unavailable rather than retrying it on every subsequent call.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=repo_root,
            timeout=_CHURN_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    head = result.stdout.strip()
    return (repo_root, head, window_days) if head else None


def _parse_numstat(log_text: str) -> dict[str, int]:
    """Parse NUL-terminated ``git log --numstat -z`` records.

    NUL termination is required for correctness: Git's default line format
    quotes unusual paths, while ``-z`` preserves tabs and newlines in file
    names without making the graph-path lookup ambiguous.
    """
    counts: dict[str, int] = {}
    for record in log_text.split("\0"):
        if not record:
            continue
        fields = record.split("\t", 2)
        if len(fields) != 3:
            continue
        added, deleted, path = fields
        if (
            not path
            or _NUMSTAT_COUNT.fullmatch(added) is None
            or _NUMSTAT_COUNT.fullmatch(deleted) is None
        ):
            continue
        counts[path] = counts.get(path, 0) + 1
    return counts


def compute_file_churn(
    repo_root: str,
    window_days: int | None = None,
) -> dict[str, int]:
    """Count commits touching each file over a trailing window.

    Thin wrapper over :func:`compute_file_churn_with_status` for callers that
    only want the counts.
    """
    return compute_file_churn_with_status(repo_root, window_days)[0]


def compute_file_churn_with_status(
    repo_root: str,
    window_days: int | None = None,
) -> tuple[dict[str, int], str]:
    """Count commits touching each file, and say whether the count is real.

    Memoised per ``(repo, HEAD commit, window)``: the answer cannot change
    until a new commit lands, and the walk is too expensive to repeat inside
    every tool call. The history walk is capped at ``_CHURN_MAX_COMMITS`` and
    given ``_CHURN_TIMEOUT`` seconds.

    Returns ``({}, CHURN_UNAVAILABLE)`` when Git is slow, missing, or fails,
    which leaves the change-frequency risk term at zero -- exactly the
    behaviour callers had before churn was enabled for them -- and records
    that for the life of the process so the next call costs nothing. An
    invalid or non-positive window is ``CHURN_OFF``: churn was switched off,
    not attempted and lost. Renames are deliberately not followed: churn
    belongs to the path that existed in each commit.
    """
    if window_days is None:
        raw_window = os.environ.get("CRG_CHURN_WINDOW_DAYS", "90")
        try:
            window_days = int(raw_window)
        except ValueError:
            logger.warning(
                "Invalid CRG_CHURN_WINDOW_DAYS value %r; churn disabled",
                raw_window,
            )
            return {}, CHURN_OFF
    if window_days <= 0:
        return {}, CHURN_OFF

    # Checked before any subprocess: on the repositories this exists for, the
    # cheap-looking rev-parse below is itself what times out.
    fail_key = (repo_root, window_days)
    if _churn_is_unavailable(fail_key):
        return {}, CHURN_UNAVAILABLE

    cache_key = _churn_cache_key(repo_root, window_days)
    if cache_key is None:
        _mark_churn_unavailable(fail_key)
        return {}, CHURN_UNAVAILABLE
    with _CHURN_CACHE_LOCK:
        cached = _CHURN_CACHE.get(cache_key)
    if cached is not None:
        return dict(cached), CHURN_OK

    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                "core.quotepath=off",
                "log",
                f"--since={window_days}.days.ago",
                f"--max-count={_CHURN_MAX_COMMITS}",
                "--numstat",
                "--no-renames",
                "--format=",
                "-z",
                "--",
            ],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=repo_root,
            timeout=_CHURN_TIMEOUT,
        )
        if result.returncode != 0:
            logger.warning(
                "git log failed (rc=%d): %s",
                result.returncode,
                result.stderr[:200],
            )
            _mark_churn_unavailable(fail_key)
            return {}, CHURN_UNAVAILABLE
        counts = _parse_numstat(result.stdout)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("git log error: %s", exc)
        _mark_churn_unavailable(fail_key)
        return {}, CHURN_UNAVAILABLE

    with _CHURN_CACHE_LOCK:
        if len(_CHURN_CACHE) >= _CHURN_CACHE_MAX_ENTRIES:
            _CHURN_CACHE.clear()
        _CHURN_CACHE[cache_key] = dict(counts)
    return counts, CHURN_OK


# ---------------------------------------------------------------------------
# 3. map_changes_to_nodes
# ---------------------------------------------------------------------------


def map_changes_to_nodes(
    store: GraphStore,
    changed_ranges: dict[str, list[tuple[int, int]]],
) -> list[GraphNode]:
    """Find graph nodes whose line ranges overlap the changed lines.

    Args:
        store: The graph store.
        changed_ranges: Mapping of file paths to ``(start, end)`` tuples.

    Returns:
        Deduplicated list of overlapping graph nodes.
    """
    seen: set[str] = set()
    result: list[GraphNode] = []

    for file_path, ranges in changed_ranges.items():
        # Try the path as-is, then also try all nodes to match relative paths.
        nodes = store.get_nodes_by_file(file_path)
        if not nodes:
            # The graph may store absolute paths; try a suffix match.
            matched_paths = store.get_files_matching(file_path)
            for mp in matched_paths:
                nodes.extend(store.get_nodes_by_file(mp))

        for node in nodes:
            if node.qualified_name in seen:
                continue
            if node.line_start is None or node.line_end is None:
                continue
            yaml_ranges = node.extra.get("range_samples")
            if (
                node.kind in ("YamlPath", "JsonPath")
                and isinstance(yaml_ranges, list)
                and not node.extra.get("lines_truncated")
            ):
                overlaps = any(
                    isinstance(sample, list)
                    and len(sample) == 2
                    and isinstance(sample[0], int)
                    and isinstance(sample[1], int)
                    and sample[0] <= end
                    and sample[1] >= start
                    for sample in yaml_ranges
                    for start, end in ranges
                )
                if overlaps:
                    result.append(node)
                    seen.add(node.qualified_name)
                continue
            # Check overlap with any changed range.
            for start, end in ranges:
                if node.line_start <= end and node.line_end >= start:
                    result.append(node)
                    seen.add(node.qualified_name)
                    break

    return result


# ---------------------------------------------------------------------------
# 3b. Pure deletions: what survived, what was removed, who still uses it
# ---------------------------------------------------------------------------

# The kinds reported as changed functions. A pure deletion is judged on these
# from the old side; every other kind keeps the plain position rule.
_CODE_KINDS = ("Function", "Test", "Class")
# Old files read for one analysis: one ``git show`` and one parse each. Past
# the cap a file falls back to the new-side rule and the summary says so.
_MAX_DELETION_FILES = env_int("CRG_MAX_DELETION_FILES", 100)
# Removed symbols whose users are looked up, a few indexed queries each.
_MAX_REMOVED_SYMBOLS = 200
# Users listed per removed symbol; ``used_by_total`` keeps the full count.
# Each costs ~45 tokens, and every MCP response lists up to 25 symbols.
_MAX_REMOVED_USERS_SHOWN = 3
_SELF_RECEIVERS = frozenset({"self", "this", "cls"})


@dataclass
class _DeletionReview:
    changed: list[GraphNode] = field(default_factory=list)
    removed: list[dict[str, Any]] = field(default_factory=list)
    removed_total: int = 0
    files_not_read: int = 0


def _nodes_in_file(store: GraphStore, file_path: str) -> list[GraphNode]:
    """The file's nodes, with the same suffix fallback as map_changes_to_nodes."""
    nodes = store.get_nodes_by_file(file_path)
    if not nodes:
        for matched in store.get_files_matching(file_path):
            nodes.extend(store.get_nodes_by_file(matched))
    return nodes


@dataclass
class _OldSide:
    """The code symbols of a file as it was at the base, and their calls."""

    nodes: list[tuple[str, NodeInfo]]
    calls: dict[str, list[str]]


def _old_side_nodes(
    parser: CodeParser,
    repo_root: str,
    base: str,
    old_path: str,
    parse_path: str,
) -> _OldSide | None:
    """Parse the file as it was at *base*, or None when that cannot be done.

    The old bytes are parsed as if they lived at *parse_path*, the path the
    graph knows the file by now, so their qualified names compare directly
    with the stored ones even across a rename. None means "unknown": git
    could not produce the old file, or the parser does not read its language.
    """
    if not _SAFE_GIT_REF.match(base):
        return None
    try:
        shown = subprocess.run(
            ["git", "show", f"{base}:{old_path}"],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            cwd=repo_root,
            timeout=_GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("git show failed for %s: %s", old_path, exc)
        return None
    if shown.returncode != 0:
        return None
    try:
        nodes, edges = parser.parse_bytes(Path(parse_path), shown.stdout)
    except Exception as exc:
        # Any parser fault means "unknown". The parser covers dozens of
        # grammars, and the update path treats a failure in any of them the
        # same way: log it and carry on.
        logger.warning("Could not parse the old side of %s: %s", old_path, exc)
        return None
    if not nodes:
        return None
    calls: dict[str, list[str]] = {}
    for edge in edges:
        if edge.kind == "CALLS":
            calls.setdefault(edge.source, []).append(edge.target)
    return _OldSide(
        nodes=[
            (qualified_name_of(node), node)
            for node in nodes
            if node.kind in _CODE_KINDS
            and not node.extra.get("verilog_kind")
            and node.line_start
            and node.line_end
        ],
        calls=calls,
    )


def _bare_call_reaches(edge: GraphEdge, removed: GraphNode) -> bool:
    """Can this unqualified call be a call to *removed*?

    Once a symbol is gone, the calls that named it only through their own
    file or a module or class receiver are re-parsed with nothing to resolve
    to, so they keep only the bare name. The name alone is not enough:
    ``handle.close()`` is a file's close, not ``Box.close``. Receiver
    evidence is held to the resolver's own rule, wherever the call is.
    Without any, only a call in the symbol's own file counts: a plain call
    for a top-level function, or one from inside the class for a method.
    """
    extra = edge.extra or {}
    if "ambiguous_targets" in extra or "unresolved_targets" in extra:
        return False
    if "receiver_binding" in extra:
        return GraphStore.receiver_evidence_admits(extra, removed)
    if normalize_file_path(edge.file_path) != removed.file_path:
        return False
    if not removed.parent_name:
        return "receiver" not in extra
    own_class = f"{removed.file_path}::{removed.parent_name}."
    return (
        extra.get("receiver") in _SELF_RECEIVERS or "receiver" not in extra
    ) and edge.source_qualified.startswith(own_class)


@dataclass
class _RemovedUsers:
    """What the graph still records as using a removed symbol."""

    users: list[dict[str, Any]]
    # One entry per CALLS edge, as compute_risk_score counts callers.
    call_sources: list[str]
    caller_ids: set[int]


def _as_graph_node(qualified_name: str, info: NodeInfo) -> GraphNode:
    """A parsed node that is not in the store, in the store's own shape."""
    return GraphNode(
        id=0, kind=info.kind, name=info.name, qualified_name=qualified_name,
        file_path=info.file_path, line_start=info.line_start,
        line_end=info.line_end, language=info.language,
        parent_name=info.parent_name, params=info.params,
        return_type=info.return_type, is_test=info.is_test, file_hash=None,
        extra=info.extra,
    )


def _removed_symbol_users(store: GraphStore, removed: GraphNode) -> _RemovedUsers:
    """Every node the graph still records as using a removed symbol."""
    qualified_name = removed.qualified_name
    users: dict[str, dict[str, Any]] = {}
    found = _RemovedUsers(users=[], call_sources=[], caller_ids=set())

    def add(source: str, relation: str, line: int) -> None:
        node = store.get_node(source) if source != qualified_name else None
        if node is None:
            return
        if relation == "CALLS":
            found.call_sources.append(source)
            found.caller_ids.add(node.id)
        if source in users:
            return
        # The qualified name carries the file; repeating it doubles the cost.
        users[source] = {
            "name": _sanitize_name(node.name),
            "qualified_name": _sanitize_name(node.qualified_name),
            "line": line,
            "relation": relation,
        }

    for edge in store.get_edges_by_target(qualified_name):
        if edge.kind != "CONTAINS":
            add(edge.source_qualified, edge.kind, edge.line)
    # TESTED_BY points from the production symbol to its test.
    for edge in store.get_edges_by_source(qualified_name):
        if edge.kind == "TESTED_BY":
            add(edge.target_qualified, edge.kind, edge.line)
    bare_reached: set[int] = set()
    for edge in store.search_edges_by_target_name(removed.name, kind="CALLS"):
        if _bare_call_reaches(edge, removed):
            bare_reached.add(edge.id)
            add(edge.source_qualified, edge.kind, edge.line)
    # Once the symbol is gone, an edge that named it is settled back to the
    # plain name with the old name kept as its claim (#984).
    for edge in store.get_edges_claiming(qualified_name):
        if edge.id in bare_reached:
            continue
        if edge.kind == "TESTED_BY":
            add(edge.target_qualified, edge.kind, edge.line)
        elif edge.kind != "CONTAINS":
            add(edge.source_qualified, edge.kind, edge.line)
    found.users = sorted(
        users.values(), key=lambda u: (u["qualified_name"], u["line"] or 0),
    )
    return found


@dataclass
class _RemovedSymbol:
    """A symbol a diff removed, with what is needed to score it."""

    node: GraphNode
    # Its calls as the old file made them.
    old_calls: list[str]
    # The community its file's surviving symbols share, or None.
    file_community: int | None
    found: _RemovedUsers = field(
        default_factory=lambda: _RemovedUsers(users=[], call_sources=[], caller_ids=set()),
    )


def _removed_flow_criticalities(
    store: GraphStore, removed: list[_RemovedSymbol],
) -> dict[str, list[float]]:
    """The flows each removed symbol was part of, traced again.

    Flows are re-traced after every update, so the ones that ran through a
    removed symbol are gone, and a caller whose only callee it was no longer
    starts a flow at all. They are rebuilt in memory: the removed symbols go
    back into the call graph with the calls their old file made, and every
    entry point that could reach them -- one whose flow contains a caller,
    or a caller that is itself an entry point -- is traced again with the
    same tracer and criticality score the stored flows use.
    """
    adj = store.load_flow_adjacency()
    by_qn: dict[str, int] = {}
    for index, item in enumerate(removed):
        # A negative id cannot collide with a stored node.
        node = replace(item.node, id=-(index + 1))
        by_qn[node.qualified_name] = node.id
        adj.nodes_by_qn[node.qualified_name] = node
        adj.nodes_by_id[node.id] = node
    for item in removed:
        node = item.node
        own_file = f"{node.file_path}::"
        targets = adj.calls_out.setdefault(node.qualified_name, [])
        for target in item.old_calls:
            # The old file names same-file callees by bare name at times.
            if target not in adj.nodes_by_qn and "::" not in target:
                target = own_file + target
            targets.append(target)
        for source in set(item.found.call_sources):
            callees = adj.calls_out.setdefault(source, [])
            if node.qualified_name not in callees:
                callees.append(node.qualified_name)

    caller_ids = set().union(*(item.found.caller_ids for item in removed))
    starts = store.get_flow_entry_point_ids(caller_ids)
    entry_ids = {ep.id for ep in detect_entry_points(store)}
    starts |= caller_ids & entry_ids
    criticalities: dict[str, list[float]] = {qn: [] for qn in by_qn}
    for node_id in starts:
        entry = adj.nodes_by_id.get(node_id)
        flow = _trace_single_flow(adj, entry) if entry is not None else None
        if flow is None:
            continue
        on_path = set(flow["path"])
        for qn, removed_id in by_qn.items():
            if removed_id in on_path:
                criticalities[qn].append(flow["criticality"])
    return criticalities


def _score_removed(
    store: GraphStore,
    removed: list[_RemovedSymbol],
    churn_counts: dict[str, int] | None,
) -> dict[str, float]:
    """Score removed symbols as :func:`compute_risk_score` scores a change.

    Removing a symbol that is still used breaks its users, so it is at least
    as risky as changing it would be. The node is gone, so each term is
    rebuilt from what survives:

    * flows: those it was part of, traced again
      (:func:`_removed_flow_criticalities`);
    * community crossing: its callers outside its file's community;
    * test coverage, security, caller count and churn: as for a change,
      since the TESTED_BY and CALLS edges that name it are still stored.

    A removed symbol nothing uses breaks nothing and scores 0.
    """
    used = [item for item in removed if item.found.users]
    scores = {item.node.qualified_name: 0.0 for item in removed}
    if not used:
        return scores
    flows = _removed_flow_criticalities(store, used)
    for item in used:
        qn = item.node.qualified_name
        scores[qn] = _score_risk(
            store,
            item.node,
            churn_counts,
            flow_criticalities=flows[qn],
            flow_count=0,
            caller_qns=item.found.call_sources,
            cross_community=_cross_community(
                store, item.found.call_sources, item.file_community,
                own_group=True,
            ),
        )
    return scores


def _file_community(store: GraphStore, nodes: list[GraphNode]) -> int | None:
    """The community most of a file's symbols belong to, or None."""
    cids = store.get_community_ids_by_qualified_names(
        [node.qualified_name for node in nodes if node.kind != "File"],
    )
    counts = Counter(cid for cid in cids.values() if cid is not None)
    return counts.most_common(1)[0][0] if counts else None


def _review_pure_deletions(
    store: GraphStore,
    deletions: dict[str, FileDeletions],
    repo_root: str | None,
    base: str,
    churn_counts: dict[str, int] | None = None,
) -> _DeletionReview:
    """Judge pure-deletion hunks from the old side of the diff.

    For each code symbol of the old file whose lines a hunk removed:

    * gone from the graph -> removed, reported with whatever still uses it
      and scored by :func:`_score_removed`;
    * still there, and the hunk took only part of it -> changed;
    * still there, but the hunk took all of it -> it was defined twice or
      moved within the file, and the lines that added it say so.

    Code around a hole that lost no lines is never marked. When the old side
    cannot be read the rule falls back to the new side alone: a symbol is
    changed only when surviving lines of it sit on both sides of the hole.
    That misses a deleted tail, but it never marks an untouched neighbour.
    """
    review = _DeletionReview()
    parser: CodeParser | None = None
    files_read = 0
    candidates: list[_RemovedSymbol] = []
    for file_path, held in deletions.items():
        current = _nodes_in_file(store, file_path)
        if not current:
            # The graph does not know this file, so it cannot say what
            # survived; guessing would report every symbol as removed.
            continue
        old_side: _OldSide | None = None
        if repo_root is not None and files_read < _MAX_DELETION_FILES:
            files_read += 1
            if parser is None:
                parser = CodeParser(Path(repo_root))
            old_side = _old_side_nodes(
                parser, repo_root, base, held.old_path, file_path,
            )
        if old_side is None:
            review.files_not_read += 1
            review.changed.extend(
                node for node in current
                if node.kind in _CODE_KINDS
                and node.line_start is not None
                and node.line_end is not None
                and any(
                    node.line_start <= hunk.anchor < node.line_end
                    for hunk in held.hunks
                )
            )
            continue
        survivors = {node.qualified_name: node for node in current}
        community: int | None = None
        community_known = False
        for qualified_name, info in old_side.nodes:
            hit = [
                hunk for hunk in held.hunks
                if info.line_start <= hunk.old_end
                and info.line_end >= hunk.old_start
            ]
            if not hit:
                continue
            survivor = survivors.get(qualified_name)
            if survivor is None:
                if not community_known:
                    community = _file_community(store, current)
                    community_known = True
                candidates.append(_RemovedSymbol(
                    node=_as_graph_node(qualified_name, info),
                    old_calls=old_side.calls.get(qualified_name, []),
                    file_community=community,
                ))
            elif not any(
                hunk.old_start <= info.line_start
                and info.line_end <= hunk.old_end
                for hunk in hit
            ):
                review.changed.append(survivor)

    review.removed_total = len(candidates)
    looked_up = candidates[:_MAX_REMOVED_SYMBOLS]
    for item in looked_up:
        item.found = _removed_symbol_users(store, item.node)
    scores = _score_removed(store, looked_up, churn_counts)
    for item in looked_up:
        node = item.node
        review.removed.append({
            "name": _sanitize_name(node.name),
            "qualified_name": _sanitize_name(node.qualified_name),
            "kind": node.kind,
            "file": node.file_path,
            "old_line_start": node.line_start,
            "old_line_end": node.line_end,
            "risk_score": scores[node.qualified_name],
            "used_by": item.found.users[:_MAX_REMOVED_USERS_SHOWN],
            "used_by_total": len(item.found.users),
        })
    # What still uses a removed symbol is what may break, so it leads.
    review.removed.sort(
        key=lambda r: (
            -r["risk_score"], -r["used_by_total"], r["file"], r["old_line_start"],
        ),
    )
    return review


def _removed_summary_lines(review: _DeletionReview) -> list[str]:
    """Summary lines for removed symbols: how many, and which are still used."""
    if not review.removed_total:
        return []
    lines = [f"  - {review.removed_total} removed function(s)/class(es)"]
    still_used = [r for r in review.removed if r["used_by_total"]]
    if still_used:
        named = []
        for entry in still_used[:5]:
            # "File.convert", not "convert": two users can share a name.
            users = ", ".join(_symbol_part(u["qualified_name"]) for u in entry["used_by"])
            more = entry["used_by_total"] - len(entry["used_by"])
            named.append(
                f"{_symbol_part(entry['qualified_name'])} (risk "
                f"{entry['risk_score']:.2f}, by {users}"
                f"{f' +{more} more' if more else ''})"
            )
        lines.append(f"  - Removed but still used: {'; '.join(named)}")
    return lines


def _symbol_part(qualified_name: str) -> str:
    """``path::Class.name`` -> ``Class.name``; a file node -> its file name."""
    if "::" in qualified_name:
        return qualified_name.split("::", 1)[1]
    return qualified_name.rsplit("/", 1)[-1]


# ---------------------------------------------------------------------------
# 4. compute_risk_score
# ---------------------------------------------------------------------------


def compute_risk_score(
    store: GraphStore,
    node: GraphNode,
    churn_counts: dict[str, int] | None = None,
) -> float:
    """Compute a risk score (0.0 - 1.0) for a single node.

    Scoring factors:
      - Flow participation: 0.05 per flow membership, capped at 0.25
      - Community crossing: 0.05 per caller from a different community, capped at 0.15
      - Test coverage: 0.30 (untested) scaling down to 0.05 (5+ TESTED_BY edges);
        none for a lifecycle method in test code, which has no coverage to lack
      - Security sensitivity: 0.20 if name matches security keywords
      - Caller count: callers / 20, capped at 0.10
      - Change frequency (opt-in): commits touching the file / 10, capped
        at 0.15

    Only direct and transitive TESTED_BY edges move the coverage term. A
    symbol that merely sits under a tested caller scores as untested here; see
    :meth:`~code_review_graph.graph.GraphStore.get_caller_test_routes` for why
    that reachability is reported beside the score rather than folded into it.

    The score is the sum of :func:`risk_signals`, so a report that explains
    a score with those signals can never disagree with the score itself.
    """
    return _sum_signals(risk_signals(store, node, churn_counts))


def risk_signals(
    store: GraphStore,
    node: GraphNode,
    churn_counts: dict[str, int] | None = None,
) -> dict[str, dict[str, float]]:
    """Each term of :func:`compute_risk_score`, with the count behind it.

    Returns ``{name: {"score": float, "count": int}}`` for ``flows``,
    ``cross_community``, ``tests``, ``security``, ``callers`` and ``churn``,
    in the order the score adds them. A term that does not apply scores 0.0.
    ``count`` is what the term measured: flow memberships, callers in other
    communities, tests reaching the node, 1 for a security-looking name,
    callers, and commits to the file.
    """
    flow_criticalities = store.get_flow_criticalities_for_node(node.id)
    callers = store.get_edges_by_target(node.qualified_name)
    caller_qns = [e.source_qualified for e in callers if e.kind == "CALLS"]
    return _risk_signals_from(
        store,
        node,
        churn_counts,
        flow_criticalities=flow_criticalities,
        flow_count=(
            0 if flow_criticalities else store.count_flow_memberships(node.id)
        ),
        caller_qns=caller_qns,
        cross_community=_cross_community(
            store, caller_qns, store.get_node_community_id(node.id),
        ),
    )


def _cross_community(
    store: GraphStore,
    caller_qns: list[str],
    node_cid: int | None,
    *,
    own_group: bool = False,
) -> int:
    """How many distinct callers sit in a community other than *node_cid*.

    *own_group* is for a removed symbol whose file is left in no community:
    detection drops a group that shrinks to one symbol, so the file's group
    can vanish with the removal. The symbol was in that group, which no
    caller in another community shares, so each of those callers crossed.
    """
    if not caller_qns or (node_cid is None and not own_group):
        return 0
    cid_map = store.get_community_ids_by_qualified_names(caller_qns)
    return sum(
        1 for cid in cid_map.values() if cid is not None and cid != node_cid
    )


def _sum_signals(signals: dict[str, dict[str, float]]) -> float:
    """The risk score the signals add up to, clamped to 0.0 - 1.0."""
    score = 0.0
    for signal in signals.values():
        score += signal["score"]
    return round(min(max(score, 0.0), 1.0), 4)


def _score_risk(
    store: GraphStore,
    node: GraphNode,
    churn_counts: dict[str, int] | None,
    *,
    flow_criticalities: list[float],
    flow_count: int,
    caller_qns: list[str],
    cross_community: int,
) -> float:
    """The terms of :func:`compute_risk_score`, from evidence already gathered.

    Split out so a symbol that is no longer in the graph -- one a diff
    removed -- is scored on exactly the same scale; see
    :func:`_score_removed` for where its evidence comes from.
    *caller_qns* holds one entry per incoming CALLS edge.
    """
    return _sum_signals(_risk_signals_from(
        store,
        node,
        churn_counts,
        flow_criticalities=flow_criticalities,
        flow_count=flow_count,
        caller_qns=caller_qns,
        cross_community=cross_community,
    ))


def _risk_signals_from(
    store: GraphStore,
    node: GraphNode,
    churn_counts: dict[str, int] | None,
    *,
    flow_criticalities: list[float],
    flow_count: int,
    caller_qns: list[str],
    cross_community: int,
) -> dict[str, dict[str, float]]:
    """:func:`risk_signals` from evidence already gathered (see :func:`_score_risk`)."""
    signals: dict[str, dict[str, float]] = {}

    # --- Flow participation (cap 0.25), weighted by criticality ---
    if flow_criticalities:
        signals["flows"] = {
            "score": min(sum(flow_criticalities), 0.25),
            "count": len(flow_criticalities),
        }
    else:
        signals["flows"] = {"score": min(flow_count * 0.05, 0.25), "count": flow_count}

    # --- Community crossing (cap 0.15) ---
    signals["cross_community"] = {
        "score": min(cross_community * 0.05, 0.15),
        "count": cross_community,
    }

    # --- Test coverage (direct + transitive) ---
    # A lifecycle method in test code has no coverage to lack, so the term
    # does not apply to it at all.
    signals["tests"] = {"score": 0.0, "count": 0}
    if not _is_test_lifecycle(node, store):
        transitive_tests = store.get_transitive_tests(
            node.qualified_name, include_helper_routes=False,
        )
        test_count = len(transitive_tests)
        signals["tests"] = {
            "score": 0.30 - (min(test_count / 5.0, 1.0) * 0.25),
            "count": test_count,
        }

    # --- Security sensitivity ---
    name_lower = node.name.lower()
    qn_lower = node.qualified_name.lower()
    sensitive = any(kw in name_lower or kw in qn_lower for kw in _SECURITY_KEYWORDS)
    signals["security"] = {"score": 0.20 if sensitive else 0.0, "count": int(sensitive)}

    # --- Caller count (cap 0.10) ---
    caller_count = len(caller_qns)
    signals["callers"] = {"score": min(caller_count / 20.0, 0.10), "count": caller_count}

    # --- Change frequency (opt-in, cap 0.15) ---
    signals["churn"] = {"score": 0.0, "count": 0}
    if churn_counts and node.file_path:
        commit_count = churn_counts.get(node.file_path, 0)
        signals["churn"] = {
            "score": min(commit_count / _CHURN_SATURATION, 1.0) * _CHURN_WEIGHT,
            "count": commit_count,
        }

    return signals


def review_churn_counts(
    repo_root: str | None, include_churn: bool,
) -> tuple[dict[str, int] | None, str]:
    """Commit counts per file for the opt-in churn term, and their status.

    Keys are both the repo-relative paths ``git log`` prints and the absolute
    normalized paths the graph stores, so a lookup by ``node.file_path``
    finds them. Returns ``(None, "off")`` when churn was not asked for.
    """
    if not include_churn or repo_root is None:
        return None, CHURN_OFF
    raw_churn, churn_status = compute_file_churn_with_status(repo_root)
    churn_counts: dict[str, int] = {}
    root_path = Path(repo_root)
    for key, count in raw_churn.items():
        churn_counts[key] = count
        churn_counts[normalize_file_path(root_path / key)] = count
    return churn_counts, churn_status


# ---------------------------------------------------------------------------
# 5. analyze_changes
# ---------------------------------------------------------------------------


def analyze_changes(
    store: GraphStore,
    changed_files: list[str],
    changed_ranges: dict[str, list[tuple[int, int]]] | None = None,
    repo_root: str | None = None,
    base: str = "HEAD~1",
    include_churn: bool = False,
    require_vcs: bool = False,
) -> dict[str, Any]:
    """Analyze changes and produce risk-scored review guidance.

    Args:
        store: The graph store.
        changed_files: List of changed file paths.
        changed_ranges: Optional pre-parsed diff ranges. If not provided and
            ``repo_root`` is given, they are computed via the detected VCS
            (Git or SVN).
        repo_root: Repository root (for git/svn diff).
        base: Git ref or SVN revision range to diff against.
        include_churn: Add an opt-in change-frequency term to each node's
            risk score. The trailing window defaults to 90 days and can be
            configured with ``CRG_CHURN_WINDOW_DAYS``.
        require_vcs: Raise
            :class:`~code_review_graph.errors.ChangeDiscoveryError` when the
            diff cannot be read at all, rather than silently degrading to a
            file-level analysis. Review gates pass this.

    Returns:
        Dict with ``summary``, ``risk_score``, ``changed_functions``,
        ``affected_flows``, ``test_gaps``, ``review_priorities`` and
        ``churn_status`` (``"ok"``, ``"unavailable"`` when the git history
        could not be read and the scores therefore exclude the
        change-frequency term, or ``"off"`` when it was not requested).

        ``removed_symbols`` lists the functions and classes a pure-deletion
        hunk took out of a file that still exists, read from the old side of
        the diff, each with ``old_line_start``/``old_line_end``, up to three
        ``used_by`` entries (callers, subclasses, importers and tests the
        graph still records) and ``used_by_total``; ``removed_symbols_total``
        is the untruncated count. A removed symbol is never in
        ``changed_functions``, and neither is the untouched code around it.

        Each ``test_gaps`` entry carries ``coverage``: ``"none"`` when no test
        reaches the symbol at all, or ``"indirect"`` when one only reaches it
        through a caller -- those entries also carry ``covered_via``,
        ``covered_depth`` and ``covered_by``. Unreached entries sort first, and
        ``test_gaps_uncovered``/``test_gaps_indirect`` hold the two counts for
        consumers that truncate the list.
    """
    # Compute changed ranges if not provided.
    ranges_unavailable = ""
    if changed_ranges is None and repo_root is not None:
        # Diff keys are forward-slash paths relative to the repo root, but
        # the graph stores absolute native paths. Remap so lookups work on
        # Windows, where the LIKE-suffix fallback cannot bridge
        # "src/app.py" to "C:\repo\src\app.py" (#528). Keys that are
        # already absolute pass through pathlib joining unchanged. The
        # explicit changed_ranges path (MCP) is untouched — tools/review.py
        # remaps before calling, and remapping twice would corrupt keys.
        root_path = Path(repo_root)
        try:
            raw_ranges = parse_diff_ranges(repo_root, base, require_vcs=require_vcs)
        except ChangeDiscoveryError as exc:
            if not changed_files:
                # Nothing else to go on: an empty answer here would be an
                # all-clear the tool has not earned.
                raise
            # The changed files are already known, so an unreadable
            # line-level diff costs precision, not honesty. Degrade to
            # whole-file scoring and say so in the summary rather than
            # presenting a file-level answer as a line-level one.
            logger.warning("%s; scoring whole files instead", exc)
            ranges_unavailable = str(exc)
            raw_ranges = {}
        if not isinstance(raw_ranges, DiffRanges):
            raw_ranges = DiffRanges(raw_ranges)
        changed_ranges = raw_ranges.remapped(root_path)

    # The affected-flows lookup and the no-ranges fallback match
    # changed_files against nodes.file_path, which stores absolute
    # normalized paths. CLI callers pass repo-relative diff paths, so an
    # exact IN (...) match finds no nodes and detect-changes reports
    # "0 affected flow(s)" even when the MCP tool reports hundreds on the
    # same input (#848). Remap the same way as changed_ranges keys;
    # already-absolute inputs (MCP) pass through pathlib joining unchanged.
    if repo_root is not None:
        _root = Path(repo_root)
        changed_files = [
            normalize_file_path(_root / fp) for fp in changed_files
        ]

    # Computed before the mapping: removed symbols are scored as it runs.
    churn_counts, churn_status = review_churn_counts(repo_root, include_churn)

    # Map changes to nodes.
    deletions: dict[str, FileDeletions] = (
        changed_ranges.deletions if isinstance(changed_ranges, DiffRanges) else {}
    )
    deletion_review = _DeletionReview()
    if deletions and isinstance(changed_ranges, DiffRanges):
        # A pure deletion has no new lines, and the lines around the hole are
        # lines it never touched (PR #1167 reported the case). Code symbols
        # are judged from the old side instead; other kinds, such as YAML
        # paths, keep the plain position rule.
        changed_nodes = map_changes_to_nodes(store, changed_ranges.edit_ranges())
        seen_qns = {node.qualified_name for node in changed_nodes}
        deletion_review = _review_pure_deletions(
            store, deletions, repo_root, base, churn_counts,
        )
        positions = {
            path: [(hunk.anchor, hunk.anchor) for hunk in held.hunks]
            for path, held in deletions.items()
        }
        positional = [
            node for node in map_changes_to_nodes(store, positions)
            if node.kind not in _CODE_KINDS
        ]
        for node in [*positional, *deletion_review.changed]:
            if node.qualified_name not in seen_qns:
                seen_qns.add(node.qualified_name)
                changed_nodes.append(node)
    elif changed_ranges:
        changed_nodes = map_changes_to_nodes(store, changed_ranges)
    else:
        # Fallback: all nodes in changed files.
        changed_nodes = []
        for fp in changed_files:
            changed_nodes.extend(store.get_nodes_by_file(fp))

    # RTL declarations are stored as Function nodes for compatibility but
    # are not callable/testable functions.
    changed_funcs = [
        n for n in changed_nodes
        if n.kind in ("Function", "Test", "Class")
        and not n.extra.get("verilog_kind")
    ]
    changed_yaml_paths = [n for n in changed_nodes if n.kind == "YamlPath"]
    changed_json_paths = [n for n in changed_nodes if n.kind == "JsonPath"]

    # Cap to prevent O(N*M) query explosion on large PRs.
    _max_funcs = env_int("CRG_MAX_CHANGED_FUNCS", 500)
    funcs_truncated = len(changed_funcs) > _max_funcs
    if funcs_truncated:
        changed_funcs = changed_funcs[:_max_funcs]

    _max_yaml_paths = env_int("CRG_MAX_CHANGED_YAML_PATHS", 200)
    yaml_paths_truncated = len(changed_yaml_paths) > _max_yaml_paths
    if yaml_paths_truncated:
        changed_yaml_paths = changed_yaml_paths[:_max_yaml_paths]
    json_paths_truncated = len(changed_json_paths) > _max_yaml_paths
    if json_paths_truncated:
        changed_json_paths = changed_json_paths[:_max_yaml_paths]

    # Keep one public YAML metadata contract. ``node_to_dict`` owns
    # sanitization and output limits for query and change analysis.
    yaml_path_results = [node_to_dict(node) for node in changed_yaml_paths]
    json_path_results = [node_to_dict(node) for node in changed_json_paths]

    # Classify test coverage before scoring: the same answer decides both the
    # gap report and the coverage term of every node's risk score, and doing
    # it once for the whole change set keeps it to a handful of batched
    # queries instead of a walk per symbol.
    #
    # Stored file paths are absolute, so test-ness is judged against the path
    # relative to the repository root: reading ``tests/`` out of an absolute
    # path would also match a directory above the checkout, and a repository
    # cloned into a CI workspace named "test" would report no gaps at all
    # (issue #1023). ``repo_root`` is the caller's; the graph's own recorded
    # root covers callers that pass none.
    gap_root = repo_root or store.get_repo_root()
    # A symbol that lives in a test file is test code and can never be a gap
    # in production test coverage. The path is checked as well as the stored
    # flag so a graph built before the parser marked non-function test nodes
    # still gives the right answer: those rows carry ``is_test = 0`` and used
    # to be reported back to the author as their own tests needing tests
    # (issue #1014).
    # TypeScript interfaces and type aliases have no runtime behavior to test,
    # so they are never gap candidates (issue #1109).
    gap_candidates = [
        node for node in changed_funcs
        if not (node.is_test or is_test_file(node.file_path, gap_root))
        and not is_test_gap_exempt(node, gap_root)
        and not node.extra.get("typescript_kind")
    ]
    # TESTED_BY edges are stored as source=production, target=test by the
    # parser, so a changed production function finds its tests by source.
    # See: #515
    directly_tested = store.get_directly_tested(
        node.qualified_name for node in gap_candidates
    )
    # A private helper that no test names by hand is still run by the tests of
    # the public function calling it, and calling it flatly "untested" is a
    # false alarm the author cannot act on (#1047). Walk up the CALLS graph to
    # find such a caller. The answer is a pointer, never a suppression and
    # never a discount: the symbol keeps its full untested risk score and its
    # place in the gap list, and only gains a note saying where a test already
    # runs nearby. A static path is not execution -- see
    # ``get_caller_test_routes`` for the coverage measurement behind that.
    caller_routes = store.get_caller_test_routes(
        (
            node.qualified_name for node in gap_candidates
            if node.qualified_name not in directly_tested
        ),
        repo_root=gap_root,
    )

    # Compute per-node risk scores.
    node_risks: list[dict[str, Any]] = []
    for node in changed_funcs:
        risk = compute_risk_score(store, node, churn_counts)
        node_risks.append({
            **node_to_dict(node),
            "risk_score": risk,
        })

    # Overall risk score: max of individual risks, or 0. A removed symbol
    # that is still used breaks its users, so its score counts as well; that
    # is also what the Action's ``fail-on-risk`` gate reads.
    overall_risk = max(
        (
            *(nr["risk_score"] for nr in node_risks),
            *(r["risk_score"] for r in deletion_review.removed),
        ),
        default=0.0,
    )

    # Affected flows.
    affected = get_affected_flows(store, changed_files)

    # Label each gap with the two claims a reader has to tell apart: no tested
    # caller was found anywhere near this symbol, versus one reaches it but
    # only through a caller. Both stay in one list in node order, and
    # ``indirect_gaps`` is only a view for the summary.
    #
    # The order is deliberately class-blind. Sorting the reached ones last
    # looks prudent -- keep the "real" gaps when the list is truncated -- but
    # every consumer bounds this list, so it made the weaker class the
    # guaranteed first casualty: at ``detect_changes_tool``'s default of 25
    # rows, a delta with 73 unreached gaps shipped zero reached ones while
    # still reporting a count of 9, and a consumer deriving its own "tested"
    # column from the truncated list then labelled those symbols as having
    # their own tests. A class must not be announced in the counts and
    # withheld from the payload.
    test_gaps: list[dict[str, Any]] = []
    indirect_gaps: list[dict[str, Any]] = []
    for node in gap_candidates:
        if node.qualified_name in directly_tested:
            continue
        entry: dict[str, Any] = {
            "name": _sanitize_name(node.name),
            "qualified_name": _sanitize_name(node.qualified_name),
            "file": node.file_path,
            "line_start": node.line_start,
            "line_end": node.line_end,
        }
        route = caller_routes.get(node.qualified_name)
        if route is None:
            entry["coverage"] = "none"
        else:
            entry["coverage"] = "indirect"
            entry["covered_via"] = _sanitize_name(str(route["via"]))
            entry["covered_depth"] = route["depth"]
            # One test names the route; three cost triple the payload for a
            # reader who opens the first one anyway.
            entry["covered_by"] = [
                _sanitize_name(str(t)) for t in route["tests"][:1]
            ]
            indirect_gaps.append(entry)
        test_gaps.append(entry)
    indirect_count = len(indirect_gaps)
    uncovered_count = len(test_gaps) - indirect_count

    # Review priorities: top 10 by risk score.
    review_priorities = sorted(node_risks, key=lambda x: x["risk_score"], reverse=True)[:10]

    # Build summary.
    # "no direct test", not "no test in reach": the graph sees TESTED_BY edges
    # and CALLS paths, not the suite. On the very delta of #1047 two symbols in
    # the unreached group (auto_promote.py::cmd_verify and
    # ::ForeignPullRequestError) are exercised by tests that load the script
    # through importlib, so no edge records it. The weaker claim is true; the
    # stronger one is not ours to make.
    gap_line = f"  - {len(test_gaps)} test gap(s)"
    if indirect_count:
        gap_line += (
            f" ({uncovered_count} with no tested caller found, "
            f"{indirect_count} reached only through a caller)"
        )
    summary_parts = [
        f"Analyzed {len(changed_files)} changed file(s):",
        f"  - {len(changed_funcs)} changed function(s)/class(es)",
        *([f"  - {len(changed_yaml_paths)} changed YAML path(s)"] if changed_yaml_paths else []),
        *([f"  - {len(changed_json_paths)} changed JSON path(s)"] if changed_json_paths else []),
        *_removed_summary_lines(deletion_review),
        f"  - {affected['total']} affected flow(s)",
        gap_line,
        f"  - Overall risk score: {overall_risk:.2f}",
    ]

    # Dedup by bare name in the human summary. The underlying test_gaps list
    # keeps every entry (a downstream consumer needs precision via
    # qualified_name), but a graph that ended up with the same function
    # stored under two qualified_names (e.g. relative + absolute path
    # variants) would otherwise print "X, X, Y, Y" — surfacing graph
    # corruption as a UX bug. The root cause is path normalization;
    # this is the defensive last line.
    def _gap_names(entries: list[dict[str, Any]], limit: int = 5) -> list[str]:
        seen_names: set[str] = set()
        names: list[str] = []
        for g in entries:
            n = g["name"]
            if n in seen_names:
                continue
            seen_names.add(n)
            names.append(n)
            if len(names) >= limit:
                break
        return names

    if uncovered_count:
        # Filtered by class, not sliced: the list is in node order, so the
        # two classes are interleaved.
        unreached_gaps = [g for g in test_gaps if g["coverage"] != "indirect"]
        summary_parts.append(
            f"  - Untested: {', '.join(_gap_names(unreached_gaps))}"
        )
    if indirect_count:
        # Named separately, because acting on the two lists differs: these
        # symbols already run under a test and need an assertion of their own,
        # not a test written from nothing.
        summary_parts.append(
            "  - Reached only through a caller: "
            f"{', '.join(_gap_names(indirect_gaps))}"
        )
    if any(
        node.language == "php" and node.qualified_name not in directly_tested
        for node in gap_candidates
    ):
        # A Laravel test reaches code through the container or an HTTP call,
        # and the graph traces neither, so a PHP gap is a guess (#850, #851).
        summary_parts.append(
            "  - PHP gaps are a guess: tests that reach code through the "
            "container or an HTTP call are not traced (tests_for names "
            "likely ones)"
        )
    if funcs_truncated:
        summary_parts.append(
            f"  - Warning: analysis capped at {_max_funcs} functions "
            f"(set CRG_MAX_CHANGED_FUNCS to adjust)"
        )
    if yaml_paths_truncated:
        summary_parts.append(
            f"  - Warning: analysis capped at {_max_yaml_paths} YAML paths "
            f"(set CRG_MAX_CHANGED_YAML_PATHS to adjust)"
        )
    if json_paths_truncated:
        summary_parts.append(
            f"  - Warning: analysis capped at {_max_yaml_paths} JSON paths "
            f"(set CRG_MAX_CHANGED_YAML_PATHS to adjust)"
        )
    if ranges_unavailable:
        summary_parts.append(
            "  - Warning: line-level diff unavailable, whole files scored "
            f"({ranges_unavailable})"
        )
    if deletion_review.files_not_read:
        summary_parts.append(
            f"  - Note: the old version of {deletion_review.files_not_read} "
            "file(s) could not be read, so code deleted there is not listed "
            "as removed (CRG_MAX_DELETION_FILES sets how many files are read)"
        )
    if churn_status == CHURN_UNAVAILABLE:
        # Say it in the summary, not only in a log line nobody reads: the
        # scores below are missing a term worth up to 0.15, and a reviewer
        # comparing two runs deserves to know which one was degraded.
        summary_parts.append(
            "  - Degraded: change-frequency risk unavailable "
            f"(git history did not answer within {_CHURN_TIMEOUT:.3g}s; "
            "set CRG_CHURN_TIMEOUT to raise the budget). Risk scores "
            "exclude the churn term."
        )

    return {
        "summary": "\n".join(summary_parts),
        "risk_score": overall_risk,
        "changed_functions": node_risks,
        "changed_yaml_paths": yaml_path_results,
        "changed_json_paths": json_path_results,
        "affected_flows": affected["affected_flows"],
        "test_gaps": test_gaps,
        # Counts, not just the list: every consumer bounds ``test_gaps``, and
        # both the total and the split between "no tested caller found" and
        # "reached through a caller" must survive that truncation. A renderer
        # that takes its headline from ``len(test_gaps)`` but its split from
        # these prints a total that does not equal its own parts.
        "test_gaps_total": len(test_gaps),
        "test_gaps_uncovered": uncovered_count,
        "test_gaps_indirect": indirect_count,
        # Symbols the diff deleted outright, each with what still uses it.
        # They are not in ``changed_functions``: nothing of them is left to
        # score, and the code around the hole did not change.
        "removed_symbols": deletion_review.removed,
        "removed_symbols_total": deletion_review.removed_total,
        "review_priorities": review_priorities,
        "functions_truncated": funcs_truncated,
        "yaml_paths_truncated": yaml_paths_truncated,
        "json_paths_truncated": json_paths_truncated,
        "diff_ranges_unavailable": ranges_unavailable,
        "churn_status": churn_status,
    }
