"""Tests for the embeddings module."""

import json
from unittest.mock import MagicMock, patch

import pytest

from code_review_graph.embeddings import (
    EmbeddingStore,
    MiniMaxEmbeddingProvider,
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


class TestMiniMaxEmbeddingProvider:
    """Unit tests for MiniMaxEmbeddingProvider."""

    def test_name(self):
        provider = MiniMaxEmbeddingProvider(api_key="test-key")
        assert provider.name == "minimax:embo-01"

    def test_dimension(self):
        provider = MiniMaxEmbeddingProvider(api_key="test-key")
        assert provider.dimension == 1536

    def test_embed_calls_api_with_db_type(self):
        """embed() should call the API with type='db' for indexing."""
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
        # Verify the request payload
        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        payload = json.loads(req.data.decode("utf-8"))
        assert payload["type"] == "db"
        assert payload["model"] == "embo-01"
        assert payload["texts"] == ["hello", "world"]

    def test_embed_query_calls_api_with_query_type(self):
        """embed_query() should call the API with type='query' for search."""
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
        assert payload["texts"] == ["search term"]

    def test_embed_sets_authorization_header(self):
        """API calls should include the Bearer token."""
        provider = MiniMaxEmbeddingProvider(api_key="sk-test-123")
        mock_response = json.dumps({
            "vectors": [[0.1] * 1536],
            "total_tokens": 1,
            "base_resp": {"status_code": 0, "status_msg": "success"},
        }).encode("utf-8")

        mock_resp_obj = MagicMock()
        mock_resp_obj.read.return_value = mock_response
        mock_resp_obj.__enter__ = MagicMock(return_value=mock_resp_obj)
        mock_resp_obj.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp_obj) as mock_urlopen:
            provider.embed_query("test")

        req = mock_urlopen.call_args[0][0]
        assert req.get_header("Authorization") == "Bearer sk-test-123"

    def test_embed_api_error_raises(self):
        """Non-zero status_code in base_resp should raise RuntimeError."""
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

    def test_embed_batches_large_input(self):
        """Inputs larger than batch_size=100 should be split into batches."""
        provider = MiniMaxEmbeddingProvider(api_key="test-key")
        texts = [f"text_{i}" for i in range(150)]

        call_count = 0

        def mock_urlopen(req, timeout=None):
            nonlocal call_count
            call_count += 1
            payload = json.loads(req.data.decode("utf-8"))
            n = len(payload["texts"])
            mock_response = json.dumps({
                "vectors": [[0.1] * 1536] * n,
                "total_tokens": n,
                "base_resp": {"status_code": 0, "status_msg": "success"},
            }).encode("utf-8")
            resp = MagicMock()
            resp.read.return_value = mock_response
            resp.__enter__ = MagicMock(return_value=resp)
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            result = provider.embed(texts)

        assert len(result) == 150
        assert call_count == 2  # 100 + 50

    def test_embed_retries_on_429(self):
        """Should retry on rate-limit (429) errors."""
        provider = MiniMaxEmbeddingProvider(api_key="test-key")

        attempt = 0

        def mock_urlopen(req, timeout=None):
            nonlocal attempt
            attempt += 1
            if attempt == 1:
                raise Exception("HTTP Error 429: Too Many Requests")
            mock_response = json.dumps({
                "vectors": [[0.1] * 1536],
                "total_tokens": 1,
                "base_resp": {"status_code": 0, "status_msg": "success"},
            }).encode("utf-8")
            resp = MagicMock()
            resp.read.return_value = mock_response
            resp.__enter__ = MagicMock(return_value=resp)
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            with patch("time.sleep"):  # Skip actual sleep
                result = provider.embed_query("test")

        assert len(result) == 1536
        assert attempt == 2

    def test_embed_no_retry_on_auth_error(self):
        """Should NOT retry on authentication errors (non-retryable)."""
        provider = MiniMaxEmbeddingProvider(api_key="bad-key")

        with patch("urllib.request.urlopen", side_effect=Exception("HTTP Error 401: Unauthorized")):
            with pytest.raises(Exception, match="401"):
                provider.embed_query("test")


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

    def test_get_provider_none_returns_local(self):
        """Default provider (None) should attempt local."""
        # We just verify it doesn't return MiniMax
        result = get_provider(None)
        if result is not None:
            assert "minimax" not in result.name

    def test_get_provider_unknown_falls_to_local(self):
        """Unknown provider name should fall through to local."""
        result = get_provider("unknown_provider")
        # Should try local (may return None if sentence-transformers not installed)
        if result is not None:
            assert "minimax" not in result.name


class TestMiniMaxIntegration:
    """Integration tests for MiniMax embedding provider (require MINIMAX_API_KEY)."""

    @pytest.fixture
    def api_key(self):
        import os
        key = os.environ.get("MINIMAX_API_KEY")
        if not key:
            pytest.skip("MINIMAX_API_KEY not set")
        return key

    def test_embed_single_text(self, api_key):
        provider = MiniMaxEmbeddingProvider(api_key=api_key)
        result = provider.embed(["hello world"])
        assert len(result) == 1
        assert len(result[0]) == 1536
        assert all(isinstance(v, float) for v in result[0])

    def test_embed_query_returns_vector(self, api_key):
        provider = MiniMaxEmbeddingProvider(api_key=api_key)
        result = provider.embed_query("search for functions")
        assert len(result) == 1536
        assert all(isinstance(v, float) for v in result)

    def test_embed_multiple_texts(self, api_key):
        provider = MiniMaxEmbeddingProvider(api_key=api_key)
        result = provider.embed(["hello", "world", "test"])
        assert len(result) == 3
        for vec in result:
            assert len(vec) == 1536
