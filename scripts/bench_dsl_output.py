#!/usr/bin/env python3
"""DSL vs dict output-mode benchmark.

Builds a realistic-shaped graph (functions across deep namespaces with
realistic file paths) and measures token reduction when returning blast-radius
and query_graph responses in dict vs dsl format.

Usage:
    python3 scripts/bench_dsl_output.py

Token counts use a coarse 4-chars-per-token approximation (matching the
estimate used in code_review_graph/context_savings.py); for tiktoken-accurate
numbers, replace ``approx_tokens`` with a real tokenizer call.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

# Ensure package on path when running from a checkout
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from code_review_graph.graph import (  # noqa: E402
    EdgeInfo,
    GraphStore,
    NodeInfo,
)
from code_review_graph.tools.query import (  # noqa: E402
    get_impact_radius,
    query_graph,
)


def approx_tokens(text: str) -> int:
    """Rough ~4-chars-per-token estimate. Good enough for relative comparisons."""
    return max(1, len(text) // 4)


def seed_realistic_graph(store: GraphStore, root: Path, n_files: int = 50) -> str:
    """Build a graph approximating a real backend service:

    - ``n_files`` Python files, each with one class and 5 functions
    - Cross-file CALLS edges so a single changed file has a real blast radius
    - Deep file paths so qualified_names are long (~70-100 chars), matching
      real codebases where DSL compression matters most
    """
    files: list[str] = []
    for i in range(n_files):
        path = str(
            root
            / "src"
            / "backend"
            / "services"
            / f"module_{i:02d}"
            / "handlers.py"
        )
        files.append(path)
        store.upsert_node(NodeInfo(
            kind="File", name=path, file_path=path,
            line_start=1, line_end=500, language="python",
        ))
        store.upsert_node(NodeInfo(
            kind="Class", name=f"Module{i:02d}Handler", file_path=path,
            line_start=10, line_end=400, language="python",
        ))
        for j in range(5):
            store.upsert_node(NodeInfo(
                kind="Function", name=f"handle_request_{j}", file_path=path,
                line_start=50 + j * 60, line_end=50 + j * 60 + 40,
                language="python",
                parent_name=f"Module{i:02d}Handler",
            ))

    # Add cross-file CALLS so blast radius is rich:
    # every module's handle_request_0 calls 4 other modules' handlers,
    # producing fan-out that compounds at multi-hop depth.
    for i in range(n_files):
        src_path = files[i]
        for k, offset in enumerate([1, 3, 7, 13]):
            tgt_idx = (i + offset) % n_files
            tgt_path = files[tgt_idx]
            store.upsert_edge(EdgeInfo(
                kind="CALLS",
                source=f"{src_path}::Module{i:02d}Handler.handle_request_0",
                target=f"{tgt_path}::Module{tgt_idx:02d}Handler.handle_request_{k}",
                file_path=src_path,
                line=55 + k,
            ))
        # And a CONTAINS edge for the class
        store.upsert_edge(EdgeInfo(
            kind="CONTAINS",
            source=src_path,
            target=f"{src_path}::Module{i:02d}Handler",
            file_path=src_path,
            line=10,
        ))

    store.commit()
    return files[0]  # canonical "changed" file


def bench_impact_radius(changed_file: str, repo_root: str) -> None:
    print("\n=== get_impact_radius ===")
    dict_resp = get_impact_radius(
        changed_files=[changed_file],
        repo_root=repo_root,
        max_depth=4,
        format="dict",
    )
    dsl_resp = get_impact_radius(
        changed_files=[changed_file],
        repo_root=repo_root,
        max_depth=4,
        format="dsl",
    )
    dict_json = json.dumps(dict_resp)
    dsl_json = json.dumps(dsl_resp, ensure_ascii=False)
    n_nodes = len(dict_resp.get("impacted_nodes", []))
    n_edges = len(dict_resp.get("edges", []))
    print(f"  impacted nodes: {n_nodes}")
    print(f"  edges:          {n_edges}")
    print(f"  dict response:  {len(dict_json):>7,} chars  ~{approx_tokens(dict_json):>6,} tokens")
    print(f"  dsl response:   {len(dsl_json):>7,} chars  ~{approx_tokens(dsl_json):>6,} tokens")
    if dsl_json:
        ratio = len(dict_json) / max(1, len(dsl_json))
        print(f"  compression:    {ratio:.2f}× smaller "
              f"({100 * (1 - len(dsl_json)/len(dict_json)):.1f}% fewer chars)")
        print(
            f"  absolute save:  "
            f"~{approx_tokens(dict_json) - approx_tokens(dsl_json):,}"
            f" tokens per call"
        )

    # Per-payload breakdown: just the nodes+edges arrays, no envelope
    print("\n  per-payload breakdown (nodes + edges only):")
    dict_payload = json.dumps({
        "changed_nodes": dict_resp.get("changed_nodes", []),
        "impacted_nodes": dict_resp.get("impacted_nodes", []),
        "edges": dict_resp.get("edges", []),
    })
    dsl_payload = json.dumps({
        "changed_nodes": dsl_resp.get("changed_nodes", []),
        "impacted_nodes": dsl_resp.get("impacted_nodes", []),
        "edges": dsl_resp.get("edges", []),
    }, ensure_ascii=False)
    if dsl_payload:
        payload_ratio = len(dict_payload) / max(1, len(dsl_payload))
        print(
            f"    dict payload: {len(dict_payload):>7,} chars"
            f"  ~{approx_tokens(dict_payload):>6,} tokens"
        )
        print(
            f"    dsl payload:  {len(dsl_payload):>7,} chars"
            f"  ~{approx_tokens(dsl_payload):>6,} tokens"
        )
        print(f"    compression:  {payload_ratio:.2f}× smaller "
              f"({100 * (1 - len(dsl_payload)/len(dict_payload)):.1f}% fewer chars)")


def bench_query_graph(target: str, repo_root: str) -> None:
    print("\n=== query_graph (callers_of, deep transitive) ===")
    # callers_of returns dicts mixed with synthetic dicts; most realistic
    # is file_summary against a large file
    dict_resp = query_graph(
        pattern="file_summary",
        target=target,
        repo_root=repo_root,
        format="dict",
    )
    dsl_resp = query_graph(
        pattern="file_summary",
        target=target,
        repo_root=repo_root,
        format="dsl",
    )
    dict_json = json.dumps(dict_resp)
    dsl_json = json.dumps(dsl_resp, ensure_ascii=False)
    n_results = len(dict_resp.get("results", []))
    print(f"  results:        {n_results}")
    print(f"  dict response:  {len(dict_json):>7,} chars  ~{approx_tokens(dict_json):>6,} tokens")
    print(f"  dsl response:   {len(dsl_json):>7,} chars  ~{approx_tokens(dsl_json):>6,} tokens")
    if dsl_json:
        ratio = len(dict_json) / max(1, len(dsl_json))
        print(f"  compression:    {ratio:.2f}× smaller "
              f"({100 * (1 - len(dsl_json)/len(dict_json)):.1f}% fewer chars)")


def main() -> int:
    tmp_dir = tempfile.mkdtemp()
    root = Path(tmp_dir).resolve()
    (root / ".git").mkdir()
    crg_dir = root / ".code-review-graph"
    crg_dir.mkdir()
    db_path = crg_dir / "graph.db"

    print(f"Seeding realistic graph at {root}...")
    store = GraphStore(db_path)
    try:
        changed_file = seed_realistic_graph(store, root, n_files=50)
        stats = store.get_stats()
        print(f"Graph: {stats.total_nodes} nodes, {stats.total_edges} edges, "
              f"{stats.files_count} files")
    finally:
        store.close()

    bench_impact_radius(changed_file, str(root))
    bench_query_graph(changed_file, str(root))

    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
