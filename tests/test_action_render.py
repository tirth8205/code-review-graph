"""Tests for scripts/render_pr_comment.py (GitHub Action comment renderer)."""

from __future__ import annotations

import builtins
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "render_pr_comment.py"
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "detect_changes_sample.json"

_spec = importlib.util.spec_from_file_location("render_pr_comment", SCRIPT)
assert _spec is not None and _spec.loader is not None
render = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(render)


@pytest.fixture()
def report() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# risk_level
# ---------------------------------------------------------------------------


def test_risk_level_mapping():
    assert render.risk_level(0.9) == "critical"
    assert render.risk_level(0.85) == "critical"
    assert render.risk_level(0.72) == "high"
    assert render.risk_level(0.7) == "high"
    assert render.risk_level(0.5) == "medium"
    assert render.risk_level(0.4) == "medium"
    assert render.risk_level(0.1) == "low"
    assert render.risk_level(0.0) == "low"


# ---------------------------------------------------------------------------
# md_escape
# ---------------------------------------------------------------------------


def test_md_escape_escapes_pipes_and_backticks():
    escaped = render.md_escape("a|b`c")
    assert "|" not in escaped.replace("\\|", "")
    assert "\\|" in escaped
    assert "\\`" in escaped


def test_md_escape_strips_control_chars_and_newlines():
    escaped = render.md_escape("evil\x00name\nwith\rbreaks\x1b[31m")
    assert "\x00" not in escaped
    assert "\n" not in escaped
    assert "\r" not in escaped
    assert "\x1b" not in escaped


def test_md_escape_caps_length():
    escaped = render.md_escape("x" * 500)
    assert len(escaped) <= render._MAX_CELL


# ---------------------------------------------------------------------------
# relativize_path (strip CI-runner absolute prefixes)
# ---------------------------------------------------------------------------


def test_relativize_strips_github_workspace(monkeypatch):
    monkeypatch.setenv("GITHUB_WORKSPACE", "/home/runner/work/repo/repo")
    out = render.relativize_path(
        "/home/runner/work/repo/repo/code_review_graph/embeddings.py"
    )
    assert out == "code_review_graph/embeddings.py"


def test_relativize_keeps_symbol_suffix(monkeypatch):
    monkeypatch.setenv("GITHUB_WORKSPACE", "/home/runner/work/repo/repo")
    out = render.relativize_path(
        "/home/runner/work/repo/repo/code_review_graph/embeddings.py::get_provider"
    )
    assert out == "code_review_graph/embeddings.py::get_provider"


def test_relativize_handles_workspace_with_trailing_slash(monkeypatch):
    monkeypatch.setenv("GITHUB_WORKSPACE", "/home/runner/work/repo/repo/")
    out = render.relativize_path(
        "/home/runner/work/repo/repo/pkg/mod.py::fn"
    )
    assert out == "pkg/mod.py::fn"


def test_relativize_leaves_already_relative_paths(monkeypatch):
    monkeypatch.setenv("GITHUB_WORKSPACE", "/home/runner/work/repo/repo")
    assert render.relativize_path("auth/session.py::rotate_token") == (
        "auth/session.py::rotate_token"
    )
    assert render.relativize_path("auth/session.py") == "auth/session.py"


def test_relativize_falls_back_to_repo_segment_without_env(monkeypatch):
    monkeypatch.delenv("GITHUB_WORKSPACE", raising=False)
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/code-review-graph")
    out = render.relativize_path(
        "/home/runner/work/code-review-graph/code-review-graph/"
        "scripts/render_pr_comment.py::main"
    )
    assert out == "scripts/render_pr_comment.py::main"


def test_relativize_no_env_no_match_returns_input(monkeypatch):
    monkeypatch.delenv("GITHUB_WORKSPACE", raising=False)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    # Nothing to strip against; render the path as-is rather than mangling it.
    weird = "/opt/build/some/place/file.py::fn"
    assert render.relativize_path(weird) == weird


def test_relativize_handles_none_and_question_mark(monkeypatch):
    monkeypatch.setenv("GITHUB_WORKSPACE", "/home/runner/work/repo/repo")
    assert render.relativize_path("?") == "?"


def test_render_markdown_relativizes_absolute_paths(monkeypatch):
    monkeypatch.setenv("GITHUB_WORKSPACE", "/home/runner/work/repo/repo")
    ws = "/home/runner/work/repo/repo"
    abs_report = {
        "risk_score": 0.72,
        "review_priorities": [
            {
                "qualified_name": f"{ws}/code_review_graph/embeddings.py::get_provider",
                "file_path": f"{ws}/code_review_graph/embeddings.py",
                "line_start": 42,
                "risk_score": 0.72,
                "is_test": False,
            }
        ],
        "affected_flows": [],
        "test_gaps": [],
    }
    body = render.render_markdown(abs_report)
    # Absolute CI-runner prefix must not leak into the rendered comment.
    assert "/home/runner/work" not in body
    assert render.md_escape("code_review_graph/embeddings.py::get_provider") in body
    # Location column path is markdown-escaped (underscores) like every cell.
    assert f"{render.md_escape('code_review_graph/embeddings.py')}:42" in body


