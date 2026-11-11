"""Regression checks for optional dependency metadata and failure modes."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_review_graph import cli
from code_review_graph.exports import MissingOptionalDependencyError, export_svg

try:
    import tomllib
except ImportError:  # pragma: no cover - Python 3.10
    import tomli as tomllib


ROOT = Path(__file__).parents[1]

SVG_INSTALL_HINT = (
    'Error: SVG export requires matplotlib. '
    'Run: pip install "code-review-graph[eval]"'
)


def _optional_dependencies() -> dict[str, list[str]]:
    with (ROOT / "pyproject.toml").open("rb") as pyproject:
        return tomllib.load(pyproject)["project"]["optional-dependencies"]


def test_google_embeddings_extra_installs_current_google_sdk():
    optional = _optional_dependencies()
    assert optional["google-embeddings"] == ["google-genai>=1.0.0,<3"]


def test_all_extra_includes_google_embeddings():
    optional = _optional_dependencies()
    assert "code-review-graph[google-embeddings]" in optional["all"]


def test_matplotlib_ships_in_the_extra_the_svg_hint_names():
    """The install hint is only useful while the extra really carries it."""
    optional = _optional_dependencies()
    assert any(req.startswith("matplotlib") for req in optional["eval"])


def test_export_svg_without_matplotlib_raises_missing_optional_dependency(monkeypatch):
    # Simulate the default install: matplotlib is not importable.
    monkeypatch.setitem(sys.modules, "matplotlib", None)

    with pytest.raises(MissingOptionalDependencyError) as raised:
        export_svg(MagicMock(), Path("graph.svg"))

    message = str(raised.value)
    assert "matplotlib" in message
    assert 'pip install "code-review-graph[eval]"' in message


def test_visualize_svg_without_matplotlib_prints_one_line_and_exits(
    tmp_path, capsys, monkeypatch
):
    """A missing optional dependency must not surface as a traceback."""
    monkeypatch.setitem(sys.modules, "matplotlib", None)

    data_dir = tmp_path / ".code-review-graph"
    data_dir.mkdir()
    # visualize is read-only for missing graphs (#803); the path must exist.
    db_path = data_dir / "graph.db"
    db_path.touch()
    store = MagicMock()
    argv = [
        "code-review-graph",
        "visualize",
        "--repo",
        str(tmp_path),
        "--format",
        "svg",
    ]

    with patch.object(sys, "argv", argv):
        with patch("code_review_graph.graph.GraphStore", return_value=store):
            with patch(
                "code_review_graph.incremental.get_db_path",
                return_value=db_path,
            ):
                with patch(
                    "code_review_graph.incremental.get_data_dir",
                    return_value=data_dir,
                ):
                    with pytest.raises(SystemExit) as raised:
                        cli.main()

    assert raised.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == SVG_INSTALL_HINT
    assert "Traceback" not in captured.err
    store.close.assert_called_once()


# Every "pip install" hint for an extra must quote it: zsh reads the bare
# square brackets as a glob and fails with "no matches found" (gstack devex
# review, item 12). The four messages below are the ones a user can see.
_UNQUOTED_EXTRA = __import__("re").compile(r"pip install code-review-graph\[")


def test_no_unquoted_extra_hint_anywhere_in_the_package():
    offenders = [
        f"{path.relative_to(ROOT)}:{number}"
        for path in sorted((ROOT / "code_review_graph").rglob("*.py"))
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        )
        if _UNQUOTED_EXTRA.search(line)
    ]
    assert not offenders, f"unquoted pip extras: {offenders}"


def test_local_embedding_hint_quotes_the_extra(monkeypatch):
    from code_review_graph import embeddings

    monkeypatch.setitem(sys.modules, "sentence_transformers", None)
    monkeypatch.setattr(embeddings, "_MODEL_CACHE", {})
    provider = embeddings.LocalEmbeddingProvider("not-cached-model")

    with pytest.raises(ImportError) as raised:
        provider.embed(["x"])

    assert 'pip install "code-review-graph[embeddings]"' in str(raised.value)


def test_embed_graph_hint_quotes_the_extra(tmp_path):
    from code_review_graph.tools import docs

    (tmp_path / ".git").mkdir()
    unavailable = MagicMock(available=False)
    with patch.object(docs, "EmbeddingStore", return_value=unavailable):
        result = docs.embed_graph(repo_root=str(tmp_path))

    assert result["status"] == "error"
    assert 'pip install "code-review-graph[embeddings]"' in result["error"]


def test_eval_yaml_hint_quotes_the_extra(monkeypatch):
    from code_review_graph.eval import runner

    monkeypatch.setattr(runner, "yaml", None)
    with pytest.raises(ImportError) as raised:
        runner._require_yaml()

    assert 'pip install "code-review-graph[eval]"' in str(raised.value)
