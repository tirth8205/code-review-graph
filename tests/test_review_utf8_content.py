"""Review snippets must preserve source text on legacy locale defaults."""

from pathlib import Path
from unittest.mock import MagicMock

from code_review_graph.tools.review import get_review_context


def test_review_snippet_preserves_utf8_under_legacy_default(tmp_path, monkeypatch):
    source = tmp_path / "module.py"
    source.write_text('message = "你好 café"\n', encoding="utf-8")
    original = Path.read_text

    def legacy_read(path, encoding=None, errors=None, **kwargs):
        return original(path, encoding=encoding or "cp1252", errors=errors, **kwargs)

    monkeypatch.setattr(Path, "read_text", legacy_read)
    store = MagicMock()
    store.get_impact_radius.return_value = {
        "changed_nodes": [],
        "impacted_nodes": [],
        "impacted_files": [],
        "edges": [],
    }
    monkeypatch.setattr(
        "code_review_graph.tools.review._get_store",
        lambda _root: (store, tmp_path),
    )
    result = get_review_context(changed_files=["module.py"], repo_root=str(tmp_path))
    assert result["status"] == "ok"
    assert result["context"]["source_snippets"]["module.py"] == '1: message = "你好 café"'
