"""Post-build resolver for PHP/Rust/C# scoped calls and receiver-typed calls.

Tree-sitter extraction records a scoped/static call such as ``Mailer::send($x)``
(PHP) or ``Mailer::send(x)`` / ``Self::new()`` (Rust) as a ``CALLS`` edge whose
target is the intermediate ``Class::method`` string.  That string matches
neither the canonical node key (``<file>::Class.method``) nor a bare method
name, so ``callers_of``, ``get_impact_radius`` and ``tests_for`` never see the
edge and report zero callers for a method that is obviously being called
(GitHub #567).

PHP instance calls keep their historical bare method target during parsing.
When a local receiver was directly constructed (``$x = new Type(); $x->run()``),
the parser stores the class as ``extra.receiver_scope`` and this pass uses that
evidence to resolve the call without changing parse-only output (GitHub #745).

C# receiver calls (``Service.StaticCall()``, ``obj.InstanceCall()``,
``obj?.ConditionalCall()``) follow the same pattern: ``using`` directives import
namespaces rather than files, so a single-file parse cannot know which file
defines the receiver's class. The parser keeps the bare method target and
records the receiver class in ``extra.receiver_scope`` (from the static class
receiver itself, or from a declared/constructed local variable type); this pass
resolves it against Class/method nodes in the graph, disambiguating multiple
same-named candidates by call-site file and by namespace visibility
(``using`` directives plus the caller file's own declared namespaces)
(GitHub #612).

This module runs after the graph is built and rewrites the resolvable
``Class::method`` targets to the canonical qualified name of the defined
method node, in the same style as the Spring/Temporal/ReScript resolvers.

It is deliberately conservative so it never fabricates an edge:

* Only genuine ``Class::method`` targets are considered; already-resolved
  targets (``<file>::Class.method``) are left alone.
* ``self`` / ``static`` (PHP) and ``self`` / ``Self`` (Rust) resolve to the
  enclosing class/type of the caller.
* A target resolves when exactly one ``(class, method)`` node exists in the
  graph, or when the caller file's ``IMPORTS_FROM`` edges disambiguate between
  several same-named definitions.
* Ambiguous or unknown targets (external types such as ``Vec::new`` or
  ``Redis::get`` with no in-graph definition) are left untouched, keeping the
  behaviour of unresolved external calls exactly as it is today.

Rewritten edges are tagged ``confidence_tier = INFERRED`` to distinguish a
heuristically resolved scoped call from a directly-extracted one.

Language notes:

* PHP scopes are namespace-qualified (``App\\Mail\\Mailer``) and PHP keywords
  are case-insensitive, so ``self`` / ``static`` (any case) mean the enclosing
  class.
* Rust scoped calls come through as ``::``-joined path segments.  Only genuine
  two-segment ``Type::method`` targets are resolved; multi-segment module paths
  (``crate::mailer::Mailer::new``) are left alone because resolving them by
  their last two segments would point at an unrelated type.  ``Self`` (the
  associated-function receiver) resolves to the enclosing type, while lowercase
  ``self`` is a *module* path — never a type — so it is not resolved here.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .graph import GraphStore

logger = logging.getLogger(__name__)

# Languages whose scoped/static calls dangle as ``Class::method`` targets.
_SCOPED_LANGUAGES = ("php", "rust", "csharp")

# Languages whose parsers keep a bare method target and record the receiver
# class in ``extra.receiver_scope`` for this pass to resolve.
_RECEIVER_SCOPE_LANGUAGES = ("php", "csharp")

# Node kinds that can be a scoped-call target (methods live under a class).
_METHOD_KINDS = ("Function", "Test", "Method")

# PHP scope receivers (case-insensitive) that mean the enclosing class.
_PHP_SELF_SCOPES = {"self", "static"}

# Separators that split an import target into path segments across languages
# (PHP ``App\Mail\Mailer``, Rust ``crate::mail::Mailer``, resolved paths).
_IMPORT_SEGMENT_SEPARATORS = ("::", "\\", "/")


def _import_segments(target: str) -> list[str]:
    """Split an import target into its path segments on any separator."""
    normalized = target
    for sep in _IMPORT_SEGMENT_SEPARATORS[1:]:
        normalized = normalized.replace(sep, "::")
    return [seg for seg in normalized.split("::") if seg]


def _fold(name: str, language: str) -> str:
    """Case-fold identifiers for PHP (case-insensitive) but not Rust.

    PHP class and function names are matched case-insensitively by the language,
    so ``Mailer::DISPATCH`` and ``mailer::dispatch`` name the same symbol.  Rust
    identifiers are case-sensitive, so ``Mailer::send`` and ``Mailer::Send`` are
    different symbols and must never be conflated.
    """
    return name.casefold() if language == "php" else name


def _csharp_scope_suffixes(name: str) -> list[str]:
    """Return a C# type path from most to least qualified."""
    parts = name.split(".")
    return [".".join(parts[index:]) for index in range(len(parts))]


