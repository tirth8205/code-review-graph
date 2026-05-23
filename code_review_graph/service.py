"""Shared graph service contract for CLI, editor, LSP, and web clients."""

from __future__ import annotations

import time
from contextlib import AbstractContextManager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .graph import GraphEdge, GraphNode, GraphStats, GraphStore, edge_to_dict, node_to_dict
from .incremental import find_project_root, get_db_path
from .telemetry import estimate_tokens_from_chars, record_event, summarize_events


def _normalize_path(path: str | Path, repo_root: Path) -> str:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    return str(candidate)


def _node_span_contains(node: GraphNode, line: int) -> bool:
    return node.line_start <= line <= node.line_end


class GraphService(AbstractContextManager["GraphService"]):
    """High-level graph API consumed by all non-MCP product surfaces.

    This class deliberately wraps ``GraphStore`` instead of exposing SQLite
    details.  Clients should depend on the dictionaries returned here so the
    persistence layer can evolve independently.
    """

    def __init__(self, repo_root: str | Path | None = None) -> None:
        self.repo_root = Path(repo_root) if repo_root else find_project_root()
        self.repo_root = self.repo_root.resolve()
        self.db_path = get_db_path(self.repo_root)
        self.store = GraphStore(self.db_path)
        self._source_token_cache: tuple[float, float, int, int] | None = None

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def close(self) -> None:
        self.store.close()

    def status(self) -> dict[str, Any]:
        stats = self.store.get_stats()
        return {
            "status": "ok",
            "repo_root": str(self.repo_root),
            "db_path": str(self.db_path),
            "stats": _stats_to_dict(stats),
            "token_estimate": self.token_estimate(stats=stats),
            "telemetry": self.telemetry_summary(),
            "metadata": {
                "git_branch": self.store.get_metadata("git_branch"),
                "git_head_sha": self.store.get_metadata("git_head_sha"),
                "last_build_type": self.store.get_metadata("last_build_type"),
            },
        }

    def search(self, query: str, limit: int = 20) -> dict[str, Any]:
        nodes = self.store.search_nodes(query, limit=max(1, min(limit, 200)))
        return {
            "status": "ok",
            "query": query,
            "nodes": [node_to_dict(n) for n in nodes],
        }

    def get_node(self, qualified_name: str) -> dict[str, Any]:
        node = self.store.get_node(qualified_name)
        if not node:
            return {"status": "not_found", "qualified_name": qualified_name}
        return {"status": "ok", "node": node_to_dict(node)}

    def file_summary(self, file_path: str) -> dict[str, Any]:
        abs_path = _normalize_path(file_path, self.repo_root)
        nodes = self.store.get_nodes_by_file(abs_path)
        if not nodes and str(file_path) != abs_path:
            nodes = self.store.get_nodes_by_file(str(file_path))
        return {
            "status": "ok",
            "file_path": abs_path,
            "nodes": [node_to_dict(n) for n in nodes],
        }

    def node_at(self, file_path: str, line: int) -> GraphNode | None:
        """Return the most specific graph node containing a 1-based line."""
        abs_path = _normalize_path(file_path, self.repo_root)
        nodes = self.store.get_nodes_by_file(abs_path)
        if not nodes:
            nodes = self.store.get_nodes_by_file(str(file_path))
        candidates = [n for n in nodes if _node_span_contains(n, line)]
        if not candidates:
            return None
        return sorted(
            candidates,
            key=lambda n: (n.line_end - n.line_start, n.kind == "File"),
        )[0]

    def callers(self, target: str, limit: int = 100) -> dict[str, Any]:
        node = self._resolve_node(target)
        if not node:
            ambiguous = self._ambiguous_response(target)
            if ambiguous:
                return ambiguous
            return {"status": "not_found", "target": target}
        edges = [
            e for e in self.store.get_edges_by_target(node.qualified_name)
            if e.kind == "CALLS"
        ]
        if not edges:
            edges = self.store.search_edges_by_target_name(node.name)
        return self._edge_endpoint_response("callers", node, edges, source=True, limit=limit)

    def callees(self, target: str, limit: int = 100) -> dict[str, Any]:
        node = self._resolve_node(target)
        if not node:
            ambiguous = self._ambiguous_response(target)
            if ambiguous:
                return ambiguous
            return {"status": "not_found", "target": target}
        edges = [
            e for e in self.store.get_edges_by_source(node.qualified_name)
            if e.kind == "CALLS"
        ]
        return self._edge_endpoint_response("callees", node, edges, source=False, limit=limit)

    def query(self, pattern: str, target: str, limit: int = 100) -> dict[str, Any]:
        if pattern == "callers_of":
            return self.callers(target, limit=limit)
        if pattern == "callees_of":
            return self.callees(target, limit=limit)
        if pattern == "file_summary":
            return self.file_summary(target)
        return {"status": "error", "error": f"unsupported pattern: {pattern}"}

    def impact(
        self,
        changed_files: list[str],
        max_depth: int = 2,
        max_results: int = 500,
    ) -> dict[str, Any]:
        files = [_normalize_path(f, self.repo_root) for f in changed_files]
        result = self.store.get_impact_radius(
            files,
            max_depth=max(1, min(max_depth, 10)),
            max_nodes=max(1, min(max_results, 5000)),
        )
        return {
            "status": "ok",
            "changed_files": changed_files,
            "changed_nodes": [node_to_dict(n) for n in result["changed_nodes"]],
            "impacted_nodes": [node_to_dict(n) for n in result["impacted_nodes"]],
            "impacted_files": result["impacted_files"],
            "edges": [edge_to_dict(e) for e in result["edges"]],
            "truncated": result["truncated"],
            "total_impacted": result["total_impacted"],
        }

    def graph(self, limit: int = 1000) -> dict[str, Any]:
        files = self.store.get_all_files()
        nodes: list[GraphNode] = []
        for file_path in files:
            nodes.extend(self.store.get_nodes_by_file(file_path))
            if len(nodes) >= limit:
                nodes = nodes[:limit]
                break

        qns = {n.qualified_name for n in nodes}
        edges = self.store.get_edges_among(qns)
        return {
            "status": "ok",
            "nodes": [node_to_dict(n) for n in nodes],
            "edges": [edge_to_dict(e) for e in edges],
            "truncated": len(nodes) >= limit,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }

    def token_estimate(
        self,
        graph_limit: int = 250,
        stats: GraphStats | None = None,
    ) -> dict[str, Any]:
        """Estimate full-source versus graph-context token counts.

        This is a local approximation for product telemetry and dashboards,
        not model-provider billing data.  It uses the common chars/4 heuristic.
        """
        source_tokens, missing_files = self._source_token_estimate()
        graph_payload = {
            "stats": _stats_to_dict(stats or self.store.get_stats()),
            "graph": self.graph(limit=graph_limit),
        }
        graph_tokens = estimate_tokens_from_chars(
            len(_stable_json(graph_payload))
        )
        saved_tokens = max(source_tokens - graph_tokens, 0)
        reduction_percent = (
            round((saved_tokens / source_tokens) * 100, 2)
            if source_tokens > 0 else 0.0
        )
        compression_ratio = (
            round(source_tokens / graph_tokens, 2)
            if graph_tokens > 0 else 0.0
        )
        return {
            "status": "estimated",
            "method": "chars_div_4",
            "scope": f"indexed source files vs graph overview payload (limit={graph_limit})",
            "source_tokens": source_tokens,
            "graph_tokens": graph_tokens,
            "saved_tokens": saved_tokens,
            "reduction_percent": reduction_percent,
            "compression_ratio": compression_ratio,
            "missing_files": missing_files,
        }

    def record_telemetry(
        self,
        operation: str,
        payload: dict[str, Any],
        surface: str = "web",
    ) -> dict[str, Any]:
        baseline_tokens, missing_files = self._source_token_estimate()
        payload_tokens = estimate_tokens_from_chars(len(_stable_json(payload)))
        return record_event(
            self.db_path,
            surface=surface,
            operation=operation,
            baseline_tokens=baseline_tokens,
            payload_tokens=payload_tokens,
            extra={"missing_files": missing_files},
        )

    def telemetry_summary(self) -> dict[str, Any]:
        return summarize_events(self.db_path)

    def _source_token_estimate(self) -> tuple[int, int]:
        db_mtime = self.db_path.stat().st_mtime if self.db_path.exists() else 0.0
        now = time.time()
        if self._source_token_cache:
            cached_at, cached_mtime, tokens, missing = self._source_token_cache
            if cached_mtime == db_mtime and now - cached_at < 5:
                return tokens, missing

        source_bytes = 0
        missing_files = 0
        for file_path in self.store.get_all_files():
            try:
                source_bytes += Path(file_path).stat().st_size
            except OSError:
                missing_files += 1
        source_tokens = estimate_tokens_from_chars(source_bytes)
        self._source_token_cache = (now, db_mtime, source_tokens, missing_files)
        return source_tokens, missing_files

    def _resolve_node(self, target: str) -> GraphNode | None:
        node = self.store.get_node(target)
        if node:
            return node
        abs_target = _normalize_path(target, self.repo_root)
        node = self.store.get_node(abs_target)
        if node:
            return node
        matches = self.store.search_nodes(target, limit=2)
        return matches[0] if len(matches) == 1 else None

    def _ambiguous_response(self, target: str) -> dict[str, Any] | None:
        candidates = self.store.search_nodes(target, limit=5)
        if len(candidates) <= 1:
            return None
        return {
            "status": "ambiguous",
            "target": target,
            "candidates": [node_to_dict(n) for n in candidates],
        }

    def _edge_endpoint_response(
        self,
        kind: str,
        node: GraphNode,
        edges: list[GraphEdge],
        *,
        source: bool,
        limit: int,
    ) -> dict[str, Any]:
        capped = edges[: max(1, min(limit, 500))]
        endpoint_nodes = []
        for edge in capped:
            qn = edge.source_qualified if source else edge.target_qualified
            endpoint = self.store.get_node(qn)
            if endpoint:
                endpoint_nodes.append(node_to_dict(endpoint))
        return {
            "status": "ok",
            "kind": kind,
            "target": node_to_dict(node),
            "nodes": endpoint_nodes,
            "edges": [edge_to_dict(e) for e in capped],
            "truncated": len(edges) > len(capped),
            "total": len(edges),
        }


def _stats_to_dict(stats: GraphStats) -> dict[str, Any]:
    return asdict(stats)


def _stable_json(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, separators=(",", ":"), sort_keys=True)
