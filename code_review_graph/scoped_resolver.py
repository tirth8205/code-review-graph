"""Post-build scoped/static call resolver (``Class::method`` targets).

Grammars that emit a ``::`` scope separator in call expressions store a CALLS
edge whose target is the intermediate ``Class::method`` form rather than a
node qualified name:

    PHP   ``Mailer::send($x)``      -> target ``Mailer::send``
    Rust  ``Notifier::notify(x)``   -> target ``Notifier::notify``

Every node is keyed by its dotted qualified name (``<file>::Class.method``),
so these ``Class::method`` targets match neither the qualified-name nor the
bare-name lookup paths and dangle forever — ``callers_of`` /
``get_impact_radius`` / ``tests_for`` silently under-report the caller.

This module runs as a post-build pass (like the Spring/Temporal/ReScript
resolvers) and rewrites resolvable ``Class::method`` targets to the canonical
node qualified name.  Resolution is conservative: an edge is only rewritten
when the ``(class, method)`` pair maps to exactly one node, or to exactly one
node whose file the source file imports.  Unresolvable targets (external
types such as ``Vec::new``) are left untouched so no false edge is created.

The intra-class receivers ``self`` / ``static`` / ``Self`` / ``this`` resolve
to the enclosing class of the call site.  (Languages whose grammars strip
these keywords — e.g. PHP emits ``self::m()`` as the bare target ``m`` — are
already handled by the same-file resolver and never reach this pass.)

Safe to call multiple times — already-resolved edges (whose target is a node
qualified name, i.e. has a file path before ``::``) are not re-selected.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .graph import GraphStore

logger = logging.getLogger(__name__)

# Receivers that refer to the enclosing class rather than a named class.
# Rust uses ``Self``; ``self`` is kept as a defensive guard.  (``static`` /
# ``this`` never reach this pass for the supported languages.)
_SELF_RECEIVERS = {"self", "Self"}


def _is_scoped_target(target: str) -> bool:
    """True if *target* is a 2-part ``Class::method`` form (not a node name).

    Two filters:

    * **Exactly one** ``::`` — a real ``Class::method``.  Multi-segment paths
      such as Rust ``a::b::run`` (module ``a`` -> submodule ``b`` -> free fn
      ``run``) have more than one ``::`` and are *not* resolved, since their
      leading segments are module paths, not a class name — resolving them by
      the last two segments would fabricate edges to unrelated classes.
    * The segment before ``::`` is a class (or PHP namespaced class), not a
      file path.  Node qualified names carry a file path there
      (``src/Mailer.php::Mailer.send``), which has a ``/`` or a file-extension
      ``.``.  ``\\`` is allowed so PHP namespaces (``App\\Mailer``) qualify;
      Windows file paths are still excluded by their ``.`` extension.
    """
    if target.count("::") != 1:
        return False
    prefix = target.split("::", 1)[0]
    return not any(ch in prefix for ch in ("/", "."))


def _enclosing(source_qualified: str) -> tuple[str | None, str | None]:
    """Return ``(file, enclosing_class)`` for a ``<file>::Class.method`` source."""
    if "::" not in source_qualified:
        return None, None
    file_part, after = source_qualified.split("::", 1)
    enclosing_class = after.split(".")[0] if "." in after else None
    return file_part, enclosing_class


def resolve_scoped_calls(store: GraphStore) -> dict:
    """Resolve ``Class::method`` CALLS targets to canonical node names.

    Returns a dict with resolution counts for telemetry.
    """
    conn = store._conn

    candidates = [
        row
        for row in conn.execute(
            "SELECT id, source_qualified, target_qualified, file_path, extra "
            "FROM edges WHERE kind = 'CALLS' AND target_qualified LIKE '%::%'"
        ).fetchall()
        if _is_scoped_target(row["target_qualified"])
    ]
    if not candidates:
        return {"calls_resolved": 0}

    # (class_name, method_name) -> list of node qualified_names
    method_to_qual: dict[tuple[str, str], list[str]] = {}
    for row in conn.execute(
        "SELECT name, qualified_name, parent_name FROM nodes "
        "WHERE kind IN ('Function', 'Test') AND parent_name IS NOT NULL"
    ).fetchall():
        method_to_qual.setdefault((row["parent_name"], row["name"]), []).append(
            row["qualified_name"]
        )

    # source file -> set of imported files (for disambiguation)
    import_targets: dict[str, set[str]] = {}
    for row in conn.execute(
        "SELECT DISTINCT file_path, target_qualified FROM edges "
        "WHERE kind = 'IMPORTS_FROM'"
    ).fetchall():
        target = row["target_qualified"]
        target_file = target.split("::", 1)[0] if "::" in target else target
        import_targets.setdefault(row["file_path"], set()).add(target_file)

    resolved = 0
    for edge in candidates:
        try:
            extra = json.loads(edge["extra"] or "{}")
        except (json.JSONDecodeError, TypeError):
            extra = {}
        if extra.get("scoped_resolved"):
            continue

        # _is_scoped_target guarantees exactly one "::", so this is 2 parts.
        class_name, method = edge["target_qualified"].split("::", 1)
        # Strip a PHP namespace prefix to the bare class name (App\Mailer -> Mailer).
        if "\\" in class_name:
            class_name = class_name.rsplit("\\", 1)[-1]

        src_file, enclosing_class = _enclosing(edge["source_qualified"])
        same_file_only = False

        if class_name in _SELF_RECEIVERS:
            if not enclosing_class:
                continue
            class_name = enclosing_class
            same_file_only = True

        cands = method_to_qual.get((class_name, method), [])
        if same_file_only and src_file is not None:
            cands = [c for c in cands if c.split("::", 1)[0] == src_file]

        if not cands:
            continue
        if len(cands) == 1:
            new_target = cands[0]
        else:
            imported_files = import_targets.get(edge["file_path"], set())
            imported = [c for c in cands if c.split("::", 1)[0] in imported_files]
            if len(imported) == 1:
                new_target = imported[0]
            else:
                continue

        extra["scoped_resolved"] = True
        conn.execute(
            "UPDATE edges SET target_qualified = ?, extra = ?, "
            "confidence = ?, confidence_tier = ? WHERE id = ?",
            (new_target, json.dumps(extra), 0.9, "INFERRED", edge["id"]),
        )
        resolved += 1
        logger.debug("Scoped resolved: %s -> %s", edge["target_qualified"], new_target)

    if resolved:
        conn.commit()

    logger.info("Scoped call resolver: resolved %d Class::method CALLS edges", resolved)
    return {"calls_resolved": resolved}
