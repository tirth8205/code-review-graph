"""Tests for compact estimated context savings metadata."""

from __future__ import annotations

import json

from code_review_graph.context_savings import (
    estimate_context_savings,
    estimate_file_tokens,
    estimate_tokens,
    format_context_savings,
    format_context_savings_panel,
)


def test_estimate_tokens_uses_conservative_character_approximation():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2


def test_estimate_context_savings_returns_tiny_metadata():
    estimate = estimate_context_savings(
        original_tokens=100,
        returned_context="x" * 80,
    )

    assert estimate == {
        "estimated": True,
        "saved_tokens": 80,
        "saved_percent": 80,
    }
    assert len(json.dumps(estimate, separators=(",", ":"))) < 64


def test_estimate_context_savings_reports_a_loss_when_the_graph_costs_more():
    """A panel that clamps at zero can only ever report a win.

    Graph context genuinely costs more than a plain file read on small
    single-file edits. Clamping ``saved`` at 0 hid exactly that case, so the
    Token Savings panel was structurally incapable of showing a loss. The
    metadata is signed: 10 tokens of baseline replaced by 50 tokens of
    response is -40 tokens, -400%.
    """
    estimate = estimate_context_savings(
        original_tokens=10,
        returned_context="x" * 200,
    )

    assert estimate == {
        "estimated": True,
        "saved_tokens": -40,
        "saved_percent": -400,
    }


def test_estimate_context_savings_unknown_original_returns_none():
    assert estimate_context_savings(original_tokens=0, returned_context="x") is None


def test_estimate_file_tokens_uses_file_sizes_without_reading_contents(tmp_path):
    source = tmp_path / "source.py"
    source.write_text("x" * 17, encoding="utf-8")

    assert estimate_file_tokens(tmp_path, ["source.py", "missing.py"]) == 5


def test_format_context_savings_is_one_short_line():
    text = format_context_savings(
        {"estimated": True, "saved_tokens": 1240, "saved_percent": 18}
    )

    assert text == "Estimated context saved: ~1,240 tokens (~18%)"


def test_format_context_savings_says_a_loss_in_words():
    text = format_context_savings(
        {"estimated": True, "saved_tokens": -310, "saved_percent": -25}
    )

    assert text == "Estimated context cost: ~310 tokens more than the baseline (~25%)"


def test_panel_reports_a_loss_and_still_reconstructs_the_two_sides():
    panel = format_context_savings_panel(
        {"estimated": True, "saved_tokens": -40, "saved_percent": -400}
    )

    assert panel is not None
    assert "Cost more:" in panel
    assert "Saved:" not in panel
    # baseline 10, response 50 -> both recovered from the signed metadata
    assert "10 tokens" in panel
    assert "50 tokens" in panel


def test_panel_reports_a_win_unchanged():
    panel = format_context_savings_panel(
        {"estimated": True, "saved_tokens": 12159, "saved_percent": 94}
    )

    assert panel is not None
    assert "Saved:" in panel
    assert "Cost more:" not in panel
