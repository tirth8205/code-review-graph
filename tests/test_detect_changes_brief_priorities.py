"""``detect-changes --brief`` says where to look first, not only how much changed.

The brief output used to be the summary's counts and a savings panel. It now
names the top three review priorities, each with its file and line, its risk
out of 1.00 with a level, who calls it and which tests reach it, and says the
overall score is out of 1.00 too.

Found by the developer-experience review of 2.4.0 (product item 9).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from code_review_graph.changes import analyze_changes, format_brief, risk_label
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, timeout=30,
    )


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _write(repo, ".gitignore", ".code-review-graph/\n")
    _write(repo, "app/__init__.py", "")
    _write(
        repo, "app/auth.py",
        "def login(user):\n    return check(user)\n\n\n"
        "def check(user):\n    return bool(user)\n",
    )
    _write(
        repo, "app/handlers.py",
        "from app.auth import login\n\n\ndef handle_login(user):\n    return login(user)\n",
    )
    _write(
        repo, "tests/test_auth.py",
        "from app.auth import login\n\n\ndef test_login_ok():\n    assert login('x')\n",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "first")
    _write(
        repo, "app/auth.py",
        "def login(user):\n    return check(user) and user != 'root'\n\n\n"
        "def check(user):\n    return bool(user)\n",
    )
    _git(repo, "commit", "-q", "-am", "refuse root")
    return repo


def test_brief_names_the_top_priorities(repo: Path, tmp_path: Path) -> None:
    with GraphStore(tmp_path / "graph.db") as store:
        full_build(repo, store)
        analysis = analyze_changes(store, ["app/auth.py"], repo_root=str(repo.resolve()))
        text = format_brief(store, analysis, repo.resolve())

    score = analysis["risk_score"]
    assert f"Overall risk score: {score:.2f} of 1.00 ({risk_label(score)})" in text
    assert "Review first (risk out of 1.00):" in text
    priority = next(line for line in text.splitlines() if line.startswith("  1. "))
    assert "login  app/auth.py:1  risk " in priority
    assert "called by handle_login  tested by test_login_ok" in priority


def test_brief_without_priorities_is_the_summary(tmp_path: Path) -> None:
    with GraphStore(tmp_path / "graph.db") as store:
        text = format_brief(store, {"summary": "summary only"})

    assert text == "summary only"


def test_brief_lists_at_most_three(tmp_path: Path) -> None:
    priorities = [
        {"name": f"f{i}", "qualified_name": "", "file_path": "/r/m.py",
         "line_start": i, "risk_score": 0.1}
        for i in range(1, 6)
    ]
    with GraphStore(tmp_path / "graph.db") as store:
        text = format_brief(
            store,
            {"summary": "  - Overall risk score: 0.10", "risk_score": 0.1,
             "review_priorities": priorities},
            "/r",
        )

    assert "  3. f3  m.py:3  risk 0.10 (low)  no callers found  no tests found" in text
    assert "  4. " not in text
    assert "Overall risk score: 0.10 of 1.00 (low)" in text


@pytest.mark.parametrize(
    "score,label", [(0.0, "low"), (0.4, "low"), (0.41, "medium"), (0.7, "medium"), (0.71, "high")],
)
def test_risk_label(score: float, label: str) -> None:
    assert risk_label(score) == label


def test_cli_brief_shows_priorities(repo: Path) -> None:
    env = {k: v for k, v in os.environ.items() if k not in ("CRG_DATA_DIR", "CRG_REPO_ROOT")}

    def cli(*argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "code_review_graph", *argv],
            cwd=str(repo), capture_output=True, text=True, timeout=120, env=env,
            stdin=subprocess.DEVNULL,
        )

    assert cli("build", "-q").returncode == 0
    result = cli("detect-changes", "--brief")

    assert result.returncode == 0, result
    assert "Review first (risk out of 1.00):" in result.stdout
    assert "  1. login  app/auth.py:1  risk " in result.stdout
    assert "of 1.00" in result.stdout
