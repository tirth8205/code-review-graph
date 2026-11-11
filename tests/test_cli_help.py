"""The banner and --help list every command, and a typo gets a suggestion.

From the gstack developer-experience review (item 14): the banner listed 16
of 35 commands, --help opened with a brace list of every name, the hook-only
commands sat among the real ones, and a mistyped command got a wall of names
with no suggestion.
"""

from __future__ import annotations

import argparse
import sys

import pytest

from code_review_graph import cli

HOOK_ONLY = {"enrich", "hook-update", "hook-status"}


def _commands() -> dict[str, argparse.ArgumentParser]:
    parser = cli.build_parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    raise AssertionError("the parser has no subcommands")


def _listed_in_help() -> set[str]:
    parser = cli.build_parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return {choice.dest for choice in action._choices_actions}
    raise AssertionError("the parser has no subcommands")


def _banner(monkeypatch, capsys) -> str:
    monkeypatch.setenv("NO_COLOR", "1")
    cli._print_banner()
    return capsys.readouterr().out


def _run(monkeypatch, capsys, *argv: str) -> tuple[int, str]:
    monkeypatch.setattr(sys, "argv", ["code-review-graph", *argv])
    with pytest.raises(SystemExit) as raised:
        cli.main()
    captured = capsys.readouterr()
    return raised.value.code, captured.out + captured.err


def test_banner_lists_every_command_a_user_types(monkeypatch, capsys):
    banner = _banner(monkeypatch, capsys)
    in_banner = {
        line.split()[0]
        for line in banner.splitlines()
        if line.startswith("    ") and line.split()
    }
    assert in_banner == set(_commands()) - HOOK_ONLY


def test_banner_has_a_start_line(monkeypatch, capsys):
    banner = _banner(monkeypatch, capsys)
    assert "Start: code-review-graph install && code-review-graph build" in banner


def test_banner_rows_line_up(monkeypatch, capsys):
    """``detect-changes`` used to push its own description out of line."""
    banner = _banner(monkeypatch, capsys)
    rows = [line for line in banner.splitlines() if line.startswith("    ")]
    columns = {len(row) - len(row[4:].split(None, 1)[1]) for row in rows}
    assert len(columns) == 1, rows


def test_help_shows_command_not_a_brace_list(monkeypatch, capsys):
    code, output = _run(monkeypatch, capsys, "--help")
    assert code == 0
    assert "usage: code-review-graph [-h] [-v] <command> ..." in output
    assert "{install," not in output


def test_hook_only_commands_are_hidden_from_help_but_still_run(monkeypatch, capsys):
    assert _listed_in_help() == set(_commands()) - HOOK_ONLY
    code, output = _run(monkeypatch, capsys, "--help")
    assert code == 0
    for name in HOOK_ONLY:
        assert f" {name} " not in output
        assert "==SUPPRESS==" not in output
    for name in HOOK_ONLY:
        code, output = _run(monkeypatch, capsys, name, "--help")
        assert code == 0
        assert f"code-review-graph {name}" in output


def test_a_typo_gets_a_suggestion_and_exit_two(monkeypatch, capsys):
    code, output = _run(monkeypatch, capsys, "statsu")
    assert code == 2
    assert "usage:" in output
    assert "unknown command 'statsu'. Did you mean status?" in output
    # The old message listed every command name on one line.
    assert "choose from" not in output


def test_a_word_that_matches_nothing_gets_no_suggestion(monkeypatch, capsys):
    code, output = _run(monkeypatch, capsys, "xyzzy")
    assert code == 2
    assert "unknown command 'xyzzy'." in output
    assert "Did you mean" not in output


def test_hook_only_commands_are_never_suggested(monkeypatch, capsys):
    code, output = _run(monkeypatch, capsys, "enrichh")
    assert code == 2
    assert "Did you mean enrich" not in output


def test_options_before_the_command_are_skipped_over(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["code-review-graph", "-v"])
    cli.main()
    assert capsys.readouterr().out.startswith("code-review-graph ")
    monkeypatch.setattr(sys, "argv", ["code-review-graph", "-v", "status", "--help"])
    with pytest.raises(SystemExit) as raised:
        cli.main()
    assert raised.value.code == 0
    assert "usage: code-review-graph status" in capsys.readouterr().out


def test_help_uses_british_spelling(monkeypatch, capsys):
    texts = [_run(monkeypatch, capsys, "--help")[1]]
    for name in ("visualize", "detect-changes", "impact", "daemon"):
        texts.append(_run(monkeypatch, capsys, name, "--help")[1])
    texts.append(_run(monkeypatch, capsys, "daemon", "start", "--help")[1])
    joined = "\n".join(texts)
    for american in ("Analyze", "visualization", "daemonizing"):
        assert american not in joined
