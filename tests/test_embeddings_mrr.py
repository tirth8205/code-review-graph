"""MRR regression test for body-enriched embeddings (AC-6 Iter 3).

Runs the Local sentence-transformers provider against a fixed mini_repo +
10 golden queries and asserts that enabling body enrichment does NOT
regress Mean Reciprocal Rank at top-3 beyond the -0.02 tolerance AND
produces a strictly positive delta (non-inferiority + soft signal).

Skipped when ``sentence-transformers`` is not installed, so the project's
default ``.[dev]`` test matrix keeps passing; CI jobs that care about the
gate should install ``.[embeddings]`` to activate this module.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from code_review_graph.graph import GraphNode

pytest.importorskip("sentence_transformers")

from code_review_graph.embeddings import (  # noqa: E402  — after importorskip
    LocalEmbeddingProvider,
    _cosine_similarity,
    _FileLineCache,
    _node_to_text,
    _resolve_body_max_chars,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "embeddings" / "eval"
MINI_REPO = FIXTURE_DIR / "mini_repo"


def _mk(qualified_name, file_rel, line_start, line_end, *,
        kind="Function", name=None, parent_name=None, params=None,
        return_type=None):
    """Build a GraphNode for mini_repo fixture nodes."""
    return GraphNode(
        id=0, kind=kind,
        name=name or qualified_name.rsplit("::", 1)[1].split(".")[-1],
        qualified_name=qualified_name,
        file_path=str(MINI_REPO / file_rel),
        line_start=line_start, line_end=line_end,
        language="python",
        parent_name=parent_name, params=params, return_type=return_type,
        is_test=False, file_hash=None, extra={},
    )


# Accurate line ranges from mini_repo files (see tests/fixtures/.../mini_repo/).
NODES: list[GraphNode] = [
    # parser.py
    _mk("parser.py::parse_tree",        "parser.py",         4,  9,  params="(source)"),
    _mk("parser.py::walk_nodes",        "parser.py",        11, 16,  params="(tree, visitor)"),
    _mk("parser.py::extract_functions", "parser.py",        19, 22,  params="(tree)"),
    _mk("parser.py::tokenize",          "parser.py",        25, 26,  params="(source)"),
    _mk("parser.py::build_root",        "parser.py",        29, 33,  params="(tokens)"),
    # graph.py
    _mk("graph.py::Graph.__init__",     "graph.py",          7,  9,  params="(self)",
        parent_name="Graph"),
    _mk("graph.py::Graph.add_node",     "graph.py",         11, 13,  params="(self, name)",
        parent_name="Graph"),
    _mk("graph.py::Graph.add_edge",     "graph.py",         15, 16,  params="(self, src, dst)",
        parent_name="Graph"),
    _mk("graph.py::Graph.neighbors",    "graph.py",         18, 19,  params="(self, name)",
        parent_name="Graph"),
    _mk("graph.py::Graph.impact_radius","graph.py",         21, 32,
        params="(self, start, depth)", parent_name="Graph"),
    # embeddings.py
    _mk("embeddings.py::cosine_similarity",        "embeddings.py",  4, 12,
        params="(a, b)"),
    _mk("embeddings.py::embed_text",               "embeddings.py", 15, 17,
        params="(text)"),
    _mk("embeddings.py::EmbeddingIndex.__init__",  "embeddings.py", 21, 22,
        params="(self)", parent_name="EmbeddingIndex"),
    _mk("embeddings.py::EmbeddingIndex.add",       "embeddings.py", 24, 25,
        params="(self, qname, vec)", parent_name="EmbeddingIndex"),
    _mk("embeddings.py::EmbeddingIndex.search",    "embeddings.py", 27, 32,
        params="(self, query_vec, limit=10)", parent_name="EmbeddingIndex"),
    # visualization.py
    _mk("visualization.py::esc_h",        "visualization.py",  4, 10, params="(s)"),
    _mk("visualization.py::render_html",  "visualization.py", 13, 18, params="(graph)"),
    # api_client.py
    _mk("api_client.py::OpenAIClient.__init__",   "api_client.py",  7, 10,
        params="(self)", parent_name="OpenAIClient"),
    _mk("api_client.py::GeminiClient.__init__",   "api_client.py", 14, 17,
        params="(self)", parent_name="GeminiClient"),
    _mk("api_client.py::MinimaxClient.__init__",  "api_client.py", 21, 24,
        params="(self)", parent_name="MinimaxClient"),
    # incremental.py
    _mk("incremental.py::detect_changes",     "incremental.py",  6, 12,
        params="(repo_root, since_ref)"),
    _mk("incremental.py::get_changed_files",  "incremental.py", 15, 16,
        params="(repo_root)"),
]


def _rank_top(query_vec, doc_vecs, qnames, limit=3):
    scored = [
        (qn, _cosine_similarity(query_vec, v))
        for qn, v in zip(qnames, doc_vecs)
    ]
    scored.sort(key=lambda x: x[1], reverse=True)
    return [qn for qn, _ in scored[:limit]]


def _reciprocal_rank(ranked_top: list[str], expected: set[str]) -> float:
    for i, qn in enumerate(ranked_top, start=1):
        if qn in expected:
            return 1.0 / i
    return 0.0


def _mrr(rankings: list[list[str]], expectations: list[set[str]]) -> float:
    if not rankings:
        return 0.0
    return sum(
        _reciprocal_rank(r, e) for r, e in zip(rankings, expectations)
    ) / len(rankings)


def test_mrr_body_not_worse_than_legacy():
    """AC-6 (Iter 3 D-iter3-9): non-inferiority MRR gate.

    Two runs with the Local provider:
      - legacy: ``_node_to_text(node, reader=None)`` — metadata only
      - body-on: ``_node_to_text(node, reader, max_chars=...)`` — body enriched

    The body-on run must not regress MRR@3 beyond the tolerance
    (``MRR_on >= MRR_legacy - 0.02``) AND must strictly improve it
    (``MRR_on > MRR_legacy``).
    """
    provider = LocalEmbeddingProvider()
    max_chars = _resolve_body_max_chars(provider.name)
    reader = _FileLineCache()

    legacy_texts = [_node_to_text(n, reader=None) for n in NODES]
    body_texts = [
        _node_to_text(n, reader=reader, max_chars=max_chars) for n in NODES
    ]

    legacy_vecs = provider.embed(legacy_texts)
    body_vecs = provider.embed(body_texts)

    qnames = [n.qualified_name for n in NODES]

    queries = json.loads((FIXTURE_DIR / "golden_queries.json").read_text())
    # Sanity: all golden expected nodes exist in our fixture.
    known = set(qnames)
    for q in queries:
        for exp in q["expected_top3"]:
            assert exp in known, f"Golden query references unknown node: {exp}"

    query_texts = [q["query"] for q in queries]
    query_vecs = [provider.embed_query(t) for t in query_texts]
    expectations = [set(q["expected_top3"]) for q in queries]

    legacy_rankings = [_rank_top(qv, legacy_vecs, qnames) for qv in query_vecs]
    body_rankings = [_rank_top(qv, body_vecs, qnames) for qv in query_vecs]

    mrr_legacy = _mrr(legacy_rankings, expectations)
    mrr_body = _mrr(body_rankings, expectations)

    # Diagnostic logging (captured on failure)
    print(f"MRR@3 legacy={mrr_legacy:.4f} body={mrr_body:.4f}")
    for q, lr, br in zip(queries, legacy_rankings, body_rankings):
        print(f"  [{q['type']:19s}] {q['query']}")
        print(f"      legacy top-3: {lr}")
        print(f"      body top-3  : {br}")

    # non-inferiority hard gate (must not regress beyond tolerance)
    assert mrr_body >= mrr_legacy - 0.02, (
        f"body enrichment regressed MRR beyond tolerance "
        f"(body={mrr_body:.4f}, legacy={mrr_legacy:.4f}, "
        f"delta={mrr_body - mrr_legacy:+.4f})"
    )
    # soft signal: must strictly improve (not just match)
    assert mrr_body > mrr_legacy, (
        f"body enrichment failed to improve MRR "
        f"(body={mrr_body:.4f}, legacy={mrr_legacy:.4f})"
    )
