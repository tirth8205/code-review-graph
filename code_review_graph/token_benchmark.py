"""Token reduction benchmark -- measures graph query efficiency vs naive file reading."""

from __future__ import annotations

import logging
import sqlite3
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

    ``average_reduction_ratio`` is ``naive_corpus_tokens / avg_graph_tokens``
    -- a ratio of averages, so it reproduces exactly from the two numbers
    reported alongside it. It is deliberately *not* the mean of the
    per-question ``reduction_ratio`` values, which is larger and does not
    match the table it is printed next to.

    The default sample questions are natural language and require semantic
    search to match. If no embeddings are present in the graph, ``hybrid_search``
    falls back to FTS5/LIKE matching on node names, which produces no hits for
    questions like "how does authentication work" — every per-question ratio
    becomes 0 and the benchmark silently appears to fail. We log a clear
    warning when that is the case so callers know to run ``embed_graph`` first
    (or to pass keyword-matching questions).
    """
    if questions is None:
        questions = _SAMPLE_QUESTIONS

    using_default_questions = questions is _SAMPLE_QUESTIONS
    try:
        cur = store._conn.execute("SELECT count(*) FROM embeddings")
        embedding_count = cur.fetchone()[0]
    except sqlite3.OperationalError:
        embedding_count = 0
    if embedding_count == 0 and using_default_questions:
        logger.warning(
            "No embeddings found in this graph. The default sample questions "
            "are natural language and will not match via FTS5/LIKE alone — "
            "every reduction ratio is likely to be 0. Run "
            "`code-review-graph embed` first, or pass keyword-matching `questions=`."
        )

    naive_total = compute_naive_tokens(repo_root)

    results: list[dict[str, Any]] = []
    # Kept alongside ``results`` so the aggregate arithmetic below works on
    # plain ints rather than re-reading heterogeneous dict values.
    answered_token_counts: list[int] = []
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
            answered_token_counts.append(graph_tokens)
        else:
            ratio = 0
        results.append({
            "question": q,
            "naive_tokens": naive_total,
            "graph_tokens": graph_tokens,
            "reduction_ratio": round(ratio, 1),
        })

    # The headline ratio is a ratio of averages, not an average of ratios.
    #
    # The published figure has to survive the arithmetic a reader does from
    # the table itself: naive_corpus_tokens / avg_graph_tokens. Averaging the
    # per-question ratios does not give that number. Because x -> naive/x is
    # convex, mean(naive/x_i) >= naive/mean(x_i) (Jensen), so the old
    # average was always the larger of the two and every published row was
    # inflated -- code-review-graph quoted 68.1x where 208,821 / 3,190 gives
    # 65.5x, and httpx quoted 60.6x where its own columns give 53.5x.
    #
    # Questions that returned no hits (graph_tokens == 0) are not
    # measurements; they are excluded from the average and counted instead,
    # so a search failure cannot shrink the denominator into a bigger "win".
    #
    # The division uses the *rounded* ``avg_graph_tokens`` -- the integer this
    # function publishes -- not the underlying float. Dividing by the float
    # and publishing the rounded denominator lets the two disagree in the last
    # digit (flask: 143,594 / 2,711.6 rounds to 53.0, but a reader dividing
    # the printed 2,712 gets 52.9). The published pair must be self-consistent.
    answered = len(answered_token_counts)
    if answered:
        avg_graph_tokens = int(round(sum(answered_token_counts) / answered))
        avg_ratio = naive_total / avg_graph_tokens if avg_graph_tokens else 0.0
    else:
        avg_graph_tokens = 0
        avg_ratio = 0.0

    return {
        "naive_corpus_tokens": naive_total,
        "per_question": results,
        "answered_questions": answered,
        "unanswered_questions": len(results) - answered,
        "avg_graph_tokens": avg_graph_tokens,
        "average_reduction_ratio": round(avg_ratio, 1),
        "summary": (
            f"Graph queries use ~{avg_ratio:.0f}x fewer tokens "
            f"than reading all source files"
        ),
    }