# ---------------------------------------------------------------------------
# render_markdown
# ---------------------------------------------------------------------------


def test_marker_is_first_line(report):
    body = render.render_markdown(report)
    assert body.splitlines()[0] == render.MARKER


def test_overall_risk_line(report):
    body = render.render_markdown(report)
    assert "**Overall risk: 0.72 (HIGH)**" in body
    assert "3 changed function(s)/class(es)" in body
    assert "2 affected flow(s)" in body
    assert "1 test gap(s)" in body


def test_risk_table_lists_top_functions(report):
    body = render.render_markdown(report)
    assert "### Risk-scored changes" in body
    assert render.md_escape("auth/session.py::rotate_token") in body
    assert render.md_escape("auth/session.py::validate_session") in body
    assert "| 0.72 | high |" in body
    assert "| 0.41 | medium |" in body
    assert "| 0.10 | low |" in body
    # Untested function marked "no", tested ones "yes".
    rotate_row = next(line for line in body.splitlines() if "rotate" in line and "| 0.72" in line)
    assert rotate_row.rstrip().endswith("| no |")
    validate_row = next(line for line in body.splitlines() if "| 0.41" in line)
    assert validate_row.rstrip().endswith("| yes |")


def test_risk_table_location_includes_line_number(report):
    body = render.render_markdown(report)
    assert "auth/session.py:42" in body


def test_affected_flows_section(report):
    body = render.render_markdown(report)
    assert "### Affected execution flows" in body
    assert render.md_escape("login_handler -> rotate_token") in body
    assert "criticality 0.83" in body
    assert "6 node(s) across 3 file(s)" in body


def test_test_gaps_section(report):
    body = render.render_markdown(report)
    assert "### Test gaps" in body
    assert "(auth/session.py:42)" in body


# ---------------------------------------------------------------------------
# Indirect coverage (#1047): reached through a caller vs. no test in reach
# ---------------------------------------------------------------------------


def _indirect_report() -> dict:
    """One unreached gap and one reached only through its caller."""
    return {
        "risk_score": 0.80,
        "review_priorities": [
            {
                "qualified_name": "code_review_graph/main.py::_offload",
                "name": "_offload",
                "file_path": "code_review_graph/main.py",
                "line_start": 130,
                "risk_score": 0.62,
                "is_test": False,
            },
            {
                "qualified_name": "code_review_graph/incremental.py::orphan",
                "name": "orphan",
                "file_path": "code_review_graph/incremental.py",
                "line_start": 900,
                "risk_score": 0.55,
                "is_test": False,
            },
        ],
        "affected_flows": [],
        "test_gaps": [
            {
                "name": "orphan",
                "qualified_name": "code_review_graph/incremental.py::orphan",
                "file": "code_review_graph/incremental.py",
                "line_start": 900,
                "line_end": 929,
                "coverage": "none",
            },
            {
                "name": "_offload",
                "qualified_name": "code_review_graph/main.py::_offload",
                "file": "code_review_graph/main.py",
                "line_start": 130,
                "line_end": 140,
                "coverage": "indirect",
                "covered_via": "code_review_graph/main.py::_run_off_loop",
                "covered_depth": 1,
                "covered_by": ["tests/test_main.py::test_offload"],
            },
        ],
        "test_gaps_uncovered": 1,
        "test_gaps_indirect": 1,
    }


def test_headline_splits_the_two_gap_kinds():
    body = render.render_markdown(_indirect_report())
    assert (
        "2 test gap(s) (1 with no tested caller found, "
        "1 reached only through a caller)"
    ) in body


def test_headline_never_claims_more_than_the_graph_knows():
    """"No test in reach" is a claim about the suite; the graph reads edges.

    Two symbols on the delta of #1047 are exercised by tests that load the
    file through importlib, so no edge records it. They are correctly listed
    as gaps, and describing them as having no test in reach was false.
    """
    body = render.render_markdown(_indirect_report())
    assert "no test in reach" not in body
    assert "That is execution" not in body
    assert "not a record of execution" in body


def test_indirect_gaps_get_their_own_section_naming_the_caller():
    body = render.render_markdown(_indirect_report())
    assert "### Test gaps" in body
    assert "### Reached only through a caller" in body
    gaps_at = body.index("### Test gaps")
    indirect_at = body.index("### Reached only through a caller")
    assert gaps_at < indirect_at
    # The unreached one is under the first heading, not the second.
    assert body.index(render.md_escape("incremental.py::orphan")) < indirect_at
    assert render.md_escape("main.py::_run_off_loop") in body
    assert "1 hop(s)" in body


def test_tested_column_says_indirect_not_no():
    body = render.render_markdown(_indirect_report())
    offload_row = next(
        line for line in body.splitlines() if "_offload" in line and "| 0.62" in line
    )
    assert offload_row.rstrip().endswith("| indirect |")
    orphan_row = next(
        line for line in body.splitlines() if "orphan" in line and "| 0.55" in line
    )
    assert orphan_row.rstrip().endswith("| no |")