def _csharp_lexical_scopes(enclosing: Optional[str], receiver: str) -> list[str]:
    """Return the receiver resolved against each enclosing scope, innermost first.

    C# name lookup starts in the innermost enclosing type and walks outward
    before it ever considers a global type, so inside ``Outer.Consumer`` the
    receiver ``D.E`` means ``Outer.D.E`` even when a top-level ``D.E`` exists.
    ``("Outer.Consumer", "D.E")`` → ``["Outer.Consumer.D.E", "Outer.D.E"]``.
    """
    if not enclosing:
        return []
    parts = enclosing.split(".")
    return [
        ".".join(parts[: index + 1]) + f".{receiver}"
        for index in reversed(range(len(parts)))
    ]


def _path_tokens(file_path: str) -> list[str]:
    """Path split into segments, with the file extension stripped off the last.

    ``/repo/src/Queue/Mailer.php`` → ``["repo", "src", "Queue", "Mailer"]``;
    ``/repo/src/mailer.rs`` → ``["repo", "src", "mailer"]``.
    """
    parts = [p for p in file_path.replace("\\", "/").split("/") if p]
    if parts:
        parts[-1] = parts[-1].rsplit(".", 1)[0]
    return parts


def _path_key(file_path: str) -> str:
    """Normalize path separators for exact resolved-import comparisons."""
    return file_path.replace("\\", "/")


def _path_ends_with(
    path_tokens: list[str], suffix: list[str], language: str
) -> bool:
    """Whether a complete import/module suffix matches a defining file path."""
    if not suffix or len(suffix) > len(path_tokens):
        return False
    path_tail = path_tokens[-len(suffix) :]
    return all(
        _fold(left, language) == _fold(right, language)
        for left, right in zip(path_tail, suffix)
    )


def _safe_import_suffixes(segments: list[str], language: str) -> list[list[str]]:
    """Return complete import suffixes that can safely identify a source path.

    PHP projects commonly map one leading namespace root (for example ``App``)
    onto a source directory. Rust imports carry a trailing type and may start
    with a module-root keyword. We strip only those known structural segments;
    arbitrary leading namespace/module segments are never discarded.
    """
    if language == "php":
        suffixes = [segments]
        if len(segments) > 2:
            suffixes.append(segments[1:])
        return [suffix for suffix in suffixes if len(suffix) >= 2]

    if language == "rust" and len(segments) >= 2:
        module_path = segments[:-1]
        suffixes = [module_path]
        if module_path and module_path[0] in {"crate", "self", "super"}:
            suffixes.append(module_path[1:])
        return [suffix for suffix in suffixes if suffix]

    return []


def _is_unresolved_scoped_target(target: str) -> bool:
    """True when *target* is a raw ``Class::method`` string, not a node key.

    Canonical node names have the shape ``<abs-file>::Class.method`` — the head
    before the first ``::`` is a filesystem path and the tail after the last
    ``::`` contains a ``.``.  A raw scoped call (``Mailer::dispatch``,
    ``App\\Mail\\Mailer::dispatch``) has neither, so it is what we resolve.
    """
    if "::" not in target:
        return False
    head = target.split("::", 1)[0]
    if "/" in head:  # already a <file>::... qualified name
        return False
    tail = target.rsplit("::", 1)[1]
    if "." in tail:  # already a Class.method qualified name
        return False
    return True


def _enclosing_class(source_qualified: str) -> Optional[str]:
    """Return the class/type short name that encloses a caller node, if any.

    ``<file>::Mailer.dispatch`` → ``Mailer``; a free function
    ``<file>::register`` (no ``.``) → ``None``.
    """
    if "::" not in source_qualified:
        return None
    tail = source_qualified.split("::", 1)[1]
    if "." not in tail:
        return None
    return tail.rsplit(".", 1)[0]


