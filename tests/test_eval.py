"""Tests for the evaluation framework (scorer, reporter, runner, benchmarks)."""

import csv
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from code_review_graph.eval.reporter import (
    generate_full_report,
    generate_markdown_report,
    generate_readme_tables,
)

try:
    import yaml as _yaml  # noqa: F401

    from code_review_graph.eval.runner import write_csv
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False
    write_csv = None  # type: ignore[assignment]
from code_review_graph.eval.scorer import (
    compute_mrr,
    compute_precision_recall,
    compute_token_efficiency,
)

# --- Existing scorer tests ---


def test_token_efficiency():
    result = compute_token_efficiency(10000, 3000)
    assert result["raw_tokens"] == 10000
    assert result["graph_tokens"] == 3000
    assert result["ratio"] == 0.3
    assert result["reduction_percent"] == 70.0


def test_token_efficiency_zero_raw():
    result = compute_token_efficiency(0, 100)
    assert result["ratio"] == 0.0
    assert result["reduction_percent"] == 0.0


def test_mrr_found_at_rank_2():
    result = compute_mrr("b", ["a", "b", "c"])
    assert result == 0.5


def test_mrr_found_at_rank_1():
    result = compute_mrr("a", ["a", "b", "c"])
    assert result == 1.0


def test_mrr_not_found():
    result = compute_mrr("z", ["a", "b", "c"])
    assert result == 0.0


def test_precision_recall():
    predicted = {"a", "b", "c", "d"}
    actual = {"b", "c", "e"}
    result = compute_precision_recall(predicted, actual)
    assert result["precision"] == 0.5
    assert result["recall"] == round(2 / 3, 4)
    expected_f1 = round(2 * 0.5 * (2 / 3) / (0.5 + 2 / 3), 4)
    assert result["f1"] == expected_f1


def test_precision_recall_empty_sets():
    result = compute_precision_recall(set(), set())
    assert result["precision"] == 1.0
    assert result["recall"] == 1.0
    assert result["f1"] == 1.0


def test_precision_recall_no_overlap():
    result = compute_precision_recall({"a"}, {"b"})
    assert result["precision"] == 0.0
    assert result["recall"] == 0.0
    assert result["f1"] == 0.0


def test_generate_markdown_report():
    results = [
        {
            "benchmark": "token_efficiency",
            "ratio": 0.3,
            "reduction_percent": 70.0,
        },
        {
            "benchmark": "search_mrr",
            "ratio": "-",
            "reduction_percent": "-",
        },
    ]
    report = generate_markdown_report(results)
    assert "# Evaluation Report" in report
    assert "## Summary" in report
    assert "token_efficiency" in report
    assert "search_mrr" in report
    assert "70.0" in report
    assert "| Benchmark |" in report


def test_generate_markdown_report_empty():
    report = generate_markdown_report([])
    assert "No benchmark results" in report


# --- New tests ---


@pytest.mark.skipif(not _HAS_YAML, reason="pyyaml not installed")
def test_load_config():
    """Load a temp YAML config and verify structure."""
    import yaml

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False
    ) as f:
        yaml.dump(
            {
                "name": "test-repo",
                "url": "https://example.com/repo.git",
                "commit": "HEAD",
                "language": "python",
                "size_category": "small",
                "test_commits": [{"sha": "abc123", "description": "test"}],
                "entry_points": ["main.py::main"],
                "search_queries": [
                    {"query": "hello", "expected": "main.py::greet"}
                ],
            },
            f,
        )
        tmp_path = f.name

    try:
        import yaml as _yaml

        with open(tmp_path) as fh:
            config = _yaml.safe_load(fh)

        assert config["name"] == "test-repo"
        assert config["language"] == "python"
        assert len(config["test_commits"]) == 1
        assert len(config["entry_points"]) == 1
        assert len(config["search_queries"]) == 1
    finally:
        os.unlink(tmp_path)


