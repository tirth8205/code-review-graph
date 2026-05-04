"""Post-build global resolution of bare-name CALLS edges.

The parser only resolves call targets against **same-file** definitions
(:func:`code_review_graph.parser.CodeParser._resolve_call_targets`). On
multi-file C codebases — e.g. a Ghidra-decompiled binary split across
hundreds of files — that means the vast majority of cross-file calls
land in the database as **bare** edges:

.. code-block:: text

    source_qualified = '<caller-file>.c::FUN_004c5560'
    target_qualified = 'FUN_004c51e0'        # NOT '<callee-file>.c::FUN_004c51e0'

Bare targets break the primary indexed lookup ``WHERE target_qualified =
'<file>::<name>'``, forcing ``callers_of`` to a name-fallback that only
half-works and silently dropping result rows on ``callees_of`` whenever
the bare target can't be resolved back to a node.

For decompiled binaries, ``FUN_XXXXXXXX`` symbols are globally unique by
address, so a single global rewrite pass can lift ~80% of these edges to
qualified form. For named symbols that legitimately collide across files
(``Assemble``, template instantiations, ``static`` helpers) we keep the
edge bare — Fix A's name-fallback in ``tools/query.py`` handles those.

The pass is **idempotent**: re-running it does nothing if everything
that can be resolved already has been. It runs from
``_run_postprocess`` in ``tools/build.py``.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

logger = logging.getLogger(__name__)


def resolve_bare_call_targets(store: Any) -> dict[str, int]:
    """Rewrite unambiguous bare CALLS targets to qualified names.

    Args:
        store: A :class:`code_review_graph.graph.GraphStore` (we read
            ``store._conn`` and call ``store.commit()``).

    Returns:
        A stats dict with:

        - ``bare_targets_seen``: distinct bare ``target_qualified`` values
          observed in CALLS edges before the pass.
        - ``unique_bare_names``: count of bare names that map to exactly
          one Function/Test node and were rewritten.
        - ``ambiguous_bare_names``: bare names with multiple defining
          nodes (left as-is).
        - ``unresolved_bare_names``: bare names with no matching node
          (external/library symbols — left as-is, surfaced via Fix A).
        - ``edges_rewritten``: total CALLS edge rows updated.
    """
    conn: sqlite3.Connection = store._conn

    # Build bare → qualified index for callable nodes.
    by_name: dict[str, str] = {}
    ambiguous: set[str] = set()
    for row in conn.execute(
        "SELECT name, qualified_name FROM nodes "
        "WHERE kind IN ('Function', 'Test')",
    ):
        name = row[0]
        qn = row[1]
        if name in ambiguous:
            continue
        existing = by_name.get(name)
        if existing is None:
            by_name[name] = qn
        elif existing != qn:
            ambiguous.add(name)
            del by_name[name]

    # Distinct bare CALLS targets currently in the DB.
    bare_targets = [
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT target_qualified FROM edges "
            "WHERE kind = 'CALLS' AND target_qualified NOT LIKE '%::%'",
        ).fetchall()
    ]

    unique_resolvable = [b for b in bare_targets if b in by_name]
    unresolved = [
        b for b in bare_targets if b not in by_name and b not in ambiguous
    ]
    ambiguous_present = [b for b in bare_targets if b in ambiguous]

    edges_rewritten = 0
    cursor = conn.cursor()
    for bare in unique_resolvable:
        cursor.execute(
            "UPDATE edges SET target_qualified = ? "
            "WHERE kind = 'CALLS' AND target_qualified = ?",
            (by_name[bare], bare),
        )
        edges_rewritten += cursor.rowcount

    store.commit()

    stats = {
        "bare_targets_seen": len(bare_targets),
        "unique_bare_names": len(unique_resolvable),
        "ambiguous_bare_names": len(ambiguous_present),
        "unresolved_bare_names": len(unresolved),
        "edges_rewritten": edges_rewritten,
    }
    logger.info("bare_call_resolution: %s", stats)
    return stats
