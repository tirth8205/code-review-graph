"""Tests for CLI helpers."""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from importlib.metadata import PackageNotFoundError

import pytest

from code_review_graph import cli


def test_get_version_logs_and_falls_back_to_dev(monkeypatch, caplog):
    def _raise_package_not_found(_dist_name: str) -> str:
        raise PackageNotFoundError("code-review-graph")

    monkeypatch.setattr(cli, "pkg_version", _raise_package_not_found)

    with caplog.at_level(logging.DEBUG, logger="code_review_graph.cli"):
        version = cli._get_version()

    assert version == "dev"
    assert "Package metadata unavailable" in caplog.text


# ---------------------------------------------------------------------------
# --auto-embed-hook / --no-auto-embed-hook tests
# ---------------------------------------------------------------------------


def _run_cli(*args: str, cwd: str | None = None) -> subprocess.CompletedProcess:
    """Run the CLI via `python -m code_review_graph` with the given args."""
    return subprocess.run(
        [sys.executable, "-m", "code_review_graph", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )


def test_install_parser_accepts_auto_embed_hook(tmp_path):
    result = _run_cli(
        "install",
        "--auto-embed-hook",
        "--dry-run",
        "--platform", "claude",
        "--repo", str(tmp_path),
        "-y",
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"


def test_install_parser_rejects_both_flags(tmp_path):
    """Mutually exclusive group: argparse must reject both flags together."""
    result = _run_cli(
        "install",
        "--auto-embed-hook",
        "--no-auto-embed-hook",
        "--dry-run",
        "--platform", "claude",
        "--repo", str(tmp_path),
    )
    assert result.returncode == 2, f"argparse should exit 2 on mutex violation"
    assert "not allowed with argument" in result.stderr or "mutually exclusive" in result.stderr


def test_init_alias_accepts_auto_embed_hook(tmp_path):
    """`init` is an alias for `install`; mutex flags must be symmetric."""
    result = _run_cli(
        "init",
        "--auto-embed-hook",
        "--dry-run",
        "--platform", "claude",
        "--repo", str(tmp_path),
        "-y",
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"


def test_dry_run_with_auto_embed_hook_writes_nothing(tmp_path):
    """--dry-run must not write .claude/settings.json even with --auto-embed-hook."""
    result = _run_cli(
        "install",
        "--auto-embed-hook",
        "--dry-run",
        "--platform", "claude",
        "--repo", str(tmp_path),
        "-y",
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert "[dry-run]" in result.stdout
    assert not (tmp_path / ".claude" / "settings.json").exists()


def _build_install_args(tmp_path, **overrides) -> argparse.Namespace:
    """Minimal args Namespace matching the install subparser defaults."""
    base = dict(
        repo=str(tmp_path),
        dry_run=False,
        no_skills=True,       # skip expensive skill generation in unit tests
        no_hooks=False,
        no_instructions=True,
        yes=True,
        skills=False,
        hooks=False,
        install_all=False,
        platform="claude",
        auto_embed_hook=False,
        no_auto_embed_hook=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def test_auto_embed_hook_warns_when_extras_missing(tmp_path, monkeypatch, capsys):
    """When --auto-embed-hook is set and sentence_transformers is missing,
    _handle_init must print a warning to stderr (non-blocking).
    """
    import importlib.util

    real_find_spec = importlib.util.find_spec

    def fake_find_spec(name: str, *a, **kw):
        if name == "sentence_transformers":
            return None
        return real_find_spec(name, *a, **kw)

    monkeypatch.setattr("importlib.util.find_spec", fake_find_spec)
    # Default local provider (no CRG_EMBED_PROVIDER set).
    monkeypatch.delenv("CRG_EMBED_PROVIDER", raising=False)

    args = _build_install_args(tmp_path, auto_embed_hook=True)
    (tmp_path / ".git").mkdir()  # satisfy git-repo guard in git_hook
    cli._handle_init(args)

    captured = capsys.readouterr()
    assert "sentence-transformers" in captured.err
    assert "pip install code-review-graph[embeddings]" in captured.err


def test_auto_embed_hook_with_cursor_platform_warns(tmp_path, monkeypatch, capsys):
    """--platform cursor + --auto-embed-hook → stderr warning, no hook written."""
    # Mock find_spec so extras-missing warning doesn't confuse assertion.
    monkeypatch.setattr(
        "importlib.util.find_spec", lambda name, *a, **kw: object(),
    )
    args = _build_install_args(tmp_path, platform="cursor", auto_embed_hook=True)
    (tmp_path / ".git").mkdir()
    cli._handle_init(args)

    captured = capsys.readouterr()
    assert "only applies to claude/qoder" in captured.err
    # Cursor platform uses .cursor/mcp.json, not .claude/settings.json.
    assert not (tmp_path / ".claude" / "settings.json").exists()


@pytest.mark.parametrize("subcommand", ["install", "init"])
def test_both_subcommands_expose_auto_embed_flag(subcommand, tmp_path):
    """Regression guard: install AND init must both accept the flag."""
    result = _run_cli(
        subcommand,
        "--help",
    )
    assert "--auto-embed-hook" in result.stdout
    assert "--no-auto-embed-hook" in result.stdout
