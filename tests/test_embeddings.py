"""Tests for the embeddings module."""

import os
from unittest.mock import MagicMock, patch

from code_review_graph.embeddings import (
    LOCAL_DEFAULT_MODEL,
    EmbeddingStore,
    LocalEmbeddingProvider,
    _cosine_similarity,
    _decode_vector,
    _encode_vector,
    _node_to_text,
    get_provider,
)
from code_review_graph.graph import GraphNode


class TestVectorEncoding:
    def test_roundtrip(self):
        original = [1.0, 2.5, -3.14, 0.0, 100.0]
        blob = _encode_vector(original)
        decoded = _decode_vector(blob)
        assert len(decoded) == len(original)
        for a, b in zip(original, decoded):
            assert abs(a - b) < 1e-5

    def test_empty_vector(self):
        blob = _encode_vector([])
        decoded = _decode_vector(blob)
        assert decoded == []

    def test_blob_size(self):
        vec = [1.0, 2.0, 3.0]
        blob = _encode_vector(vec)
        assert len(blob) == 12  # 3 floats * 4 bytes each


class TestCosineSimilarity:
    def test_identical_vectors(self):
        v = [1.0, 2.0, 3.0]
        assert abs(_cosine_similarity(v, v) - 1.0) < 1e-6

    def test_orthogonal_vectors(self):
        a = [1.0, 0.0]
        b = [0.0, 1.0]
        assert abs(_cosine_similarity(a, b)) < 1e-6

    def test_opposite_vectors(self):
        a = [1.0, 0.0]
        b = [-1.0, 0.0]
        assert abs(_cosine_similarity(a, b) - (-1.0)) < 1e-6

    def test_zero_vector(self):
        a = [0.0, 0.0]
        b = [1.0, 2.0]
        assert _cosine_similarity(a, b) == 0.0

    def test_dimension_mismatch(self):
        a = [1.0, 2.0, 3.0]
        b = [1.0, 2.0]
        assert _cosine_similarity(a, b) == 0.0


class TestNodeToText:
    def _make_node(self, **kwargs):
        defaults = dict(
            id=1, kind="Function", name="my_func",
            qualified_name="file.py::my_func", file_path="file.py",
            line_start=1, line_end=10, language="python",
            parent_name=None, params=None, return_type=None,
            is_test=False, file_hash=None, extra={},
        )
        defaults.update(kwargs)
        return GraphNode(**defaults)

    def test_basic_function(self):
        node = self._make_node()
        text = _node_to_text(node)
        assert "my_func" in text
        assert "function" in text
        assert "python" in text

    def test_method_with_parent(self):
        node = self._make_node(parent_name="MyClass")
        text = _node_to_text(node)
        assert "in MyClass" in text

    def test_with_params_and_return_type(self):
        node = self._make_node(params="(x: int, y: str)", return_type="bool")
        text = _node_to_text(node)
        assert "(x: int, y: str)" in text
        assert "returns bool" in text

    def test_file_node_no_kind(self):
        node = self._make_node(kind="File", name="file.py")
        text = _node_to_text(node)
        # File kind should not add "file" as a kind label
        assert "file.py" in text


class TestEmbeddingStore:
    def test_store_initializes(self, tmp_path):
        db = tmp_path / "embeddings.db"
        with patch("code_review_graph.embeddings.get_provider", return_value=None):
            store = EmbeddingStore(db)
            assert store.count() == 0
            store.close()

    def test_count_empty(self, tmp_path):
        db = tmp_path / "embeddings.db"
        with patch("code_review_graph.embeddings.get_provider", return_value=None):
            store = EmbeddingStore(db)
            assert store.count() == 0
            store.close()

    def test_embed_nodes_returns_zero_when_unavailable(self, tmp_path):
        db = tmp_path / "embeddings.db"
        with patch("code_review_graph.embeddings.get_provider", return_value=None):
            store = EmbeddingStore(db)
            result = store.embed_nodes([])
            assert result == 0
            store.close()

    def test_search_returns_empty_when_unavailable(self, tmp_path):
        db = tmp_path / "embeddings.db"
        with patch("code_review_graph.embeddings.get_provider", return_value=None):
            store = EmbeddingStore(db)
            results = store.search("query")
            assert results == []
            store.close()

    def test_remove_node(self, tmp_path):
        db = tmp_path / "embeddings.db"
        with patch("code_review_graph.embeddings.get_provider", return_value=None):
            store = EmbeddingStore(db)
            # Should not raise even if node doesn't exist
            store.remove_node("nonexistent::func")
            store.close()


