"""Multi-hop retrieval benchmark.

Tests a two-step tool chain that mimics how an LLM agent actually uses the
graph for complex tasks:

  1. ``hybrid_search(nl_query)`` to find a starting anchor from a natural-
     language question.
  2. ``query_graph(pattern, target=anchor)`` to traverse one hop along the
     requested edge kind (callers_of / callees_of / tests_for / ...).

For each task the benchmark records:

- ``anchor_found`` — did semantic search return a node whose qualified_name
  ends with the expected suffix in the top-K?
- ``anchor_rank`` — index in the search result list (lower is better).
- ``neighbor_count`` — number of neighbors returned by the traversal.
- ``neighbor_recall`` — fraction of ``expected_neighbor_names`` that appear
  among the neighbor names.
- ``score`` — ``int(anchor_found) * neighbor_recall``. Range 0–1.

Tasks are defined per-config under ``multi_hop_tasks:`` in
``code_review_graph/eval/configs/*.yaml``. See
``docs/REPRODUCING.md`` for the schema and the curated canonical task set.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _name_set(rows: list[dict[str, Any]]) -> set[str]:
    out: set[str] = set()
    for r in rows:
        name = (r.get("name") or "").lower()
        if name:
            out.add(name)
    return out


def run(repo_path: Path, store, config: dict) -> list[dict]:
    """Run the multi-hop retrieval benchmark for one repo."""
    # Imports are local so an import-time failure in one optional benchmark
    # does not poison the whole runner.
    from code_review_graph.search import hybrid_search
    from code_review_graph.tools.query import query_graph

    repo_root = str(repo_path)
    results: list[dict] = []

    for task in config.get("multi_hop_tasks", []):
        task_id = task["id"]
        nl_query = task["nl_query"]
        suffix = task["anchor_qualified_suffix"].lower()
        traversal = task.get("traversal_pattern", "callers_of")
        expected = [e.lower() for e in task.get("expected_neighbor_names", [])]
        k = int(task.get("k", 10))

        # Step 1 — semantic search
        try:
            hits = hybrid_search(store, nl_query, limit=k)
        except Exception as exc:  # noqa: BLE001 — benchmark must not abort the runner
            logger.warning("hybrid_search failed on %s: %s", task_id, exc)
            hits = []

        anchor = None
        anchor_rank = -1
        for i, h in enumerate(hits):
            qn = (h.get("qualified_name") or "").lower()
            if qn.endswith(suffix):
                anchor = h
                anchor_rank = i
                break

        if anchor is None:
            results.append({
                "repo": config["name"],
                "task_id": task_id,
                "nl_query": nl_query,
                "anchor_found": False,
                "anchor_rank": -1,
                "neighbor_count": 0,
                "expected_count": len(expected),
                "matched_count": 0,
                "neighbor_recall": 0.0,
                "score": 0.0,
            })
            continue

        # Step 2 — single-hop graph traversal from the anchor
        try:
            trav = query_graph(
                pattern=traversal,
                target=anchor["qualified_name"],
                repo_root=repo_root,
                detail_level="standard",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "query_graph(%s) failed on %s: %s", traversal, task_id, exc,
            )
            trav = {}

        rows = trav.get("data") or trav.get("results") or []
        names = _name_set(rows)
        matched = sum(1 for e in expected if e in names)
        recall = matched / len(expected) if expected else 0.0

        results.append({
            "repo": config["name"],
            "task_id": task_id,
            "nl_query": nl_query,
            "anchor_found": True,
            "anchor_rank": anchor_rank,
            "neighbor_count": len(rows),
            "expected_count": len(expected),
            "matched_count": matched,
            "neighbor_recall": round(recall, 3),
            "score": round(recall, 3),
        })

    return results
