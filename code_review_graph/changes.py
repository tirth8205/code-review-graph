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
from pathlib import Path
from typing import Any

from .constants import GIT_TIMEOUT as _GIT_TIMEOUT
from .constants import SECURITY_KEYWORDS as _SECURITY_KEYWORDS
from .constants import env_float, env_int
from .errors import ChangeDiscoveryError
from .flows import get_affected_flows
from .graph import GraphNode, GraphStore, _sanitize_name, node_to_dict
from .parser import is_test_file, normalize_file_path

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


def _diff_header_path(rest: str) -> str | None:
    """Extract the post-image path from the text after ``+++ ``.

    Git writes the path three ways, and only the plainest one is a bare
    ``b/path``: it appends a TAB when the path contains a space, and
    C-quotes the whole ``"b/path"`` when it contains a non-ASCII or control
    byte. Returns None for ``/dev/null``, the post-image of a deleted file.
    """
    rest = rest.rstrip("\r")
    if rest.startswith('"'):
        closing = rest.rfind('"')
        path = _unquote_c_path(rest[: closing + 1])
    else:
        # The trailing TAB is a separator, never part of the path: git emits
        # one only when the path itself contains a space.
        path = rest.split("\t", 1)[0]
    if path.startswith("b/"):
        path = path[2:]
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


