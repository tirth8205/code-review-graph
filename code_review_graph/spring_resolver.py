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
    field_map: dict[tuple[str, str], tuple[str, str | None]] = {}
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
        java_type = extra.get("java_type")
        field_map[(class_qual, fname)] = (
            row["target_qualified"],
            java_type if isinstance(java_type, str) else None,
        )

    if not field_map:
        logger.info("Spring resolver: no INJECTS edges with field_name found, skipping")
        return {"files_indexed": len(java_files), "calls_resolved": 0}

    # -----------------------------------------------------------------------
    # Build package-qualified Java type indexes from class nodes.  Bare names
    # remain available only for old graphs that predate the Java metadata.
    # -----------------------------------------------------------------------
    java_kinds: dict[str, set[str]] = {}
    class_quals_by_type: dict[str, set[str]] = {}
    for row in conn.execute(
        "SELECT name, qualified_name, extra FROM nodes "
        "WHERE kind = 'Class' AND language = 'java'"
    ).fetchall():
        bare = row["name"]
        qual = row["qualified_name"]
        try:
            node_extra = json.loads(row["extra"] or "{}")
        except (json.JSONDecodeError, TypeError):
            node_extra = {}
        package = node_extra.get("java_package")
        java_type = f"{package}.{bare}" if isinstance(package, str) and package else bare
        class_quals_by_type.setdefault(java_type, set()).add(qual)
        java_kind = node_extra.get("java_kind")
        if isinstance(java_kind, str):
            java_kinds.setdefault(java_type, set()).add(java_kind)
            # Older graphs do not have java_package.  Keep a bare-name index
            # for their compatibility fallback, but never use it to override
            # an unambiguous package-qualified identity.
            if java_type != bare:
                java_kinds.setdefault(bare, set()).add(java_kind)

    # Also index Function nodes so selected implementations resolve to their
    # exact file/class method, without homonym collisions.
    method_to_qual: dict[tuple[str, str], str] = {}
    for row in conn.execute(
        "SELECT name, qualified_name, parent_name, file_path FROM nodes "
        "WHERE kind IN ('Function', 'Test') AND language = 'java' AND parent_name IS NOT NULL"
    ).fetchall():
        class_qual = f"{row['file_path']}::{row['parent_name']}"
        method_to_qual[(class_qual, row["name"])] = row["qualified_name"]
        bare_key = (row["parent_name"].split(".")[-1], row["name"])
        previous = method_to_qual.get(bare_key)
        if previous is None or len(row["qualified_name"]) < len(previous):
            method_to_qual[bare_key] = row["qualified_name"]

    node_is_test = {
        row["qualified_name"]: bool(row["is_test"])
        for row in conn.execute("SELECT qualified_name, is_test FROM nodes").fetchall()
    }

    # -----------------------------------------------------------------------
    # Build implementors: package-qualified Java type → implementing class quals
    # from INHERITS edges (Java uses INHERITS for both extends and implements).
    # JVM edges without Java metadata stay keyed by their historical bare name.
    # -----------------------------------------------------------------------
    implementors: dict[str, set[str]] = {}
    for row in conn.execute(
        "SELECT e.source_qualified, e.target_qualified, e.extra FROM edges e "
        "JOIN nodes n ON n.qualified_name = e.source_qualified "
        "WHERE e.kind = 'INHERITS' AND n.language IN ('java', 'kotlin', 'scala')"
    ).fetchall():
        try:
            edge_extra = json.loads(row["extra"] or "{}")
        except (json.JSONDecodeError, TypeError):
            edge_extra = {}
        java_type = edge_extra.get("java_type")
        iface = java_type if isinstance(java_type, str) else row["target_qualified"]
        impl = row["source_qualified"]
        implementors.setdefault(iface, set()).add(impl)

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
        injection = field_map.get((enclosing_class_qual, receiver))
        if not injection:
            continue
        injected_type, java_type = injection
        type_key = java_type or injected_type

        source_is_test = node_is_test.get(source_qual, False)
        kind_values = java_kinds.get(type_key)
        if not kind_values and type_key != injected_type:
            # A graph written before qualified Java metadata existed can still
            # resolve through its historical bare-name indexes.
            kind_values = java_kinds.get(injected_type)
        java_kind = next(iter(kind_values)) if kind_values and len(kind_values) == 1 else None
        impl_values = implementors.get(type_key)
        if not impl_values and type_key != injected_type:
            # Kotlin/Scala inheritance edges historically use bare names.  Use
            # them only when the qualified Java identity has no candidates.
            impl_values = implementors.get(injected_type)
        impls = (
            sorted(impl_values or [])
            if java_kind is None or java_kind in {"interface", "abstract"}
            else []
        )
        if not source_is_test:
            impls = [
                impl
                for impl in impls
                if not node_is_test.get(impl, False)
            ]
        if len(impls) == 1:
            fallback = f"{impls[0]}.{method_name}"
            new_target = method_to_qual.get((impls[0], method_name)) or fallback
        else:
            type_bare = injected_type.rsplit(".", 1)[-1]
            declared_classes = class_quals_by_type.get(type_key, set())
            if len(declared_classes) == 1:
                declared_class = next(iter(declared_classes))
                new_target = method_to_qual.get(
                    (declared_class, method_name),
                ) or f"{declared_class}.{method_name}"
            else:
                fallback = f"{injected_type}.{method_name}"
                new_target = method_to_qual.get((type_bare, method_name)) or fallback

        extra["spring_resolved"] = True
        extra["injected_type"] = injected_type
        if java_type:
            extra["java_type"] = java_type
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
