"""Change analysis is complete by default and honours caller limits exactly.

``detect_changes_tool`` and ``get_review_context_tool`` used to clamp every
list to hidden ceilings (200 files, 100 changed functions, 200 flows) and
``analyze_changes`` silently stopped at ``CRG_MAX_CHANGED_FUNCS=500``. A
reviewer could not tell a small change from a truncated one. These tests
build one real graph that is larger than every former ceiling and prove:

* with no caller limit the whole analysis is returned and nothing reports
  truncation;
* an explicit caller limit -- including one above a former ceiling -- returns
  exactly that many items, keeps the untruncated ``*_total`` counts, and
  states the cut in ``truncated`` and the summary.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from code_review_graph import main as crg_main
from code_review_graph.changes import analyze_changes
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build
from code_review_graph.parser import normalize_file_path

# 18 packages x 12 modules x 3 functions = 648 functions (former cap: 500)
# in 234 files (former cap: 200). Each module's last function is an
# uncalled entry point, giving one flow per module: 216 flows (former cap:
# 200).
_PACKAGES = 18
_MODULES_PER_PACKAGE = 12
_FUNCS_PER_MODULE = 3


def _write_repo(root: Path) -> list[str]:
    (root / ".code-review-graph").mkdir(parents=True, exist_ok=True)
    rel_paths: list[str] = []
    for pkg in range(_PACKAGES):
        pkg_dir = root / f"pkg{pkg}"
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").write_text("", encoding="utf-8")
        rel_paths.append(f"pkg{pkg}/__init__.py")
        for mod in range(_MODULES_PER_PACKAGE):
            lines = [f'"""Module {pkg}.{mod}."""', ""]
            for fn in range(_FUNCS_PER_MODULE):
                lines.append(f"def step_{pkg}_{mod}_{fn}(value):")
                if fn > 0:
                    lines.append(f"    value = step_{pkg}_{mod}_{fn - 1}(value)")
                lines.append("    return value + 1")
                lines.append("")
            (pkg_dir / f"mod{mod}.py").write_text("\n".join(lines), encoding="utf-8")
            rel_paths.append(f"pkg{pkg}/mod{mod}.py")
    return rel_paths


@pytest.fixture(scope="module")
def big_change(tmp_path_factory) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("complete-change-analysis")
    rel_paths = _write_repo(root)
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("CRG_SERIAL_PARSE", "1")
        with GraphStore(root / ".code-review-graph" / "graph.db") as store:
            full_build(root, store)
        asyncio.run(crg_main.run_postprocess_tool(repo_root=str(root)))
    return {"root": str(root), "files": rel_paths}


@pytest.fixture(autouse=True)
def _no_opt_in_bounds(monkeypatch):
    """Measure the product defaults, not the developer's shell."""
    monkeypatch.delenv("CRG_MAX_CHANGED_FUNCS", raising=False)
    monkeypatch.delenv("CRG_DETAIL_LEVEL", raising=False)


