"""Spot-checks that Fix B's bare-call rewrite produced sane call edges."""

from __future__ import annotations

import sys

from code_review_graph.graph import GraphStore


def main() -> int:
    store = GraphStore(sys.argv[1])
    conn = store._conn

    print("=== callers of any node ending ::FUN_004c51e0 ===")
    for src, line in conn.execute(
        "SELECT source_qualified, line FROM edges "
        "WHERE kind='CALLS' AND target_qualified LIKE '%::FUN_004c51e0'",
    ):
        short = src.rsplit("::", 1)[-1] if "::" in src else src
        print(f"  {short} (line {line})")

    name = "SendSystemMessage"
    qual = conn.execute(
        "SELECT COUNT(*) FROM edges WHERE kind='CALLS' "
        "AND target_qualified LIKE '%::' || ?",
        (name,),
    ).fetchone()[0]
    bare = conn.execute(
        "SELECT COUNT(*) FROM edges WHERE kind='CALLS' AND target_qualified = ?",
        (name,),
    ).fetchone()[0]
    defs = conn.execute(
        "SELECT COUNT(DISTINCT qualified_name) FROM nodes "
        "WHERE kind='Function' AND name = ?",
        (name,),
    ).fetchone()[0]
    print(f"\n=== {name} ===")
    print(f"  qualified-target callers: {qual}")
    print(f"  bare-target callers:      {bare}")
    print(f"  Function nodes named {name!r}: {defs}")

    print("\n=== top 10 most-called functions (qualified targets) ===")
    rows = conn.execute(
        "SELECT target_qualified, COUNT(*) c FROM edges "
        "WHERE kind='CALLS' AND target_qualified LIKE '%::%' "
        "GROUP BY target_qualified ORDER BY c DESC LIMIT 10",
    ).fetchall()
    for tgt, c in rows:
        short = tgt.rsplit("::", 1)[-1]
        print(f"  {c:>5}  {short}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
