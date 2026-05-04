"""One-shot driver: apply bare-call resolution to a graph DB.

Usage:
    PYTHONPATH=<fork-root> python scripts/run_fix_b.py <db_path>

Idempotent — safe to re-run after every ``code-review-graph build``.
"""

from __future__ import annotations

import json
import sys

from code_review_graph.bare_call_resolution import resolve_bare_call_targets
from code_review_graph.graph import GraphStore


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <db_path>", file=sys.stderr)
        return 2
    db_path = sys.argv[1]
    store = GraphStore(db_path)

    bare_q = (
        "SELECT COUNT(*) FROM edges "
        "WHERE kind='CALLS' AND target_qualified NOT LIKE '%::%'"
    )
    qual_q = (
        "SELECT COUNT(*) FROM edges "
        "WHERE kind='CALLS' AND target_qualified LIKE '%::%'"
    )

    bare_before = store._conn.execute(bare_q).fetchone()[0]
    qual_before = store._conn.execute(qual_q).fetchone()[0]

    stats = resolve_bare_call_targets(store)

    bare_after = store._conn.execute(bare_q).fetchone()[0]
    qual_after = store._conn.execute(qual_q).fetchone()[0]

    print(json.dumps(stats, indent=2))
    print()
    print(f"bare CALLS edges:      {bare_before:>6} -> {bare_after:>6}  "
          f"(delta {bare_before - bare_after:+d})")
    print(f"qualified CALLS edges: {qual_before:>6} -> {qual_after:>6}  "
          f"(delta {qual_after - qual_before:+d})")

    sample = store._conn.execute(
        "SELECT COUNT(*) FROM edges "
        "WHERE kind='CALLS' AND target_qualified LIKE '%::FUN_004c5560'",
    ).fetchone()[0]
    print(f"\nedges resolving to ::FUN_004c5560 after pass: {sample}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