def test_a_report_without_coverage_keys_renders_as_before(report):
    """Back-compat: a pre-#1047 report has no ``coverage`` field at all."""
    body = render.render_markdown(report)
    assert "### Test gaps" in body
    assert "Covered only through a caller" not in body
    assert "1 test gap(s)" in body
    assert "no test in reach" not in body


def test_truncated_report_uses_the_reported_counts():
    """The gap list is bounded upstream; the split must not be recounted."""
    payload = _indirect_report()
    payload["test_gaps"] = payload["test_gaps"][:1]
    payload["test_gaps_uncovered"] = 60
    payload["test_gaps_indirect"] = 14
    body = render.render_markdown(payload)
    assert "60 with no tested caller found, 14 reached only through a caller" in body


def test_truncated_headline_total_equals_its_own_parts():
    """The total and the split have to come from the same place.

    The headline used ``len(test_gaps)`` -- which every consumer bounds --
    beside a split taken from the untruncated counts, so a bounded report
    printed "25 test gap(s) (73 ..., 9 ...)": a number next to parts that do
    not add up to it.
    """
    payload = _indirect_report()
    payload["test_gaps"] = payload["test_gaps"][:1]
    payload["test_gaps_uncovered"] = 60
    payload["test_gaps_indirect"] = 14
    payload["test_gaps_total"] = 74
    body = render.render_markdown(payload)
    headline = next(line for line in body.splitlines() if "Overall risk" in line)
    assert "74 test gap(s)" in headline
    assert "1 test gap(s)" not in headline


def test_a_truncated_table_does_not_claim_a_symbol_is_tested():
    """Absence from a bounded gap list is not evidence of having tests.

    The Tested column is derived from the shipped ``test_gaps`` rows, so a
    symbol the report itself classified as a gap rendered as "yes" once
    truncation dropped its row -- the strongest possible overclaim.
    """
    payload = _indirect_report()
    payload["test_gaps"] = []
    payload["test_gaps_uncovered"] = 60
    payload["test_gaps_indirect"] = 14
    payload["test_gaps_total"] = 74
    body = render.render_markdown(payload)
    rows = [line for line in body.splitlines() if line.startswith("| 0.")]
    assert rows
    assert not any(row.rstrip().endswith("| yes |") for row in rows)
    assert all(row.rstrip().endswith("| ? |") for row in rows)


def test_a_truncated_report_still_accounts_for_the_indirect_class():
    """The headline promises the class; the body must not simply omit it."""
    payload = _indirect_report()
    # Only the unreached row survives truncation.
    payload["test_gaps"] = payload["test_gaps"][:1]
    payload["test_gaps_uncovered"] = 60
    payload["test_gaps_indirect"] = 14
    payload["test_gaps_total"] = 74
    body = render.render_markdown(payload)
    assert "14 more gap(s) are reached only through a caller" in body


def test_token_savings_line(report):
    body = render.render_markdown(report)
    assert "**Token savings:**" in body
    assert "12,159" in body
    assert "94%" in body
    assert "estimated" in body


def test_token_savings_line_omitted_when_zero(report):
    report["context_savings"] = {"estimated": True, "saved_tokens": 0, "saved_percent": 0}
    body = render.render_markdown(report)
    assert "**Token savings:**" not in body


def test_token_savings_line_omitted_when_absent(report):
    del report["context_savings"]
    body = render.render_markdown(report)
    assert "**Token savings:**" not in body


def test_footer_powered_by(report):
    body = render.render_markdown(report)
    assert "Powered by [code-review-graph]" in body
    assert "local-first" in body


def test_max_functions_cap(report):
    body = render.render_markdown(report, max_functions=1)
    assert render.md_escape("auth/session.py::rotate_token") in body
    assert render.md_escape("auth/display.py::format_expiry") not in body
    assert "and 2 more changed symbol(s)" in body


def test_max_flows_cap(report):
    body = render.render_markdown(report, max_flows=1)
    assert render.md_escape("login_handler -> rotate_token") in body
    assert render.md_escape("cli_main -> validate_session") not in body
    assert "and 1 more affected flow(s)" in body


def test_truncated_analysis_note(report):
    report["functions_truncated"] = True
    body = render.render_markdown(report)
    assert "CRG_MAX_CHANGED_FUNCS" in body


def test_markdown_injection_in_names_is_escaped(report):
    report["review_priorities"][0]["qualified_name"] = "x|y`z<script>"
    body = render.render_markdown(report)
    assert "x\\|y\\`z" in body
    assert "<script>" not in body


def test_empty_report_renders_minimal_body():
    body = render.render_markdown({})
    assert body.startswith(render.MARKER)
    assert "**Overall risk: 0.00 (LOW)**" in body
    assert "### Risk-scored changes" not in body
    assert "Powered by [code-review-graph]" in body


def test_body_size_capped():
    huge = {
        "risk_score": 0.5,
        "review_priorities": [
            {"qualified_name": f"mod.py::fn_{i}" + "x" * 100, "risk_score": 0.5,
             "file_path": "mod.py", "line_start": i}
            for i in range(5000)
        ],
    }
    body = render.render_markdown(huge, max_functions=5000)
    assert len(body) < render._MAX_BODY + 1000
    assert "Powered by [code-review-graph]" in body


