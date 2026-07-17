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
    agent_baseline,
    build_performance,
    flow_completeness,
    impact_accuracy,
    multi_hop_retrieval,
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
    "multi_hop_retrieval": multi_hop_retrieval.run,
    "agent_baseline": agent_baseline.run,
}

CONFIGS_DIR = Path(__file__).parent / "configs"
DEFAULT_OUTPUT = Path("evaluate/results")
DEFAULT_REPOS = Path("evaluate/test_repos")


def _require_yaml():
    if yaml is None:
        raise ImportError("pyyaml is required: pip install code-review-graph[eval]")


def _validate_config(config: object, path: Path) -> dict:
    """Validate snapshot invariants required for reproducible benchmarks."""
    if not isinstance(config, dict):
        raise ValueError(f"{path}: evaluation config must be a mapping")
    test_commits = config.get("test_commits", [])
    if test_commits:
        latest = test_commits[-1].get("sha")
        if not latest or config.get("commit") != latest:
            raise ValueError(
                f"{path}: commit pin must equal latest test_commit {latest}"
            )
    return config


def load_config(name: str) -> dict:
    """Load a single benchmark config by name."""
    _require_yaml()
    path = CONFIGS_DIR / f"{name}.yaml"
    with open(path) as f:
        return _validate_config(yaml.safe_load(f), path)


def load_all_configs() -> list[dict]:
    """Load all benchmark configs from the configs directory."""
    _require_yaml()
    configs = []
    for p in sorted(CONFIGS_DIR.glob("*.yaml")):
        with open(p) as f:
            configs.append(_validate_config(yaml.safe_load(f), p))
    return configs


def clone_or_update(config: dict, repos_dir: Path | None = None) -> Path:
    """Clone or update a repository at the config's pinned ``commit`` SHA.

    Full clones (no ``--depth``) are required: the pinned ``test_commits`` are
    often older than any reasonable shallow-clone window, and a missed SHA
    used to silently fall back to ``git diff HEAD~1 HEAD`` — producing
    benchmark numbers tied to whatever upstream HEAD looked like that day.

    Every subprocess call's exit status is checked; failures raise
    ``RuntimeError`` so reproducibility issues surface immediately instead of
    yielding garbage results.
    """
    repos_dir = repos_dir or DEFAULT_REPOS
    repos_dir.mkdir(parents=True, exist_ok=True)
    repo_path = repos_dir / config["name"]

    if repo_path.exists():
        proc = subprocess.run(
            ["git", "fetch", "--all", "--tags"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"git fetch failed in {repo_path}: {proc.stderr.strip()}"
            )
    else:
        proc = subprocess.run(
            ["git", "clone", config["url"], str(repo_path)],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"git clone failed for {config['url']}: {proc.stderr.strip()}"
            )

    commit = config.get("commit", "HEAD")
    if commit != "HEAD":
        proc = subprocess.run(
            ["git", "checkout", commit],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"git checkout {commit} failed in {repo_path}: "
                f"{proc.stderr.strip()}"
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

        # Resolve the repo path to an absolute Path before handing it to
        # full_build / get_db_path so the stored qualified_names match what
        # the CLI/MCP layer produces (those paths go through _get_store ->
        # _validate_repo_root which .resolve()s). Without this, a later
        # ``code-review-graph update --repo <relative>`` writes the same
        # function under a new absolute-prefixed qualified_name, leaving the
        # graph with duplicate nodes for the same source location.
        repo_path = clone_or_update(config).resolve()

        # Build graph
        from code_review_graph.graph import GraphStore
        from code_review_graph.incremental import full_build, get_db_path
        from code_review_graph.postprocessing import run_post_processing

        db_path = get_db_path(repo_path)
        store = GraphStore(db_path)

        full_build(repo_path, store)
        # full_build is the parsing-only primitive; the higher-level CLI/MCP
        # wrappers run postprocessing on top. The eval framework bypasses
        # those, so call it directly here. Without this, FTS5 stays empty
        # and downstream benchmarks (token_efficiency, search_quality)
        # silently produce useless results. See: search.rebuild_fts_index.
        pp_result = run_post_processing(store)
        for warning in pp_result.get("warnings", []):
            logger.warning("  postprocessing: %s", warning)

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
