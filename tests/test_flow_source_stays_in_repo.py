"""A flow step's cached path never inlines source from outside the repository.

From PR #1101. The confinement itself lives in ``path_within_repo`` (#269).
"""
from unittest.mock import MagicMock, patch

from code_review_graph.tools.flows_tools import get_flow


def test_flow_does_not_inline_outside_cached_path(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("PRIVATE_SOURCE_MARKER", encoding="utf-8")
    flow = {
        "name": "cached", "node_count": 1, "depth": 1, "criticality": 0.5,
        "steps": [{"file": str(outside), "line_start": 1, "line_end": 1}],
    }
    with (
        patch("code_review_graph.tools.flows_tools._get_store", return_value=(MagicMock(), root)),
        patch("code_review_graph.tools.flows_tools.get_flow_by_id", return_value=flow),
    ):
        result = get_flow(repo_root=str(root), flow_id=1, include_source=True)
    assert result["status"] == "ok"
    assert "source" not in result["flow"]["steps"][0]
    assert "PRIVATE_SOURCE_MARKER" not in str(result)