def _parse_unified_diff(diff_text: str) -> dict[str, list[tuple[int, int]]]:
    """Parse unified diff output into file -> line-range mappings.

    Handles the ``@@ -old,count +new,count @@`` hunk header format.
    """
    ranges: dict[str, list[tuple[int, int]]] = {}
    current_file: str | None = None

    # Match "@@ ... +start,count @@" or "@@ ... +start @@"
    hunk_pattern = re.compile(r"^@@ .+? \+(\d+)(?:,(\d+))? @@")

    for line in diff_text.splitlines():
        file_match = _POST_IMAGE_HEADER.match(line)
        if file_match:
            current_file = _diff_header_path(file_match.group(1))
            continue

        hunk_match = hunk_pattern.match(line)
        if hunk_match and current_file is not None:
            start = int(hunk_match.group(1))
            count = int(hunk_match.group(2)) if hunk_match.group(2) else 1
            if count == 0:
                # Pure deletion hunk (no lines added); still note the position.
                end = start
            else:
                end = start + count - 1
            ranges.setdefault(current_file, []).append((start, end))

    return ranges


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
            # Check overlap with any changed range.
            for start, end in ranges:
                if node.line_start <= end and node.line_end >= start:
                    result.append(node)
                    seen.add(node.qualified_name)
                    break

    return result


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
      - Test coverage: 0.30 (untested) scaling down to 0.05 (5+ TESTED_BY edges)
      - Security sensitivity: 0.20 if name matches security keywords
      - Caller count: callers / 20, capped at 0.10
      - Change frequency (opt-in): commits touching the file / 10, capped
        at 0.15
    """
    score = 0.0

    # --- Flow participation (cap 0.25), weighted by criticality ---
    flow_criticalities = store.get_flow_criticalities_for_node(node.id)
    if flow_criticalities:
        score += min(sum(flow_criticalities), 0.25)
    else:
        flow_count = store.count_flow_memberships(node.id)
        score += min(flow_count * 0.05, 0.25)

    # --- Community crossing (cap 0.15) ---
    callers = store.get_edges_by_target(node.qualified_name)
    caller_edges = [e for e in callers if e.kind == "CALLS"]

    cross_community = 0
    node_cid = store.get_node_community_id(node.id)

    if node_cid is not None and caller_edges:
        caller_qns = [edge.source_qualified for edge in caller_edges]
        cid_map = store.get_community_ids_by_qualified_names(caller_qns)
        for cid in cid_map.values():
            if cid is not None and cid != node_cid:
                cross_community += 1
    score += min(cross_community * 0.05, 0.15)

    # --- Test coverage (direct + transitive) ---
    transitive_tests = store.get_transitive_tests(node.qualified_name)
    test_count = len(transitive_tests)
    score += 0.30 - (min(test_count / 5.0, 1.0) * 0.25)

    # --- Security sensitivity ---
    name_lower = node.name.lower()
    qn_lower = node.qualified_name.lower()
    if any(kw in name_lower or kw in qn_lower for kw in _SECURITY_KEYWORDS):
        score += 0.20

    # --- Caller count (cap 0.10) ---
    caller_count = len(caller_edges)
    score += min(caller_count / 20.0, 0.10)

    # --- Change frequency (opt-in, cap 0.15) ---
    if churn_counts and node.file_path:
        commit_count = churn_counts.get(node.file_path, 0)
        score += min(commit_count / _CHURN_SATURATION, 1.0) * _CHURN_WEIGHT

    return round(min(max(score, 0.0), 1.0), 4)


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
        changed_ranges = {
            normalize_file_path(root_path / key): ranges
            for key, ranges in raw_ranges.items()
        }

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

    # Map changes to nodes.
    if changed_ranges:
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

    # Cap to prevent O(N*M) query explosion on large PRs.
    _max_funcs = env_int("CRG_MAX_CHANGED_FUNCS", 500)
    funcs_truncated = len(changed_funcs) > _max_funcs
    if funcs_truncated:
        changed_funcs = changed_funcs[:_max_funcs]

    churn_counts: dict[str, int] | None = None
    churn_status = CHURN_OFF
    if include_churn and repo_root is not None:
        raw_churn, churn_status = compute_file_churn_with_status(repo_root)
        churn_counts = {}
        root_path = Path(repo_root)
        for key, count in raw_churn.items():
            churn_counts[key] = count
            churn_counts[normalize_file_path(root_path / key)] = count

    # Compute per-node risk scores.
    node_risks: list[dict[str, Any]] = []
    for node in changed_funcs:
        risk = compute_risk_score(store, node, churn_counts)
        node_risks.append({
            **node_to_dict(node),
            "risk_score": risk,
        })

    # Overall risk score: max of individual risks, or 0.
    overall_risk = max((nr["risk_score"] for nr in node_risks), default=0.0)

    # Affected flows.
    affected = get_affected_flows(store, changed_files)

    # Detect test gaps: changed functions without TESTED_BY edges.
    #
    # Stored file paths are absolute, so test-ness is judged against the path
    # relative to the repository root: reading ``tests/`` out of an absolute
    # path would also match a directory above the checkout, and a repository
    # cloned into a CI workspace named "test" would report no gaps at all
    # (issue #1023). ``repo_root`` is the caller's; the graph's own recorded
    # root covers callers that pass none.
    gap_root = repo_root or store.get_repo_root()
    test_gaps: list[dict[str, Any]] = []
    for node in changed_funcs:
        # A symbol that lives in a test file is test code and can never be a
        # gap in production test coverage. The path is checked as well as the
        # stored flag so a graph built before the parser marked non-function
        # test nodes still gives the right answer: those rows carry
        # ``is_test = 0`` and used to be reported back to the author as their
        # own tests needing tests (issue #1014).
        if node.is_test or is_test_file(node.file_path, gap_root):
            continue
        if node.name in _TEST_GAP_EXEMPT_NAMES:
            continue
        # TESTED_BY edges are stored as source=production, target=test by the
        # parser, so a changed production function finds its tests by source.
        # See: #515
        tested = store.get_edges_by_source(node.qualified_name)
        if not any(e.kind == "TESTED_BY" for e in tested):
            test_gaps.append({
                "name": _sanitize_name(node.name),
                "qualified_name": _sanitize_name(node.qualified_name),
                "file": node.file_path,
                "line_start": node.line_start,
                "line_end": node.line_end,
            })

    # Review priorities: top 10 by risk score.
    review_priorities = sorted(node_risks, key=lambda x: x["risk_score"], reverse=True)[:10]

    # Build summary.
    summary_parts = [
        f"Analyzed {len(changed_files)} changed file(s):",
        f"  - {len(changed_funcs)} changed function(s)/class(es)",
        f"  - {affected['total']} affected flow(s)",
        f"  - {len(test_gaps)} test gap(s)",
        f"  - Overall risk score: {overall_risk:.2f}",
    ]
    if test_gaps:
        # Dedup by bare name in the human summary. The underlying test_gaps
        # list keeps every entry (a downstream consumer needs precision via
        # qualified_name), but a graph that ended up with the same function
        # stored under two qualified_names (e.g. relative + absolute path
        # variants) would otherwise print "X, X, Y, Y" — surfacing graph
        # corruption as a UX bug. The root cause is path normalization;
        # this is the defensive last line.
        seen_names: set[str] = set()
        gap_names: list[str] = []
        for g in test_gaps:
            n = g["name"]
            if n in seen_names:
                continue
            seen_names.add(n)
            gap_names.append(n)
            if len(gap_names) >= 5:
                break
        summary_parts.append(f"  - Untested: {', '.join(gap_names)}")
    if funcs_truncated:
        summary_parts.append(
            f"  - Warning: analysis capped at {_max_funcs} functions "
            f"(set CRG_MAX_CHANGED_FUNCS to adjust)"
        )
    if ranges_unavailable:
        summary_parts.append(
            "  - Warning: line-level diff unavailable, whole files scored "
            f"({ranges_unavailable})"
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
        "affected_flows": affected["affected_flows"],
        "test_gaps": test_gaps,
        "review_priorities": review_priorities,
        "functions_truncated": funcs_truncated,
        "diff_ranges_unavailable": ranges_unavailable,
        "churn_status": churn_status,
    }