@pytest.mark.skipif(not _HAS_YAML, reason="pyyaml not installed")
def test_write_csv():
    """Write results to CSV and read back."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "results" / "test.csv"
        results = [
            {"repo": "foo", "tokens": 100, "ratio": 2.5},
            {"repo": "bar", "tokens": 200, "ratio": 1.5},
        ]
        write_csv(results, path)

        assert path.exists()
        with open(path, newline="") as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        assert len(rows) == 2
        assert rows[0]["repo"] == "foo"
        assert rows[1]["tokens"] == "200"


@pytest.mark.skipif(not _HAS_YAML, reason="pyyaml not installed")
def test_write_csv_empty():
    """Writing empty results should be a no-op."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "empty.csv"
        write_csv([], path)
        assert not path.exists()


def test_generate_readme_tables():
    """Feed sample CSV data and verify table format."""
    with tempfile.TemporaryDirectory() as tmpdir:
        results_dir = Path(tmpdir)

        # Write token efficiency CSV
        te_path = results_dir / "test_token_efficiency_2026-01-01.csv"
        with open(te_path, "w", newline="") as f:
            w = csv.DictWriter(
                f,
                fieldnames=[
                    "repo", "commit", "description", "changed_files",
                    "naive_tokens", "standard_tokens", "graph_tokens",
                    "naive_to_graph_ratio", "standard_to_graph_ratio",
                ],
            )
            w.writeheader()
            w.writerow({
                "repo": "myrepo", "commit": "abc", "description": "test",
                "changed_files": "3", "naive_tokens": "1000",
                "standard_tokens": "500", "graph_tokens": "200",
                "naive_to_graph_ratio": "5.0",
                "standard_to_graph_ratio": "2.5",
            })

        tables = generate_readme_tables(results_dir)
        assert "### Token Efficiency" in tables
        assert "myrepo" in tables
        assert "1000" in tables


def test_generate_full_report():
    """Feed sample CSV data and verify report sections."""
    with tempfile.TemporaryDirectory() as tmpdir:
        results_dir = Path(tmpdir)

        # Write a build_performance CSV
        bp_path = results_dir / "test_build_performance_2026-01-01.csv"
        with open(bp_path, "w", newline="") as f:
            w = csv.DictWriter(
                f,
                fieldnames=[
                    "repo", "file_count", "node_count", "edge_count",
                    "flow_detection_seconds", "community_detection_seconds",
                    "search_avg_ms", "nodes_per_second",
                ],
            )
            w.writeheader()
            w.writerow({
                "repo": "testrepo", "file_count": "10", "node_count": "50",
                "edge_count": "30", "flow_detection_seconds": "0.1",
                "community_detection_seconds": "0.2",
                "search_avg_ms": "5.0", "nodes_per_second": "500",
            })

        report = generate_full_report(results_dir)
        assert "# Evaluation Report" in report
        assert "## Methodology" in report
        assert "## Build Performance" in report
        assert "testrepo" in report


