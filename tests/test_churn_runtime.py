"""The churn term for MCP callers, and the git cost that comes with it.

``compute_risk_score`` has a change-frequency term worth up to 0.15, but
``include_churn`` defaulted to False and the only caller that passed True was
``cli.py``. Every agent-driven review therefore scored risk with that term
pinned at zero.

Turning it on for the MCP tools puts ``git log --since --numstat`` inside a
tool call an agent is waiting on, so these tests pin the operational
contract: the result is cached per commit, the history walk is bounded, and a
slow or failing git degrades to the pre-change behaviour instead of hanging.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from code_review_graph import changes as changes_mod
from code_review_graph.changes import (
    clear_churn_cache,
    compute_file_churn,
    compute_file_churn_with_status,
)
from code_review_graph.graph import GraphStore, NodeInfo
from code_review_graph.tools.context import get_minimal_context
from code_review_graph.tools.review import detect_changes_func


@pytest.fixture(autouse=True)
def _clean_cache():
    clear_churn_cache()
    yield
    clear_churn_cache()


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "git",
            "-c", "user.email=test@example.com",
            "-c", "user.name=Test",
            "-c", "commit.gpgsign=false",
            *args,
        ],
        capture_output=True,
        check=True,
        cwd=repo,
        stdin=subprocess.DEVNULL,
        text=True,
        timeout=20,
    )


def _repo_with_commits(root: Path, commits: int = 2) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    app = root / "app.py"
    for i in range(commits):
        app.write_text(f"a = {i}\n", encoding="utf-8")
        _git(root, "add", "app.py")
        _git(root, "commit", "-q", "-m", f"c{i}")
    return root


def _seeded_graph(root: Path) -> None:
    (root / ".code-review-graph").mkdir(parents=True, exist_ok=True)
    with GraphStore(root / ".code-review-graph" / "graph.db") as store:
        store.upsert_node(NodeInfo(
            kind="Function",
            name="handler",
            file_path=str(root / "app.py"),
            line_start=1,
            line_end=2,
            language="python",
        ))
        store.commit()


class TestChurnCache:
    def test_second_call_for_the_same_commit_skips_git_log(self, tmp_path):
        repo = _repo_with_commits(tmp_path / "repo")

        first = compute_file_churn(str(repo))
        assert first == {"app.py": 2}

        with patch(
            "code_review_graph.changes.subprocess.run",
            wraps=subprocess.run,
        ) as run:
            second = compute_file_churn(str(repo))
        assert second == first
        log_calls = [c for c in run.call_args_list if "log" in c.args[0]]
        assert log_calls == [], "a cached commit must not re-walk history"

    def test_a_new_commit_invalidates_the_cache(self, tmp_path):
        repo = _repo_with_commits(tmp_path / "repo")
        assert compute_file_churn(str(repo)) == {"app.py": 2}

        (repo / "util.py").write_text("b = 1\n", encoding="utf-8")
        _git(repo, "add", "util.py")
        _git(repo, "commit", "-q", "-m", "add util")

        assert compute_file_churn(str(repo)) == {"app.py": 2, "util.py": 1}

    def test_a_failed_lookup_is_cached_so_the_next_call_is_cheap(self, tmp_path):
        repo = _repo_with_commits(tmp_path / "repo")
        real_run = subprocess.run

        def slow_log(cmd, *args, **kwargs):
            if "log" in cmd:
                raise subprocess.TimeoutExpired(cmd, 5)
            return real_run(cmd, *args, **kwargs)

        with patch(
            "code_review_graph.changes.subprocess.run", side_effect=slow_log,
        ):
            assert compute_file_churn(str(repo)) == {}

        with patch(
            "code_review_graph.changes.subprocess.run", wraps=real_run,
        ) as run:
            assert compute_file_churn(str(repo)) == {}
        assert [c for c in run.call_args_list if "log" in c.args[0]] == []

    def test_a_slow_repository_pays_the_timeout_once_not_once_per_call(
        self, tmp_path,
    ):
        """The case the cache was claimed to cover, and did not.

        Caching failures by HEAD commit only helps when ``git rev-parse``
        still answers. On a repository slow enough to trip the churn timeout
        the rev-parse that produces the key times out too, so nothing was
        ever cached and every call paid two timeouts.
        """
        repo = _repo_with_commits(tmp_path / "repo")
        calls: list[list[str]] = []

        def everything_is_slow(cmd, *args, **kwargs):
            calls.append(list(cmd))
            raise subprocess.TimeoutExpired(cmd, 5)

        with patch(
            "code_review_graph.changes.subprocess.run",
            side_effect=everything_is_slow,
        ):
            for _ in range(5):
                assert compute_file_churn(str(repo)) == {}

        assert len(calls) == 1, (
            f"five calls span {len(calls)} git subprocesses; a slow "
            "repository must stop paying after the first"
        )
        assert [c for c in calls if "log" in c] == []

    def test_the_failure_cache_is_cleared_with_the_result_cache(self, tmp_path):
        repo = _repo_with_commits(tmp_path / "repo")
        with patch(
            "code_review_graph.changes.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["git"], 5),
        ):
            assert compute_file_churn(str(repo)) == {}

        clear_churn_cache()
        assert compute_file_churn(str(repo)) == {"app.py": 2}

    def test_a_repository_without_commits_is_not_retried(self, tmp_path):
        repo = tmp_path / "empty"
        repo.mkdir()
        _git(repo, "init", "-q")

        assert compute_file_churn(str(repo)) == {}
        with patch(
            "code_review_graph.changes.subprocess.run",
            wraps=subprocess.run,
        ) as run:
            assert compute_file_churn(str(repo)) == {}
        assert run.call_args_list == []


class TestChurnDegradationIsVisible:
    """A degraded risk score has to say so, not just log it."""

    def test_status_distinguishes_ok_from_unavailable_from_off(self, tmp_path):
        repo = _repo_with_commits(tmp_path / "repo")
        counts, status = compute_file_churn_with_status(str(repo))
        assert (counts, status) == ({"app.py": 2}, "ok")

        assert compute_file_churn_with_status(str(repo), window_days=0) == (
            {}, "off",
        )

        clear_churn_cache()
        with patch(
            "code_review_graph.changes.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["git"], 5),
        ):
            assert compute_file_churn_with_status(str(repo)) == (
                {}, "unavailable",
            )

    def test_detect_changes_reports_a_healthy_churn_lookup(self, tmp_path):
        repo = _repo_with_commits(tmp_path / "repo")
        _seeded_graph(repo)
        result = detect_changes_func(
            repo_root=str(repo), changed_files=["app.py"],
        )
        assert result["status"] == "ok"
        assert result["churn_status"] == "ok"
        assert "Degraded" not in result["summary"]

    def test_detect_changes_says_when_the_churn_term_is_missing(self, tmp_path):
        repo = _repo_with_commits(tmp_path / "repo")
        _seeded_graph(repo)
        with patch(
            "code_review_graph.changes.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["git"], 5),
        ):
            result = detect_changes_func(
                repo_root=str(repo), changed_files=["app.py"],
            )
        assert result["status"] == "ok"
        assert result["churn_status"] == "unavailable"
        assert "change-frequency risk unavailable" in result["summary"]

    def test_minimal_detail_still_carries_the_degradation(self, tmp_path):
        repo = _repo_with_commits(tmp_path / "repo")
        _seeded_graph(repo)
        with patch(
            "code_review_graph.changes.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["git"], 5),
        ):
            result = detect_changes_func(
                repo_root=str(repo), changed_files=["app.py"],
                detail_level="minimal",
            )
        assert result["churn_status"] == "unavailable"

    def test_minimal_context_says_when_risk_excludes_churn(self, tmp_path):
        repo = _repo_with_commits(tmp_path / "repo")
        _seeded_graph(repo)
        with patch(
            "code_review_graph.changes.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["git"], 5),
        ):
            result = get_minimal_context(
                task="review the pull request",
                changed_files=["app.py"],
                repo_root=str(repo),
            )
        assert result["status"] == "ok"
        assert "risk excludes churn" in result["summary"]


class TestChurnDegradesGracefully:
    def test_timeout_returns_empty_instead_of_raising(self, tmp_path):
        repo = _repo_with_commits(tmp_path / "repo")
        with patch(
            "code_review_graph.changes.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["git"], 5),
        ):
            assert compute_file_churn(str(repo)) == {}

    def test_history_walk_is_bounded(self, tmp_path):
        repo = _repo_with_commits(tmp_path / "repo", commits=1)
        with patch(
            "code_review_graph.changes.subprocess.run",
            wraps=subprocess.run,
        ) as run:
            compute_file_churn(str(repo))
        log_calls = [c for c in run.call_args_list if "log" in c.args[0]]
        assert log_calls, "expected one git log call"
        command = log_calls[0].args[0]
        assert f"--max-count={changes_mod._CHURN_MAX_COMMITS}" in command

    def test_churn_uses_its_own_short_timeout(self, tmp_path):
        repo = _repo_with_commits(tmp_path / "repo", commits=1)
        with patch(
            "code_review_graph.changes.subprocess.run",
            wraps=subprocess.run,
        ) as run:
            compute_file_churn(str(repo))
        log_calls = [c for c in run.call_args_list if "log" in c.args[0]]
        assert log_calls[0].kwargs["timeout"] == changes_mod._CHURN_TIMEOUT
        assert changes_mod._CHURN_TIMEOUT < changes_mod._GIT_TIMEOUT


class TestMcpCallersEnableChurn:
    def test_detect_changes_asks_for_churn(self, tmp_path):
        repo = _repo_with_commits(tmp_path / "repo")
        _seeded_graph(repo)
        with patch(
            "code_review_graph.tools.review.analyze_changes",
            return_value={
                "summary": "", "risk_score": 0.0, "changed_functions": [],
                "affected_flows": [], "test_gaps": [], "review_priorities": [],
            },
        ) as analyze:
            result = detect_changes_func(
                repo_root=str(repo), changed_files=["app.py"],
            )
        assert result["status"] == "ok"
        assert analyze.call_args.kwargs["include_churn"] is True

    def test_minimal_context_asks_for_churn(self, tmp_path):
        repo = _repo_with_commits(tmp_path / "repo")
        _seeded_graph(repo)
        with patch(
            "code_review_graph.changes.analyze_changes",
            return_value={
                "risk_score": 0.0, "changed_functions": [], "test_gaps": [],
            },
        ) as analyze:
            result = get_minimal_context(
                task="review the pull request",
                changed_files=["app.py"],
                repo_root=str(repo),
            )
        assert result["status"] == "ok"
        assert analyze.call_args.kwargs["include_churn"] is True

    def test_churn_failure_does_not_fail_the_tool_call(self, tmp_path):
        repo = _repo_with_commits(tmp_path / "repo")
        _seeded_graph(repo)
        with patch(
            "code_review_graph.changes.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["git"], 5),
        ):
            result = detect_changes_func(
                repo_root=str(repo), changed_files=["app.py"],
            )
        assert result["status"] == "ok"
