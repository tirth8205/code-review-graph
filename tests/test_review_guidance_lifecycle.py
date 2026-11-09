"""Review surfaces agree with the lifecycle test-gap exemption (#865, PR #871)."""

import pytest

from code_review_graph.changes import compute_risk_score
from code_review_graph.graph import GraphStore
from code_review_graph.parser import CodeParser, EdgeInfo, NodeInfo
from code_review_graph.tools import review
from code_review_graph.tools.review import _generate_review_guidance


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A repo whose ``auth.py`` holds a lifecycle method and a real function."""
    db = tmp_path / "graph.db"
    with GraphStore(db) as store:
        for name, path in (
            ("setUp", "auth.py"),
            ("refresh_token", "auth.py"),
            ("__init__", "only_lifecycle.py"),
            ("tearDown", "only_lifecycle.py"),
        ):
            store.upsert_node(NodeInfo(
                kind="Function", name=name, file_path=path,
                line_start=1, line_end=3, language="python",
            ))
        store.commit()
    monkeypatch.setattr(review, "_get_store", lambda _: (GraphStore(db), tmp_path))
    monkeypatch.setattr(review, "resolve_review_base", lambda root, base: base)
    return db


def _impact(store, path):
    return {
        "changed_nodes": store.get_nodes_by_file(path),
        "edges": [],
        "impacted_nodes": [],
        "impacted_files": [],
    }


def test_guidance_omits_lifecycle_methods_and_keeps_real_gaps(repo):
    with GraphStore(repo) as store:
        guidance = _generate_review_guidance(_impact(store, "auth.py"), ["auth.py"])
    assert "setUp" not in guidance
    assert "1 changed function(s) have no direct test: refresh_token" in guidance


def test_guidance_with_only_lifecycle_methods_reports_no_gap(repo):
    with GraphStore(repo) as store:
        guidance = _generate_review_guidance(
            _impact(store, "only_lifecycle.py"), ["only_lifecycle.py"],
        )
    assert "no direct test" not in guidance
    assert "well-contained" in guidance


@pytest.mark.parametrize(
    ("path", "gaps"), [("auth.py", 1), ("only_lifecycle.py", 0)],
)
def test_minimal_review_context_does_not_count_lifecycle_methods(repo, path, gaps):
    result = review.get_review_context(changed_files=[path], detail_level="minimal")
    assert result["test_gaps"] == gaps


def test_standard_review_context_guidance_omits_lifecycle_methods(repo):
    result = review.get_review_context(changed_files=["auth.py"], include_source=False)
    guidance = result["context"]["review_guidance"]
    assert "setUp" not in guidance
    assert "refresh_token" in guidance


def test_parsed_test_lifecycle_method_has_no_coverage_risk(tmp_path):
    source = tmp_path / "tests" / "test_widget.py"
    source.parent.mkdir()
    source.write_text(
        "import unittest\n\n\n"
        "class TestWidget(unittest.TestCase):\n"
        "    def setUp(self):\n"
        "        self.value = 1\n",
        encoding="utf-8",
    )
    nodes, edges = CodeParser().parse_file(source)
    with GraphStore(tmp_path / "graph.db") as store:
        store.store_file_nodes_edges(str(source), nodes, edges, "")
        store.commit()
        node = next(n for n in store.get_nodes_by_file(str(source)) if n.name == "setUp")
        assert compute_risk_score(store, node) == 0.0


def test_covered_test_lifecycle_method_also_scores_no_coverage_term(tmp_path):
    with GraphStore(tmp_path / "graph.db") as store:
        path = "tests/test_widget.py"
        store.upsert_node(NodeInfo(
            kind="Function", name="setUp", file_path=path,
            line_start=1, line_end=3, language="python",
        ))
        store.upsert_edge(EdgeInfo(
            kind="TESTED_BY", source=f"{path}::setUp", target=f"{path}::test_x",
            file_path=path, line=1,
        ))
        store.commit()
        node = store.get_node(f"{path}::setUp")
        assert compute_risk_score(store, node) == 0.0
