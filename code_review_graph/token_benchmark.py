"""Token reduction benchmark -- measures graph query efficiency vs naive file reading."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .graph import GraphStore
from .search import hybrid_search

logger = logging.getLogger(__name__)

# Sample questions for benchmarking
_SAMPLE_QUESTIONS = [
    "how does authentication work",
    "what is the main entry point",
    "how are database connections managed",
    "what error handling patterns are used",
    "how do tests verify core functionality",
]


def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token."""
    return max(1, len(text) // 4)


def compute_naive_tokens(repo_root: Path) -> int:
    """Count tokens in all parseable source files."""
    total = 0
    exts = (
        ".py", ".js", ".ts", ".go", ".rs", ".java",
        ".c", ".cpp", ".rb", ".php", ".swift", ".kt",
    )
    for ext in exts:
        for f in repo_root.rglob(f"*{ext}"):
            try:
                total += estimate_tokens(
                    f.read_text(errors="replace")
                )
            except OSError:
                continue
    return total


def run_token_benchmark(
    store: GraphStore,
    repo_root: Path,
    questions: list[str] | None = None,
) -> dict[str, Any]:
    """Run token reduction benchmark.

    Compares naive full-corpus token cost vs graph query token
    cost for a set of sample questions.
    """
    if questions is None:
        questions = _SAMPLE_QUESTIONS

    naive_total = compute_naive_tokens(repo_root)

    results = []
    for q in questions:
        search_results = hybrid_search(store, q, limit=5)
        # Simulate graph context: search results + neighbors
        graph_tokens = 0
        for r in search_results:
            graph_tokens += estimate_tokens(str(r))
            # Add approximate neighbor context
            qn = r.get("qualified_name", "")
            edges = store.get_edges_by_source(qn)[:5]
            for e in edges:
                graph_tokens += estimate_tokens(str(e))

        if graph_tokens > 0:
            ratio = naive_total / graph_tokens
        else:
            ratio = 0
        results.append({
            "question": q,
            "naive_tokens": naive_total,
            "graph_tokens": graph_tokens,
            "reduction_ratio": round(ratio, 1),
        })

    if results:
        total = sum(
            r["reduction_ratio"] for r in results  # type: ignore[misc]
        )
        avg_ratio = float(total) / len(results)  # type: ignore[arg-type]
    else:
        avg_ratio = 0.0

    return {
        "naive_corpus_tokens": naive_total,
        "per_question": results,
        "average_reduction_ratio": round(avg_ratio, 1),
        "summary": (
            f"Graph queries use ~{avg_ratio:.0f}x fewer tokens "
            f"than reading all source files"
        ),
    }
