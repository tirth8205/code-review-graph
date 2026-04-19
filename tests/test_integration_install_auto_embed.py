"""Integration tests for `code-review-graph install --auto-embed-hook`.

Runs the CLI via `python -m code_review_graph` in a real subprocess
against a tmp_path git repo, then asserts the resulting
``.claude/settings.json`` matches the schema the feature promises.

These tests cost ~1–2 s each but exercise the whole argparse→install
path, catching regressions that pure unit tests against
``generate_auto_embed_hook_entry`` cannot (wiring, import order,
subprocess entry point).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _run_install(tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable, "-m", "code_review_graph", "install",
            *args,
            "--platform", "claude",
            "--repo", str(tmp_path),
            "-y",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )


def test_install_auto_embed_hook_end_to_end(tmp_path: Path) -> None:
    """install --auto-embed-hook writes a compliant PostToolUse entry."""
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    result = _run_install(tmp_path, "--auto-embed-hook")
    assert result.returncode == 0, f"stderr: {result.stderr}"

    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    entries = settings["hooks"]["PostToolUse"]
    # Match on the literal CLI token `code-review-graph embed`, NOT just
    # `embed` — tmp_path names in pytest may contain the word "embed"
    # (e.g. test_install_auto_embed_hook_*) and would collide with the
    # standalone update hook whose command contains that path.
    embed_entry = next(
        e for e in entries
        if any("code-review-graph embed" in h["command"] for h in e.get("hooks", []))
    )
    inner = embed_entry["hooks"][0]

    # Schema: Claude Code Jan 2026 async field, NOT run_in_background.
    assert inner["async"] is True
    assert "run_in_background" not in inner

    # MCP stdio hygiene: both update and embed redirect stdout+stderr.
    assert inner["command"].count(">/dev/null 2>&1") >= 2

    # Exit-code guard.
    assert inner["command"].rstrip().endswith("|| true")

    # Body-mode lockout: env var must appear in command.
    assert "CRG_EMBED_INCLUDE_BODY=0" in inner["command"]

    # repo_arg is baked in at install time (not $CLAUDE_PROJECT_DIR).
    assert "$CLAUDE_PROJECT_DIR" not in inner["command"]
    assert str(tmp_path.resolve()) in inner["command"]

    # Matcher alignment (Q2 decision).
    assert embed_entry["matcher"] == "Edit|Write|MultiEdit"


def test_install_then_remove_auto_embed_hook(tmp_path: Path) -> None:
    """install --auto-embed-hook → install --no-auto-embed-hook round-trip.

    The update entry must survive; the embed entry must be gone.
    """
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)

    install_result = _run_install(tmp_path, "--auto-embed-hook")
    assert install_result.returncode == 0, f"stderr: {install_result.stderr}"

    remove_result = _run_install(tmp_path, "--no-auto-embed-hook")
    assert remove_result.returncode == 0, f"stderr: {remove_result.stderr}"

    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    commands = [
        h["command"]
        for entry in settings["hooks"]["PostToolUse"]
        for h in entry.get("hooks", [])
    ]
    # Update entry (exists — original install_hooks behaviour).
    assert any("update --skip-flows" in c and "embed" not in c for c in commands)
    # Embed entry (gone — remove_auto_embed_hook_entry worked).
    assert not any("code-review-graph embed" in c for c in commands)
