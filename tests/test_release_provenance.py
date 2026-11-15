"""Fork release assets must come from the tagged dc-use package revision."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

PUBLISH = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release-assets.yml"
GATE = "Verify dc-use release provenance"

needs_git_and_bash = pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("bash") is None or sys.platform == "win32",
    reason="needs git and bash",
)


def _steps() -> list[dict]:
    doc = yaml.safe_load(PUBLISH.read_text(encoding="utf-8"))
    return doc["jobs"]["assets"]["steps"]


def _names() -> list[str]:
    return [step.get("name", step.get("uses", "")) for step in _steps()]


def test_the_check_runs_before_the_build_and_the_upload():
    names = _names()
    assert GATE in names
    assert names.index(GATE) < names.index("Build package")
    assert names.index(GATE) < names.index("Upload release assets")


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
    """A dc-use release and a newer commit that has not reached integration."""
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "dc-use")
    (work / "a.txt").write_text("a\n", encoding="utf-8")
    (work / "pyproject.toml").write_text(
        '[project]\nname = "fixture"\nversion = "1.0.0+dc.1"\n', encoding="utf-8"
    )
    _git(work, "add", "a.txt", "pyproject.toml")
    _git(work, "commit", "-q", "-m", "on dc-use")
    for tag in ("v1.0.0-dc.1", "v1.0.0", "v1.0.0-dc.2"):
        _git(work, "tag", "-a", tag, "-m", tag)
    _git(work, "checkout", "-q", "-b", "staging")
    (work / "b.txt").write_text("b\n", encoding="utf-8")
    _git(work, "add", "b.txt")
    _git(work, "commit", "-q", "-m", "only on staging")
    _git(work, "tag", "-a", "v1.1.0-dc.1", "-m", "v1.1.0-dc.1")
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
def test_a_matching_tag_on_dc_use_is_published(clone: Path):
    result = _run_gate(clone, "v1.0.0-dc.1")
    assert result.returncode == 0, result.stdout + result.stderr


@needs_git_and_bash
def test_a_tag_that_is_not_on_dc_use_is_refused(clone: Path):
    result = _run_gate(clone, "v1.1.0-dc.1")
    assert result.returncode != 0
    assert "v1.1.0-dc.1" in result.stdout + result.stderr
    assert "dc-use" in result.stdout + result.stderr


@needs_git_and_bash
@pytest.mark.parametrize("tag", ["v1.0.0", "v1.0.0-dc.2"])
def test_package_version_must_match_the_fork_tag(clone: Path, tag: str):
    result = _run_gate(clone, tag)
    assert result.returncode != 0
    assert "does not match dc package" in result.stdout + result.stderr


@needs_git_and_bash
def test_checkout_must_match_the_release_tag(clone: Path):
    _git(clone, "checkout", "-q", "--detach", "v1.1.0-dc.1")
    script = next(s for s in _steps() if s.get("name") == GATE)["run"]
    result = subprocess.run(
        ["bash", "-c", script], cwd=clone,
        env={**os.environ, "TAG": "v1.0.0-dc.1"},
        capture_output=True, text=True, check=False,
    )
    assert result.returncode != 0
    assert "checked-out commit" in result.stdout + result.stderr
