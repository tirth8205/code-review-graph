"""PyPI gets only code that reached ``main`` (crg-4eb.4).

``publish.yml`` runs on any published release, and a release can name a tag
on any branch. A tag cut from ``staging`` or a feature branch would ship code
no promotion ever reviewed. The workflow now checks the tagged commit is on
``origin/main`` before it builds anything.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

PUBLISH = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "publish.yml"
GATE = "Refuse a tag that is not on main"

needs_git_and_bash = pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("bash") is None or sys.platform == "win32",
    reason="needs git and bash",
)


def _steps() -> list[dict]:
    doc = yaml.safe_load(PUBLISH.read_text(encoding="utf-8"))
    return doc["jobs"]["publish"]["steps"]


def _names() -> list[str]:
    return [step.get("name", step.get("uses", "")) for step in _steps()]


def test_the_check_runs_before_the_build_and_the_upload():
    names = _names()
    assert GATE in names
    assert names.index(GATE) < names.index("Build package")
    assert names.index(GATE) < names.index("Publish to PyPI")


def test_the_checkout_has_the_history_the_check_needs():
    checkout = next(s for s in _steps() if str(s.get("uses", "")).startswith("actions/checkout"))
    assert checkout.get("with", {}).get("fetch-depth") == 0


def _git(cwd: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture()
def clone(tmp_path: Path) -> Path:
    """A clone of an origin whose ``main`` has one commit and ``staging`` one more."""
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    (work / "a.txt").write_text("a\n", encoding="utf-8")
    _git(work, "add", "a.txt")
    _git(work, "commit", "-q", "-m", "on main")
    _git(work, "tag", "-a", "v1.0.0", "-m", "v1.0.0")
    _git(work, "checkout", "-q", "-b", "staging")
    (work / "b.txt").write_text("b\n", encoding="utf-8")
    _git(work, "add", "b.txt")
    _git(work, "commit", "-q", "-m", "only on staging")
    _git(work, "tag", "-a", "v1.1.0", "-m", "v1.1.0")
    origin = tmp_path / "origin.git"
    _git(tmp_path, "clone", "-q", "--bare", str(work), str(origin))
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    return clone


def _run_gate(clone: Path, tag: str) -> subprocess.CompletedProcess[str]:
    _git(clone, "checkout", "-q", "--detach", tag)
    script = next(s for s in _steps() if s.get("name") == GATE)["run"]
    return subprocess.run(
        ["bash", "-c", script], cwd=clone, env={**os.environ, "TAG": tag},
        capture_output=True, text=True, check=False,
    )


@needs_git_and_bash
def test_a_tag_on_main_is_published(clone: Path):
    result = _run_gate(clone, "v1.0.0")
    assert result.returncode == 0, result.stdout + result.stderr


@needs_git_and_bash
def test_a_tag_that_is_not_on_main_is_refused(clone: Path):
    result = _run_gate(clone, "v1.1.0")
    assert result.returncode != 0
    assert "v1.1.0" in result.stdout + result.stderr
    assert "main" in result.stdout + result.stderr
