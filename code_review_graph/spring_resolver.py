"""Post-build Spring DI call resolver.

After tree-sitter parsing, Java CALLS edges whose target is a bare method
name (e.g. ``calculate``) carry ``extra.receiver`` naming the local variable
that was called on (e.g. ``invoiceCalculationService``).  This module
resolves those receivers through the INJECTS map to their declared type, then
optionally to the unique concrete implementation via INHERITS edges.

Resolution chain:
    receiver variable name
        → injected interface/class (from INJECTS.extra.field_name)
        → concrete implementation (from INHERITS, when unique)

Only Java files are processed.  Edges that are already qualified (contain
``::``) or have no ``receiver`` extra key are skipped.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .graph import GraphStore

logger = logging.getLogger(__name__)


def resolve_spring_di_calls(store: GraphStore) -> dict:
    """Resolve Java CALLS edges whose receiver is a Spring-injected field.

    Safe to call multiple times — already-resolved edges (targets containing
    ``::``) are skipped.

    Returns a dict with resolution counts for telemetry.
    """
    conn = store._conn

    # Only process Java files
    java_files: set[str] = {
        row["file_path"]
        for row in conn.execute(
            "SELECT DISTINCT file_path FROM nodes WHERE language = 'java'"
        ).fetchall()
    }
    if not java_files:
        return {"files_indexed": 0, "calls_resolved": 0}

    # -----------------------------------------------------------------------
    # Build field_map: (source_qualified_class, field_name) → injected_type
    # from INJECTS edges that carry extra.field_name
    # -----------------------------------------------------------------------
    field_map: dict[tuple[str, str], str] = {}
    # (class_qual, field_name) → @Qualifier bean name at the injection point
    qualifier_map: dict[tuple[str, str], str] = {}
    injects_rows = conn.execute(
        "SELECT source_qualified, target_qualified, extra FROM edges WHERE kind = 'INJECTS'"
    ).fetchall()
    for row in injects_rows:
        try:
            extra = json.loads(row["extra"] or "{}")
        except (json.JSONDecodeError, TypeError):
            extra = {}
        fname = extra.get("field_name")
        if not fname:
            continue
        # source_qualified is the full class qualified name
        class_qual = row["source_qualified"]
        field_map[(class_qual, fname)] = row["target_qualified"]
        qualifier = extra.get("qualifier")
        if qualifier:
            qualifier_map[(class_qual, fname)] = qualifier

    if not field_map:
        logger.info("Spring resolver: no INJECTS edges with field_name found, skipping")
        return {"files_indexed": len(java_files), "calls_resolved": 0}

    # -----------------------------------------------------------------------
    # Build class_name → qualified_name lookup from nodes.
    # Keyed by bare class name; value is the full "file_path::ClassName" form
    # that callers_of uses for its target_qualified exact-match lookup.
    # When a name appears in multiple files (e.g. same interface in several
    # services), we keep the entry with the shortest path as a tiebreaker —
    # this is overridden by the concrete-implementation lookup below.
    # -----------------------------------------------------------------------
    name_to_qual: dict[str, str] = {}
    for row in conn.execute(
        "SELECT name, qualified_name FROM nodes WHERE kind = 'Class' AND language = 'java'"
    ).fetchall():
        bare = row["name"]
        qual = row["qualified_name"]
        if bare not in name_to_qual or len(qual) < len(name_to_qual[bare]):
            name_to_qual[bare] = qual

    # Also index Function nodes so we can build "file::Class.method" targets.
    # key: (class_name, method_name) → full qualified_name of the method node
    method_to_qual: dict[tuple[str, str], str] = {}
    for row in conn.execute(
        "SELECT name, qualified_name, parent_name FROM nodes "
        "WHERE kind IN ('Function', 'Test') AND language = 'java' AND parent_name IS NOT NULL"
    ).fetchall():
        method_to_qual[(row["parent_name"], row["name"])] = row["qualified_name"]

    # -----------------------------------------------------------------------
    # Build implementors: bare interface name → list of implementing class quals
    # from INHERITS edges (Java uses INHERITS for both extends and implements)
    # -----------------------------------------------------------------------
    # bare interface name → list of (impl_qual, bean_name, is_primary)
    implementors: dict[str, list[tuple[str, str | None, bool]]] = {}
    for row in conn.execute(
        "SELECT source_qualified, target_qualified, extra FROM edges WHERE kind = 'INHERITS'"
    ).fetchall():
        iface = row["target_qualified"]
        impl = row["source_qualified"]
        try:
            iextra = json.loads(row["extra"] or "{}")
        except (json.JSONDecodeError, TypeError):
            iextra = {}
        if any(impl.startswith(f) for f in java_files) or "::" in impl:
            implementors.setdefault(iface, []).append(
                (impl, iextra.get("bean_name"), bool(iextra.get("primary")))
            )

    # -----------------------------------------------------------------------
    # Resolve CALLS edges
    # -----------------------------------------------------------------------
    calls_rows = conn.execute(
        "SELECT id, source_qualified, target_qualified, extra, file_path "
        "FROM edges WHERE kind = 'CALLS'"
    ).fetchall()

    resolved = 0

    for row in calls_rows:
        if row["file_path"] not in java_files:
            continue

        try:
            extra = json.loads(row["extra"] or "{}")
        except (json.JSONDecodeError, TypeError):
            extra = {}

        receiver = extra.get("receiver")
        if not receiver:
            continue

        # Skip edges already spring-resolved in a previous pass
        if extra.get("spring_resolved"):
            continue

        # Strip any prior (possibly wrong) qualification — we have a receiver so
        # we can do a better resolution.  E.g. "file::ClassName.method" → "method"
        raw_target = row["target_qualified"]
        if "::" in raw_target:
            after = raw_target.split("::", 1)[1]
            method_name = after.split(".")[-1] if "." in after else after
        else:
            method_name = raw_target
        source_qual = row["source_qualified"]

        # Derive the enclosing class qualified name from source
        # source_qual format: "file_path::ClassName.method_name"
        enclosing_class_qual: str | None = None
        if "::" in source_qual:
            after_sep = source_qual.split("::", 1)[1]
            if "." in after_sep:
                class_part = after_sep.split(".")[0]
                prefix = source_qual.split("::")[0]
                enclosing_class_qual = f"{prefix}::{class_part}"
            else:
                enclosing_class_qual = source_qual

        if not enclosing_class_qual:
            continue

        # Look up receiver in field_map for this class
        injected_type = field_map.get((enclosing_class_qual, receiver))
        if not injected_type:
            continue

        # Resolve to a concrete implementation.
        #   1 impl            → that impl
        #   N impls + @Qualifier("bean") at injection point → matching bean
        #   N impls + exactly one @Primary impl             → the primary
        #   otherwise         → leave at interface level (ambiguous)
        impls = implementors.get(injected_type, [])
        chosen: str | None = None
        if len(impls) == 1:
            chosen = impls[0][0]
        elif len(impls) > 1:
            wanted = qualifier_map.get((enclosing_class_qual, receiver))
            if wanted:
                for impl_qual, bean_name, _primary in impls:
                    if bean_name == wanted:
                        chosen = impl_qual
                        break
            if chosen is None:
                primaries = [i for i in impls if i[2]]
                if len(primaries) == 1:
                    chosen = primaries[0][0]

        if chosen is not None:
            concrete_class = chosen.split("::")[-1]
            fallback = f"{chosen}.{method_name}"
            new_target = method_to_qual.get((concrete_class, method_name)) or fallback
        else:
            type_bare = injected_type.rsplit(".", 1)[-1]
            fallback = f"{injected_type}.{method_name}"
            new_target = method_to_qual.get((type_bare, method_name)) or fallback

        extra["spring_resolved"] = True
        extra["injected_type"] = injected_type
        new_extra = json.dumps(extra)

        conn.execute(
            "UPDATE edges SET target_qualified = ?, extra = ? WHERE id = ?",
            (new_target, new_extra, row["id"]),
        )
        resolved += 1
        logger.debug(
            "Spring resolved: %s → %s (was %s, receiver=%s)",
            source_qual, new_target, method_name, receiver,
        )

    if resolved:
        conn.commit()

    logger.info("Spring DI resolver: resolved %d CALLS edges in %d Java files",
                resolved, len(java_files))
    return {"files_indexed": len(java_files), "calls_resolved": resolved}
