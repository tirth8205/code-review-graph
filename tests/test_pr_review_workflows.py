"""Static security regressions for the split fork-PR review workflows."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]
ANALYSIS_WORKFLOW = ROOT / ".github" / "workflows" / "pr-review.yml"
COMMENT_WORKFLOW = ROOT / ".github" / "workflows" / "pr-review-comment.yml"
ACTION = ROOT / "action.yml"
DOCS = ROOT / "docs" / "GITHUB_ACTION.md"


def test_analysis_workflow_is_unprivileged_and_exports_temp_artifact():
    workflow = ANALYSIS_WORKFLOW.read_text(encoding="utf-8")

    assert "permissions:\n  contents: read\n" in workflow
    assert "pull-requests: write" not in workflow
    assert 'comment: "false"' in workflow
    assert "id: review" in workflow
    assert "steps.review.outputs.comment-file" in workflow
    assert "${{ runner.temp }}/crg-report" in workflow
    assert "actions/upload-artifact@v7" in workflow
    assert "if-no-files-found: error" in workflow
    assert "retention-days: 1" in workflow


def test_action_exposes_the_rendered_comment_file():
    action = ACTION.read_text(encoding="utf-8")

    assert "outputs:" in action
    assert "comment-file:" in action
    assert "value: ${{ steps.render.outputs.comment-file }}" in action
    assert "id: render" in action
    assert 'echo "comment-file=${RUNNER_TEMP}/crg-comment.md" >> "${GITHUB_OUTPUT}"' in action


def test_privileged_workflow_has_minimal_permissions_and_source_gate():
    workflow = COMMENT_WORKFLOW.read_text(encoding="utf-8")

    assert "actions: read" in workflow
    assert "pull-requests: write" in workflow
    assert "workflow_run.conclusion == 'success'" in workflow
    assert "workflow_run.event == 'pull_request'" in workflow
    assert "actions/checkout" not in workflow
    assert "uses: ./" not in workflow


def test_privileged_workflow_confines_and_validates_untrusted_artifact():
    workflow = COMMENT_WORKFLOW.read_text(encoding="utf-8")

    assert "MAX_ARCHIVE_BYTES" in workflow
    assert "size_in_bytes" in workflow
    assert "artifact-ids: ${{ steps.artifact.outputs.artifact-id }}" in workflow
    assert "path: ${{ runner.temp }}/crg-report-download" in workflow
    assert "actions/download-artifact@v8" in workflow
    assert "MAX_REPORT_BYTES" in workflow
    assert "MAX_PR_NUMBER_BYTES" in workflow
    assert 'decode("utf-8")' in workflow
    assert "fullmatch" in workflow
    assert "is_symlink" in workflow
    assert "workflow_run.head_sha" in workflow
    assert "actual_sha" in workflow


def test_privileged_workflow_adds_its_own_marker_before_posting():
    workflow = COMMENT_WORKFLOW.read_text(encoding="utf-8")

    assert "TRUSTED_MARKER: <!-- code-review-graph-report -->" in workflow
    assert 'text.replace(marker, "")' in workflow
    assert 'body = f"{marker}\\n\\n{text}"' in workflow
    assert '-F body=@"${COMMENT_BODY}"' in workflow
    assert "-F body=@crg-comment.md" not in workflow


def test_docs_recommend_the_split_workflow_instead_of_pull_request_target():
    docs = DOCS.read_text(encoding="utf-8")

    assert "pr-review-comment.yml" in docs
    assert "workflow_run" in docs
    assert "`actions: read`" in docs
    assert "default branch" in docs
    assert "Avoid `pull_request_target`" in docs


def _upsert_script() -> str:
    doc = yaml.safe_load(COMMENT_WORKFLOW.read_text(encoding="utf-8"))
    steps = doc["jobs"]["comment"]["steps"]
    return next(s for s in steps if s.get("name") == "Upsert sticky PR comment")["run"]


def _run_upsert(tmp_path: Path, *, analysed: str, head: str) -> tuple[
    subprocess.CompletedProcess[str], str
]:
    """Run the comment step with a stub ``gh`` whose PR head is *head*."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "gh-calls.log"
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        'echo "$*" >> "${GH_CALLS}"\n'
        'if [[ "$*" == *"/pulls/"* ]]; then echo "${STUB_HEAD}"; fi\n',
        encoding="utf-8",
    )
    gh.chmod(0o755)
    pr_file = tmp_path / "pr.txt"
    pr_file.write_text("7", encoding="ascii")
    body = tmp_path / "body.md"
    body.write_text("report", encoding="utf-8")
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "GH_CALLS": str(calls),
        "STUB_HEAD": head,
        "COMMENT_BODY": str(body),
        "GH_TOKEN": "unused",
        "HEAD_SHA": analysed,
        "PR_NUMBER_FILE": str(pr_file),
        "GITHUB_REPOSITORY": "owner/repo",
    }
    result = subprocess.run(
        ["bash", "-c", _upsert_script()],
        env=env, capture_output=True, text=True, check=False,
    )
    return result, calls.read_text(encoding="utf-8") if calls.exists() else ""


_needs_bash = pytest.mark.skipif(
    shutil.which("bash") is None or sys.platform == "win32",
    reason="needs bash and a POSIX stub for gh",
)


@_needs_bash
def test_a_head_that_moved_after_the_analysis_is_a_notice_not_a_failure(tmp_path):
    """The PR moved while its analysis ran. Declining to comment is right, but
    a red run for an expected race teaches people to ignore red runs, so the
    step says which commit it analysed and which is the head, and passes
    (crg-3ov)."""
    result, calls = _run_upsert(tmp_path, analysed="a" * 40, head="b" * 40)

    assert result.returncode == 0, result.stderr
    assert "::notice" in result.stdout
    assert "a" * 40 in result.stdout
    assert "b" * 40 in result.stdout
    assert "--method" not in calls, "it must not post or edit a comment"


@_needs_bash
def test_the_analysed_head_still_gets_its_comment(tmp_path):
    result, calls = _run_upsert(tmp_path, analysed="c" * 40, head="c" * 40)

    assert result.returncode == 0, result.stderr
    assert "--method POST" in calls
