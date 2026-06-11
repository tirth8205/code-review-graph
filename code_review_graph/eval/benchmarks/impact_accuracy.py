"""Impact accuracy benchmark: measures precision/recall of change impact analysis.

Two ground-truth modes are emitted side by side (``ground_truth_mode`` column):

- **graph-derived (circular — upper bound)** — the historical mode. Ground
  truth is the changed files plus files with CALLS/IMPORTS_FROM edges into
  them, i.e. derived from the same graph the predictor traverses. Recall in
  this mode is an upper bound by construction, not independent evidence.
- **co-change (same commit, seed excluded)** — the honest mode. The predictor
  is seeded with a single changed file and graded against the *other* files
  the author actually touched in the same commit. The ground truth comes from
  git history, not from the graph.

Failure semantics: if ``analyze_changes`` throws, the row is recorded with
``status="error"`` and empty metric fields — it stays in the CSV but is
excluded from aggregates. (Previously a failure silently set
``predicted = set(changed)``, guaranteeing a fake recall of 1.0.)
"""

from __future__ import annotations

import logging
import statistics
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

MODE_GRAPH_DERIVED = "graph-derived (circular — upper bound)"
MODE_CO_CHANGE = "co-change (same commit, seed excluded)"


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


def _files_from_analysis(analysis: dict) -> set[str]:
    """Extract predicted file paths from an ``analyze_changes`` result."""
    predicted: set[str] = set()
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
    return predicted


def _graph_neighbor_files(store, files: list[str]) -> set[str]:
    """Files with CALLS/IMPORTS_FROM edges into any node of *files* (one hop)."""
    out: set[str] = set()
    for f in files:
        for node in store.get_nodes_by_file(f):
            for edge in store.get_edges_by_target(node.qualified_name):
                if edge.kind in ("CALLS", "IMPORTS_FROM"):
                    src_qual = edge.source_qualified
                    src_file = src_qual.split("::")[0] if "::" in src_qual else ""
                    if src_file:
                        out.add(src_file)
    return out


def _base_row(repo: str, sha: str, mode: str, seed: str) -> dict:
    return {
        "repo": repo,
        "commit": sha,
        "ground_truth_mode": mode,
        "seed_file": seed,
        "predicted_files": "",
        "actual_files": "",
        "true_positives": "",
        "precision": "",
        "recall": "",
        "f1": "",
        "status": "ok",
        "error": "",
    }


def _scored_row(
    repo: str, sha: str, mode: str, seed: str,
    predicted: set[str], actual: set[str],
) -> dict:
    tp = len(predicted & actual)
    precision = tp / max(len(predicted), 1)
    recall = tp / max(len(actual), 1)
    f1 = 2 * precision * recall / max(precision + recall, 0.001)
    row = _base_row(repo, sha, mode, seed)
    row.update({
        "predicted_files": len(predicted),
        "actual_files": len(actual),
        "true_positives": tp,
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "f1": round(f1, 3),
    })
    return row


def _error_row(repo: str, sha: str, mode: str, seed: str, exc: Exception) -> dict:
    row = _base_row(repo, sha, mode, seed)
    row["status"] = "error"
    row["error"] = str(exc)[:200]
    return row


def run(repo_path: Path, store, config: dict) -> list[dict]:
    """Run impact accuracy benchmark (both ground-truth modes)."""
    from code_review_graph.changes import analyze_changes

    results = []
    repo = config["name"]
    for tc in config.get("test_commits", []):
        sha = tc["sha"]
        changed = _get_changed_files(repo_path, sha)
        if not changed:
            continue

        # --- Mode 1: graph-derived ground truth (circular — upper bound) ---
        try:
            analysis = analyze_changes(
                store, changed, repo_root=str(repo_path), base=sha + "~1",
            )
        except Exception as exc:
            # Old behaviour set predicted = set(changed) here, which
            # guarantees recall 1.0 on a *failed* run. Mark failed instead.
            logger.warning("analyze_changes failed on %s: %s", sha, exc)
            results.append(_error_row(repo, sha, MODE_GRAPH_DERIVED, "", exc))
            analysis = None

        if analysis is not None:
            predicted = set(changed) | _files_from_analysis(analysis)
            actual = set(changed) | _graph_neighbor_files(store, changed)
            results.append(
                _scored_row(repo, sha, MODE_GRAPH_DERIVED, "", predicted, actual)
            )

        # --- Mode 2: co-change ground truth (honest) ---
        # Seed the predictor with a single changed file and grade against
        # the other files the author touched in the same commit. Note the
        # seed analysis deliberately gets no repo_root/diff: it must only
        # see the seed file, never the full commit diff.
        seed = sorted(changed)[0]
        co_actual = set(changed) - {seed}
        if not co_actual:
            row = _base_row(repo, sha, MODE_CO_CHANGE, seed)
            row["status"] = "skipped"
            row["error"] = "single-file commit: no co-changed files to grade against"
            results.append(row)
            continue

        try:
            seed_analysis = analyze_changes(store, [seed])
        except Exception as exc:
            logger.warning("analyze_changes (seed=%s) failed on %s: %s", seed, sha, exc)
            results.append(_error_row(repo, sha, MODE_CO_CHANGE, seed, exc))
            continue

        co_predicted = _files_from_analysis(seed_analysis)
        co_predicted |= _graph_neighbor_files(store, [seed])
        co_predicted.discard(seed)
        results.append(
            _scored_row(repo, sha, MODE_CO_CHANGE, seed, co_predicted, co_actual)
        )

    return results


def aggregate(results: list[dict]) -> dict:
    """Per-mode means over successful rows only.

    Error/skipped rows stay in the CSV but never contribute to a number.
    """
    out: dict = {
        "total_rows": len(results),
        "error_rows": sum(1 for r in results if r.get("status") == "error"),
        "skipped_rows": sum(1 for r in results if r.get("status") == "skipped"),
    }
    for key, mode in (
        ("graph_derived", MODE_GRAPH_DERIVED),
        ("co_change", MODE_CO_CHANGE),
    ):
        rows = [
            r for r in results
            if r.get("ground_truth_mode") == mode and r.get("status") == "ok"
        ]
        out[key] = {
            "ok_rows": len(rows),
            "mean_precision": (
                round(statistics.mean(float(r["precision"]) for r in rows), 3)
                if rows else None
            ),
            "mean_recall": (
                round(statistics.mean(float(r["recall"]) for r in rows), 3)
                if rows else None
            ),
            "mean_f1": (
                round(statistics.mean(float(r["f1"]) for r in rows), 3)
                if rows else None
            ),
        }
    return out
