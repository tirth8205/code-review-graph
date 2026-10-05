"""The shared numeric environment reader behind #912.

Twenty-two settings used to be parsed with a bare
``int(os.environ.get("CRG_...", "..."))`` at module scope. A typo in a shell
profile therefore aborted the import with ``ValueError: invalid literal for
int()`` — a traceback that never named the variable at fault, from a command
that may not even use the setting. They all read through ``env_int`` /
``env_float`` now; this pins the contract they rely on.

``tests/test_cli_surface.py`` proves the end-to-end half (no traceback, exit
0, every variable). The parts that are only visible in-process are here:
which values count as invalid, and that the warning names the variable and
appears once.
"""

from __future__ import annotations

import logging

import pytest

from code_review_graph import constants


@pytest.fixture(autouse=True)
def _forget_previous_warnings():
    """The warn-once set is module state; each test starts from empty."""
    saved = set(constants._warned_env_vars)
    constants._warned_env_vars.clear()
    yield
    constants._warned_env_vars.clear()
    constants._warned_env_vars.update(saved)


def test_unset_variable_uses_the_default_silently(monkeypatch, caplog):
    monkeypatch.delenv("CRG_TEST_INT", raising=False)
    with caplog.at_level(logging.WARNING, logger=constants.logger.name):
        assert constants.env_int("CRG_TEST_INT", 7) == 7
    assert caplog.records == []


def test_valid_value_wins(monkeypatch):
    monkeypatch.setenv("CRG_TEST_INT", "42")
    assert constants.env_int("CRG_TEST_INT", 7) == 42


def test_surrounding_whitespace_is_tolerated(monkeypatch):
    """A trailing newline from `export X=$(...)` is not a typo."""
    monkeypatch.setenv("CRG_TEST_INT", "  42\n")
    assert constants.env_int("CRG_TEST_INT", 7) == 42


@pytest.mark.parametrize("bad", ["", "   ", "abc", "1.5.2", "-", "12x"])
def test_invalid_value_falls_back_and_names_the_variable(bad, monkeypatch, caplog):
    monkeypatch.setenv("CRG_TEST_INT", bad)
    with caplog.at_level(logging.WARNING, logger=constants.logger.name):
        assert constants.env_int("CRG_TEST_INT", 7) == 7
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "CRG_TEST_INT" in message, message
    assert repr(bad) in message, message
    assert "7" in message, message


def test_the_warning_is_emitted_once_per_variable(monkeypatch, caplog):
    """Module-scope reads run once, but function-scope reads run per call."""
    monkeypatch.setenv("CRG_TEST_INT", "nonsense")
    monkeypatch.setenv("CRG_OTHER_INT", "nonsense")
    with caplog.at_level(logging.WARNING, logger=constants.logger.name):
        for _ in range(5):
            constants.env_int("CRG_TEST_INT", 1)
        constants.env_int("CRG_OTHER_INT", 2)
    named = [record.getMessage() for record in caplog.records]
    assert len(named) == 2, named
    assert any("CRG_TEST_INT" in message for message in named)
    assert any("CRG_OTHER_INT" in message for message in named)


def test_env_float_accepts_a_float(monkeypatch):
    monkeypatch.setenv("CRG_TEST_FLOAT", "0.25")
    assert constants.env_float("CRG_TEST_FLOAT", 1.0) == 0.25


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf", "Infinity"])
def test_env_float_rejects_non_finite_values(bad, monkeypatch, caplog):
    """``float()`` accepts these; every comparison downstream does not."""
    monkeypatch.setenv("CRG_TEST_FLOAT", bad)
    with caplog.at_level(logging.WARNING, logger=constants.logger.name):
        assert constants.env_float("CRG_TEST_FLOAT", 1.5) == 1.5
    assert len(caplog.records) == 1
    assert "CRG_TEST_FLOAT" in caplog.records[0].getMessage()


def test_no_numeric_setting_is_parsed_without_the_helper():
    """Guards the fix itself: a new bare parse would reintroduce #912.

    ``tests/test_cli_surface.py`` enforces this across the package with an
    AST walk; this is the cheap version that runs in the ordinary suite.
    """
    import re
    from pathlib import Path

    package = Path(constants.__file__).parent
    bare = re.compile(r"(?:int|float)\(\s*os\.(?:environ\.get|getenv)\(")
    offenders = []
    for path in sorted(package.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), start=1):
            if bare.search(line):
                offenders.append(f"{path.name}:{number}")
    assert not offenders, (
        "numeric environment settings parsed without env_int/env_float; an "
        f"invalid value would abort the process again (#912): {offenders}"
    )
