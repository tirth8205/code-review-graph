"""Tests for the embeddings module."""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

from code_review_graph.embeddings import (
    LOCAL_DEFAULT_MODEL,
    EmbeddingStore,
    LocalEmbeddingProvider,
    MiniMaxEmbeddingProvider,
    VoyageAIEmbeddingProvider,
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
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CRG_EMBEDDING_MODEL", None)
            os.environ.pop("EMBEDDING_MODEL", None)
            provider = LocalEmbeddingProvider()
            assert provider._model_name == LOCAL_DEFAULT_MODEL
            assert provider.name == f"local:{LOCAL_DEFAULT_MODEL}"

    def test_explicit_model_name(self):
        with patch.dict(os.environ, {"CRG_EMBEDDING_MODEL": "should-be-ignored"}):
            provider = LocalEmbeddingProvider(model_name="custom/model")
            assert provider._model_name == "custom/model"
            assert provider.name == "local:custom/model"

    def test_crg_env_var_fallback(self):
        """CRG_EMBEDDING_MODEL still works for backward compat."""
        with patch.dict(os.environ, {"CRG_EMBEDDING_MODEL": "BAAI/bge-small-en-v1.5"}):
            os.environ.pop("EMBEDDING_MODEL", None)
            provider = LocalEmbeddingProvider()
            assert provider._model_name == "BAAI/bge-small-en-v1.5"
            assert provider.name == "local:BAAI/bge-small-en-v1.5"

    def test_embedding_model_env_var(self):
        """EMBEDDING_MODEL takes precedence over CRG_EMBEDDING_MODEL."""
        with patch.dict(os.environ, {
            "EMBEDDING_MODEL": "new-model",
            "CRG_EMBEDDING_MODEL": "old-model",
        }):
            provider = LocalEmbeddingProvider()
            assert provider._model_name == "new-model"


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


class TestMiniMaxEmbeddingProvider:
    """Unit tests for MiniMaxEmbeddingProvider."""

    def test_name(self):
        provider = MiniMaxEmbeddingProvider(api_key="test-key")
        assert provider.name == "minimax:embo-01"

    def test_dimension(self):
        provider = MiniMaxEmbeddingProvider(api_key="test-key")
        assert provider.dimension == 1536

    def test_embed_calls_api_with_db_type(self):
        provider = MiniMaxEmbeddingProvider(api_key="test-key")
        mock_vectors = [[0.1] * 1536, [0.2] * 1536]
        mock_response = json.dumps({
            "vectors": mock_vectors,
            "total_tokens": 10,
            "base_resp": {"status_code": 0, "status_msg": "success"},
        }).encode("utf-8")

        mock_resp_obj = MagicMock()
        mock_resp_obj.read.return_value = mock_response
        mock_resp_obj.__enter__ = MagicMock(return_value=mock_resp_obj)
        mock_resp_obj.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp_obj) as mock_urlopen:
            result = provider.embed(["hello", "world"])

        assert len(result) == 2
        assert len(result[0]) == 1536
        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        payload = json.loads(req.data.decode("utf-8"))
        assert payload["type"] == "db"
        assert payload["model"] == "embo-01"

    def test_embed_query_calls_api_with_query_type(self):
        provider = MiniMaxEmbeddingProvider(api_key="test-key")
        mock_vectors = [[0.5] * 1536]
        mock_response = json.dumps({
            "vectors": mock_vectors,
            "total_tokens": 5,
            "base_resp": {"status_code": 0, "status_msg": "success"},
        }).encode("utf-8")

        mock_resp_obj = MagicMock()
        mock_resp_obj.read.return_value = mock_response
        mock_resp_obj.__enter__ = MagicMock(return_value=mock_resp_obj)
        mock_resp_obj.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp_obj) as mock_urlopen:
            result = provider.embed_query("search term")

        assert len(result) == 1536
        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        payload = json.loads(req.data.decode("utf-8"))
        assert payload["type"] == "query"

    def test_embed_api_error_raises(self):
        provider = MiniMaxEmbeddingProvider(api_key="test-key")
        mock_response = json.dumps({
            "vectors": [],
            "base_resp": {"status_code": 1001, "status_msg": "invalid api key"},
        }).encode("utf-8")

        mock_resp_obj = MagicMock()
        mock_resp_obj.read.return_value = mock_response
        mock_resp_obj.__enter__ = MagicMock(return_value=mock_resp_obj)
        mock_resp_obj.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp_obj):
            with pytest.raises(RuntimeError, match="invalid api key"):
                provider.embed_query("test")