class TestLocalEmbeddingProviderModelName:
    """Tests for configurable model name on LocalEmbeddingProvider."""

    def test_default_model_name(self):
        """Without args or env var, uses LOCAL_DEFAULT_MODEL."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CRG_EMBEDDING_MODEL", None)
            provider = LocalEmbeddingProvider()
            assert provider._model_name == LOCAL_DEFAULT_MODEL
            assert provider.name == f"local:{LOCAL_DEFAULT_MODEL}"

    def test_explicit_model_name(self):
        """Explicit model_name param takes priority over env var."""
        with patch.dict(os.environ, {"CRG_EMBEDDING_MODEL": "should-be-ignored"}):
            provider = LocalEmbeddingProvider(model_name="custom/model")
            assert provider._model_name == "custom/model"
            assert provider.name == "local:custom/model"

    def test_env_var_fallback(self):
        """CRG_EMBEDDING_MODEL env var is used when model_name is None."""
        with patch.dict(os.environ, {"CRG_EMBEDDING_MODEL": "BAAI/bge-small-en-v1.5"}):
            provider = LocalEmbeddingProvider()
            assert provider._model_name == "BAAI/bge-small-en-v1.5"
            assert provider.name == "local:BAAI/bge-small-en-v1.5"

    def test_env_var_not_used_when_explicit(self):
        """Explicit model_name='' is falsy but falls through to env var."""
        with patch.dict(os.environ, {"CRG_EMBEDDING_MODEL": "from-env"}):
            provider = LocalEmbeddingProvider(model_name="")
            assert provider._model_name == "from-env"


class TestGetProviderModel:
    """Tests for model parameter in get_provider()."""

    @patch("code_review_graph.embeddings.LocalEmbeddingProvider")
    def test_local_passes_model(self, mock_cls):
        mock_cls.return_value = MagicMock()
        get_provider(provider=None, model="custom/model")
        mock_cls.assert_called_once_with(model_name="custom/model")

    @patch("code_review_graph.embeddings.LocalEmbeddingProvider")
    def test_local_default_passes_none(self, mock_cls):
        mock_cls.return_value = MagicMock()
        get_provider(provider=None, model=None)
        mock_cls.assert_called_once_with(model_name=None)


class TestEmbeddingStoreModelPassthrough:
    """Tests that EmbeddingStore passes model to get_provider."""

    def test_model_forwarded_to_get_provider(self, tmp_path):
        db = tmp_path / "embeddings.db"
        with patch("code_review_graph.embeddings.get_provider", return_value=None) as mock_gp:
            EmbeddingStore(db, model="custom/model").close()
            mock_gp.assert_called_once_with(None, model="custom/model")

    def test_provider_and_model_forwarded(self, tmp_path):
        db = tmp_path / "embeddings.db"
        with patch("code_review_graph.embeddings.get_provider", return_value=None) as mock_gp:
            EmbeddingStore(db, provider="local", model="custom/model").close()
            mock_gp.assert_called_once_with("local", model="custom/model")


class TestReEmbedOnProviderChange:
    """Tests that changing the model triggers re-embedding."""

    def _make_node(self, name="my_func", qn="file.py::my_func"):
        return GraphNode(
            id=1, kind="Function", name=name,
            qualified_name=qn, file_path="file.py",
            line_start=1, line_end=10, language="python",
            parent_name=None, params=None, return_type=None,
            is_test=False, file_hash=None, extra={},
        )

    def _make_mock_provider(self, name="local:model-a", dim=3):
        provider = MagicMock()
        provider.name = name
        provider.dimension = dim
        provider.embed.return_value = [[1.0, 0.0, 0.0]]
        provider.embed_query.return_value = [1.0, 0.0, 0.0]
        return provider

    def test_same_provider_skips_reembed(self, tmp_path):
        db = tmp_path / "embeddings.db"
        provider = self._make_mock_provider("local:model-a")
        node = self._make_node()

        with patch("code_review_graph.embeddings.get_provider", return_value=provider):
            store = EmbeddingStore(db)
            assert store.embed_nodes([node]) == 1  # first time
            assert store.embed_nodes([node]) == 0  # cached, skip
            store.close()

    def test_different_provider_triggers_reembed(self, tmp_path):
        db = tmp_path / "embeddings.db"
        node = self._make_node()

        provider_a = self._make_mock_provider("local:model-a")
        with patch("code_review_graph.embeddings.get_provider", return_value=provider_a):
            store = EmbeddingStore(db)
            assert store.embed_nodes([node]) == 1
            store.close()

        provider_b = self._make_mock_provider("local:model-b")
        with patch("code_review_graph.embeddings.get_provider", return_value=provider_b):
            store = EmbeddingStore(db)
            assert store.embed_nodes([node]) == 1  # re-embedded!
            store.close()
