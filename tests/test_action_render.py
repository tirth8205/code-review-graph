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