def _detect(big_change: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    return asyncio.run(
        crg_main.detect_changes_tool(
            repo_root=big_change["root"],
            changed_files=big_change["files"],
            detail_level="standard",
            **kwargs,
        )
    )


def _review(big_change: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    return asyncio.run(
        crg_main.get_review_context_tool(
            repo_root=big_change["root"],
            changed_files=big_change["files"],
            detail_level="standard",
            **kwargs,
        )
    )


def test_fixture_exceeds_every_former_ceiling(big_change):
    assert len(big_change["files"]) > 200
    result = _detect(big_change)
    assert result["changed_functions_total"] > 500
    assert result["affected_flows_total"] > 200


class TestCompleteByDefault:
    def test_analyze_changes_returns_every_changed_function(self, big_change):
        root = Path(big_change["root"])
        abs_files = [normalize_file_path(root / f) for f in big_change["files"]]
        with GraphStore(root / ".code-review-graph" / "graph.db") as store:
            result = analyze_changes(store, abs_files)
        expected = _PACKAGES * _MODULES_PER_PACKAGE * _FUNCS_PER_MODULE
        assert result["changed_functions_total"] == expected
        assert len(result["changed_functions"]) == expected
        assert result["functions_truncated"] is False
        assert len(result["affected_flows"]) > 200

    def test_detect_changes_returns_the_complete_analysis(self, big_change):
        result = _detect(big_change)

        assert result["status"] == "ok"
        assert result["truncated"] is False
        assert result["functions_truncated"] is False
        assert "omitted" not in result
        assert "Response bounded" not in result["summary"]
        assert len(result["changed_functions"]) == result["changed_functions_total"]
        assert len(result["test_gaps"]) == result["test_gaps_total"]
        assert len(result["affected_flows"]) == result["affected_flows_total"]
        assert result["changed_files"] == big_change["files"]
        assert result["changed_file_count"] == len(big_change["files"])

    def test_detect_changes_reports_source_cut_without_losing_functions(self, big_change):
        result = _detect(big_change, include_source=True)

        assert result["truncated"] is True
        assert result["source_truncated"] is True
        assert len(result["changed_functions"]) == result["changed_functions_total"]
        assert 0 < sum("source" in func for func in result["changed_functions"]) < len(
            result["changed_functions"]
        )

    def test_review_context_returns_the_complete_context(self, big_change):
        result = _review(big_change, include_source=False)

        context = result["context"]
        graph = context["graph"]
        assert result["status"] == "ok"
        assert "omitted" not in result
        assert context["truncated"] is False
        assert context["impact_truncated"] is False
        assert set(context["changed_files"]) == set(big_change["files"])
        scores = [context["file_risk"][path] for path in context["changed_files"]]
        assert scores == sorted(scores, reverse=True)
        assert context["changed_files_total"] == len(big_change["files"])
        assert len(graph["changed_nodes"]) == graph["changed_nodes_total"]
        assert len(graph["changed_nodes"]) > 500
        assert len(graph["impacted_nodes"]) == graph["impacted_nodes_total"]
        assert len(graph["edges"]) == graph["edges_total"]
        assert "showing" not in result["summary"]


class TestCallerLimitsAreExact:
    def test_detect_changes_small_limits(self, big_change):
        result = _detect(big_change, max_results=7, max_flows=3)

        assert result["truncated"] is True
        assert len(result["changed_functions"]) == 7
        assert result["changed_functions_total"] > 500
        assert len(result["test_gaps"]) == 7
        assert result["test_gaps_total"] > 7
        assert len(result["changed_files"]) == 7
        assert result["changed_file_count"] == len(big_change["files"])
        assert len(result["affected_flows"]) == 3
        assert result["affected_flows_total"] > 200
        assert (
            f"Response bounded: 7 of {result['changed_functions_total']} changed function(s)"
        ) in result["summary"]
        assert f"3 of {result['affected_flows_total']} flow(s)" in result["summary"]
        assert (f"7 of {len(big_change['files'])} changed file(s) shown") in result["summary"]

    def test_detect_changes_limits_above_former_ceilings(self, big_change):
        """450 functions and 210 flows are more than the old 100/200 clamps."""
        result = _detect(big_change, max_results=450, max_flows=210)

        assert result["truncated"] is True
        assert len(result["changed_functions"]) == 450
        assert len(result["changed_files"]) == len(big_change["files"])
        assert len(result["affected_flows"]) == 210
        assert result["affected_flows_total"] > 210

    def test_detect_changes_token_budget_reports_omissions(self, big_change):
        result = _detect(big_change, max_tokens=20_000)

        assert result["truncated"] is True
        omitted = result["omitted"]
        assert omitted["max_tokens"] == 20_000
        assert omitted["changed_functions"] + omitted["affected_flows"] > 0
        assert omitted["note"] in result["summary"]
        assert result["changed_functions_total"] > len(result["changed_functions"])

    def test_detect_changes_opt_in_function_bound_is_reported(
        self,
        big_change,
        monkeypatch,
    ):
        monkeypatch.setenv("CRG_MAX_CHANGED_FUNCS", "40")
        result = _detect(big_change)

        expected = _PACKAGES * _MODULES_PER_PACKAGE * _FUNCS_PER_MODULE
        assert result["truncated"] is True
        assert result["functions_truncated"] is True
        assert len(result["changed_functions"]) == 40
        assert result["changed_functions_total"] == expected
        assert f"limited to 40 of {expected} changed functions" in result["summary"]
        assert f"40 of {expected} changed function(s)" in result["summary"]

    def test_review_context_small_limits(self, big_change):
        result = _review(big_change, max_results=5, max_files=4)

        context = result["context"]
        graph = context["graph"]
        assert context["truncated"] is True
        assert len(context["changed_files"]) == 4
        assert context["changed_files_total"] == len(big_change["files"])
        assert len(graph["changed_nodes"]) == 5
        assert graph["changed_nodes_total"] > 500
        assert len(graph["edges"]) == 5
        assert graph["edges_total"] > 5
        assert len(context["source_snippets"]) == 4
        assert f"showing 4 of {len(big_change['files'])}" in result["summary"]
        assert f"showing 5 of {graph['changed_nodes_total']}" in result["summary"]

    def test_review_context_limits_above_former_ceilings(self, big_change):
        """220 files is more than the old 200-file clamp."""
        result = _review(big_change, max_results=600, max_files=220)

        context = result["context"]
        assert len(context["changed_files"]) == 220
        assert 0 < len(context["source_snippets"]) < 220
        assert context["source_regions"]["shown"] < context["source_regions"]["total"]
        assert len(context["graph"]["changed_nodes"]) == 600
        assert context["truncated"] is True

    def test_review_context_token_budget_reports_omissions(self, big_change):
        baseline = _review(big_change)["context"]
        result = _review(big_change, max_tokens=5_000)

        assert result["context"]["truncated"] is True
        omitted = result["omitted"]
        assert omitted["source_files"] > 0
        assert omitted["note"] in result["summary"]
        assert len(result["context"]["source_snippets"]) + omitted["source_files"] == len(
            baseline["source_snippets"]
        )
        assert result["context"]["graph"] == baseline["graph"]
        assert result["context"]["source_regions"]["shown"] < baseline["source_regions"]["shown"]

    @pytest.mark.parametrize(
        ("tool", "kwargs"),
        [
            ("detect_changes_tool", {"max_results": 0}),
            ("detect_changes_tool", {"max_flows": True}),
            ("get_review_context_tool", {"max_files": 0}),
            ("get_review_context_tool", {"max_results": -1}),
        ],
    )
    def test_invalid_limits_raise(self, big_change, tool, kwargs):
        func = getattr(crg_main, tool)
        with pytest.raises(ValueError, match="greater than or equal to 1"):
            result = func(repo_root=big_change["root"], **kwargs)
            if asyncio.iscoroutine(result):
                asyncio.run(result)
