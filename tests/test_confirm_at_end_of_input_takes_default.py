"""A yes/no question that meets the end of input takes the default, and says so.

On Windows, stdin redirected from ``NUL`` counts as a terminal, so the
question was asked, ``input()`` hit the end of input at once and the answer
was "no" whatever the default was. Install then skipped the instructions an
agent run should have written. The end of input now takes the default with
the same "No terminal" note as a pipe.
"""

from __future__ import annotations

import io

import pytest

from code_review_graph import cli


class _EmptyTerminal(io.StringIO):
    """A stdin that says it is a terminal but has nothing to read, like NUL."""

    def isatty(self) -> bool:
        return True


@pytest.mark.parametrize(("default_yes", "word"), [(True, "yes"), (False, "no")])
def test_end_of_input_takes_the_default(monkeypatch, capsys, default_yes, word):
    monkeypatch.setattr("sys.stdin", _EmptyTerminal())

    answer = cli._confirm_yes_no(
        "Go ahead?", default_yes, no_terminal_hint="Use --yes to go ahead."
    )

    assert answer is default_yes
    out = capsys.readouterr().out
    assert f"No terminal: taking the default ({word}). Use --yes to go ahead." in out


def test_a_typed_answer_still_wins(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", _EmptyTerminal())
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")

    assert cli._confirm_yes_no("Go ahead?", True) is False
    assert "No terminal" not in capsys.readouterr().out
