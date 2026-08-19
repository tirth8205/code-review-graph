"""Review-guidance consistency with lifecycle test-gap exemptions (#865)."""

from types import SimpleNamespace

from code_review_graph.tools.review import _generate_review_guidance


def _function(name: str, qualified_name: str) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        qualified_name=qualified_name,
        kind="Function",
        is_test=False,
    )


def test_review_guidance_exempts_lifecycle_methods():
    impact = {
        "changed_nodes": [
            _function("setUp", "tests::setUp"),
            _function("refresh_token", "auth::refresh_token"),
        ],
        "edges": [],
        "impacted_nodes": [],
        "impacted_files": [],
    }

    guidance = _generate_review_guidance(impact, ["auth.py"])

    assert "setUp" not in guidance
    assert "1 changed function(s) lack test coverage: refresh_token" in guidance