@pytest.mark.skipif(not _HAS_YAML, reason="pyyaml not installed")
def test_runner_with_mock_repo():
    """Create a tiny git repo with 2 Python files, run benchmarks, verify output."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir) / "mock_repo"
        repo_path.mkdir()

        # Init git repo
        subprocess.run(
            ["git", "init"], cwd=str(repo_path), capture_output=True
        )
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=str(repo_path), capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=str(repo_path), capture_output=True,
        )

        # Create two Python files
        (repo_path / "main.py").write_text(
            'from helper import greet\n\ndef main():\n    greet("world")\n',
            encoding="utf-8",
        )
        (repo_path / "helper.py").write_text(
            'def greet(name):\n    print(f"Hello {name}")\n',
            encoding="utf-8",
        )

        subprocess.run(
            ["git", "add", "."], cwd=str(repo_path), capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "initial"],
            cwd=str(repo_path), capture_output=True,
        )

        # Second commit: modify helper.py
        (repo_path / "helper.py").write_text(
            'def greet(name):\n    print(f"Hi {name}!")\n',
            encoding="utf-8",
        )
        subprocess.run(
            ["git", "add", "."], cwd=str(repo_path), capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "update greeting"],
            cwd=str(repo_path), capture_output=True,
        )

        # Build graph
        from code_review_graph.graph import GraphStore
        from code_review_graph.incremental import full_build, get_db_path

        db_path = get_db_path(repo_path)
        store = GraphStore(db_path)
        full_build(repo_path, store)

        config = {
            "name": "mock",
            "language": "python",
            "test_commits": [
                {"sha": "HEAD", "description": "update greeting"},
            ],
            "entry_points": ["main.py::main"],
            "search_queries": [
                {"query": "greet", "expected": "helper.py::greet"},
            ],
        }

        # Run token_efficiency
        from code_review_graph.eval.benchmarks import token_efficiency
        te_results = token_efficiency.run(repo_path, store, config)
        assert len(te_results) >= 1
        assert "naive_tokens" in te_results[0]
        assert "graph_tokens" in te_results[0]

        # Run impact_accuracy
        from code_review_graph.eval.benchmarks import impact_accuracy
        ia_results = impact_accuracy.run(repo_path, store, config)
        assert len(ia_results) >= 1
        assert "precision" in ia_results[0]
        assert "f1" in ia_results[0]

        # Run search_quality
        from code_review_graph.eval.benchmarks import search_quality
        sq_results = search_quality.run(repo_path, store, config)
        assert len(sq_results) == 1
        assert "reciprocal_rank" in sq_results[0]

        # Run build_performance
        from code_review_graph.eval.benchmarks import build_performance
        bp_results = build_performance.run(repo_path, store, config)
        assert len(bp_results) == 1
        assert "node_count" in bp_results[0]
        assert bp_results[0]["node_count"] > 0

        store.close()


# --- Token benchmark tests ---


def test_estimate_tokens_basic():
    """estimate_tokens should return a reasonable approximation."""
    from code_review_graph.eval.token_benchmark import estimate_tokens

    # Simple string: "hello" => JSON '"hello"' (7 chars) => 7 // 4 = 1
    assert estimate_tokens("hello") == 1

    # Dict: {"a": 1} => '{"a": 1}' (8 chars) => 8 // 4 = 2
    assert estimate_tokens({"a": 1}) == 2

    # Longer content should scale proportionally
    long_text = "x" * 400
    tokens = estimate_tokens(long_text)
    # JSON adds 2 quote chars: (400 + 2) // 4 = 100
    assert tokens == 100


def test_estimate_tokens_nested():
    """estimate_tokens handles nested structures."""
    from code_review_graph.eval.token_benchmark import estimate_tokens

    nested = {"nodes": [{"name": "foo"}, {"name": "bar"}], "count": 2}
    tokens = estimate_tokens(nested)
    assert tokens > 0
    assert isinstance(tokens, int)


def test_estimate_tokens_non_serializable():
    """estimate_tokens uses default=str for non-serializable objects."""
    from pathlib import Path

    from code_review_graph.eval.token_benchmark import estimate_tokens

    # Path objects are not JSON-serializable but default=str handles them
    tokens = estimate_tokens({"path": Path("/tmp/test")})
    assert tokens > 0


def test_benchmark_review_workflow():
    """benchmark_review_workflow completes and returns expected structure."""
    from code_review_graph.eval.token_benchmark import benchmark_review_workflow

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir) / "bench_repo"
        repo_path.mkdir()

        # Init git repo with two commits
        subprocess.run(
            ["git", "init"], cwd=str(repo_path), capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=str(repo_path), capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=str(repo_path), capture_output=True,
        )

        (repo_path / "main.py").write_text(
            'from helper import greet\n\ndef main():\n    greet("world")\n',
            encoding="utf-8",
        )
        (repo_path / "helper.py").write_text(
            'def greet(name):\n    print(f"Hello {name}")\n',
            encoding="utf-8",
        )

        subprocess.run(
            ["git", "add", "."], cwd=str(repo_path), capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "initial"],
            cwd=str(repo_path), capture_output=True,
        )

        # Second commit
        (repo_path / "helper.py").write_text(
            'def greet(name):\n    print(f"Hi {name}!")\n',
            encoding="utf-8",
        )
        subprocess.run(
            ["git", "add", "."], cwd=str(repo_path), capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "update greeting"],
            cwd=str(repo_path), capture_output=True,
        )

        # Build graph
        from code_review_graph.graph import GraphStore
        from code_review_graph.incremental import full_build, get_db_path

        db_path = get_db_path(repo_path)
        store = GraphStore(db_path)
        full_build(repo_path, store)
        store.close()

        # Run the review benchmark
        result = benchmark_review_workflow(
            repo_root=str(repo_path), base="HEAD~1",
        )

        assert result["workflow"] == "review"
        assert result["total_tokens"] > 0
        assert result["tool_calls"] == 2
        assert len(result["calls"]) == 2
        assert result["calls"][0]["tool"] == "get_minimal_context"
        assert result["calls"][1]["tool"] == "detect_changes_minimal"
        for call in result["calls"]:
            assert call["tokens"] >= 0


def test_run_all_benchmarks():
    """run_all_benchmarks returns results for all workflows."""
    from code_review_graph.eval.token_benchmark import run_all_benchmarks

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir) / "all_bench_repo"
        repo_path.mkdir()

        subprocess.run(
            ["git", "init"], cwd=str(repo_path), capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=str(repo_path), capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=str(repo_path), capture_output=True,
        )

        (repo_path / "app.py").write_text(
            'def main():\n    print("hello")\n',
            encoding="utf-8",
        )

        subprocess.run(
            ["git", "add", "."], cwd=str(repo_path), capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "initial"],
            cwd=str(repo_path), capture_output=True,
        )

        (repo_path / "app.py").write_text(
            'def main():\n    print("hi")\n',
            encoding="utf-8",
        )
        subprocess.run(
            ["git", "add", "."], cwd=str(repo_path), capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "update"],
            cwd=str(repo_path), capture_output=True,
        )

        from code_review_graph.graph import GraphStore
        from code_review_graph.incremental import full_build, get_db_path

        db_path = get_db_path(repo_path)
        store = GraphStore(db_path)
        full_build(repo_path, store)
        store.close()

        results = run_all_benchmarks(repo_root=str(repo_path), base="HEAD~1")

        # Should have one result per workflow (5 total)
        assert len(results) == 5

        workflow_names = {r["workflow"] for r in results}
        assert workflow_names == {
            "review", "architecture", "debug", "onboard", "pre_merge",
        }

        # Each successful result should have total_tokens
        for r in results:
            if "error" not in r:
                assert r["total_tokens"] >= 0
                assert "calls" in r


# --- Failure-inflation regression tests + agent_baseline + co-change mode ---


def _git(repo_path, *args):
    subprocess.run(["git", *args], cwd=str(repo_path), capture_output=True)


def _make_repo(tmpdir, two_file_commit=False):
    """Tiny git repo: initial commit, then a second commit touching 1 or 2 files."""
    repo_path = Path(tmpdir) / "mock_repo"
    repo_path.mkdir()
    _git(repo_path, "init")
    _git(repo_path, "config", "user.email", "test@test.com")
    _git(repo_path, "config", "user.name", "Test")

    (repo_path / "main.py").write_text(
        'from helper import greet\n\ndef main():\n    greet("world")\n',
        encoding="utf-8",
    )
    (repo_path / "helper.py").write_text(
        'def greet(name):\n    print(f"Hello {name}")\n',
        encoding="utf-8",
    )
    _git(repo_path, "add", ".")
    _git(repo_path, "commit", "-m", "initial")

    (repo_path / "helper.py").write_text(
        'def greet(name):\n    print(f"Hi {name}!")\n',
        encoding="utf-8",
    )
    if two_file_commit:
        (repo_path / "main.py").write_text(
            'from helper import greet\n\ndef main():\n    greet("there")\n',
            encoding="utf-8",
        )
    _git(repo_path, "add", ".")
    _git(repo_path, "commit", "-m", "update greeting")
    return repo_path


def _build_store(repo_path):
    from code_review_graph.graph import GraphStore
    from code_review_graph.incremental import full_build, get_db_path

    store = GraphStore(get_db_path(repo_path))
    full_build(repo_path, store)
    return store


def _mock_config(**extra):
    config = {
        "name": "mock",
        "language": "python",
        "test_commits": [{"sha": "HEAD", "description": "update greeting"}],
        "entry_points": ["main.py::main"],
        "search_queries": [{"query": "greet", "expected": "helper.py::greet"}],
    }
    config.update(extra)
    return config


def test_token_efficiency_failure_marked_error_not_inflated(monkeypatch):
    """A thrown get_review_context must yield status=error, not ratio=naive/1."""
    from code_review_graph.eval.benchmarks import token_efficiency

    def _boom(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("code_review_graph.tools.get_review_context", _boom)

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = _make_repo(tmpdir)
        store = _build_store(repo_path)
        try:
            results = token_efficiency.run(repo_path, store, _mock_config())
        finally:
            store.close()

    assert len(results) >= 1
    for row in results:
        assert row["status"] == "error"
        assert "boom" in row["error"]
        # Failed measurements must not look like valid (inflated) ratios.
        assert row["graph_tokens"] == ""
        assert row["naive_to_graph_ratio"] == ""
        assert row["standard_to_graph_ratio"] == ""

    agg = token_efficiency.aggregate(results)
    assert agg["ok_rows"] == 0
    assert agg["error_rows"] == len(results)
    assert agg["median_naive_to_graph_ratio"] is None


def test_token_efficiency_success_rows_status_ok():
    from code_review_graph.eval.benchmarks import token_efficiency

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = _make_repo(tmpdir)
        store = _build_store(repo_path)
        try:
            results = token_efficiency.run(repo_path, store, _mock_config())
        finally:
            store.close()

    assert len(results) >= 1
    for row in results:
        assert row["status"] == "ok"
        assert row["error"] == ""
        assert isinstance(row["graph_tokens"], int)
        assert isinstance(row["naive_to_graph_ratio"], float)

    agg = token_efficiency.aggregate(results)
    assert agg["ok_rows"] == len(results)
    assert agg["error_rows"] == 0
    assert isinstance(agg["median_naive_to_graph_ratio"], float)


def test_impact_accuracy_failure_marked_error_not_perfect_recall(monkeypatch):
    """A thrown analyze_changes must not silently score recall 1.0."""
    from code_review_graph.eval.benchmarks import impact_accuracy

    def _boom(*args, **kwargs):
        raise RuntimeError("analysis exploded")

    monkeypatch.setattr("code_review_graph.changes.analyze_changes", _boom)

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = _make_repo(tmpdir, two_file_commit=True)
        store = _build_store(repo_path)
        try:
            results = impact_accuracy.run(repo_path, store, _mock_config())
        finally:
            store.close()

    assert len(results) >= 2  # both modes attempted, both failed
    for row in results:
        assert row["status"] == "error"
        assert "analysis exploded" in row["error"]
        assert row["recall"] == ""  # NOT 1.0
        assert row["precision"] == ""
        assert row["f1"] == ""

    agg = impact_accuracy.aggregate(results)
    assert agg["graph_derived"]["ok_rows"] == 0
    assert agg["co_change"]["ok_rows"] == 0
    assert agg["graph_derived"]["mean_recall"] is None
    assert agg["error_rows"] == len(results)


def test_impact_accuracy_emits_both_ground_truth_modes():
    from code_review_graph.eval.benchmarks import impact_accuracy

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = _make_repo(tmpdir, two_file_commit=True)
        store = _build_store(repo_path)
        try:
            results = impact_accuracy.run(repo_path, store, _mock_config())
        finally:
            store.close()

    modes = {r["ground_truth_mode"] for r in results}
    assert impact_accuracy.MODE_GRAPH_DERIVED in modes
    assert impact_accuracy.MODE_CO_CHANGE in modes

    graph_rows = [
        r for r in results
        if r["ground_truth_mode"] == impact_accuracy.MODE_GRAPH_DERIVED
    ]
    co_rows = [
        r for r in results
        if r["ground_truth_mode"] == impact_accuracy.MODE_CO_CHANGE
    ]

    for row in graph_rows:
        assert row["status"] == "ok"
        assert 0.0 <= row["recall"] <= 1.0
        assert row["seed_file"] == ""

    # Commit touched helper.py + main.py: seed is the sorted-first file and
    # the ground truth is the *other* co-changed file — independent of the graph.
    assert len(co_rows) == 1
    co = co_rows[0]
    assert co["status"] == "ok"
    assert co["seed_file"] == "helper.py"
    assert co["actual_files"] == 1
    assert 0.0 <= co["precision"] <= 1.0
    assert 0.0 <= co["recall"] <= 1.0


def test_impact_accuracy_co_change_skipped_for_single_file_commit():
    from code_review_graph.eval.benchmarks import impact_accuracy

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = _make_repo(tmpdir, two_file_commit=False)
        store = _build_store(repo_path)
        try:
            results = impact_accuracy.run(repo_path, store, _mock_config())
        finally:
            store.close()

    co_rows = [
        r for r in results
        if r["ground_truth_mode"] == impact_accuracy.MODE_CO_CHANGE
    ]
    assert len(co_rows) == 1
    assert co_rows[0]["status"] == "skipped"
    assert "co-changed" in co_rows[0]["error"]

    agg = impact_accuracy.aggregate(results)
    assert agg["skipped_rows"] == 1
    assert agg["co_change"]["ok_rows"] == 0


# --- agent_baseline benchmark ---


def test_derive_search_terms_extracts_identifiers_and_keywords():
    from code_review_graph.eval.benchmarks.agent_baseline import derive_search_terms

    terms = derive_search_terms("How does Client.request send an HTTP request?")
    assert "client.request" in terms
    assert "how" not in terms  # stopword
    assert "does" not in terms  # stopword
    assert all(t == t.lower() for t in terms)


def test_grep_rank_orders_by_match_count_and_takes_top_k():
    from code_review_graph.eval.benchmarks.agent_baseline import grep_rank

    with tempfile.TemporaryDirectory() as tmpdir:
        corpus = Path(tmpdir)
        (corpus / "a.py").write_text("greet()\ngreet()\ngreet()\n", encoding="utf-8")
        (corpus / "b.py").write_text("greet()\n", encoding="utf-8")
        (corpus / "c.py").write_text("nothing here\n", encoding="utf-8")
        (corpus / "d.txt").write_text("greet greet greet greet\n", encoding="utf-8")
        sub = corpus / "node_modules"
        sub.mkdir()
        (sub / "e.py").write_text("greet greet greet greet greet\n", encoding="utf-8")

        ranked = grep_rank(corpus, ["greet"], k=3)
        # d.txt (non-source ext) and node_modules/e.py (skipped dir) excluded
        assert ranked == [("a.py", 3), ("b.py", 1)]

        top1 = grep_rank(corpus, ["greet"], k=1)
        assert top1 == [("a.py", 3)]

        assert grep_rank(corpus, [], k=3) == []


def test_grep_rank_tie_breaks_on_path():
    from code_review_graph.eval.benchmarks.agent_baseline import grep_rank

    with tempfile.TemporaryDirectory() as tmpdir:
        corpus = Path(tmpdir)
        (corpus / "zz.py").write_text("token token\n", encoding="utf-8")
        (corpus / "aa.py").write_text("token token\n", encoding="utf-8")
        ranked = grep_rank(corpus, ["token"], k=2)
        assert ranked == [("aa.py", 2), ("zz.py", 2)]


def test_agent_baseline_run_with_mock_repo():
    from code_review_graph.eval.benchmarks import agent_baseline

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = _make_repo(tmpdir)
        store = _build_store(repo_path)
        config = _mock_config(
            agent_questions=["How does greet print a greeting"],
        )
        try:
            results = agent_baseline.run(repo_path, store, config)
        finally:
            store.close()

    assert len(results) == 1
    row = results[0]
    assert row["question"] == "How does greet print a greeting"
    assert "greet" in row["terms"]
    assert row["files_matched"] >= 1
    assert "helper.py" in row["top_files"]
    assert row["baseline_tokens"] > 0
    assert row["status"] in ("ok", "no_graph_results")
    if row["status"] == "ok":
        assert isinstance(row["baseline_to_graph_ratio"], float)


def test_agent_baseline_falls_back_to_search_queries():
    from code_review_graph.eval.benchmarks import agent_baseline

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = _make_repo(tmpdir)
        store = _build_store(repo_path)
        try:
            results = agent_baseline.run(repo_path, store, _mock_config())
        finally:
            store.close()

    assert len(results) == 1
    assert results[0]["question"] == "greet"


def test_agent_baseline_search_failure_marked_error(monkeypatch):
    from code_review_graph.eval.benchmarks import agent_baseline

    def _boom(*args, **kwargs):
        raise RuntimeError("search down")

    monkeypatch.setattr("code_review_graph.search.hybrid_search", _boom)

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = _make_repo(tmpdir)
        store = _build_store(repo_path)
        config = _mock_config(agent_questions=["How does greet work"])
        try:
            results = agent_baseline.run(repo_path, store, config)
        finally:
            store.close()

    assert len(results) == 1
    assert results[0]["status"] == "error"
    assert "search down" in results[0]["error"]
    assert results[0]["baseline_to_graph_ratio"] == ""

    agg = agent_baseline.aggregate(results)
    assert agg["ok_rows"] == 0
    assert agg["error_rows"] == 1
    assert agg["median_baseline_to_graph_ratio"] is None


def test_agent_baseline_aggregate_excludes_non_ok_rows():
    from code_review_graph.eval.benchmarks import agent_baseline

    rows = [
        {"status": "ok", "baseline_to_graph_ratio": 4.0},
        {"status": "ok", "baseline_to_graph_ratio": 8.0},
        {"status": "error", "baseline_to_graph_ratio": ""},
        {"status": "no_graph_results", "baseline_to_graph_ratio": ""},
    ]
    agg = agent_baseline.aggregate(rows)
    assert agg["total_rows"] == 4
    assert agg["ok_rows"] == 2
    assert agg["error_rows"] == 1
    assert agg["median_baseline_to_graph_ratio"] == 6.0


@pytest.mark.skipif(not _HAS_YAML, reason="pyyaml not installed")
def test_agent_baseline_registered_in_runner():
    from code_review_graph.eval.runner import BENCHMARK_REGISTRY

    assert "agent_baseline" in BENCHMARK_REGISTRY


def test_reporter_impact_f1_skips_error_and_co_change_rows():
    """Table B must aggregate only ok graph-derived rows."""
    with tempfile.TemporaryDirectory() as tmpdir:
        results_dir = Path(tmpdir)
        ia_path = results_dir / "mock_impact_accuracy_2026-01-01.csv"
        fieldnames = [
            "repo", "commit", "ground_truth_mode", "seed_file",
            "predicted_files", "actual_files", "true_positives",
            "precision", "recall", "f1", "status", "error",
        ]
        with open(ia_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerow({
                "repo": "mock", "commit": "abc",
                "ground_truth_mode": "graph-derived (circular — upper bound)",
                "seed_file": "", "predicted_files": "2", "actual_files": "2",
                "true_positives": "1", "precision": "0.5", "recall": "0.5",
                "f1": "0.5", "status": "ok", "error": "",
            })
            w.writerow({
                "repo": "mock", "commit": "def",
                "ground_truth_mode": "graph-derived (circular — upper bound)",
                "seed_file": "", "predicted_files": "", "actual_files": "",
                "true_positives": "", "precision": "", "recall": "",
                "f1": "", "status": "error", "error": "boom",
            })
            w.writerow({
                "repo": "mock", "commit": "abc",
                "ground_truth_mode": "co-change (same commit, seed excluded)",
                "seed_file": "a.py", "predicted_files": "1", "actual_files": "1",
                "true_positives": "1", "precision": "1.0", "recall": "1.0",
                "f1": "0.9", "status": "ok", "error": "",
            })

        tables = generate_readme_tables(results_dir)

    # 0.5 comes only from the single ok graph-derived row; the error row and
    # the co-change row (different metric) must not pollute the column.
    assert "0.5" in tables
    assert "0.9" not in tables
