"""Tests for scripts/render_pr_comment.py (GitHub Action comment renderer)."""

from __future__ import annotations

import importlib.util
import json
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
