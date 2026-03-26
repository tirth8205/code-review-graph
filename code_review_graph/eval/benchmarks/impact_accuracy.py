"""Impact accuracy benchmark: measures precision/recall of change impact analysis."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def _get_changed_files(repo_path: Path, sha: str) -> list[str]:
    """Get list of changed files for a commit."""
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{sha}~1", sha],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD~1", "HEAD"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
        )
    return [f.strip() for f in result.stdout.strip().splitlines() if f.strip()]


def run(repo_path: Path, store, config: dict) -> list[dict]:
    """Run impact accuracy benchmark."""
    results = []
    for tc in config.get("test_commits", []):
        changed = _get_changed_files(repo_path, tc["sha"])
        if not changed:
            continue

        # Get predicted impact from our tool
        try:
            from code_review_graph.changes import analyze_changes
            analysis = analyze_changes(
                store, changed, repo_root=str(repo_path),
                base=tc["sha"] + "~1",
            )
            # Extract files from changed_functions and affected_flows
            predicted = set(changed)
            for f in analysis.get("changed_functions", []):
                if isinstance(f, dict) and "file_path" in f:
                    predicted.add(f["file_path"])
                elif isinstance(f, dict) and "file" in f:
                    predicted.add(f["file"])
            for flow in analysis.get("affected_flows", []):
                if isinstance(flow, dict):
                    for node in flow.get("nodes", []):
                        if isinstance(node, dict) and "file_path" in node:
                            predicted.add(node["file_path"])
        except Exception as exc:
            logger.warning("analyze_changes failed: %s", exc)
            predicted = set(changed)

        # Ground truth: changed files + files that import from changed files
        actual = set(changed)
        for f in changed:
            nodes = store.get_nodes_by_file(f)
            for node in nodes:
                for edge in store.get_edges_by_target(node.qualified_name):
                    if edge.kind in ("CALLS", "IMPORTS_FROM"):
                        src_qual = edge.source_qualified
                        src_file = (
                            src_qual.split("::")[0] if "::" in src_qual else ""
                        )
                        if src_file:
                            actual.add(src_file)

        tp = len(predicted & actual)
        precision = tp / max(len(predicted), 1)
        recall = tp / max(len(actual), 1)
        f1 = 2 * precision * recall / max(precision + recall, 0.001)

        results.append({
            "repo": config["name"],
            "commit": tc["sha"],
            "predicted_files": len(predicted),
            "actual_files": len(actual),
            "true_positives": tp,
            "precision": round(precision, 3),
            "recall": round(recall, 3),
            "f1": round(f1, 3),
        })
    return results
