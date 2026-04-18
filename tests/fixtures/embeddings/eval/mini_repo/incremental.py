"""Mini git change detection for MRR eval fixture."""

import subprocess


def detect_changes(repo_root, since_ref):
    """Return list of files changed in repo since the given git ref."""
    out = subprocess.run(
        ["git", "-C", repo_root, "diff", "--name-only", since_ref, "HEAD"],
        capture_output=True, text=True, check=False,
    )
    return [line for line in out.stdout.splitlines() if line.strip()]


def get_changed_files(repo_root):
    return detect_changes(repo_root, "HEAD~1")
