"""Agent baseline benchmark: grep-and-read-top-k versus a graph query.

The whole-corpus baseline in the standalone token benchmark is an upper
bound no real agent pays: a competent agent greps for identifiers from the
question and reads only the best-matching files. This benchmark measures
that realistic baseline:

1. Derive search terms from the question (identifier-shaped tokens via
   ``search.extract_query_identifiers`` plus plain keywords).
2. Pure-python grep over the corpus (no external ``rg``/``grep`` binary),
   ranking files by total case-insensitive match count.
3. Read the top-k files (k=3) and token-count them with the chars/4 utility
   (``token_benchmark.estimate_tokens``) as ``baseline_tokens``.
4. Compare against the graph-query cost for the same question — hybrid
   search hits plus one hop of neighbor edges, the same accounting used by
   ``code_review_graph/token_benchmark.py``.

Questions come from ``agent_questions:`` in the repo config, falling back to
the ``search_queries`` query strings when absent.

Failure semantics match the other benchmarks: a thrown search is recorded
with ``status="error"`` and excluded from aggregates; rows where either side
of the ratio is zero get ``status="no_graph_results"`` /
``status="no_baseline_match"`` and are likewise excluded.
"""

from __future__ import annotations

import logging
import statistics
from collections.abc import Iterator
from pathlib import Path

from code_review_graph.token_benchmark import estimate_tokens

logger = logging.getLogger(__name__)

DEFAULT_TOP_K = 3

_SOURCE_EXTS = (
    ".py", ".js", ".ts", ".tsx", ".go", ".rs", ".java",
    ".c", ".cpp", ".h", ".rb", ".php", ".swift", ".kt",
)

_SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__",
    ".code-review-graph", ".venv", "venv", "dist", "build",
}

_STOPWORDS = {
    "how", "does", "do", "the", "a", "an", "is", "are", "was", "what",
    "where", "when", "which", "who", "why", "and", "or", "in", "on", "of",
    "to", "for", "with", "via", "into", "from", "this", "that", "it", "its",
}


def derive_search_terms(question: str) -> list[str]:
    """Derive lowercase grep terms: identifiers first, then plain keywords.

    Identifier-shaped tokens (``Client.request``, ``get_users``, ``APIRoute``)
    are extracted via ``search.extract_query_identifiers``; remaining words of
    3+ characters that are not stopwords are appended. Order is deterministic.
    """
    from code_review_graph.search import extract_query_identifiers

    terms: list[str] = []
    seen: set[str] = set()
    for ident in extract_query_identifiers(question):
        if ident not in seen:
            seen.add(ident)
            terms.append(ident)
    for word in question.split():
        w = word.strip(".,;:!?\"'()[]{}`").lower()
        if len(w) >= 3 and w not in _STOPWORDS and w not in seen:
            seen.add(w)
            terms.append(w)
    return terms


def iter_source_files(repo_path: Path) -> Iterator[Path]:
    """Yield source files under *repo_path*, skipping vendored/VCS dirs."""
    for path in sorted(repo_path.rglob("*")):
        if path.suffix not in _SOURCE_EXTS or not path.is_file():
            continue
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        yield path


def grep_rank(
    repo_path: Path, terms: list[str], k: int = DEFAULT_TOP_K,
) -> list[tuple[str, int]]:
    """Rank source files by total case-insensitive term matches; take top-k.

    Pure python — no external grep/rg dependency. Deterministic: ties break
    on the relative path. Files with zero matches are dropped.
    """
    lowered = [t.lower() for t in terms if t]
    if not lowered:
        return []
    scores: list[tuple[str, int]] = []
    for path in iter_source_files(repo_path):
        try:
            text = path.read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            continue
        count = sum(text.count(term) for term in lowered)
        if count > 0:
            scores.append((str(path.relative_to(repo_path)), count))
    scores.sort(key=lambda item: (-item[1], item[0]))
    return scores[:k]


def run(repo_path: Path, store, config: dict) -> list[dict]:
    """Run the agent baseline benchmark for one repo."""
    questions = list(config.get("agent_questions") or [])
    if not questions:
        questions = [sq["query"] for sq in config.get("search_queries", [])]

    k = int(config.get("agent_baseline_top_k", DEFAULT_TOP_K))
    results: list[dict] = []

    for question in questions:
        terms = derive_search_terms(question)
        top = grep_rank(repo_path, terms, k=k)
        baseline_tokens = 0
        for rel, _count in top:
            try:
                baseline_tokens += estimate_tokens(
                    (repo_path / rel).read_text(encoding="utf-8", errors="replace")
                )
            except OSError:
                continue

        row: dict = {
            "repo": config["name"],
            "question": question,
            "terms": " ".join(terms),
            "files_matched": len(top),
            "top_files": ";".join(rel for rel, _ in top),
            "baseline_tokens": baseline_tokens,
            "graph_tokens": "",
            "baseline_to_graph_ratio": "",
            "status": "ok",
            "error": "",
        }

        try:
            from code_review_graph.search import hybrid_search
            hits = hybrid_search(store, question, limit=5)
        except Exception as exc:
            logger.warning("hybrid_search failed on %r: %s", question, exc)
            row["status"] = "error"
            row["error"] = str(exc)[:200]
            results.append(row)
            continue

        # Same accounting as the standalone token benchmark: search hits
        # plus up to 5 outgoing edges of neighbor context per hit.
        graph_tokens = 0
        for hit in hits:
            graph_tokens += estimate_tokens(str(hit))
            qn = hit.get("qualified_name", "")
            for edge in store.get_edges_by_source(qn)[:5]:
                graph_tokens += estimate_tokens(str(edge))

        row["graph_tokens"] = graph_tokens
        if baseline_tokens > 0 and graph_tokens > 0:
            row["baseline_to_graph_ratio"] = round(baseline_tokens / graph_tokens, 1)
        elif graph_tokens == 0:
            row["status"] = "no_graph_results"
        else:
            row["status"] = "no_baseline_match"
        results.append(row)

    return results


def aggregate(results: list[dict]) -> dict:
    """Aggregate over rows where both sides of the comparison exist."""
    ok = [r for r in results if r.get("status") == "ok"]
    ratios = [float(r["baseline_to_graph_ratio"]) for r in ok]
    return {
        "total_rows": len(results),
        "ok_rows": len(ok),
        "error_rows": sum(1 for r in results if r.get("status") == "error"),
        "median_baseline_to_graph_ratio": (
            round(statistics.median(ratios), 1) if ratios else None
        ),
        "mean_baseline_to_graph_ratio": (
            round(statistics.mean(ratios), 1) if ratios else None
        ),
    }