class TestVoyageAIEmbeddingProvider:
    """Unit tests for VoyageAIEmbeddingProvider."""

    @staticmethod
    def _make_provider(model=None):
        """Create a VoyageAIEmbeddingProvider with a mocked voyageai SDK."""
        mock_voyageai = MagicMock()
        mock_client = mock_voyageai.Client.return_value
        with patch.dict(sys.modules, {"voyageai": mock_voyageai}):
            if model:
                provider = VoyageAIEmbeddingProvider(
                    api_key="test-key", model=model
                )
            else:
                provider = VoyageAIEmbeddingProvider(api_key="test-key")
        return provider, mock_client

    def test_name(self):
        provider, _ = self._make_provider()
        assert provider.name == "voyageai:voyage-code-3"

    def test_name_custom_model(self):
        provider, _ = self._make_provider(model="voyage-3")
        assert provider.name == "voyageai:voyage-3"

    def test_dimension(self):
        provider, _ = self._make_provider()
        assert provider.dimension == 1024

    def test_embed_calls_api_with_document_type(self):
        provider, mock_client = self._make_provider()
        mock_response = MagicMock()
        mock_response.embeddings = [[0.1] * 1024, [0.2] * 1024]
        mock_client.embed.return_value = mock_response

        result = provider.embed(["hello", "world"])

        assert len(result) == 2
        assert len(result[0]) == 1024
        mock_client.embed.assert_called_once_with(
            texts=["hello", "world"],
            model="voyage-code-3",
            input_type="document",
        )

    def test_embed_query_calls_api_with_query_type(self):
        provider, mock_client = self._make_provider()
        mock_response = MagicMock()
        mock_response.embeddings = [[0.5] * 1024]
        mock_client.embed.return_value = mock_response

        result = provider.embed_query("search term")

        assert len(result) == 1024
        mock_client.embed.assert_called_once_with(
            texts=["search term"],
            model="voyage-code-3",
            input_type="query",
        )

    def test_retry_on_transient_error(self):
        provider, mock_client = self._make_provider()
        mock_response = MagicMock()
        mock_response.embeddings = [[0.1] * 1024]

        # First call raises 429, second succeeds
        mock_client.embed.side_effect = [
            Exception("429 rate limit exceeded"),
            mock_response,
        ]

        with patch("code_review_graph.embeddings.time.sleep"):
            result = provider.embed(["test"])

        assert len(result) == 1
        assert mock_client.embed.call_count == 2

    def test_permanent_error_raises_immediately(self):
        provider, mock_client = self._make_provider()
        mock_client.embed.side_effect = Exception("invalid request: bad input")

        with pytest.raises(Exception, match="invalid request"):
            provider.embed(["test"])

        # Should not retry on non-transient error
        assert mock_client.embed.call_count == 1

    def test_import_error_without_sdk(self):
        with patch.dict(sys.modules, {"voyageai": None}):
            with pytest.raises(ImportError, match="voyage-embeddings"):
                VoyageAIEmbeddingProvider(api_key="test-key")


class TestGetProviderVoyage:
    """Tests for get_provider() with Voyage AI."""

    def test_get_provider_voyage_with_key(self):
        mock_voyageai = MagicMock()
        with patch.dict("os.environ", {"VOYAGEAI_API_KEY": "test-key"}):
            with patch.dict(sys.modules, {"voyageai": mock_voyageai}):
                provider = get_provider("voyage")
        assert isinstance(provider, VoyageAIEmbeddingProvider)
        assert provider.name == "voyageai:voyage-code-3"

    def test_get_provider_voyage_without_key_raises(self):
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(ValueError, match="VOYAGEAI_API_KEY"):
                get_provider("voyage")

    def test_get_provider_voyage_missing_sdk_returns_none(self):
        with patch.dict("os.environ", {"VOYAGEAI_API_KEY": "test-key"}):
            with patch.dict(sys.modules, {"voyageai": None}):
                provider = get_provider("voyage")
        assert provider is None


