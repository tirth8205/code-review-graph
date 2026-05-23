"""Tests for the stdio LSP bridge helpers."""

from code_review_graph.lsp import path_to_uri, uri_to_path


def test_file_uri_roundtrip(tmp_path):
    path = tmp_path / "example.py"
    path.write_text("def f():\n    pass\n", encoding="utf-8")

    uri = path_to_uri(str(path))

    assert uri.startswith("file://")
    assert uri_to_path(uri) == str(path)