# ---------------------------------------------------------------------------
# The byte budget, at the byte
# ---------------------------------------------------------------------------
#
# _MAX_BODY is the cap .github/workflows/pr-review-comment.yml enforces, and
# that workflow measures the artifact on disk -- which is the body plus the
# newline main() writes after it. A body sized at exactly _MAX_BODY is
# therefore a 60,001-byte file and is rejected. These tests pin the boundary
# from both sides so the reservation cannot quietly go missing again.

# Tail of the report's markdown table, in bytes, for a row whose symbol name
# is one character: "| 0.50 | medium | m.py::a | m.py:1 | yes |" plus "\n".
_TABLE_ROW_BYTES = 43


def _padded_report(rows: int, tail_name_len: int) -> dict:
    """A report whose rendered body grows one byte per unit of *tail_name_len*.

    ``rows`` identical one-character rows get the body into the right
    neighbourhood; the final row's symbol name is the fine adjustment. Both
    stay inside ``md_escape``'s 120-character cell cap, so a name of length
    *n* costs exactly *n* bytes.
    """
    def entry(name: str, line: int) -> dict:
        return {
            "qualified_name": f"m.py::{name}",
            "risk_score": 0.5,
            "file_path": "m.py",
            "line_start": line,
        }

    priorities = [entry("a", 1) for _ in range(rows)]
    priorities.append(entry("a" * tail_name_len, 1))
    return {"risk_score": 0.5, "review_priorities": priorities}


#: Every module global ``_fit_to_budget`` consults. Named rather than hard
#: coded so lifting the budget keeps working if the cap is ever split again.
_BUDGET_GLOBALS = ("_MAX_BODY", "_MAX_BODY_TEXT")


def _untruncated_size(report: dict) -> int:
    """Byte length of *report*'s body with the budget lifted out of the way."""
    saved = {
        name: getattr(render, name)
        for name in _BUDGET_GLOBALS
        if hasattr(render, name)
    }
    for name in saved:
        setattr(render, name, 1 << 30)
    try:
        body = render.render_markdown(report, max_functions=1 << 20)
    finally:
        for name, value in saved.items():
            setattr(render, name, value)
    return len(body.encode("utf-8"))


def report_rendering_to_exactly(target_bytes: int) -> dict:
    """A report whose untruncated body is exactly *target_bytes* long.

    Shared with ``tests/test_action_e2e.py``, which feeds the rendered
    artifact to the privileged workflow's own validator.
    """
    low, high = 0, 4000
    while low < high:
        mid = (low + high + 1) // 2
        if _untruncated_size(_padded_report(mid, 1)) <= target_bytes:
            low = mid
        else:
            high = mid - 1
    for tail in range(1, _TABLE_ROW_BYTES + 121):
        report = _padded_report(low, tail)
        if _untruncated_size(report) == target_bytes:
            return report
    raise AssertionError(f"could not build a report of exactly {target_bytes} bytes")


def test_boundary_fixture_is_exact():
    """Teeth for the two tests below: the fixture really hits the byte.

    Without this, a tuner that silently landed 200 bytes short would make
    every boundary assertion below pass vacuously.
    """
    for target in (render._MAX_BODY - 1, render._MAX_BODY, render._MAX_BODY + 1):
        assert _untruncated_size(report_rendering_to_exactly(target)) == target


@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_written_artifact_never_exceeds_the_consumer_cap(tmp_path, offset):
    """One byte under the cap, exactly on it, and one byte over it.

    The consumer stats the file, so the file is what is measured here.
    """
    target = render._MAX_BODY + offset
    source = tmp_path / "report.json"
    source.write_text(json.dumps(report_rendering_to_exactly(target)), encoding="utf-8")
    out = tmp_path / "comment.md"
    code = render.main(
        ["--input", str(source), "--output", str(out), "--max-functions", "100000"]
    )
    assert code == 0
    assert out.stat().st_size <= render._MAX_BODY, out.stat().st_size
    assert "Powered by [code-review-graph]" in out.read_text(encoding="utf-8")


