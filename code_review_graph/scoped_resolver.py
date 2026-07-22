"""Post-build resolver for static/scoped ``Class::method`` calls.

Tree-sitter extraction records a scoped/static call such as ``Mailer::send($x)``
(PHP) or ``Mailer::send(x)`` / ``Self::new()`` (Rust) as a ``CALLS`` edge whose
target is the intermediate ``Class::method`` string.  That string matches
neither the canonical node key (``<file>::Class.method``) nor a bare method
name, so ``callers_of``, ``get_impact_radius`` and ``tests_for`` never see the
edge and report zero callers for a method that is obviously being called
(GitHub #567).

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
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .graph import GraphStore

logger = logging.getLogger(__name__)

# Languages whose scoped/static calls dangle as ``Class::method`` targets.
_SCOPED_LANGUAGES = ("php",)

# Node kinds that can be a scoped-call target (methods live under a class).
_METHOD_KINDS = ("Function", "Test", "Method")

# Scope receivers that refer to the enclosing class rather than a named one.
_SELF_SCOPES = {"self", "static"}


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


def _php_scope_parts(target: str) -> Optional[tuple[str, str]]:
    """Split a PHP ``Class::method`` target into (class-short-name, method)."""
    scope, _, method = target.partition("::")
    if not scope or not method or "::" in method:
        return None
    # Strip a namespace prefix: ``App\Mail\Mailer`` → ``Mailer``.
    class_name = scope.strip("\\").rsplit("\\", 1)[-1]
    if not class_name:
        return None
    return class_name, method


def _short_class_name(target: str) -> str:
    """Last identifier segment of an import target (path, FQN or module path)."""
    for sep in ("/", "\\", "::"):
        if sep in target:
            target = target.rsplit(sep, 1)[-1]
    # Drop a trailing extension (``Mailer.php`` → ``Mailer``) and any ``.method``.
    return target.split(".", 1)[0]


def resolve_scoped_calls(store: GraphStore) -> dict:
    """Rewrite resolvable ``Class::method`` CALLS targets to node qualified names.

    Safe to call repeatedly — rewritten edges start with a path head (and carry
    ``extra.scoped_resolved``), so they are no longer treated as candidates.

    Returns a dict with resolution counts for telemetry.
    """
    conn = store._conn

    lang_placeholders = ",".join("?" for _ in _SCOPED_LANGUAGES)
    scoped_files: set[str] = {
        row["file_path"]
        for row in conn.execute(
            "SELECT DISTINCT file_path FROM nodes "
            f"WHERE language IN ({lang_placeholders})",  # nosec B608
            _SCOPED_LANGUAGES,
        ).fetchall()
    }
    if not scoped_files:
        return {"files_indexed": 0, "calls_resolved": 0}

    # ------------------------------------------------------------------
    # method_map: (class_casefold, method_casefold) → [qualified_name, ...]
    # ------------------------------------------------------------------
    method_map: dict[tuple[str, str], list[str]] = {}
    file_of: dict[str, str] = {}
    kind_placeholders = ",".join("?" for _ in _METHOD_KINDS)
    for row in conn.execute(
        "SELECT name, parent_name, qualified_name, file_path FROM nodes "
        f"WHERE kind IN ({kind_placeholders}) "  # nosec B608
        f"AND language IN ({lang_placeholders}) "  # nosec B608
        "AND parent_name IS NOT NULL",
        (*_METHOD_KINDS, *_SCOPED_LANGUAGES),
    ).fetchall():
        key = (row["parent_name"].casefold(), row["name"].casefold())
        method_map.setdefault(key, []).append(row["qualified_name"])
        file_of[row["qualified_name"]] = row["file_path"]

    if not method_map:
        return {"files_indexed": len(scoped_files), "calls_resolved": 0}

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

    def disambiguate(
        candidates: list[str], caller_file: str, class_name: str
    ) -> Optional[str]:
        imported = imports_by_file.get(caller_file, set())
        if not imported:
            return None
        # (1) Prefer a candidate whose defining file is explicitly imported.
        # #574's PHP import resolver rewrites resolvable ``use`` targets to the
        # absolute path of the class file, so a direct path match is exact.
        imported_paths = {t for t in imported if "/" in t}
        matched = [c for c in candidates if file_of.get(c) in imported_paths]
        if len(matched) == 1:
            return matched[0]
        # (2) Fall back to matching a fully-qualified ``use`` target that could
        # not be resolved to a path (no composer PSR-4 map): line the import's
        # namespace segments up against the candidate file paths so that
        # ``use App\Queue\Mailer`` picks ``src/Queue/Mailer.php`` over
        # ``src/Mail/Mailer.php``.
        for target in imported:
            if _short_class_name(target).casefold() != class_name.casefold():
                continue
            segments = target.replace("/", "\\").strip("\\").split("\\")
            if len(segments) < 2:
                continue
            namespace_tail = segments[-2].casefold()
            ns_matched = [
                c for c in candidates
                if namespace_tail
                in {part.casefold() for part in file_of.get(c, "").split("/")}
            ]
            if len(ns_matched) == 1:
                return ns_matched[0]
        return None

    calls_rows = conn.execute(
        "SELECT id, source_qualified, target_qualified, extra, file_path "
        "FROM edges WHERE kind = 'CALLS'"
    ).fetchall()

    resolved = 0
    for row in calls_rows:
        if row["file_path"] not in scoped_files:
            continue
        target = row["target_qualified"]
        if not _is_unresolved_scoped_target(target):
            continue

        parts = _php_scope_parts(target)
        if parts is None:
            continue
        class_name, method = parts

        if class_name.casefold() in _SELF_SCOPES:
            enclosing = _enclosing_class(row["source_qualified"])
            if not enclosing:
                continue
            class_name = enclosing

        candidates = method_map.get((class_name.casefold(), method.casefold()))
        if not candidates:
            continue

        if len(candidates) == 1:
            new_target = candidates[0]
            via = "single_match"
        else:
            new_target = disambiguate(candidates, row["file_path"], class_name)
            if new_target is None:
                continue
            via = "import"

        try:
            extra = json.loads(row["extra"] or "{}")
        except (json.JSONDecodeError, TypeError):
            extra = {}
        extra["scoped_resolved"] = True
        extra["scoped_via"] = via

        conn.execute(
            "UPDATE edges SET target_qualified = ?, extra = ?, "
            "confidence_tier = 'INFERRED' WHERE id = ?",
            (new_target, json.dumps(extra), row["id"]),
        )
        resolved += 1
        logger.debug(
            "Scoped resolver: %s → %s (via %s)",
            target, new_target, via,
        )

    if resolved:
        conn.commit()

    logger.info(
        "Scoped resolver: resolved %d CALLS edges across %d files",
        resolved, len(scoped_files),
    )
    return {"files_indexed": len(scoped_files), "calls_resolved": resolved}
