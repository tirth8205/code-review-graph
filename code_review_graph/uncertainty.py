"""Honest uncertainty markers for empty graph results (#314, #819, #850, #851).

A bare ``result_count: 0`` is ambiguous. It can mean "the code really has no
such relationship", or it can mean "this graph cannot see that relationship":
the target was never indexed, the graph is behind the working tree, or the
target's language has a known static-analysis blind spot. Reading agents take
the first meaning, and then either draw a wrong conclusion or abandon the
graph and grep the whole repository.

One short sentence on the empty case is therefore a *token saving*, not a
cost: it replaces a multi-thousand-token fallback search with roughly thirty
tokens of honesty. To keep that trade favourable the marker is hard-capped at
``MAX_CONFIDENCE_CHARS`` and is attached only when a result list is empty, so
every response that carries results stays byte-identical to before.

The language table below is data, not scattered conditionals. Every entry
describes a gap that is real in this codebase *today*; capabilities that have
since been implemented (Go struct/interface embedding, Kotlin imports,
dotted-stem JS/TS import resolution, Python keyword-argument callbacks) are
deliberately absent.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .graph import _sanitize_name

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .graph import GraphStore

logger = logging.getLogger(__name__)

# Hard token budget. Compact context is the whole point of this project, so
# an advisory sentence that grows past this is a regression, not a feature.
MAX_CONFIDENCE_CHARS = 140

# Synthetic pattern names for the entry points that take no ``pattern``
# argument, so one table can serve every caller.
IMPACT_PATTERN = "impact_radius"

_UPDATE_HINT = "run `code-review-graph update`"
_WHITESPACE = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# Known static-analysis gaps, as data
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LanguageGap:
    """One verified blind spot in the parser, scoped to the queries it affects.

    ``patterns`` is what keeps the note honest: a container-resolution caveat
    belongs on ``callers_of`` and ``tests_for``, and never on ``file_summary``,
    whose empty result has nothing to do with call resolution.
    """

    languages: frozenset[str]
    patterns: frozenset[str]
    note: str


_JS_FAMILY = frozenset({"javascript", "typescript", "tsx"})

# Patterns answered from CALLS edges, where an unresolved dynamic call site
# can hide a real answer. ``references_to`` is deliberately excluded: it reads
# REFERENCES edges, which is exactly where an unresolved handoff does land.
_CALL_PATTERNS = frozenset({
    "callers_of", "callees_of", "tests_for", IMPACT_PATTERN,
})
# Patterns answered from IMPORTS_FROM edges.
_IMPORT_PATTERNS = frozenset({"imports_of", "importers_of", IMPACT_PATTERN})

# Order matters: the first entry matching (language, pattern) is emitted.
LANGUAGE_GAPS: tuple[LanguageGap, ...] = (
    LanguageGap(
        languages=frozenset({"php"}),
        patterns=_IMPORT_PATTERNS,
        note=(
            "php include/require is not indexed as an import edge, so "
            "importers can be missing here (#819)"
        ),
    ),
    LanguageGap(
        languages=frozenset({"php"}),
        patterns=_CALL_PATTERNS,
        note=(
            "php container-resolved and constructor-injected calls are not "
            "statically traced, so callers can be missing (#850, #851)"
        ),
    ),
    LanguageGap(
        languages=_JS_FAMILY,
        patterns=frozenset({"handlers_of", "endpoints_for"}),
        note=(
            "js/ts route registration is not indexed and endpoint edges are "
            "spring-only, so handlers can be missing"
        ),
    ),
    LanguageGap(
        languages=_JS_FAMILY,
        patterns=_IMPORT_PATTERNS,
        note=(
            "npm-aliased import specifiers are not resolved, so importers "
            "can be missing here (#343)"
        ),
    ),
    LanguageGap(
        languages=_JS_FAMILY,
        patterns=_CALL_PATTERNS,
        note=(
            "js/ts callbacks land on REFERENCES not CALLS, and obj[name]() "
            "is unresolved, so callers can be missing"
        ),
    ),
    LanguageGap(
        languages=frozenset({"java"}),
        patterns=_CALL_PATTERNS,
        note=(
            "java aop advice and reflective invocation are not statically "
            "traced, so callers can be missing here (#592)"
        ),
    ),
    LanguageGap(
        languages=frozenset({"go"}),
        patterns=frozenset({"inheritors_of"}),
        note=(
            "go interface satisfaction is structural, never declared, so "
            "implementers can be missing from inheritors_of"
        ),
    ),
    LanguageGap(
        languages=frozenset({"csharp"}),
        patterns=_CALL_PATTERNS,
        note=(
            "c# di-container registrations are not statically traced, so "
            "interface-typed callers can be missing here"
        ),
    ),
    LanguageGap(
        languages=frozenset({"python"}),
        patterns=_CALL_PATTERNS,
        note=(
            "python getattr dispatch and decorator-based registration are "
            "not statically traced, so callers can be missing here"
        ),
    ),
)


def gap_note(language: str | None, pattern: str) -> str | None:
    """Return the first verified gap note for *language* under *pattern*."""
    if not language:
        return None
    normalized = language.strip().lower()
    for gap in LANGUAGE_GAPS:
        if normalized in gap.languages and pattern in gap.patterns:
            return gap.note
    return None


# ---------------------------------------------------------------------------
# Budgeting and sanitisation
# ---------------------------------------------------------------------------


def _clip(text: str, limit: int) -> str:
    """Truncate *text* to *limit* characters, marking the cut."""
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)] + "~"


def _clean(text: str) -> str:
    """Make attacker-controlled source text safe to embed in one line.

    ``_sanitize_name`` is the project's established defence: it strips ASCII
    control characters and caps length. It deliberately keeps tabs and
    newlines, which would let a crafted node name break this single-line
    advisory into fake extra lines, so they are collapsed here as well.
    """
    cleaned = _sanitize_name(text, max_len=MAX_CONFIDENCE_CHARS)
    return _WHITESPACE.sub(" ", cleaned).strip()


def _fragment(text: str, limit: int) -> str:
    """Sanitized text, guaranteed to fit *limit* characters."""
    return _clip(_clean(text), max(1, limit))


def _bounded(note: str) -> str:
    """Final gate: nothing leaves this module over the budget."""
    return _fragment(note, MAX_CONFIDENCE_CHARS)


def _interpolated(prefix: str, value: str, suffix: str) -> str:
    """Build ``prefix + value + suffix`` without letting *value* blow the cap."""
    budget = MAX_CONFIDENCE_CHARS - len(prefix) - len(suffix)
    return f"{prefix}{_fragment(value, budget)}{suffix}"


def _target_fragment(target: str, limit: int) -> str:
    """Fit a query target into *limit*, keeping the half that identifies it.

    Qualified names are ``path::symbol``, so a left-anchored truncation throws
    away the symbol and keeps a directory prefix, the least useful half. Drop
    to the bare symbol before resorting to clipping. The split runs on the raw
    target because sanitising first can cut the ``::`` off a very long path.
    """
    cleaned = _clean(target)
    if len(cleaned) <= limit:
        return cleaned
    if "::" in target:
        symbol = _clean(target.rsplit("::", 1)[-1])
        if 0 < len(symbol) <= limit:
            return symbol
    return _clip(cleaned, max(1, limit))


def _interpolated_target(prefix: str, target: str, suffix: str) -> str:
    """``_interpolated`` for query targets, which are qualified names."""
    budget = MAX_CONFIDENCE_CHARS - len(prefix) - len(suffix)
    return f"{prefix}{_target_fragment(target, budget)}{suffix}"


# ---------------------------------------------------------------------------
# Individual markers
# ---------------------------------------------------------------------------


def not_indexed_note(target: str) -> str:
    """Say that the graph never saw *target*, so the zero proves nothing."""
    return _interpolated_target(
        "target not indexed: no node matching '",
        target,
        "', so this 0 is not evidence that none exist",
    )


def unresolved_stale_note(target: str) -> str:
    """Say the target is missing from a graph that predates HEAD.

    A stale graph explains the miss and has a remedy, so it outranks the
    flat "not indexed" wording, which reads like a permanent limitation.
    """
    return _interpolated_target(
        "graph is stale: no node matching '",
        target,
        "'; the graph predates HEAD, so run `code-review-graph update` first",
    )


def _confirmed_note(target: str, current: bool) -> str:
    """Say the zero is a real absence, so the agent can stop searching.

    The strong wording is only used when currency was actually checked. When
    it could not be (no VCS, no build metadata), the weaker sentence still
    saves the fallback search without claiming something unverified.
    """
    if current:
        return _interpolated_target(
            "'", target,
            "' is indexed and the graph is current, so this 0 is a real absence",
        )
    return _interpolated_target(
        "'", target,
        "' is indexed and no such edge is recorded; graph currency unverified",
    )


def _live_git_head(root: Path) -> str | None:
    """Read the checked-out commit.

    Imported lazily: ``tools._common`` pulls in the tool modules, which import
    this module, so a module-level import would be circular. The call costs a
    subprocess, which is why staleness is only ever checked on an empty result
    and never on the hot path.
    """
    from .tools._common import _read_live_git_head

    return _read_live_git_head(root)


def _staleness(
    store: GraphStore, root: Path, file_path: str | None,
) -> tuple[str | None, bool]:
    """Return ``(stale_note, currency_verified)`` for the graph.

    Two independent signals, either of which can prove staleness: the build
    commit versus the checked-out commit, and the target file's mtime versus
    the build timestamp. The second matters because a commit match says
    nothing about uncommitted edits. ``currency_verified`` is only ``True``
    when a check actually ran and passed, so callers never confuse "checked
    and fresh" with "could not check".
    """
    stored_sha = store.get_metadata("git_head_sha")
    live_sha = _live_git_head(root) if stored_sha else None
    if stored_sha and live_sha and live_sha != stored_sha:
        return (
            "graph is stale: built at an older commit than HEAD, so this "
            f"0 may be out of date; {_UPDATE_HINT}"
        ), False
    commit_verified = bool(stored_sha and live_sha)

    built_at_raw = store.get_metadata("last_updated")
    if not built_at_raw or not file_path:
        return None, commit_verified and not file_path
    try:
        # Graphs store absolute or repo-relative paths depending on how they
        # were built, so anchor relative ones rather than stat-ing the CWD.
        path = Path(file_path)
        if not path.is_absolute():
            path = root / path
        built_at = datetime.fromisoformat(built_at_raw)
        mtime = os.stat(path).st_mtime
        # Match the stored timestamp's awareness so the comparison is valid
        # for both naive (legacy) and timezone-aware build records.
        changed_at = datetime.fromtimestamp(mtime, tz=built_at.tzinfo)
    except (OSError, OverflowError, TypeError, ValueError):
        return None, False
    if changed_at > built_at:
        return _interpolated(
            "graph is stale: ",
            path.name,
            f" changed after the last build; {_UPDATE_HINT}",
        ), False
    return None, commit_verified


# ---------------------------------------------------------------------------
# Public entry points, one per tool that can return an empty result
# ---------------------------------------------------------------------------


def empty_query_confidence(
    store: GraphStore,
    root: Path,
    pattern: str,
    target: str,
    node: Any | None = None,
) -> str | None:
    """Return the marker for an empty ``query_graph`` result, or ``None``.

    The priority order is deliberate: an unresolved target makes the zero
    meaningless, a stale graph makes it untrustworthy, and a known language
    gap makes it incomplete. Only when none of those hold is the zero worth
    believing, and saying so is what stops an agent grepping anyway.
    """
    try:
        if node is None:
            if store.get_stats().total_nodes == 0:
                return _bounded(
                    "graph is empty: nothing is indexed, so this 0 says "
                    "nothing about the code; run `code-review-graph build`"
                )
            stale, _unused = _staleness(store, root, None)
            if stale:
                return _bounded(unresolved_stale_note(target))
            return _bounded(not_indexed_note(target))

        stale, current = _staleness(store, root, getattr(node, "file_path", None))
        if stale:
            return _bounded(stale)

        gap = gap_note(getattr(node, "language", None), pattern)
        if gap:
            return _bounded(gap)

        return _bounded(_confirmed_note(target, current))
    except Exception:
        # An advisory marker must never turn a working tool call into an
        # error. Degrading to "no marker" restores exactly today's response.
        logger.debug("Could not compute empty-result confidence", exc_info=True)
        return None


def empty_impact_confidence(
    store: GraphStore,
    root: Path,
    changed_files: list[str],
    resolved_files: list[str],
    language: str | None = None,
) -> str | None:
    """Return the marker for an empty ``get_impact_radius`` blast radius."""
    try:
        if not resolved_files:
            unknown = changed_files[0] if changed_files else ""
            return _bounded(not_indexed_note(Path(unknown).name if unknown else ""))

        stale, current = _staleness(store, root, resolved_files[0])
        if stale:
            return _bounded(stale)

        gap = gap_note(language, IMPACT_PATTERN)
        if gap:
            return _bounded(gap)

        if current:
            return _bounded(
                "changed files are indexed and the graph is current, so this "
                "0 is a real absence"
            )
        return _bounded(
            "changed files are indexed and nothing depends on them; graph "
            "currency unverified"
        )
    except Exception:
        logger.debug("Could not compute empty-impact confidence", exc_info=True)
        return None


def empty_search_confidence(
    store: GraphStore,
    root: Path,
    query: str,
) -> str | None:
    """Return the marker for a ``semantic_search_nodes`` run with zero hits."""
    try:
        if store.get_stats().total_nodes == 0:
            return _bounded(
                "graph is empty: nothing is indexed, so this 0 says nothing "
                "about the code; run `code-review-graph build`"
            )

        stale, _current = _staleness(store, root, None)
        if stale:
            return _bounded(stale)

        return _bounded(_interpolated(
            "no indexed node matches '",
            query,
            "'; search covers names, paths and signatures, not source text",
        ))
    except Exception:
        logger.debug("Could not compute empty-search confidence", exc_info=True)
        return None
