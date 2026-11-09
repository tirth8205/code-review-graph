"""A person reviews and merges every promotion; no workflow may merge a pull request."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WORKFLOWS = sorted((Path(__file__).parent.parent / ".github" / "workflows").glob("*.yml"))

# The ways a workflow could merge a pull request or arrange for one to merge.
MERGE_CALLS = re.compile(
    r"gh\s+pr\s+merge|pulls/[^\s\"']*/merge|enablePullRequestAutoMerge|mergePullRequest"
)


def test_the_workflows_are_found():
    assert WORKFLOWS


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda path: path.name)
def test_no_workflow_merges_a_pull_request(workflow: Path):
    assert not MERGE_CALLS.search(workflow.read_text(encoding="utf-8"))


def test_no_workflow_promotes_on_a_schedule():
    """The daily job that merged `staging` into `testing` must not come back."""
    assert "auto-promote.yml" not in {path.name for path in WORKFLOWS}
