"""Smoke-test Fix A by exercising the patched _resolve_edge_endpoint against
a real graph DB. Runs out-of-band of the MCP server."""

from __future__ import annotations

import json
import sys

from code_review_graph.graph import GraphStore
from code_review_graph.tools.query import _resolve_edge_endpoint


def main() -> int:
    store = GraphStore(sys.argv[1])

    qualified = (
        r"C:\l2rust\engine\official\decompiled\L2Server\shared\RWLock.c"
        r"::FUN_004c51e0"
    )
    bare_with_def = "SendSystemMessage"
    bare_external = "_wcscpy"
    bare_ambiguous = "Assemble"

    cases = {
        "qualified-hit":     qualified,
        "bare-unique":       bare_with_def,
        "bare-external":     bare_external,
        "bare-ambiguous":    bare_ambiguous,
    }

    for label, target in cases.items():
        print(f"=== {label}: {target} ===")
        result = _resolve_edge_endpoint(store, target)
        print(json.dumps(result, indent=2, default=str))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
