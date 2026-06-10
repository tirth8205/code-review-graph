"""Compact estimated context savings helpers.

The project intentionally labels these values as estimates: the helper uses a
conservative character-count approximation instead of model-specific tokenizers.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

CHARS_PER_TOKEN = 4


def estimate_tokens(value: Any) -> int:
    """Estimate token count with a conservative 4 chars/token approximation."""
    if value is None:
        return 0
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(
            value,
            default=str,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    if not text:
        return 0
    return max(1, (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN)


def estimate_file_tokens(repo_root: Path, files: Iterable[str]) -> int:
    """Estimate tokens for changed files using file sizes, not file contents."""
    total = 0
    root = repo_root.resolve()
    for file_name in files:
        path = Path(file_name)
        full_path = path if path.is_absolute() else root / path
        try:
            if full_path.is_file():
                total += max(
                    1,
                    (full_path.stat().st_size + CHARS_PER_TOKEN - 1)
                    // CHARS_PER_TOKEN,
                )
        except OSError:
            continue
    return total


def estimate_context_savings(
    *,
    original_context: Any | None = None,
    returned_context: Any | None = None,
    original_tokens: int | None = None,
    returned_tokens: int | None = None,
) -> dict[str, int | bool] | None:
    """Return tiny savings metadata, or None when no baseline is available."""
    baseline = (
        original_tokens
        if original_tokens is not None
        else estimate_tokens(original_context)
    )
    returned = (
        returned_tokens
        if returned_tokens is not None
        else estimate_tokens(returned_context)
    )

    if baseline <= 0:
        return None

    saved = max(0, baseline - returned)
    percent = round((saved / baseline) * 100) if baseline else 0
    return {
        "estimated": True,
        "saved_tokens": int(saved),
        "saved_percent": int(percent),
    }


def attach_context_savings(
    result: dict[str, Any],
    *,
    original_context: Any | None = None,
    original_tokens: int | None = None,
    returned_context: Any | None = None,
    returned_tokens: int | None = None,
) -> dict[str, Any]:
    """Attach compact ``context_savings`` metadata when it can be estimated."""
    estimate = estimate_context_savings(
        original_context=original_context,
        returned_context=result if returned_context is None else returned_context,
        original_tokens=original_tokens,
        returned_tokens=returned_tokens,
    )
    if estimate is not None:
        result["context_savings"] = estimate
    return result


def format_context_savings(estimate: dict[str, Any] | None) -> str | None:
    """Format a one-line human summary for CLI output."""
    if not estimate:
        return None
    saved = int(estimate.get("saved_tokens", 0))
    percent = int(estimate.get("saved_percent", 0))
    return f"Estimated context saved: ~{saved:,} tokens (~{percent}%)"


def _fmt_compact(n: int) -> str:
    """Compact integer formatting: 1234 -> '1.2k', 9876 -> '9.9k', 500 -> '500'."""
    if n >= 10_000:
        return f"{n // 1000:,}k"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def _breakdown_from_response(response: dict[str, Any]) -> dict[str, int]:
    """Pull a per-category token estimate from a detect-changes / review response.

    Only fields that exist and have content are reported, so the breakdown
    line stays meaningful instead of padding with zeros.
    """
    # Friendly label -> response-dict key
    fields = [
        ("Functions", "changed_functions"),
        ("Flows", "affected_flows"),
        ("Tests", "test_gaps"),
        ("Risk", "review_priorities"),
        ("Impact", "impacted_nodes"),
        ("Edges", "edges"),
        ("Source", "source_snippets"),
        ("Imports", "imports"),
    ]
    out: dict[str, int] = {}
    for label, key in fields:
        value = response.get(key)
        if not value:
            continue
        tokens = estimate_tokens(value)
        if tokens > 0:
            out[label] = tokens
    return out


def verify_with_tiktoken(
    repo_root: "Path | str",
    changed_files: Iterable[str],
    response: Any,
    encoding_name: str = "cl100k_base",
) -> dict[str, int] | None:
    """Calibrate the chars/4 estimate against a real model tokenizer.

    Returns ``{"verified_baseline": int, "verified_returned": int,
    "verified_saved": int, "verified_percent": int}`` or ``None`` if
    tiktoken is not installed. Reads every changed file's content (unlike
    the stat-only ``estimate_file_tokens``) so the numbers reflect what
    an agent would actually consume.
    """
    try:
        import tiktoken  # type: ignore[import-untyped]
    except ImportError:
        return None

    enc = tiktoken.get_encoding(encoding_name)
    root = Path(repo_root).resolve()

    naive_real = 0
    for f in changed_files:
        p = root / f
        try:
            if p.is_file():
                naive_real += len(enc.encode(p.read_text(errors="replace")))
        except OSError:
            continue

    if isinstance(response, str):
        graph_real = len(enc.encode(response))
    else:
        text = json.dumps(
            response, default=str, ensure_ascii=True,
            separators=(",", ":"), sort_keys=True,
        )
        graph_real = len(enc.encode(text))

    saved = max(0, naive_real - graph_real)
    pct = round(saved * 100 / naive_real) if naive_real > 0 else 0
    return {
        "verified_baseline": naive_real,
        "verified_returned": graph_real,
        "verified_saved": saved,
        "verified_percent": pct,
    }


def format_context_savings_panel(
    estimate: dict[str, Any] | None,
    *,
    original_tokens: int | None = None,
    returned_tokens: int | None = None,
    response: dict[str, Any] | None = None,
    breakdown: dict[str, int] | None = None,
    verified: dict[str, int] | None = None,
    title: str = "Token Savings",
    width: int = 64,
) -> str | None:
    """Format the savings estimate as a boxed multi-line CLI panel.

    Example output (width=60)::

        ┌──────────────── Token Savings ────────────────┐
        │ Full context would be: 12,932 tokens          │
        │ Graph context used:       773 tokens          │
        │ Saved:                 12,159 tokens (~94%)   │
        │ Breakdown: Functions 580 · Tests 120 · ...    │
        └───────────────────────────────────────────────┘

    All numbers are labelled as estimates upstream (``estimated: true`` in the
    metadata dict) because the project uses a 4-chars-per-token approximation,
    not model-specific tokenization.

    Args:
        estimate: The ``context_savings`` dict from a tool response.
        original_tokens: Optional override for the naive baseline.
        returned_tokens: Optional override for the graph response size.
        response: When provided, breakdown is auto-derived from common keys
            (``changed_functions``, ``affected_flows``, ``test_gaps``,
            ``review_priorities``, ``impacted_nodes``, ``edges``,
            ``source_snippets``, ``imports``).
        breakdown: Explicit ``{label: tokens}`` map; takes precedence over
            ``response``-derived breakdown when both are provided.
        title: Title centered in the top border.
        width: Total panel width, capped at terminal width if larger.

    Returns:
        The panel as a single ``\\n``-joined string, or ``None`` when there
        is nothing meaningful to display.
    """
    if not estimate:
        return None

    saved = int(estimate.get("saved_tokens", 0))
    percent = int(estimate.get("saved_percent", 0))

    # Derive baseline + returned from saved+percent if not provided
    if original_tokens is None:
        if percent > 0:
            original_tokens = int(round(saved * 100 / percent))
        else:
            original_tokens = saved
    if returned_tokens is None:
        returned_tokens = max(0, (original_tokens or 0) - saved)

    if breakdown is None and response is not None:
        breakdown = _breakdown_from_response(response)

    # Top up the breakdown with an "Other" bucket so the parts sum to
    # ``returned_tokens`` exactly. "Other" covers fields the breakdown
    # doesn't enumerate (status, summary, risk_score, context_savings
    # metadata, JSON envelope chars). Skip when there's no positive
    # remainder — the breakdown already accounts for the whole response.
    if breakdown and returned_tokens is not None:
        labelled_sum = sum(breakdown.values())
        remainder = returned_tokens - labelled_sum
        if remainder > 0:
            breakdown = dict(breakdown)  # copy before mutating
            breakdown["Other"] = remainder

    # Lines that go inside the box (without borders)
    inner_lines: list[str] = [
        f"Full context would be:  {original_tokens:>9,} tokens",
        f"Graph context used:     {returned_tokens:>9,} tokens",
        f"Saved:                  {saved:>9,} tokens (~{percent}%)",
    ]
    if verified:
        vb = verified["verified_baseline"]
        vr = verified["verified_returned"]
        vs = verified["verified_saved"]
        vp = verified["verified_percent"]
        inner_lines.append(
            f"Verified (tiktoken):    {vs:>9,} tokens (~{vp}%)  "
            f"[{vb:,} → {vr:,}]"
        )
    if breakdown:
        parts = [f"{label} {_fmt_compact(tok)}" for label, tok in breakdown.items()]
        bd_line = "Breakdown: " + " · ".join(parts)
        inner_lines.append(bd_line)

    # Compute final width: at least wide enough for the longest inner line + padding
    content_width = max(len(s) for s in inner_lines)
    inner_w = max(width - 2, content_width + 2)  # +2 for one space pad each side
    # Title bar
    title_str = f" {title} "
    dash_total = inner_w - len(title_str)
    if dash_total < 4:
        dash_total = 4
    left_dash = dash_total // 2
    right_dash = dash_total - left_dash
    top = "┌" + "─" * left_dash + title_str + "─" * right_dash + "┐"
    bottom = "└" + "─" * inner_w + "┘"

    def _box_line(content: str) -> str:
        pad = inner_w - 2 - len(content)
        if pad < 0:
            pad = 0
        return f"│ {content}{' ' * pad} │"

    lines = [top]
    for s in inner_lines:
        lines.append(_box_line(s))
    lines.append(bottom)
    return "\n".join(lines)