def _scope_and_method(
    target: str, language: str
) -> Optional[tuple[Optional[str], str, bool]]:
    """Parse a raw scoped target into ``(class, method, needs_enclosing)``.

    ``class`` is ``None`` when ``needs_enclosing`` is set (``self`` / ``Self`` /
    ``static``), signalling the caller's enclosing class should be used.
    Returns ``None`` for targets that must be left untouched (``parent::``, a
    Rust module path, a lowercase ``self::`` module call, …).
    """
    if language == "php":
        scope, _, method = target.partition("::")
        if not scope or not method or "::" in method:
            return None
        lowered = scope.strip("\\").casefold()
        if lowered in _PHP_SELF_SCOPES:
            return None, method, True
        if lowered == "parent":
            return None
        # Strip a namespace prefix: ``App\Mail\Mailer`` → ``Mailer``.
        class_name = scope.strip("\\").rsplit("\\", 1)[-1]
        if not class_name:
            return None
        return class_name, method, False

    if language == "rust":
        # Only genuine two-segment ``Type::method`` calls are resolvable;
        # longer module paths are deliberately left alone.
        segments = target.split("::")
        if len(segments) != 2:
            return None
        scope, method = segments
        if not scope or not method:
            return None
        if scope == "Self":
            return None, method, True
        if scope == "self":
            # ``self::foo`` is a module-relative function, never a type method.
            return None
        return scope, method, False

    if language == "csharp":
        scope, _, method = target.partition("::")
        if not scope or not method or "::" in method:
            return None
        return scope, method, False

    return None