def test_output_file_preserves_the_bytes_measured_by_the_budget(tmp_path, report):
    """Writing a report must not add platform-specific CRLF bytes."""
    source = tmp_path / "report.json"
    source.write_text(json.dumps(report), encoding="utf-8")
    out = tmp_path / "comment.md"

    expected = render.render_markdown(report)
    assert render.main(
        ["--input", str(source), "--output", str(out)]
    ) == 0

    assert out.read_bytes() == (expected + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# The artifact under Windows newline semantics (#1076, fixed in #1077)
# ---------------------------------------------------------------------------
#
# On Windows a file opened for writing in text mode with newline=None turns
# every "\n" into "\r\n", one extra byte per line the budget never counted.
# POSIX CPython never translates, so on Linux and macOS the artifact is the
# same whichever way main() writes it and the tests above cannot tell a text
# write from a byte write. ``windows_text_writes`` reproduces the translation
# in-process by handing newline="\r\n" -- os.linesep on Windows -- to every
# text-mode write that left newline unset. The windows-native CI job does not
# run this module, so this is where the Windows write path is exercised.


def _is_text_write(mode: str) -> bool:
    return "b" not in mode and any(flag in mode for flag in "wax+")


@pytest.fixture()
def windows_text_writes(monkeypatch):
    """Make text-mode writes in this process translate newlines as Windows does."""
    real_open = io.open

    def win_open(file, mode="r", buffering=-1, encoding=None, errors=None,
                 newline=None, closefd=True, opener=None):
        if _is_text_write(mode) and newline is None:
            newline = "\r\n"
        return real_open(file, mode, buffering, encoding, errors, newline, closefd, opener)

    def win_path_open(self, mode="r", buffering=-1, encoding=None, errors=None,
                      newline=None):
        return win_open(self, mode, buffering, encoding, errors, newline)

    monkeypatch.setattr(io, "open", win_open)
    monkeypatch.setattr(builtins, "open", win_open)
    # Path.open is what Path.write_text goes through on every supported
    # Python; 3.10 bound io.open at import, so patching io alone misses it.
    monkeypatch.setattr(pathlib.Path, "open", win_path_open)


def test_the_windows_simulation_really_translates(tmp_path, windows_text_writes):
    """Teeth for the tests below: an inert simulation would pass them vacuously."""
    probe = tmp_path / "probe.md"
    probe.write_text("x\n" * 1000, encoding="utf-8")
    # The issue's own reproduction: 3000 bytes on Windows, 2000 on POSIX.
    assert len(probe.read_bytes()) == 3000
    with open(probe, "w", encoding="utf-8") as fh:
        fh.write("a\n")
    assert probe.read_bytes() == b"a\r\n"
    probe.write_bytes(b"a\n")
    assert probe.read_bytes() == b"a\n"
    assert probe.read_text(encoding="utf-8") == "a\n", "reads must stay untouched"


def _render_to_file(tmp_path: Path, report: dict, *extra: str) -> tuple[int, bytes]:
    source = tmp_path / "report.json"
    source.write_text(json.dumps(report), encoding="utf-8")
    out = tmp_path / "comment.md"
    code = render.main(["--input", str(source), "--output", str(out), *extra])
    return code, out.read_bytes()


@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_windows_artifact_never_exceeds_the_consumer_cap(
    tmp_path, windows_text_writes, offset
):
    """The boundary from #1076: 61,399 bytes on Windows before the fix."""
    code, data = _render_to_file(
        tmp_path,
        report_rendering_to_exactly(render._MAX_BODY + offset),
        "--max-functions",
        "100000",
    )
    assert code == 0
    assert b"\r" not in data
    assert len(data) <= render._MAX_BODY, len(data)
    assert b"Powered by [code-review-graph]" in data


def test_windows_artifact_that_fits_is_written_whole(tmp_path, windows_text_writes):
    """A 59,999-byte body is a 60,000-byte artifact on Windows too, not cut."""
    code, data = _render_to_file(
        tmp_path,
        report_rendering_to_exactly(render._MAX_BODY - 1),
        "--max-functions",
        "100000",
    )
    assert code == 0
    assert len(data) == render._MAX_BODY
    assert b"*Report truncated.*" not in data


def test_windows_truncated_multibyte_report_fits_the_cap(tmp_path, windows_text_writes):
    """Non-ASCII names: the cap is UTF-8 bytes on disk, and the cut stays valid UTF-8."""
    huge = {
        "risk_score": 0.5,
        "review_priorities": [
            {
                "qualified_name": f"src/café_日本.py::grüße_{i}_" + "é🚀" * 30,
                "risk_score": 0.5,
                "file_path": "src/café_日本.py",
                "line_start": i,
            }
            for i in range(3000)
        ],
    }
    code, data = _render_to_file(tmp_path, huge, "--max-functions", "3000")
    assert code == 0
    assert len(data) <= render._MAX_BODY, len(data)
    assert b"\r" not in data
    text = data.decode("utf-8")  # strict: a split code point would raise
    assert "*Report truncated.*" in text
    assert text.endswith(render.FOOTER + "\n")


_NON_ASCII_REPORT = {
    "risk_score": 0.5,
    "changed_functions": [
        {"qualified_name": "src/café.py::grüße", "file_path": "src/café.py",
         "line_start": 3, "risk_score": 0.5},
        {"qualified_name": "src/日本.py::関数_🚀", "file_path": "src/日本.py",
         "line_start": 9, "risk_score": 0.4},
    ],
    "test_gaps": [
        {"qualified_name": "src/café.py::grüße", "file_path": "src/café.py",
         "line_start": 3},
    ],
}


@pytest.mark.parametrize(
    ("input_bytes", "expected_body", "expected_code"),
    [
        pytest.param(
            FIXTURE.read_bytes(),
            lambda: render.render_markdown(
                json.loads(FIXTURE.read_text(encoding="utf-8"))
            ),
            0,
            id="analysis",
        ),
        pytest.param(
            json.dumps(_NON_ASCII_REPORT, ensure_ascii=False).encode("utf-8"),
            lambda: render.render_markdown(_NON_ASCII_REPORT),
            0,
            id="non-ascii-names",
        ),
        pytest.param(
            # An older report shape: no review_priorities, no coverage counts.
            json.dumps({"risk_score": 0.2, "changed_functions": [
                {"qualified_name": "a.py::f", "file_path": "a.py", "line_start": 1}
            ]}).encode("utf-8"),
            lambda: render.render_markdown({"risk_score": 0.2, "changed_functions": [
                {"qualified_name": "a.py::f", "file_path": "a.py", "line_start": 1}
            ]}),
            0,
            id="legacy-report-shape",
        ),
        pytest.param(b"{}", lambda: render.render_markdown({}), 0, id="empty-report"),
        pytest.param(
            # detect-changes' clean-tree line as a Windows shell would save it.
            b"No changes detected.\r\n",
            render.render_no_changes,
            0,
            id="no-changes-crlf-input",
        ),
        pytest.param(
            b"Error: could not determine the changes\r\n",
            lambda: render.render_not_analyzed(
                "Error: could not determine the changes\n"
            ),
            4,
            id="not-analyzed",
        ),
    ],
)
def test_windows_output_file_is_the_measured_utf8_bytes(
    tmp_path, windows_text_writes, input_bytes, expected_body, expected_code
):
    """Every body main() can write lands on disk as exactly the bytes it measured."""
    source = tmp_path / "report.json"
    source.write_bytes(input_bytes)
    out = tmp_path / "comment.md"
    code = render.main(["--input", str(source), "--output", str(out)])
    assert code == expected_code
    assert out.read_bytes() == (expected_body() + "\n").encode("utf-8")


def test_risk_gate_breach_still_writes_the_exact_artifact(tmp_path, windows_text_writes):
    """Exit 3 is decided after the write; the file is complete and LF-only."""
    out = tmp_path / "comment.md"
    code = render.main(
        ["--input", str(FIXTURE), "--output", str(out), "--fail-on-risk", "high"]
    )
    assert code == 3
    expected = render.render_markdown(json.loads(FIXTURE.read_text(encoding="utf-8")))
    assert out.read_bytes() == (expected + "\n").encode("utf-8")


def test_output_path_with_spaces_and_unicode(tmp_path, windows_text_writes):
    out_dir = tmp_path / "runner temp é 日本"
    out_dir.mkdir()
    out = out_dir / "crg comment 🚀.md"
    assert render.main(["--input", str(FIXTURE), "--output", str(out)]) == 0
    expected = render.render_markdown(json.loads(FIXTURE.read_text(encoding="utf-8")))
    assert out.read_bytes() == (expected + "\n").encode("utf-8")


def test_rendering_twice_replaces_a_larger_artifact_exactly(tmp_path):
    """A rerun into the same file leaves no tail of the previous, larger one."""
    out = tmp_path / "comment.md"
    out.write_bytes(b"stale\r\n" * 20_000)
    args = ["--input", str(FIXTURE), "--output", str(out)]
    assert render.main(args) == 0
    first = out.read_bytes()
    assert render.main(args) == 0
    assert out.read_bytes() == first
    expected = render.render_markdown(json.loads(FIXTURE.read_text(encoding="utf-8")))
    assert first == (expected + "\n").encode("utf-8")


@pytest.mark.parametrize("target", ["missing-dir", "is-a-directory"])
def test_unwritable_output_returns_2(tmp_path, caplog, target):
    """The byte write keeps the OSError path: exit 2 and a logged reason."""
    if target == "missing-dir":
        out = tmp_path / "no" / "such" / "dir" / "comment.md"
    else:
        out = tmp_path / "comment.md"
        out.mkdir()
    with caplog.at_level("ERROR", logger="render_pr_comment"):
        code = render.main(["--input", str(FIXTURE), "--output", str(out)])
    assert code == 2
    assert "Cannot write output file" in caplog.text
    if target == "missing-dir":
        assert not out.exists()


# The consumer itself: the validator step of the privileged workflow, read
# straight out of pr-review-comment.yml together with the cap it enforces, so
# a Windows-written artifact is judged by the real check rather than by a
# restatement of it, and the renderer's budget cannot drift from that cap.
COMMENT_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "pr-review-comment.yml"


def _trusted_validator() -> tuple[str, dict[str, str]]:
    """The validator step's Python source and its declared environment."""
    import yaml  # a runtime dependency, but only these tests need it

    workflow = yaml.safe_load(COMMENT_WORKFLOW.read_text(encoding="utf-8"))
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            run = step.get("run") or ""
            if "Report artifact is empty or too large" not in run:
                continue
            lines = run.split("\n")
            start = lines.index("python - <<'PY'") + 1
            end = next(i for i in range(start, len(lines)) if lines[i].strip() == "PY")
            source = "\n".join(lines[start:end]) + "\n"
            return source, {key: str(value) for key, value in step["env"].items()}
    raise AssertionError("validator step not found in pr-review-comment.yml")


def _run_trusted_validator(tmp_path: Path, download: Path) -> subprocess.CompletedProcess:
    source, declared = _trusted_validator()
    script = tmp_path / "validator.py"
    script.write_bytes(source.encode("utf-8"))
    env = dict(os.environ)
    env.update(declared)
    env.update(
        {
            "DOWNLOAD_DIR": str(download),
            "COMMENT_BODY": str(tmp_path / "crg-comment-body.md"),
            "VALIDATED_PR_NUMBER": str(tmp_path / "crg-pr-number.txt"),
        }
    )
    return subprocess.run(
        [sys.executable, str(script)],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        stdin=subprocess.DEVNULL,
    )


def test_renderer_budget_is_the_cap_the_trusted_workflow_enforces():
    _source, declared = _trusted_validator()
    assert render._MAX_BODY == int(declared["MAX_REPORT_BYTES"])
    assert render.MARKER == declared["TRUSTED_MARKER"]


@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_windows_artifact_at_the_cap_is_accepted_by_the_trusted_validator(
    tmp_path, windows_text_writes, offset
):
    """#1076 end to end: the file main() writes on Windows passes the real check.

    Before the fix all three were rejected ("Report artifact is empty or too
    large"); the 59,999-byte body became the 61,399-byte artifact in the issue.
    """
    _source, declared = _trusted_validator()
    cap = int(declared["MAX_REPORT_BYTES"])
    source = tmp_path / "crg-report.json"
    source.write_bytes(json.dumps(report_rendering_to_exactly(cap + offset)).encode("utf-8"))
    download = tmp_path / "crg-report-download"
    download.mkdir()
    artifact = download / "crg-comment.md"
    code = render.main(
        ["--input", str(source), "--output", str(artifact), "--max-functions", "100000"]
    )
    assert code == 0
    # pr-review.yml writes this on its Linux runner; bytes keep it exact here.
    (download / "pr-number.txt").write_bytes(b"42\n")

    completed = _run_trusted_validator(tmp_path, download)

    assert completed.returncode == 0, completed.stderr or completed.stdout
    wrapped = (tmp_path / "crg-comment-body.md").read_bytes().decode("utf-8")
    assert wrapped.startswith(declared["TRUSTED_MARKER"] + "\n\n## code-review-graph review\n")
    assert wrapped.rstrip("\n").endswith(render.FOOTER)


@pytest.mark.parametrize(
    "report_data",
    [
        pytest.param(json.loads(FIXTURE.read_text(encoding="utf-8")), id="analysis"),
        pytest.param(_NON_ASCII_REPORT, id="non-ascii-names"),
    ],
)
def test_windows_crlf_detect_changes_output_renders_the_exact_artifact(
    tmp_path, windows_text_writes, report_data
):
    """The input the Action really gets on a Windows runner.

    ``detect-changes`` prints ``json.dumps(result, indent=2)`` and the Action
    redirects it into crg-report.json; Windows stdout is text mode, so that
    file arrives ASCII-escaped with CRLF on every line. The artifact must be
    the same LF-only bytes a Linux runner produces from the LF file.
    """
    printed = json.dumps(report_data, indent=2, default=str) + "\n"
    lf_input = tmp_path / "lf.json"
    lf_input.write_bytes(printed.encode("ascii"))
    crlf_input = tmp_path / "crlf.json"
    crlf_input.write_bytes(printed.replace("\n", "\r\n").encode("ascii"))
    lf_out = tmp_path / "lf.md"
    crlf_out = tmp_path / "crlf.md"

    assert render.main(["--input", str(lf_input), "--output", str(lf_out)]) == 0
    assert render.main(["--input", str(crlf_input), "--output", str(crlf_out)]) == 0

    expected = (render.render_markdown(report_data) + "\n").encode("utf-8")
    assert crlf_out.read_bytes() == expected
    assert lf_out.read_bytes() == expected


def test_a_body_that_still_fits_is_not_truncated(tmp_path):
    """The reservation costs one byte, not a whole report.

    A body of _MAX_BODY - 1 leaves exactly room for the newline, so it must
    come through whole; truncating it would trade one bug for another.
    """
    report = report_rendering_to_exactly(render._MAX_BODY - 1)
    source = tmp_path / "report.json"
    source.write_text(json.dumps(report), encoding="utf-8")
    out = tmp_path / "comment.md"
    assert render.main(
        ["--input", str(source), "--output", str(out), "--max-functions", "100000"]
    ) == 0
    text = out.read_text(encoding="utf-8")
    assert "*Report truncated.*" not in text
    assert out.stat().st_size == render._MAX_BODY


def test_a_body_that_only_fits_without_its_newline_is_truncated(tmp_path):
    """And the byte on the other side of the line is cut.

    A body of exactly _MAX_BODY would be a 60,001-byte artifact, which is
    one byte over the cap the consumer enforces.
    """
    report = report_rendering_to_exactly(render._MAX_BODY)
    source = tmp_path / "report.json"
    source.write_text(json.dumps(report), encoding="utf-8")
    out = tmp_path / "comment.md"
    assert render.main(
        ["--input", str(source), "--output", str(out), "--max-functions", "100000"]
    ) == 0
    assert "*Report truncated.*" in out.read_text(encoding="utf-8")


@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_fit_to_budget_reserves_the_newline_the_artifact_carries(offset):
    """The same boundary, straight at ``_fit_to_budget``."""
    size = render._MAX_BODY + offset
    body = ("line\n" * (size // 5)) + "x" * (size % 5)
    assert len(body.encode("utf-8")) == size
    fitted = render._fit_to_budget(body)
    assert len((fitted + "\n").encode("utf-8")) <= render._MAX_BODY
    if offset < 0:
        assert fitted == body, "a body that already fits must not be cut"


# ---------------------------------------------------------------------------
# load_report / no-changes fallback
# ---------------------------------------------------------------------------


def test_load_report_accepts_valid_json(report):
    assert render.load_report(FIXTURE.read_text(encoding="utf-8")) is not None


def test_load_report_rejects_plain_text():
    assert render.load_report("No changes detected.") is None


def test_load_report_rejects_non_object_json():
    assert render.load_report("[1, 2, 3]") is None


def test_render_no_changes_has_marker_and_footer():
    body = render.render_no_changes()
    assert body.splitlines()[0] == render.MARKER
    assert "No analyzable code changes" in body
    assert "Powered by [code-review-graph]" in body


# ---------------------------------------------------------------------------
# main(): file IO + risk gate
# ---------------------------------------------------------------------------


def test_main_writes_output_file(tmp_path):
    out = tmp_path / "comment.md"
    code = render.main(["--input", str(FIXTURE), "--output", str(out)])
    assert code == 0
    body = out.read_text(encoding="utf-8")
    assert body.startswith(render.MARKER)
    assert "Token savings" in body


def test_main_no_changes_input(tmp_path):
    src = tmp_path / "report.json"
    src.write_text("No changes detected.\n", encoding="utf-8")
    out = tmp_path / "comment.md"
    code = render.main(["--input", str(src), "--output", str(out)])
    assert code == 0
    assert "No analyzable code changes" in out.read_text(encoding="utf-8")


def test_is_clean_tree_only_matches_detect_changes_own_line():
    """Anything else non-JSON is the analysis not having happened."""
    assert render.is_clean_tree("No changes detected.\n")
    assert not render.is_clean_tree("")
    assert not render.is_clean_tree("Error: could not determine the changes: ...")
    assert not render.is_clean_tree("No changes detected. Also: git exploded.")


def test_main_not_analyzed_returns_4_and_says_so(tmp_path):
    """A detect-changes failure must not render as an all-clear."""
    src = tmp_path / "report.json"
    src.write_text(
        "Error: could not determine the changes: git could not be run.\n",
        encoding="utf-8",
    )
    out = tmp_path / "comment.md"
    code = render.main(["--input", str(src), "--output", str(out)])
    assert code == 4
    body = out.read_text(encoding="utf-8")
    assert body.startswith(render.MARKER)
    assert "has not been reviewed" in body
    assert "not an all-clear" in body
    assert "No analyzable code changes" not in body


def test_empty_detect_changes_output_is_not_an_all_clear(tmp_path):
    """A command that died before printing anything is not a clean tree."""
    src = tmp_path / "report.json"
    src.write_text("", encoding="utf-8")
    code = render.main(["--input", str(src), "--quiet"])
    assert code == 4


def test_not_analyzed_beats_fail_on_risk_none(tmp_path):
    """An unknown risk is not a low one, so `none` cannot switch it off."""
    src = tmp_path / "report.json"
    src.write_text("Error: boom\n", encoding="utf-8")
    code = render.main(["--input", str(src), "--quiet", "--fail-on-risk", "none"])
    assert code == 4


def test_main_missing_input_returns_2(tmp_path):
    code = render.main(["--input", str(tmp_path / "nope.json"), "--quiet"])
    assert code == 2


def test_fail_on_risk_high_breached(tmp_path):
    code = render.main(["--input", str(FIXTURE), "--quiet", "--fail-on-risk", "high"])
    assert code == 3


def test_fail_on_risk_critical_not_breached(tmp_path):
    code = render.main(["--input", str(FIXTURE), "--quiet", "--fail-on-risk", "critical"])
    assert code == 0


def test_fail_on_risk_none_passes(tmp_path):
    code = render.main(["--input", str(FIXTURE), "--quiet", "--fail-on-risk", "none"])
    assert code == 0


def test_fail_on_risk_passes_for_no_changes(tmp_path):
    src = tmp_path / "report.json"
    src.write_text("No changes detected.\n", encoding="utf-8")
    code = render.main(["--input", str(src), "--quiet", "--fail-on-risk", "high"])
    assert code == 0


def test_quiet_skips_output_file(tmp_path):
    out = tmp_path / "comment.md"
    code = render.main(["--input", str(FIXTURE), "--output", str(out), "--quiet"])
    assert code == 0
    assert not out.exists()


def test_cli_subprocess_stdout():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--input", str(FIXTURE)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0
    assert result.stdout.startswith(render.MARKER)
    assert "Powered by [code-review-graph]" in result.stdout
