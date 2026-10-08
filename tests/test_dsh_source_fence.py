"""Source snippets cannot read beyond the trusted root via cached graph paths."""
from unittest.mock import MagicMock, patch

from code_review_graph.incremental import _parse_single_file
from code_review_graph.tools._common import _source_path
from code_review_graph.tools.flows_tools import get_flow


def test_cached_source_path_must_stay_in_repository(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    path = root / "main.py"
    path.write_text("safe", encoding="utf-8")
    outside = tmp_path / "outside.py"
    outside.write_text("private", encoding="utf-8")
    assert _source_path(root, "main.py") == path.resolve()
    assert _source_path(root, "../outside.py") is None
    assert _source_path(root, outside) is None


def test_parser_rejects_outside_before_reading(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("def private():\n    pass\n", encoding="utf-8")
    _, nodes, edges, error, file_hash = _parse_single_file(("../outside.py", str(root)))
    assert not nodes and not edges and not file_hash
    assert "escapes repository root" in error


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
