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
    type_metadata: dict[str, list[dict[str, object]]] = {}
    for row in conn.execute(
        "SELECT name, qualified_name, file_path, is_test, extra FROM nodes "
        "WHERE kind = 'Class' AND language = 'java'"
    ).fetchall():
        bare = row["name"]
        qual = row["qualified_name"]
        if bare not in name_to_qual or len(qual) < len(name_to_qual[bare]):
            name_to_qual[bare] = qual
        try:
            node_extra = json.loads(row["extra"] or "{}")
        except (json.JSONDecodeError, TypeError):
            node_extra = {}
        type_metadata.setdefault(bare, []).append({
            "qualified_name": qual,
            "file_path": row["file_path"],
            "is_test": bool(row["is_test"]),
            "java_kind": node_extra.get("java_kind"),
        })

    # Also index Function nodes so we can build "file::Class.method" targets.
    # key: (class_name, method_name) → full qualified_name of the method node
    method_to_qual: dict[tuple[str, str], str] = {}
    methods_by_class: dict[tuple[str, str], str] = {}
    for row in conn.execute(
        "SELECT name, qualified_name, parent_name FROM nodes "
        "WHERE kind IN ('Function', 'Test') AND language = 'java' AND parent_name IS NOT NULL"
    ).fetchall():
        method_to_qual[(row["parent_name"], row["name"])] = row["qualified_name"]
        if "::" in row["qualified_name"]:
            class_qual = row["qualified_name"].rsplit(".", 1)[0]
            methods_by_class[(class_qual, row["name"])] = row["qualified_name"]
            class_tail = class_qual.rsplit(".", 1)[-1]
            file_prefix = class_qual.split("::", 1)[0]
            methods_by_class.setdefault(
                (f"{file_prefix}::{class_tail}", row["name"]),
                row["qualified_name"],
            )

    node_is_test = {
        row["qualified_name"]: bool(row["is_test"])
        for row in conn.execute("SELECT qualified_name, is_test FROM nodes").fetchall()
    }

    # -----------------------------------------------------------------------
    # Build implementors: bare interface name → list of implementing class quals
    # from INHERITS edges (Java uses INHERITS for both extends and implements)
    # -----------------------------------------------------------------------
    implementors: dict[str, list[str]] = {}
    for row in conn.execute(
        "SELECT e.source_qualified, e.target_qualified FROM edges e "
        "JOIN nodes n ON n.qualified_name = e.source_qualified "
        "WHERE e.kind = 'INHERITS' AND n.language IN ('java', 'kotlin', 'scala')"
    ).fetchall():
        iface = row["target_qualified"]
        impl = row["source_qualified"]
        if impl.rsplit("::", 1)[-1] == iface:
            continue
        implementors.setdefault(iface, []).append(impl)

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

        declared_types = type_metadata.get(injected_type, [])
        java_type_found = bool(declared_types)
        production_types = [item for item in declared_types if not item["is_test"]]
        if len(production_types) == 1:
            declared_type = production_types[0]
        elif not production_types and len(declared_types) == 1:
            declared_type = declared_types[0]
        else:
            declared_type = None
        declared_kind = declared_type.get("java_kind") if declared_type else None
        is_polymorphic = java_type_found and declared_kind in {"interface", "abstract"}
        source_is_test = node_is_test.get(source_qual, False)

        # Java types with known metadata are resolved conservatively. Types that
        # are absent from Java nodes preserve the legacy cross-language lookup.
        impls = (
            implementors.get(injected_type, [])
            if not java_type_found or is_polymorphic
            else []
        )
        if not source_is_test:
            impls = [
                impl
                for impl in impls
                if not node_is_test.get(impl, False)
            ]
        if len(impls) == 1:
            concrete_class = impls[0].split("::")[-1]
            fallback = f"{impls[0]}.{method_name}"
            new_target = method_to_qual.get((concrete_class, method_name)) or fallback
        else:
            fallback = f"{injected_type}.{method_name}"
            declared_qual_value = (
                declared_type.get("qualified_name") if declared_type else None
            )
            declared_qual = (
                declared_qual_value if isinstance(declared_qual_value, str) else None
            )
            if declared_qual:
                new_target = methods_by_class.get((declared_qual, method_name))
                if new_target is None and "." in declared_qual.split("::", 1)[-1]:
                    file_prefix, _, symbol = declared_qual.partition("::")
                    class_tail = symbol.rsplit(".", 1)[-1]
                    new_target = methods_by_class.get(
                        (f"{file_prefix}::{class_tail}", method_name)
                    )
                new_target = new_target or fallback
            else:
                new_target = fallback

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
