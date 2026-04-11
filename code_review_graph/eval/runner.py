"""Evaluation runner: orchestrates benchmark execution across repositories."""

from __future__ import annotations

import csv
import logging
import subprocess
from datetime import date
from pathlib import Path

try:
    import yaml  # type: ignore[import-untyped]
except ImportError:
    yaml = None  # type: ignore[assignment]

from code_review_graph.eval.benchmarks import (
    build_performance,
    flow_completeness,
    impact_accuracy,
    search_quality,
    token_efficiency,
)

logger = logging.getLogger(__name__)

BENCHMARK_REGISTRY = {
    "token_efficiency": token_efficiency.run,
    "impact_accuracy": impact_accuracy.run,
    "flow_completeness": flow_completeness.run,
    "search_quality": search_quality.run,
    "build_performance": build_performance.run,
}

CONFIGS_DIR = Path(__file__).parent / "configs"
DEFAULT_OUTPUT = Path("evaluate/results")
DEFAULT_REPOS = Path("evaluate/test_repos")


def _require_yaml():
    if yaml is None:
        raise ImportError("pyyaml is required: pip install code-review-graph[eval]")


def load_config(name: str) -> dict:
    """Load a single benchmark config by name."""
    _require_yaml()
    path = CONFIGS_DIR / f"{name}.yaml"
    with open(path) as f:
        return yaml.safe_load(f)


def load_all_configs() -> list[dict]:
    """Load all benchmark configs from the configs directory."""
    _require_yaml()
    configs = []
    for p in sorted(CONFIGS_DIR.glob("*.yaml")):
        with open(p) as f:
            configs.append(yaml.safe_load(f))
    return configs


def clone_or_update(config: dict, repos_dir: Path | None = None) -> Path:
    """Clone or update a repository for benchmarking."""
    repos_dir = repos_dir or DEFAULT_REPOS
    repos_dir.mkdir(parents=True, exist_ok=True)
    repo_path = repos_dir / config["name"]

    if repo_path.exists():
        subprocess.run(
            ["git", "fetch", "--all"],
            cwd=str(repo_path),
            capture_output=True,
        )
    else:
        subprocess.run(
            ["git", "clone", "--depth", "50", config["url"], str(repo_path)],
            capture_output=True,
        )

    commit = config.get("commit", "HEAD")
    if commit != "HEAD":
        subprocess.run(
            ["git", "checkout", commit],
            cwd=str(repo_path),
            capture_output=True,
        )

    return repo_path


def write_csv(results: list[dict], path: Path) -> None:
    """Write benchmark results to a CSV file."""
    if not results:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(results[0].keys())
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)


def run_eval(
    repos: list[str] | None = None,
    benchmarks: list[str] | None = None,
    output_dir: str | Path | None = None,
) -> dict[str, list[dict]]:
    """Run evaluation benchmarks across repositories.

    Args:
        repos: List of repo config names to evaluate (None = all).
        benchmarks: List of benchmark names to run (None = all).
        output_dir: Directory for CSV output files.

    Returns:
        Dict mapping ``{repo}_{benchmark}`` to list of result dicts.
    """
    output_dir = Path(output_dir) if output_dir else DEFAULT_OUTPUT
    output_dir.mkdir(parents=True, exist_ok=True)

    if repos:
        configs = [load_config(r) for r in repos]
    else:
        configs = load_all_configs()

    benchmark_names = benchmarks or list(BENCHMARK_REGISTRY.keys())
    all_results: dict[str, list[dict]] = {}
    today = date.today().isoformat()

    for config in configs:
        name = config["name"]
        logger.info("Evaluating %s...", name)

        repo_path = clone_or_update(config)

        # Build graph
        from code_review_graph.graph import GraphStore
        from code_review_graph.incremental import full_build, get_db_path

        db_path = get_db_path(repo_path)
        store = GraphStore(db_path)

        full_build(repo_path, store)

        for bench_name in benchmark_names:
            if bench_name not in BENCHMARK_REGISTRY:
                logger.warning("Unknown benchmark: %s", bench_name)
                continue

            logger.info("  Running %s...", bench_name)
            try:
                bench_fn = BENCHMARK_REGISTRY[bench_name]
                results = bench_fn(repo_path, store, config)

                key = f"{name}_{bench_name}"
                all_results[key] = results
                write_csv(results, output_dir / f"{key}_{today}.csv")
                logger.info("  %s: %d result(s)", bench_name, len(results))
            except Exception as e:
                logger.error("  %s failed: %s", bench_name, e)
                all_results[f"{name}_{bench_name}"] = []

        store.close()

    return all_results