class TestGetProviderMiniMax:
    """Tests for get_provider() with MiniMax."""

    def test_get_provider_minimax_with_key(self):
        with patch.dict("os.environ", {"MINIMAX_API_KEY": "test-key"}):
            provider = get_provider("minimax")
        assert isinstance(provider, MiniMaxEmbeddingProvider)
        assert provider.name == "minimax:embo-01"

    def test_get_provider_minimax_without_key_raises(self):
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(ValueError, match="MINIMAX_API_KEY"):
                get_provider("minimax")


class TestAutoDetectProvider:
    """Tests for auto-detection of embedding provider based on API keys."""

    @patch("code_review_graph.embeddings.LocalEmbeddingProvider")
    def test_no_api_keys_falls_back_to_local(self, mock_local):
        mock_local.return_value = MagicMock()
        with patch.dict("os.environ", {}, clear=True):
            provider = get_provider()
        assert provider is not None
        mock_local.assert_called_once()

    def test_voyageai_key_auto_selects_voyage(self):
        mock_voyageai = MagicMock()
        with patch.dict("os.environ", {"VOYAGEAI_API_KEY": "vk"}, clear=True):
            with patch.dict(sys.modules, {"voyageai": mock_voyageai}):
                provider = get_provider()
        assert isinstance(provider, VoyageAIEmbeddingProvider)

    def test_google_key_auto_selects_google(self):
        with patch.dict("os.environ", {"GOOGLE_API_KEY": "gk"}, clear=True):
            with patch(
                "code_review_graph.embeddings.GoogleEmbeddingProvider"
            ) as mock_cls:
                mock_cls.return_value = MagicMock()
                mock_cls.return_value.name = "google:gemini-embedding-001"
                provider = get_provider()
        assert provider is not None
        mock_cls.assert_called_once()

    def test_minimax_key_auto_selects_minimax(self):
        with patch.dict("os.environ", {"MINIMAX_API_KEY": "mk"}, clear=True):
            provider = get_provider()
        assert isinstance(provider, MiniMaxEmbeddingProvider)

    def test_voyage_has_highest_priority(self):
        mock_voyageai = MagicMock()
        with patch.dict("os.environ", {
            "VOYAGEAI_API_KEY": "vk",
            "GOOGLE_API_KEY": "gk",
            "MINIMAX_API_KEY": "mk",
        }, clear=True):
            with patch.dict(sys.modules, {"voyageai": mock_voyageai}):
                provider = get_provider()
        assert isinstance(provider, VoyageAIEmbeddingProvider)

    @patch("code_review_graph.embeddings.LocalEmbeddingProvider")
    def test_voyage_key_set_but_sdk_missing_skips_to_next(self, mock_local):
        mock_local.return_value = MagicMock()
        with patch.dict("os.environ", {"VOYAGEAI_API_KEY": "vk"}, clear=True):
            with patch.dict(sys.modules, {"voyageai": None}):
                provider = get_provider()
        # Should fall through to local since SDK not installed
        mock_local.assert_called_once()

    def test_embedding_model_env_var_forwarded(self):
        mock_voyageai = MagicMock()
        with patch.dict("os.environ", {
            "VOYAGEAI_API_KEY": "vk",
            "EMBEDDING_MODEL": "voyage-3",
        }, clear=True):
            with patch.dict(sys.modules, {"voyageai": mock_voyageai}):
                provider = get_provider()
        assert isinstance(provider, VoyageAIEmbeddingProvider)
        assert provider.model == "voyage-3"

    def test_explicit_model_param_overrides_env_var(self):
        mock_voyageai = MagicMock()
        with patch.dict("os.environ", {
            "VOYAGEAI_API_KEY": "vk",
            "EMBEDDING_MODEL": "voyage-3",
        }, clear=True):
            with patch.dict(sys.modules, {"voyageai": mock_voyageai}):
                provider = get_provider(model="voyage-code-3")
        assert isinstance(provider, VoyageAIEmbeddingProvider)
        assert provider.model == "voyage-code-3"