def resolve_scoped_calls(store: GraphStore) -> dict:
    """Rewrite evidence-backed scoped CALLS targets to node qualified names.

    Safe to call repeatedly — rewritten edges start with a path head (and carry
    ``extra.scoped_resolved``), so they are no longer treated as candidates.

    Returns a dict with resolution counts for telemetry.
    """
    conn = store._conn

    lang_placeholders = ",".join("?" for _ in _SCOPED_LANGUAGES)
    # file_language: file_path → language, restricted to the scoped languages.
    file_language: dict[str, str] = {
        row["file_path"]: row["language"]
        for row in conn.execute(
            "SELECT DISTINCT file_path, language FROM nodes "
            f"WHERE language IN ({lang_placeholders})",  # nosec B608
            _SCOPED_LANGUAGES,
        ).fetchall()
    }
    if not file_language:
        return {"files_indexed": 0, "calls_resolved": 0}

    # ------------------------------------------------------------------
    # method_map: (language, class_key, method_key) → [qualified, ...]
    # Keyed by language so a PHP class never resolves a same-named Rust type;
    # keys are case-folded for PHP only (Rust identifiers are case-sensitive).
    # ------------------------------------------------------------------
    method_map: dict[tuple[str, str, str], list[str]] = {}
    file_of: dict[str, str] = {}
    kind_placeholders = ",".join("?" for _ in _METHOD_KINDS)
    for row in conn.execute(
        "SELECT name, parent_name, qualified_name, file_path, language FROM nodes "
        f"WHERE kind IN ({kind_placeholders}) "  # nosec B608
        f"AND language IN ({lang_placeholders}) "  # nosec B608
        "AND parent_name IS NOT NULL",
        (*_METHOD_KINDS, *_SCOPED_LANGUAGES),
    ).fetchall():
        lang = row["language"]
        key = (
            lang,
            _fold(row["parent_name"], lang),
            _fold(row["name"], lang),
        )
        method_map.setdefault(key, []).append(row["qualified_name"])
        file_of[row["qualified_name"]] = row["file_path"]

    if not method_map:
        return {"files_indexed": len(file_language), "calls_resolved": 0}

    # ------------------------------------------------------------------
    # imports_by_file: caller file → set of imported target strings, used to
    # disambiguate between several same-named definitions.
    # ------------------------------------------------------------------
    imports_by_file: dict[str, set[str]] = {}
    for row in conn.execute(
        "SELECT file_path, target_qualified FROM edges WHERE kind = 'IMPORTS_FROM'"
    ).fetchall():
        imports_by_file.setdefault(row["file_path"], set()).add(
            row["target_qualified"]
        )

    # ------------------------------------------------------------------
    # csharp_namespaces_by_file: .cs file → namespaces it declares. C#
    # ``using`` directives store the raw namespace string as their
    # IMPORTS_FROM target, so namespace visibility is the disambiguation
    # evidence between same-named C# classes (see #310, #612).
    # ------------------------------------------------------------------
    csharp_namespaces_by_file: dict[str, set[str]] = {}
    if "csharp" in file_language.values():
        for row in conn.execute(
            "SELECT file_path, extra FROM nodes "
            "WHERE kind = 'File' AND language = 'csharp'"
        ).fetchall():
            try:
                node_extra = json.loads(row["extra"] or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(node_extra, dict):
                continue
            declared = node_extra.get("csharp_namespaces")
            if isinstance(declared, list):
                csharp_namespaces_by_file[row["file_path"]] = {
                    ns for ns in declared if isinstance(ns, str)
                }

    def csharp_disambiguate(
        candidates: list[str], caller_file: str
    ) -> tuple[Optional[str], Optional[str]]:
        """Pick the single candidate the C# call site can actually see.

        Same-file definitions win outright; otherwise a candidate must be
        the only one whose defining file declares a namespace visible to
        the caller (via a ``using`` directive or the caller's own declared
        namespaces).
        """
        same_file = [
            candidate
            for candidate in candidates
            if file_of.get(candidate) == caller_file
        ]
        if len(same_file) == 1:
            return same_file[0], "same_file"
        visible_ns = {
            target
            for target in imports_by_file.get(caller_file, set())
            if "/" not in target and "\\" not in target
        }
        visible_ns |= csharp_namespaces_by_file.get(caller_file, set())
        visible = [
            candidate
            for candidate in candidates
            if csharp_namespaces_by_file.get(file_of.get(candidate, ""), set())
            & visible_ns
        ]
        if len(visible) == 1:
            return visible[0], "namespace"
        return None, None

    def disambiguate(
        candidates: list[str], caller_file: str, class_name: str, language: str
    ) -> Optional[str]:
        imported = imports_by_file.get(caller_file, set())
        if not imported:
            return None
        # (1) Prefer a candidate whose defining file is explicitly imported.
        # #574's PHP import resolver rewrites resolvable ``use`` targets to the
        # absolute path of the class file, so a direct path match is exact.
        imported_paths = {
            _path_key(target)
            for target in imported
            if "/" in target or "\\" in target
        }
        matched = [
            candidate
            for candidate in candidates
            if _path_key(file_of.get(candidate, "")) in imported_paths
        ]
        if len(matched) == 1:
            return matched[0]
        # (2) Fall back to a fully-qualified ``use`` target that could not be
        # resolved to a path (no composer/module map). A candidate must match a
        # complete normalized import suffix, not merely have the longest partial
        # overlap. Otherwise an unrelated import such as
        # ``App\Warehouse\Queue\Mailer`` could be fabricated as
        # ``src/Order/Queue/Mailer.php`` just because both end in Queue/Mailer.
        matched_by_suffix: set[str] = set()
        for candidate in candidates:
            tokens = _path_tokens(file_of.get(candidate, ""))
            for target in imported:
                segments = _import_segments(target)
                if not segments:
                    continue
                if _fold(segments[-1], language) != _fold(class_name, language):
                    continue
                suffixes = _safe_import_suffixes(segments, language)
                if any(
                    _path_ends_with(tokens, suffix, language)
                    for suffix in suffixes
                ):
                    matched_by_suffix.add(candidate)
                    break
        if len(matched_by_suffix) == 1:
            return next(iter(matched_by_suffix))
        return None

    calls_rows = conn.execute(
        "SELECT id, source_qualified, target_qualified, extra, file_path, line "
        "FROM edges WHERE kind = 'CALLS'"
    ).fetchall()

    resolved = 0
    for row in calls_rows:
        language = file_language.get(row["file_path"])
        if language is None:
            continue
        target = row["target_qualified"]
        try:
            extra = json.loads(row["extra"] or "{}")
        except (json.JSONDecodeError, TypeError):
            extra = {}
        if not isinstance(extra, dict):
            extra = {}

        resolution_target = target
        receiver_scope = extra.get("receiver_scope")
        if (
            language in _RECEIVER_SCOPE_LANGUAGES
            and "::" not in target
            and isinstance(receiver_scope, str)
            and receiver_scope
        ):
            resolution_target = f"{receiver_scope}::{target}"
        elif not _is_unresolved_scoped_target(target):
            continue

        parsed = _scope_and_method(resolution_target, language)
        if parsed is None:
            continue
        class_name, method, needs_enclosing = parsed

        if needs_enclosing:
            enclosing = _enclosing_class(row["source_qualified"])
            if not enclosing:
                continue
            class_name = enclosing
        assert class_name is not None  # non-enclosing parse always sets a class

        # C# resolves a receiver by binding its leading identifier to a
        # namespace or type, then binding each later component relative to what
        # preceded it. It never discards a leading component and restarts.
        #
        # Two ordered phases approximate that. Both match a containing-type path
        # exactly; there is no suffix fallback, because selecting a nested type
        # from a bare receiver would bind names C# cannot bind.
        #   1. lexical - the receiver under each enclosing type, innermost first
        #   2. exact   - the receiver, then forms with a leading namespace removed
        #
        # Phase 2 exists only because namespaces are absent from ``parent_name``
        # (they live in ``csharp_namespaces_by_file``), so a namespace-qualified
        # receiver such as ``App.Report.ExportHandler`` is stored as
        # ``Report.ExportHandler``. Every shortened form therefore has to prove
        # the part it dropped really is a namespace of the candidate's defining
        # file. Without that check the resolver would happily strip ``App`` and
        # bind a ``Report.ExportHandler`` sitting in namespace ``Other`` -- a
        # single candidate takes the single_match path, so nothing downstream
        # would catch it. ``needs_enclosing`` targets already name the caller's
        # own type, so they never take the lexical phase.
        lookups: list[tuple[str, Optional[str]]] = []
        if language == "csharp":
            if not needs_enclosing:
                lookups.extend(
                    (scope, None)
                    for scope in _csharp_lexical_scopes(
                        _enclosing_class(row["source_qualified"]), class_name,
                    )
                )
            parts = class_name.split(".")
            lookups.extend(
                (".".join(parts[index:]), ".".join(parts[:index]) or None)
                for index in range(len(parts))
            )
        else:
            lookups.append((class_name, None))

        candidates = None
        for candidate_class, dropped_namespace in lookups:
            found = method_map.get((
                language,
                _fold(candidate_class, language),
                _fold(method, language),
            ))
            if not found:
                continue
            if dropped_namespace is not None:
                # The namespace evidence is per *file*, not per node, and one C#
                # file may declare several namespaces. Membership alone would
                # only prove the dropped name appears somewhere in the candidate's
                # file, not that it encloses the candidate: a file declaring both
                # ``App`` and ``Other`` would let ``App.Ledger.AuditHandler`` bind
                # an ``Other.Ledger.AuditHandler``. Require the file to declare
                # exactly the dropped namespace, so the evidence is unambiguous.
                # This leaves genuine matches in multi-namespace files unresolved,
                # which is the right trade for a resolver that must not fabricate
                # edges; per-node namespaces would resolve them properly.
                found = [
                    candidate
                    for candidate in found
                    if csharp_namespaces_by_file.get(
                        file_of.get(candidate, ""), set(),
                    ) == {dropped_namespace}
                ]
                if not found:
                    continue
            candidates = found
            break
        if not candidates:
            continue

        if len(candidates) == 1:
            new_target = candidates[0]
            via = "single_match"
        elif language == "csharp":
            new_target, via = csharp_disambiguate(candidates, row["file_path"])
            if new_target is None:
                continue
        else:
            new_target = disambiguate(
                candidates, row["file_path"], class_name, language
            )
            if new_target is None:
                continue
            via = "import"

        extra["scoped_resolved"] = True
        extra["scoped_via"] = via
        serialized_extra = json.dumps(extra)

        conn.execute(
            "UPDATE edges SET target_qualified = ?, extra = ?, "
            "confidence_tier = 'INFERRED' WHERE id = ?",
            (new_target, serialized_extra, row["id"]),
        )
        # The parser mirrors calls made by Test nodes as TESTED_BY edges.
        # Keep that mirror aligned when a scoped target becomes canonical;
        # otherwise public tests_for still sees the original Class::method
        # source even though callers_of sees the resolved CALLS target.
        conn.execute(
            "UPDATE edges SET source_qualified = ?, extra = ?, "
            "confidence_tier = 'INFERRED' "
            "WHERE kind = 'TESTED_BY' AND source_qualified = ? "
            "AND target_qualified = ? AND file_path = ? AND line = ?",
            (
                new_target,
                serialized_extra,
                target,
                row["source_qualified"],
                row["file_path"],
                row["line"],
            ),
        )
        resolved += 1
        logger.debug(
            "Scoped resolver: %s → %s (via %s)",
            resolution_target, new_target, via,
        )

    if resolved:
        conn.commit()

    logger.info(
        "Scoped resolver: resolved %d CALLS edges across %d files",
        resolved, len(file_language),
    )
    return {"files_indexed": len(file_language), "calls_resolved": resolved}
