"""Tests for axon-web HTTP handler helpers."""

from code_review_graph.web import INDEX_HTML


def test_index_html_has_api_bootstrap():
    assert "axon-web" in INDEX_HTML
    assert "/api/status" in INDEX_HTML
    assert "/api/graph" in INDEX_HTML
    assert "Tokens saved" in INDEX_HTML
    assert "token_estimate" in INDEX_HTML
    assert "API requests" in INDEX_HTML
    assert "telemetry" in INDEX_HTML
    assert "EventSource" in INDEX_HTML
