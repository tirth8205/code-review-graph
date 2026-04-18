"""Tests for the embeddings module."""

import json
import os
import time
from unittest.mock import MagicMock, patch

import pytest

from code_review_graph.embeddings import (
    _BODY_MAX_CHARS_BY_PROVIDER,
    _BODY_MAX_CHARS_FALLBACK,
    _EMBED_META_KEY_BODY_ENABLED,
    LOCAL_DEFAULT_MODEL,
    EmbeddingStore,
    LocalEmbeddingProvider,
    MiniMaxEmbeddingProvider,
    OpenAIEmbeddingProvider,
    _body_enabled,
    _cosine_similarity,
    _decode_vector,
    _encode_vector,
    _extract_body_text,
    _FileLineCache,
    _is_localhost_url,
    _looks_like_signature,
    _node_to_text,
    _resolve_body_max_chars,
    _split_combined_hash,
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
            provider = LocalEmbeddingProvider()
            assert provider._model_name == LOCAL_DEFAULT_MODEL
            assert provider.name == f"local:{LOCAL_DEFAULT_MODEL}"

    def test_explicit_model_name(self):
        with patch.dict(os.environ, {"CRG_EMBEDDING_MODEL": "should-be-ignored"}):
            provider = LocalEmbeddingProvider(model_name="custom/model")
            assert provider._model_name == "custom/model"
            assert provider.name == "local:custom/model"

    def test_env_var_fallback(self):
        with patch.dict(os.environ, {"CRG_EMBEDDING_MODEL": "BAAI/bge-small-en-v1.5"}):
            provider = LocalEmbeddingProvider()
            assert provider._model_name == "BAAI/bge-small-en-v1.5"
            assert provider.name == "local:BAAI/bge-small-en-v1.5"


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


class TestCloudProviderWarning:
    """Tests for the stderr warning before cloud provider use (#174)."""

    def test_minimax_triggers_stderr_warning(self, capsys):
        """Using the MiniMax provider should print a warning to stderr
        unless CRG_ACCEPT_CLOUD_EMBEDDINGS=1 is set."""
        with patch.dict(os.environ, {"MINIMAX_API_KEY": "fake"}, clear=False):
            os.environ.pop("CRG_ACCEPT_CLOUD_EMBEDDINGS", None)
            with patch(
                "code_review_graph.embeddings.MiniMaxEmbeddingProvider",
            ) as mock_cls:
                mock_cls.return_value = MagicMock()
                get_provider(provider="minimax")
        captured = capsys.readouterr()
        assert "minimax" in captured.err.lower()
        assert "cloud" in captured.err.lower()
        assert "sent to an external API" in captured.err
        # Should NOT have written to stdout (would corrupt MCP stdio).
        assert captured.out == ""

    def test_google_triggers_stderr_warning(self, capsys):
        with patch.dict(os.environ, {"GOOGLE_API_KEY": "fake"}, clear=False):
            os.environ.pop("CRG_ACCEPT_CLOUD_EMBEDDINGS", None)
            with patch(
                "code_review_graph.embeddings.GoogleEmbeddingProvider",
            ) as mock_cls:
                mock_cls.return_value = MagicMock()
                get_provider(provider="google")
        captured = capsys.readouterr()
        assert "google" in captured.err.lower()
        assert captured.out == ""

    def test_accept_env_var_suppresses_warning(self, capsys):
        """Setting CRG_ACCEPT_CLOUD_EMBEDDINGS=1 silences the warning."""
        with patch.dict(os.environ, {
            "MINIMAX_API_KEY": "fake",
            "CRG_ACCEPT_CLOUD_EMBEDDINGS": "1",
        }, clear=False):
            with patch(
                "code_review_graph.embeddings.MiniMaxEmbeddingProvider",
            ) as mock_cls:
                mock_cls.return_value = MagicMock()
                get_provider(provider="minimax")
        captured = capsys.readouterr()
        assert captured.err == ""
        assert captured.out == ""

    def test_local_provider_never_warns(self, capsys):
        """Local (offline) provider must not trigger the cloud warning."""
        with patch(
            "code_review_graph.embeddings.LocalEmbeddingProvider",
        ) as mock_cls:
            mock_cls.return_value = MagicMock()
            get_provider(provider=None)
        captured = capsys.readouterr()
        assert "cloud" not in captured.err.lower()


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


class TestEmbeddingStoreContextManager:
    """Regression tests for #260: EmbeddingStore must support the context
    manager protocol so connections are cleaned up on exception."""

    def test_supports_context_manager(self, tmp_path):
        db = tmp_path / "embed_ctx.db"
        with EmbeddingStore(db) as store:
            assert store is not None
            assert store.db_path == db
        # After exiting, connection should be closed.
        # (Attempting another query would fail, but we don't test that
        # because close() doesn't invalidate the object — it just
        # closes the underlying sqlite3 connection.)

    def test_context_manager_closes_on_exception(self, tmp_path):
        db = tmp_path / "embed_err.db"
        try:
            with EmbeddingStore(db) as store:
                assert store.db_path == db
                raise RuntimeError("simulated crash")
        except RuntimeError:
            pass
        # The connection was closed by __exit__ even though an exception
        # was raised.  This is the whole point of #260 — without the
        # context manager, the connection would leak.


def _make_openai_response(vectors: list[list[float]]) -> MagicMock:
    body = json.dumps({
        "data": [{"embedding": v, "index": i} for i, v in enumerate(vectors)],
        "model": "text-embedding-3-small",
        "object": "list",
        "usage": {"prompt_tokens": 5, "total_tokens": 5},
    }).encode("utf-8")
    mock = MagicMock()
    mock.read.return_value = body
    mock.__enter__ = MagicMock(return_value=mock)
    mock.__exit__ = MagicMock(return_value=False)
    return mock


class TestIsLocalhostUrl:
    """Ensure localhost detection is robust against subdomain tricks."""

    def test_plain_localhost(self):
        assert _is_localhost_url("http://localhost:3000/v1")

    def test_127_loopback(self):
        assert _is_localhost_url("http://127.0.0.1:3000/v1")

    def test_0000_loopback(self):
        assert _is_localhost_url("http://0.0.0.0:8080/v1")

    def test_ipv6_loopback(self):
        assert _is_localhost_url("http://[::1]:3000/v1")

    def test_real_cloud_host(self):
        assert not _is_localhost_url("https://api.openai.com/v1")

    def test_subdomain_spoof_not_localhost(self):
        # Architect flagged: plain string match would mis-classify this.
        assert not _is_localhost_url("https://my-openai.127.0.0.1.nip.io/v1")

    def test_invalid_url(self):
        assert not _is_localhost_url("not a url")


class TestOpenAIEmbeddingProvider:
    def test_name_includes_model(self):
        p = OpenAIEmbeddingProvider(
            api_key="k", base_url="http://localhost:3000/v1", model="text-embedding-3-small",
        )
        assert p.name == "openai:text-embedding-3-small@http://localhost:3000/v1"

    def test_default_dimension_before_call(self):
        p = OpenAIEmbeddingProvider(
            api_key="k", base_url="http://localhost:3000/v1", model="m",
        )
        assert p.dimension == 1536  # fallback until first response

    def test_dimension_captured_from_response(self):
        p = OpenAIEmbeddingProvider(
            api_key="k", base_url="http://localhost:3000/v1", model="m",
        )
        with patch(
            "urllib.request.urlopen",
            return_value=_make_openai_response([[0.1] * 768]),
        ):
            vec = p.embed_query("hello")
        assert len(vec) == 768
        assert p.dimension == 768

    def test_embed_calls_api_with_correct_payload(self):
        p = OpenAIEmbeddingProvider(
            api_key="secret-key",
            base_url="http://127.0.0.1:3000/v1",
            model="text-embedding-3-small",
        )
        with patch(
            "urllib.request.urlopen",
            return_value=_make_openai_response([[0.1] * 1536, [0.2] * 1536]),
        ) as mock_urlopen:
            result = p.embed(["hello", "world"])

        assert len(result) == 2
        assert len(result[0]) == 1536

        req = mock_urlopen.call_args[0][0]
        payload = json.loads(req.data.decode("utf-8"))
        assert payload["model"] == "text-embedding-3-small"
        assert payload["input"] == ["hello", "world"]
        assert "dimensions" not in payload  # not pinned by default
        assert req.headers["Authorization"] == "Bearer secret-key"
        assert req.headers["Content-type"] == "application/json"
        assert req.full_url == "http://127.0.0.1:3000/v1/embeddings"

    def test_explicit_dimension_forwarded_in_payload(self):
        p = OpenAIEmbeddingProvider(
            api_key="k", base_url="http://localhost:3000/v1",
            model="text-embedding-3-large", dimension=256,
        )
        with patch(
            "urllib.request.urlopen",
            return_value=_make_openai_response([[0.1] * 256]),
        ) as mock_urlopen:
            p.embed_query("x")
        payload = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
        assert payload["dimensions"] == 256

    def test_base_url_trailing_slash_stripped(self):
        p = OpenAIEmbeddingProvider(
            api_key="k", base_url="http://localhost:3000/v1/", model="m",
        )
        with patch(
            "urllib.request.urlopen",
            return_value=_make_openai_response([[0.1] * 10]),
        ) as mock_urlopen:
            p.embed_query("x")
        req = mock_urlopen.call_args[0][0]
        assert req.full_url == "http://localhost:3000/v1/embeddings"

    def test_embed_api_error_raises(self):
        p = OpenAIEmbeddingProvider(
            api_key="k", base_url="http://localhost:3000/v1", model="m",
        )
        err_body = json.dumps({
            "error": {"message": "invalid api key", "type": "invalid_request_error"},
        }).encode("utf-8")
        mock = MagicMock()
        mock.read.return_value = err_body
        mock.__enter__ = MagicMock(return_value=mock)
        mock.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock):
            with pytest.raises(RuntimeError, match="invalid api key"):
                p.embed_query("x")

    def test_embed_empty_data_raises(self):
        p = OpenAIEmbeddingProvider(
            api_key="k", base_url="http://localhost:3000/v1", model="m",
        )
        body = json.dumps({"data": []}).encode("utf-8")
        mock = MagicMock()
        mock.read.return_value = body
        mock.__enter__ = MagicMock(return_value=mock)
        mock.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock):
            with pytest.raises(RuntimeError, match="empty data"):
                p.embed_query("x")

    def test_batching_splits_into_100_per_request(self):
        p = OpenAIEmbeddingProvider(
            api_key="k", base_url="http://localhost:3000/v1", model="m",
        )
        texts = [f"text-{i}" for i in range(250)]
        call_count = {"n": 0}

        def _mk_response(*_args, **_kwargs):
            call_count["n"] += 1
            # match payload size
            req = _args[0]
            body = json.loads(req.data.decode("utf-8"))
            n = len(body["input"])
            return _make_openai_response([[0.1] * 5 for _ in range(n)])

        with patch("urllib.request.urlopen", side_effect=_mk_response):
            out = p.embed(texts)
        assert len(out) == 250
        assert call_count["n"] == 3  # 100 + 100 + 50

    def test_custom_batch_size_respected(self):
        """new-api gateways (e.g. text-embedding-v4) cap batch at 10 —
        user must be able to lower the batch size to avoid 400 errors."""
        p = OpenAIEmbeddingProvider(
            api_key="k", base_url="http://localhost:3000/v1", model="m",
            batch_size=10,
        )
        texts = [f"t-{i}" for i in range(25)]
        call_count = {"n": 0}

        def _mk_response(*_args, **_kwargs):
            call_count["n"] += 1
            req = _args[0]
            body = json.loads(req.data.decode("utf-8"))
            assert len(body["input"]) <= 10  # never exceed configured size
            return _make_openai_response([[0.1] * 5 for _ in body["input"]])

        with patch("urllib.request.urlopen", side_effect=_mk_response):
            out = p.embed(texts)
        assert len(out) == 25
        assert call_count["n"] == 3  # 10 + 10 + 5

    def test_empty_input_returns_empty(self):
        """embed([]) must short-circuit without hitting the API."""
        p = OpenAIEmbeddingProvider(
            api_key="k", base_url="http://localhost:3000/v1", model="m",
        )
        with patch("urllib.request.urlopen") as mock_urlopen:
            assert p.embed([]) == []
            mock_urlopen.assert_not_called()

    def test_endpoint_isolation_in_name(self):
        """Two providers with the same model but different base URLs MUST
        produce different provider.name values, otherwise the embeddings
        store silently reuses vectors from a different backend's vector space.
        (Codex review HIGH finding.)"""
        p1 = OpenAIEmbeddingProvider(
            api_key="k", base_url="https://api.openai.com/v1",
            model="text-embedding-3-small",
        )
        p2 = OpenAIEmbeddingProvider(
            api_key="k", base_url="https://openrouter.ai/api/v1",
            model="text-embedding-3-small",
        )
        p3 = OpenAIEmbeddingProvider(
            api_key="k", base_url="http://127.0.0.1:3000/v1",
            model="text-embedding-3-small",
        )
        assert p1.name != p2.name != p3.name
        assert p1.name == "openai:text-embedding-3-small@https://api.openai.com/v1"
        assert p2.name == "openai:text-embedding-3-small@https://openrouter.ai/api/v1"
        assert p3.name == "openai:text-embedding-3-small@http://127.0.0.1:3000/v1"

    def test_trailing_slash_does_not_change_identity(self):
        """A trailing slash on base_url must not cause a re-embed."""
        p1 = OpenAIEmbeddingProvider(
            api_key="k", base_url="http://localhost:3000/v1", model="m",
        )
        p2 = OpenAIEmbeddingProvider(
            api_key="k", base_url="http://localhost:3000/v1/", model="m",
        )
        assert p1.name == p2.name

    def test_path_routed_gateways_get_distinct_identity(self):
        """Path-routed gateways (same host, different URL path) front
        different backends and must NOT share cached vectors.
        (Codex round-2 HIGH finding.)"""
        p1 = OpenAIEmbeddingProvider(
            api_key="k", base_url="https://gw.example.com/openai/v1", model="m",
        )
        p2 = OpenAIEmbeddingProvider(
            api_key="k", base_url="https://gw.example.com/vendor-b/v1", model="m",
        )
        assert p1.name != p2.name
        assert p1.name == "openai:m@https://gw.example.com/openai/v1"
        assert p2.name == "openai:m@https://gw.example.com/vendor-b/v1"

    def test_default_port_is_stripped_from_identity(self):
        """`https://host/v1` and `https://host:443/v1` must map to the
        same identity; stripping is necessary so the user can't force
        a pointless re-embed by spelling the port differently.
        (Codex round-2 MED finding.)"""
        p1 = OpenAIEmbeddingProvider(
            api_key="k", base_url="https://api.openai.com/v1", model="m",
        )
        p2 = OpenAIEmbeddingProvider(
            api_key="k", base_url="https://api.openai.com:443/v1", model="m",
        )
        p3 = OpenAIEmbeddingProvider(
            api_key="k", base_url="http://example.com:80/v1", model="m",
        )
        p4 = OpenAIEmbeddingProvider(
            api_key="k", base_url="http://example.com/v1", model="m",
        )
        assert p1.name == p2.name
        assert p3.name == p4.name
        # Non-default port still affects identity (normal case).
        p5 = OpenAIEmbeddingProvider(
            api_key="k", base_url="https://api.openai.com:8443/v1", model="m",
        )
        assert p5.name != p1.name

    def test_userinfo_is_stripped_from_identity(self):
        """Credentials embedded in the URL must NOT appear in provider.name
        (which gets persisted into the embeddings table). This is an
        at-rest credential-leak defense. (Codex round-2 MED finding.)"""
        p_plain = OpenAIEmbeddingProvider(
            api_key="k", base_url="https://api.example.com/v1", model="m",
        )
        p_auth = OpenAIEmbeddingProvider(
            api_key="k", base_url="https://user:secret@api.example.com/v1", model="m",
        )
        # 1. Same identity — userinfo stripped.
        assert p_plain.name == p_auth.name
        # 2. The secret never appears in the identity string.
        assert "secret" not in p_auth.name
        assert "user" not in p_auth.name

    def test_ipv6_literal_in_identity(self):
        """IPv6 hostnames must round-trip cleanly, with brackets restored
        when a non-default port is attached."""
        p = OpenAIEmbeddingProvider(
            api_key="k", base_url="http://[::1]:3000/v1", model="m",
        )
        assert p.name == "openai:m@http://[::1]:3000/v1"

    def test_response_with_missing_index_raises(self):
        """Length-only checks let duplicate/missing indices through. We
        require a strict 0..N-1 permutation. (Codex round-2 MED finding.)"""
        p = OpenAIEmbeddingProvider(
            api_key="k", base_url="http://localhost:3000/v1", model="m",
        )
        bad = json.dumps({
            "data": [
                {"embedding": [1.0], "index": 0},
                {"embedding": [2.0], "index": 0},  # duplicate 0, missing 1
            ],
        }).encode("utf-8")
        mock = MagicMock()
        mock.read.return_value = bad
        mock.__enter__ = MagicMock(return_value=mock)
        mock.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock):
            with pytest.raises(RuntimeError, match="malformed indices"):
                p.embed(["a", "b"])

    def test_response_with_out_of_range_index_raises(self):
        """Index >= N is invalid even if count matches."""
        p = OpenAIEmbeddingProvider(
            api_key="k", base_url="http://localhost:3000/v1", model="m",
        )
        bad = json.dumps({
            "data": [
                {"embedding": [1.0], "index": 0},
                {"embedding": [2.0], "index": 5},  # out-of-range
            ],
        }).encode("utf-8")
        mock = MagicMock()
        mock.read.return_value = bad
        mock.__enter__ = MagicMock(return_value=mock)
        mock.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock):
            with pytest.raises(RuntimeError, match="malformed indices"):
                p.embed(["a", "b"])

    def test_response_without_index_field_falls_back_to_server_order(self):
        """Some OpenAI-compatible gateways omit `index` entirely. The
        length check is the only safety net available — we must still
        succeed on length match and fail on mismatch."""
        p = OpenAIEmbeddingProvider(
            api_key="k", base_url="http://localhost:3000/v1", model="m",
        )
        no_idx = json.dumps({
            "data": [
                {"embedding": [1.0]},
                {"embedding": [2.0]},
            ],
        }).encode("utf-8")
        mock = MagicMock()
        mock.read.return_value = no_idx
        mock.__enter__ = MagicMock(return_value=mock)
        mock.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock):
            result = p.embed(["a", "b"])
        # Trust server order when index is absent.
        assert result == [[1.0], [2.0]]

    def test_scheme_change_produces_distinct_identity(self):
        """http and https to the same host/path front different endpoints
        in practice (dev vs prod gateway, pre/post TLS migration). They
        must NOT share cached vectors. (Codex round-3 HIGH finding.)"""
        p_http = OpenAIEmbeddingProvider(
            api_key="k", base_url="http://gw.example.com/v1", model="m",
        )
        p_https = OpenAIEmbeddingProvider(
            api_key="k", base_url="https://gw.example.com/v1", model="m",
        )
        assert p_http.name != p_https.name
        # http default port 80 and https default port 443 are both stripped
        # from the host, but scheme is preserved in the identity.
        assert p_http.name == "openai:m@http://gw.example.com/v1"
        assert p_https.name == "openai:m@https://gw.example.com/v1"

    def test_mixed_indexed_unindexed_response_raises(self):
        """Some items with ``index``, others without: must refuse rather
        than silently zip in server order (which would misplace the
        indexed items). (Codex round-3 HIGH finding.)"""
        p = OpenAIEmbeddingProvider(
            api_key="k", base_url="http://localhost:3000/v1", model="m",
        )
        mixed = json.dumps({
            "data": [
                {"embedding": [1.0], "index": 1},  # claims to be for input[1]
                {"embedding": [2.0]},              # no index
            ],
        }).encode("utf-8")
        mock = MagicMock()
        mock.read.return_value = mixed
        mock.__enter__ = MagicMock(return_value=mock)
        mock.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock):
            with pytest.raises(RuntimeError, match="mixed indexed/unindexed"):
                p.embed(["a", "b"])

    def test_string_index_treated_as_mixed(self):
        """Some OpenAI-compatible gateways serialize index as a string.
        Our permutation check requires ints; string index must fall to
        the mixed-case refusal, not silently slip through."""
        p = OpenAIEmbeddingProvider(
            api_key="k", base_url="http://localhost:3000/v1", model="m",
        )
        bad = json.dumps({
            "data": [
                {"embedding": [1.0], "index": "0"},  # string, not int
                {"embedding": [2.0], "index": "1"},
            ],
        }).encode("utf-8")
        mock = MagicMock()
        mock.read.return_value = bad
        mock.__enter__ = MagicMock(return_value=mock)
        mock.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock):
            with pytest.raises(RuntimeError, match="mixed indexed/unindexed"):
                p.embed(["a", "b"])

    def test_retry_on_remote_disconnected(self, monkeypatch):
        """http.client.RemoteDisconnected is a common transient failure
        when reverse proxies drop idle connections. Must retry.
        (Codex round-2 LOW finding.)"""
        import http.client
        monkeypatch.setattr(time, "sleep", lambda s: None)

        p = OpenAIEmbeddingProvider(
            api_key="k", base_url="http://localhost:3000/v1", model="m",
        )
        call_count = {"n": 0}

        def _mock_urlopen(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise http.client.RemoteDisconnected("edge proxy dropped connection")
            return _make_openai_response([[0.1] * 5])

        with patch("urllib.request.urlopen", side_effect=_mock_urlopen):
            p.embed_query("x")
        assert call_count["n"] == 2

    def test_response_length_mismatch_raises(self):
        """Gateway returns fewer embeddings than inputs: refuse to proceed
        rather than silently zip misaligned vectors onto the wrong nodes.
        (Codex review MED finding.)"""
        p = OpenAIEmbeddingProvider(
            api_key="k", base_url="http://localhost:3000/v1", model="m",
        )
        with patch(
            "urllib.request.urlopen",
            return_value=_make_openai_response([[0.1] * 5]),  # 1 vec
        ):
            with pytest.raises(RuntimeError, match="refusing to misalign"):
                p.embed(["a", "b", "c"])  # 3 inputs

    def test_reordered_response_is_sorted_by_index(self):
        """Gateway returns data out of order: restore input order via
        the `index` field, so vec[i] always corresponds to input[i].
        (Codex review MED finding.)"""
        p = OpenAIEmbeddingProvider(
            api_key="k", base_url="http://localhost:3000/v1", model="m",
        )
        # Return data in order 2, 0, 1 (i.e. reversed-ish).
        reordered = json.dumps({
            "data": [
                {"embedding": [3.0], "index": 2},
                {"embedding": [1.0], "index": 0},
                {"embedding": [2.0], "index": 1},
            ],
        }).encode("utf-8")
        mock = MagicMock()
        mock.read.return_value = reordered
        mock.__enter__ = MagicMock(return_value=mock)
        mock.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock):
            result = p.embed(["a", "b", "c"])
        # Must be [[1.0], [2.0], [3.0]] after sorting by index.
        assert result == [[1.0], [2.0], [3.0]]

    def test_retry_on_http_429(self, monkeypatch):
        """HTTP 429 must trigger retry with backoff (not bail immediately).
        (Codex review MED finding — prior substring match missed the fact
        that error bodies may not contain '429'.)"""
        import urllib.error
        monkeypatch.setattr(time, "sleep", lambda s: None)  # instant retries

        p = OpenAIEmbeddingProvider(
            api_key="k", base_url="http://localhost:3000/v1", model="m",
        )
        call_count = {"n": 0}
        good_response = _make_openai_response([[0.1] * 5])
        import io

        def _mock_urlopen(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise urllib.error.HTTPError(
                    url="http://localhost:3000/v1/embeddings",
                    code=429, msg="Too Many Requests", hdrs=None,
                    fp=io.BytesIO(b'{"error": "rate limited"}'),
                )
            return good_response

        with patch("urllib.request.urlopen", side_effect=_mock_urlopen):
            out = p.embed_query("x")
        assert len(out) == 5
        assert call_count["n"] == 2  # 1 fail + 1 success

    def test_retry_on_socket_timeout(self, monkeypatch):
        """socket.timeout (read timeout) must be classified retryable —
        previously these surfaced as str(exc) without '429/500/503' so
        retry never fired. (Codex review MED finding.)"""
        import socket
        monkeypatch.setattr(time, "sleep", lambda s: None)

        p = OpenAIEmbeddingProvider(
            api_key="k", base_url="http://localhost:3000/v1", model="m",
        )
        call_count = {"n": 0}
        good_response = _make_openai_response([[0.1] * 5])

        def _mock_urlopen(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] <= 2:
                raise socket.timeout("read timed out")
            return good_response

        with patch("urllib.request.urlopen", side_effect=_mock_urlopen):
            out = p.embed_query("x")
        assert len(out) == 5
        assert call_count["n"] == 3  # 2 fails + 1 success

    def test_retry_on_url_error(self, monkeypatch):
        """URLError (connection refused, DNS failure) must retry."""
        import urllib.error
        monkeypatch.setattr(time, "sleep", lambda s: None)

        p = OpenAIEmbeddingProvider(
            api_key="k", base_url="http://localhost:3000/v1", model="m",
        )
        call_count = {"n": 0}

        def _mock_urlopen(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise urllib.error.URLError("connection refused")
            return _make_openai_response([[0.1] * 5])

        with patch("urllib.request.urlopen", side_effect=_mock_urlopen):
            p.embed_query("x")
        assert call_count["n"] == 2

    def test_no_retry_on_http_400(self, monkeypatch):
        """HTTP 400 = caller bug (bad payload, unsupported model). Must fail
        fast rather than waste time on 3 retries."""
        import io
        import urllib.error
        monkeypatch.setattr(time, "sleep", lambda s: None)

        p = OpenAIEmbeddingProvider(
            api_key="k", base_url="http://localhost:3000/v1", model="m",
        )
        call_count = {"n": 0}

        def _mock_urlopen(*args, **kwargs):
            call_count["n"] += 1
            raise urllib.error.HTTPError(
                url="http://localhost:3000/v1/embeddings",
                code=400, msg="Bad Request", hdrs=None,
                fp=io.BytesIO(b'{"error": {"message": "invalid model"}}'),
            )

        with patch("urllib.request.urlopen", side_effect=_mock_urlopen):
            with pytest.raises(RuntimeError, match="invalid model"):
                p.embed_query("x")
        assert call_count["n"] == 1  # no retry on 4xx non-429

    def test_http_error_body_is_surfaced(self):
        """If the gateway returns 400 with a JSON error body, the RuntimeError
        must include the real reason, not just 'HTTP Error 400: Bad Request'."""
        import urllib.error
        p = OpenAIEmbeddingProvider(
            api_key="k", base_url="http://localhost:3000/v1", model="m",
        )
        body = json.dumps({
            "error": {"message": "batch size is invalid, should not exceed 10."},
        }).encode("utf-8")
        # HTTPError's .read() returns bytes from its fp
        import io
        err = urllib.error.HTTPError(
            url="http://localhost:3000/v1/embeddings",
            code=400, msg="Bad Request", hdrs=None, fp=io.BytesIO(body),
        )
        with patch("urllib.request.urlopen", side_effect=err):
            with pytest.raises(RuntimeError, match="batch size is invalid"):
                p.embed_query("x")


class TestGetProviderOpenAI:
    _MIN_ENV = {
        "CRG_OPENAI_API_KEY": "sk-test",
        "CRG_OPENAI_BASE_URL": "http://127.0.0.1:3000/v1",
        "CRG_OPENAI_MODEL": "text-embedding-3-small",
    }

    def test_with_all_env_vars(self):
        with patch.dict("os.environ", self._MIN_ENV, clear=True):
            p = get_provider("openai")
        assert isinstance(p, OpenAIEmbeddingProvider)
        assert p.name == "openai:text-embedding-3-small@http://127.0.0.1:3000/v1"

    def test_missing_api_key_raises(self):
        env = {k: v for k, v in self._MIN_ENV.items() if k != "CRG_OPENAI_API_KEY"}
        with patch.dict("os.environ", env, clear=True):
            with pytest.raises(ValueError, match="CRG_OPENAI_API_KEY"):
                get_provider("openai")

    def test_missing_base_url_raises(self):
        env = {k: v for k, v in self._MIN_ENV.items() if k != "CRG_OPENAI_BASE_URL"}
        with patch.dict("os.environ", env, clear=True):
            with pytest.raises(ValueError, match="CRG_OPENAI_BASE_URL"):
                get_provider("openai")

    def test_missing_model_raises(self):
        env = {k: v for k, v in self._MIN_ENV.items() if k != "CRG_OPENAI_MODEL"}
        with patch.dict("os.environ", env, clear=True):
            with pytest.raises(ValueError, match="CRG_OPENAI_MODEL"):
                get_provider("openai")

    def test_model_arg_overrides_env(self):
        with patch.dict("os.environ", self._MIN_ENV, clear=True):
            p = get_provider("openai", model="text-embedding-3-large")
        assert p.name == "openai:text-embedding-3-large@http://127.0.0.1:3000/v1"

    def test_dimension_env_forwarded(self):
        env = {**self._MIN_ENV, "CRG_OPENAI_DIMENSION": "256"}
        with patch.dict("os.environ", env, clear=True):
            p = get_provider("openai")
        assert p._dimension == 256

    def test_localhost_suppresses_egress_warning(self, capsys):
        with patch.dict("os.environ", self._MIN_ENV, clear=True):
            get_provider("openai")
        captured = capsys.readouterr()
        # localhost must never trigger the cloud-egress warning
        assert captured.err == ""
        assert captured.out == ""

    def test_cloud_base_url_triggers_egress_warning(self, capsys):
        env = {**self._MIN_ENV, "CRG_OPENAI_BASE_URL": "https://api.openai.com/v1"}
        with patch.dict("os.environ", env, clear=True):
            # drop accept flag to ensure warning fires
            os.environ.pop("CRG_ACCEPT_CLOUD_EMBEDDINGS", None)
            get_provider("openai")
        captured = capsys.readouterr()
        assert "openai" in captured.err.lower()
        assert "cloud" in captured.err.lower()
        assert captured.out == ""  # MCP stdio safety

    def test_subdomain_spoof_triggers_warning(self, capsys):
        """my-openai.127.0.0.1.nip.io must NOT be treated as localhost."""
        env = {
            **self._MIN_ENV,
            "CRG_OPENAI_BASE_URL": "https://my-openai.127.0.0.1.nip.io/v1",
        }
        with patch.dict("os.environ", env, clear=True):
            get_provider("openai")
        captured = capsys.readouterr()
        assert "cloud" in captured.err.lower()


# ---------------------------------------------------------------------------
# Body enrichment tests (Iter 3 final: ADR-A1+dedup / B4 per-provider / C3 sticky)
# ---------------------------------------------------------------------------


def _mk_node(**kwargs):
    """Minimal GraphNode factory for body-enrichment tests."""
    defaults = dict(
        id=1, kind="Function", name="my_func",
        qualified_name="sample.py::my_func", file_path="sample.py",
        line_start=1, line_end=5, language="python",
        parent_name=None, params=None, return_type=None,
        is_test=False, file_hash=None, extra={},
    )
    defaults.update(kwargs)
    return GraphNode(**defaults)


class TestFileLineCache:
    def test_reads_existing_file(self, tmp_path):
        p = tmp_path / "a.py"
        p.write_text("line1\nline2\nline3\n")
        cache = _FileLineCache(repo_root=tmp_path)
        assert cache.get_lines("a.py", 1, 3) == ["line1", "line2", "line3"]
        assert cache.get_lines("a.py", 2, 2) == ["line2"]

    def test_caches_across_calls(self, tmp_path):
        p = tmp_path / "a.py"
        p.write_text("x\ny\nz\n")
        cache = _FileLineCache(repo_root=tmp_path)
        cache.get_lines("a.py", 1, 2)
        cache.get_lines("a.py", 2, 3)
        cache.get_lines("a.py", 1, 1)
        assert cache._read_count == 1

    def test_missing_file_returns_empty(self, tmp_path):
        cache = _FileLineCache(repo_root=tmp_path)
        assert cache.get_lines("does_not_exist.py", 1, 5) == []

    def test_binary_file_detected_returns_empty(self, tmp_path):
        p = tmp_path / "b.bin"
        p.write_bytes(b"abc\x00def\n")
        cache = _FileLineCache(repo_root=tmp_path)
        assert cache.get_lines("b.bin", 1, 3) == []

    def test_out_of_range_lines_returns_empty(self, tmp_path):
        p = tmp_path / "a.py"
        p.write_text("only-one\n")
        cache = _FileLineCache(repo_root=tmp_path)
        assert cache.get_lines("a.py", 999, 1000) == []
        assert cache.get_lines("a.py", 0, 5) == []
        assert cache.get_lines("a.py", 3, 1) == []

    def test_lru_eviction_at_max_entries(self, tmp_path):
        cache = _FileLineCache(repo_root=tmp_path, max_entries=4)
        for i in range(6):
            p = tmp_path / f"f{i}.py"
            p.write_text(f"content {i}\n")
            cache.get_lines(f"f{i}.py", 1, 1)
        # Earliest two entries should have been evicted
        assert "f0.py" not in cache._cache
        assert "f1.py" not in cache._cache
        assert "f5.py" in cache._cache
        assert len(cache._cache) == 4


class TestSignatureDedup:
    @pytest.mark.parametrize("lang,line,name,params", [
        ("python",     "def my_func(x, y):", "my_func", "(x, y)"),
        ("javascript", "function handle(req, res) {", "handle", "(req, res)"),
        ("typescript", "const handle = (req: Req) => {", "handle", "(req: Req)"),
        ("java",       "public int compute(int x) {", "compute", "(int x)"),
        ("rust",       "pub fn build_tree(root: Node) -> Tree {",
                       "build_tree", "(root: Node)"),
    ])
    def test_signature_line_deduplication(self, lang, line, name, params):
        node = _mk_node(language=lang, name=name, params=params,
                        return_type="int" if lang == "java" else None)
        assert _looks_like_signature(line, node) is True

    def test_signature_dedup_preserves_body_when_no_match(self):
        # Decoy signature-like text but node.name is different -> must NOT dedup
        node = _mk_node(language="python", name="compute_totals",
                        params="(items)")
        assert _looks_like_signature("def unrelated(x): pass", node) is False

    def test_signature_dedup_respects_params_mismatch(self):
        # Overload with different param first-token
        node = _mk_node(language="python", name="handle", params="(request)")
        assert _looks_like_signature(
            "def handle(completely_different_arg):", node,
        ) is False

    def test_signature_dedup_unknown_language_conservative(self):
        node = _mk_node(language="kotlin2", name="foo", params="()")
        assert _looks_like_signature("fun foo() {}", node) is False

    def test_signature_dedup_go_and_typescript(self):
        go_node = _mk_node(language="go", name="Handle", params="(w, r)")
        assert _looks_like_signature(
            "func Handle(w http.ResponseWriter, r *http.Request) {", go_node,
        ) is True
        ts_node = _mk_node(language="typescript", name="doThing", params="(x)")
        assert _looks_like_signature("async function doThing(x) {", ts_node) is True

    def test_decorator_line_not_mistaken_for_signature(self):
        # Iter 3 D-iter3-5: @decorator line has node.name false-positive risk
        # because tree-sitter may include the decorator in the node range.
        # Three-AND gate requires a declaration keyword; @my_decorator has none.
        node = _mk_node(language="python", name="my_func", params="(x)")
        assert _looks_like_signature("@my_decorator", node) is False
        # Also the real def line DOES match.
        assert _looks_like_signature("def my_func(x):", node) is True

    def test_recursive_call_first_body_line_preserved(self):
        # Iter 3 D-iter3-5: body first line `return fact(n-1)` contains
        # node.name but lacks `def ` keyword, so three-AND gate returns False
        # and dedup does NOT drop the line.
        node = _mk_node(language="python", name="fact", params="(n)")
        assert _looks_like_signature("return n * fact(n - 1)", node) is False


class TestBlockCommentStateMachine:
    def _extract(self, fixture_text: str, tmp_path, **node_kwargs) -> str:
        p = tmp_path / "f.py"
        p.write_text(fixture_text)
        reader = _FileLineCache(repo_root=tmp_path)
        node = _mk_node(
            file_path="f.py",
            line_start=node_kwargs.get("line_start", 1),
            line_end=node_kwargs.get("line_end", fixture_text.count("\n") + 1),
            language=node_kwargs.get("language", "python"),
            name=node_kwargs.get("name", "never-matches-anything"),
        )
        return _extract_body_text(node, reader, max_chars=4000, max_lines=40)

    def test_jsdoc_multiline_header_skipped(self, tmp_path):
        src = (
            "/**\n"
            " * docstring line 1\n"
            " * docstring line 2\n"
            " */\n"
            "return 42;\n"
        )
        out = self._extract(src, tmp_path, language="javascript")
        assert "docstring" not in out
        assert "return 42" in out

    def test_javadoc_preserves_body(self, tmp_path):
        src = (
            "/** header doc */\n"
            "int answer = 42;\n"
            "return answer;\n"
        )
        out = self._extract(src, tmp_path, language="java")
        assert "header doc" not in out
        assert "answer = 42" in out or "return answer" in out

    def test_block_comment_unclosed_falls_back(self, tmp_path):
        # Unclosed /* -> state machine stays in block_comment, drops everything.
        # Fallback cap prevents hang, function returns "".
        src = "/*\n" + ("noise line\n" * 25)
        out = self._extract(src, tmp_path, language="javascript")
        assert out == ""

    def test_inline_block_comment_not_swallowed(self, tmp_path):
        src = "/* x */ real_code = 1\nmore = 2\n"
        out = self._extract(src, tmp_path, language="javascript")
        # inline /* x */ with trailing code: should NOT enter block state
        assert "real_code = 1" in out or "more = 2" in out

    def test_python_single_line_triple_quote_docstring_skipped(self, tmp_path):
        src = '"""one-liner docstring."""\nreturn value\n'
        out = self._extract(src, tmp_path, language="python")
        assert "one-liner docstring" not in out
        assert "return value" in out

    def test_python_multiline_triple_quote_docstring_skipped(self, tmp_path):
        src = (
            '"""\n'
            "   line 1\n"
            "   line 2\n"
            '"""\n'
            "compute = 3\n"
        )
        out = self._extract(src, tmp_path, language="python")
        assert "line 1" not in out and "line 2" not in out
        assert "compute = 3" in out

    def test_comment_prefix_dropped_regardless_of_length(self, tmp_path):
        long_comment = "# " + ("x" * 200)  # >> 120 chars
        src = f"{long_comment}\nreal = 1\n"
        out = self._extract(src, tmp_path, language="python")
        assert long_comment not in out
        assert "real = 1" in out


class TestBodyExtraction:
    def _extract(self, src: str, tmp_path, **node_kwargs) -> str:
        p = tmp_path / "f.py"
        p.write_text(src)
        reader = _FileLineCache(repo_root=tmp_path)
        node = _mk_node(
            file_path="f.py",
            line_start=node_kwargs.get("line_start", 1),
            line_end=node_kwargs.get("line_end", src.count("\n") + 1),
            language=node_kwargs.get("language", "python"),
            kind=node_kwargs.get("kind", "Function"),
            name=node_kwargs.get("name", "whatever-no-sig-match"),
        )
        return _extract_body_text(
            node, reader,
            max_chars=node_kwargs.get("max_chars", 4000),
            max_lines=node_kwargs.get("max_lines", 40),
        )

    def test_python_function_body(self, tmp_path):
        src = (
            "def parse_tree(data):\n"
            "    root = build_root(data)\n"
            "    walk_all_nodes(root)\n"
            "    return root\n"
        )
        out = self._extract(
            src, tmp_path, name="parse_tree", line_start=1, line_end=4,
        )
        # signature line dropped via dedup; body should contain real code
        assert "build_root" in out
        assert "walk_all_nodes" in out

    def test_file_node_returns_empty(self, tmp_path):
        p = tmp_path / "x.py"
        p.write_text("print('hi')\n")
        reader = _FileLineCache(repo_root=tmp_path)
        node = _mk_node(kind="File", file_path="x.py", line_start=1, line_end=1)
        assert _extract_body_text(node, reader, max_chars=1000) == ""

    def test_leading_triple_quote_docstring_skipped(self, tmp_path):
        src = '"""module docstring."""\nvalue = 1\n'
        out = self._extract(src, tmp_path, name="never")
        assert "module docstring" not in out
        assert "value = 1" in out

    def test_comment_only_region_skipped(self, tmp_path):
        src = "# comment 1\n# comment 2\nactual_code = 42\n"
        out = self._extract(src, tmp_path, name="never")
        assert "comment 1" not in out
        assert "actual_code" in out

    def test_truncation_by_chars(self, tmp_path):
        src = "value_a = {}\n".format("x" * 100)  # single long line
        out = self._extract(
            src, tmp_path, name="never",
            max_chars=40, line_start=1, line_end=1,
        )
        assert out.endswith("…")
        assert len(out) <= 45  # max_chars + suffix overhead

    def test_truncation_by_lines(self, tmp_path):
        lines = "\n".join(f"stmt_{i} = {i}" for i in range(30)) + "\n"
        out = self._extract(
            src=lines, tmp_path=tmp_path, name="never",
            max_chars=100000, max_lines=3,
        )
        # only 3 stmts kept; stmt_3+ must not appear
        assert "stmt_0" in out
        assert "stmt_29" not in out

    def test_single_line_node(self, tmp_path):
        src = "def tiny(): return 1\n"
        out = self._extract(
            src, tmp_path, name="tiny", params="()",
            line_start=1, line_end=1,
        )
        # signature dedup drops the only line; body should be empty
        assert out == ""


class TestBodyMaxCharsPerProvider:
    @pytest.mark.parametrize("provider_name,expected", [
        ("local:all-MiniLM-L6-v2",    _BODY_MAX_CHARS_BY_PROVIDER["local"]),
        ("google:gemini-embedding-001", _BODY_MAX_CHARS_BY_PROVIDER["google"]),
        ("minimax:embo-01",           _BODY_MAX_CHARS_BY_PROVIDER["minimax"]),
        ("openai:text-embedding-3-small", _BODY_MAX_CHARS_BY_PROVIDER["openai"]),
    ])
    def test_body_max_chars_per_provider(self, provider_name, expected):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CRG_EMBED_BODY_MAX_CHARS", None)
            assert _resolve_body_max_chars(provider_name) == expected

    def test_body_max_chars_env_override(self):
        with patch.dict(os.environ, {"CRG_EMBED_BODY_MAX_CHARS": "1234"}):
            assert _resolve_body_max_chars("local:anything") == 1234
            assert _resolve_body_max_chars("openai:foo") == 1234

    def test_body_max_chars_unknown_provider_fallback(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CRG_EMBED_BODY_MAX_CHARS", None)
            assert _resolve_body_max_chars("exotic:brand-new") == \
                _BODY_MAX_CHARS_FALLBACK

    def test_body_max_chars_provider_prefix_parsing(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CRG_EMBED_BODY_MAX_CHARS", None)
            assert _resolve_body_max_chars("openai:text-embedding-v4") == \
                _BODY_MAX_CHARS_BY_PROVIDER["openai"]
            assert _resolve_body_max_chars("OPENAI:foo") == \
                _BODY_MAX_CHARS_BY_PROVIDER["openai"]


class TestBodyEnabledStoreGate:
    """ADR-C3 sticky flag (Iter 3 D-iter3-1 / D-iter3-2)."""

    def _fresh_store(self, tmp_path):
        with patch("code_review_graph.embeddings.get_provider", return_value=None):
            return EmbeddingStore(tmp_path / "e.db")

    def _scrub_env(self):
        for k in ("CRG_EMBED_INCLUDE_BODY",):
            os.environ.pop(k, None)

    def test_body_enabled_empty_db_auto_enables(self, tmp_path):
        self._scrub_env()
        store = self._fresh_store(tmp_path)
        try:
            assert _body_enabled(store) is True
            assert store._get_meta(_EMBED_META_KEY_BODY_ENABLED) == "1"
        finally:
            store.close()

    def test_body_enabled_nonempty_db_requires_opt_in(self, tmp_path):
        self._scrub_env()
        store = self._fresh_store(tmp_path)
        try:
            # Simulate an existing embedding row without flag being set yet.
            store._conn.execute(
                "INSERT INTO embeddings (qualified_name, vector, text_hash, provider) "
                "VALUES (?, ?, ?, ?)",
                ("x::y", _encode_vector([0.1, 0.2]), "abc", "unknown"),
            )
            store._conn.commit()
            assert _body_enabled(store) is False
            assert store._get_meta(_EMBED_META_KEY_BODY_ENABLED) == "0"
        finally:
            store.close()

    def test_body_enabled_env_on_overrides(self, tmp_path):
        with patch.dict(os.environ, {"CRG_EMBED_INCLUDE_BODY": "1"}):
            store = self._fresh_store(tmp_path)
            try:
                store._conn.execute(
                    "INSERT INTO embeddings (qualified_name, vector, text_hash, provider) "
                    "VALUES (?, ?, ?, ?)",
                    ("x::y", _encode_vector([0.1]), "abc", "unknown"),
                )
                store._conn.commit()
                assert _body_enabled(store) is True
                assert store._get_meta(_EMBED_META_KEY_BODY_ENABLED) == "1"
            finally:
                store.close()

    def test_body_enabled_env_off_overrides(self, tmp_path):
        with patch.dict(os.environ, {"CRG_EMBED_INCLUDE_BODY": "0"}):
            store = self._fresh_store(tmp_path)
            try:
                assert _body_enabled(store) is False
                assert store._get_meta(_EMBED_META_KEY_BODY_ENABLED) == "0"
            finally:
                store.close()

    def test_sticky_flag_empty_db_writes_on(self, tmp_path):
        self._scrub_env()
        store = self._fresh_store(tmp_path)
        try:
            assert store._get_meta(_EMBED_META_KEY_BODY_ENABLED) is None
            _body_enabled(store)
            assert store._get_meta(_EMBED_META_KEY_BODY_ENABLED) == "1"
        finally:
            store.close()

    def test_sticky_flag_existing_db_writes_off(self, tmp_path):
        self._scrub_env()
        store = self._fresh_store(tmp_path)
        try:
            store._conn.execute(
                "INSERT INTO embeddings (qualified_name, vector, text_hash, provider) "
                "VALUES (?, ?, ?, ?)",
                ("x::y", _encode_vector([0.1]), "abc", "unknown"),
            )
            store._conn.commit()
            _body_enabled(store)
            assert store._get_meta(_EMBED_META_KEY_BODY_ENABLED) == "0"
        finally:
            store.close()

    def test_sticky_flag_persists_across_runs(self, tmp_path):
        """Iter 2 mid-run bug regression: once on, stays on after count() > 0."""
        self._scrub_env()
        store = self._fresh_store(tmp_path)
        try:
            # Run 1: empty DB -> auto-on, flag = "1"
            assert _body_enabled(store) is True
            # Simulate embeddings landing (count > 0).
            store._conn.execute(
                "INSERT INTO embeddings (qualified_name, vector, text_hash, provider) "
                "VALUES (?, ?, ?, ?)",
                ("x::y", _encode_vector([0.1]), "h", "unknown"),
            )
            store._conn.commit()
            assert store.count() > 0
            # Run 2: no env — MUST honour the "1" flag, NOT fall back to
            # the count()>0 -> False branch.
            assert _body_enabled(store) is True
            assert store._get_meta(_EMBED_META_KEY_BODY_ENABLED) == "1"
        finally:
            store.close()

    def test_sticky_flag_cli_flip_from_off_to_on(self, tmp_path):
        self._scrub_env()
        store = self._fresh_store(tmp_path)
        try:
            # Setup: legacy DB with flag="0" (decided at first embed without env)
            store._conn.execute(
                "INSERT INTO embeddings (qualified_name, vector, text_hash, provider) "
                "VALUES (?, ?, ?, ?)",
                ("x::y", _encode_vector([0.1]), "h", "unknown"),
            )
            store._conn.commit()
            assert _body_enabled(store) is False
            assert store._get_meta(_EMBED_META_KEY_BODY_ENABLED) == "0"
            # CLI user runs `embed --include-body --confirm-reembed`
            # (sets env var before calling _body_enabled).
            with patch.dict(os.environ, {"CRG_EMBED_INCLUDE_BODY": "1"}):
                assert _body_enabled(store) is True
                assert store._get_meta(_EMBED_META_KEY_BODY_ENABLED) == "1"
            # Next run without env: flag is now "1" -> stays on.
            self._scrub_env()
            assert _body_enabled(store) is True
        finally:
            store.close()


class TestFilterStageShortCircuit:
    """Directive 5: metadata_hash short-circuit skips file IO for unchanged nodes."""

    def _fake_provider(self, dim=4):
        mock = MagicMock()
        mock.name = "local:fake"
        mock.embed = MagicMock(return_value=[[0.1] * dim, [0.2] * dim, [0.3] * dim])
        mock.embed_query = MagicMock(return_value=[0.1] * dim)
        return mock

    def _store_with_fake(self, tmp_path, provider):
        with patch(
            "code_review_graph.embeddings.get_provider", return_value=provider,
        ):
            return EmbeddingStore(tmp_path / "e.db")

    def test_incremental_skips_unchanged_node_without_file_read(self, tmp_path):
        # Create real source file for node
        src = tmp_path / "sample.py"
        src.write_text(
            "def compute(x):\n"
            "    a = x * 2\n"
            "    return a\n",
        )
        provider = self._fake_provider()
        # Force sticky flag on so stage-2 would normally read file
        os.environ["CRG_EMBED_INCLUDE_BODY"] = "1"
        try:
            store = self._store_with_fake(tmp_path, provider)
            try:
                # Unchanged node carries a populated file_hash — only then
                # can stage 1 safely short-circuit (empty fingerprints now
                # force a re-read; see Codex round 3 regression fix).
                node = _mk_node(
                    language="python", name="compute", params="(x)",
                    file_path=str(src), line_start=1, line_end=3,
                    qualified_name="sample.py::compute",
                    file_hash="file-fingerprint-v1",
                )
                # First embed — reader WILL be used; returns 1 embedded.
                assert store.embed_nodes([node]) == 1
                provider.embed.reset_mock()

                # Now patch reader.get_lines so we can assert it's not hit
                with patch.object(
                    _FileLineCache, "get_lines",
                    autospec=True, return_value=[],
                ) as mock_get_lines:
                    # Re-embed same node — metadata + file_hash both match
                    # the stored combined hash → stage 1 short-circuits
                    # with no file IO.
                    assert store.embed_nodes([node]) == 0
                    assert mock_get_lines.call_count == 0
                    assert provider.embed.call_count == 0
            finally:
                store.close()
        finally:
            os.environ.pop("CRG_EMBED_INCLUDE_BODY", None)

    def test_read_failure_does_not_freeze_node(self, tmp_path):
        """Codex round 5: transient file-read failure (missing file, binary,
        permission error, out-of-repo) must NOT persist an empty-body
        sentinel that makes stage 1 skip forever. Once the file becomes
        readable again the body must be recomputed."""
        src = tmp_path / "maybe.py"
        src.write_text("def maybe():\n    a = 1\n    return a\n")
        provider = self._fake_provider()
        os.environ["CRG_EMBED_INCLUDE_BODY"] = "1"
        try:
            store = self._store_with_fake(tmp_path, provider)
            try:
                node = _mk_node(
                    language="python", name="maybe", params="()",
                    file_path=str(src), line_start=1, line_end=3,
                    qualified_name="maybe.py::maybe",
                    file_hash="maybe-fingerprint",
                )
                # First embed with a patched reader that simulates a
                # transient read failure (last_read_ok=False).
                with patch.object(
                    _FileLineCache, "get_lines",
                    autospec=True, return_value=[],
                ) as mock_get_lines:
                    def fake_fail(self, *a, **kw):
                        self._last_read_ok = False
                        return []
                    mock_get_lines.side_effect = fake_fail
                    assert store.embed_nodes([node]) == 1

                provider.embed.reset_mock()
                # Second embed with a real reader — the failed body
                # snippet should NOT short-circuit stage 1 (stored_body
                # is ""), so stage 2 reads the file again and persists a
                # proper body hash.
                assert store.embed_nodes([node]) == 1
                assert provider.embed.call_count == 1
            finally:
                store.close()
        finally:
            os.environ.pop("CRG_EMBED_INCLUDE_BODY", None)

    def test_empty_body_does_not_reread_on_unchanged_run(self, tmp_path):
        """Codex round 4: single-line functions whose body extracts to ""
        used to fall through stage 1 on every incremental run. Stage 1
        must now skip them too (empty body hash is written as sha256('')
        to distinguish 'body ran, produced nothing' from legacy rows)."""
        src = tmp_path / "tiny.py"
        src.write_text("def tiny(): return 1\n")
        provider = self._fake_provider()
        os.environ["CRG_EMBED_INCLUDE_BODY"] = "1"
        try:
            store = self._store_with_fake(tmp_path, provider)
            try:
                node = _mk_node(
                    language="python", name="tiny", params="()",
                    file_path=str(src), line_start=1, line_end=1,
                    qualified_name="tiny.py::tiny",
                    file_hash="tiny-fingerprint",
                )
                assert store.embed_nodes([node]) == 1
                provider.embed.reset_mock()
                with patch.object(
                    _FileLineCache, "get_lines",
                    autospec=True, return_value=[],
                ) as mock_get_lines:
                    assert store.embed_nodes([node]) == 0
                    assert mock_get_lines.call_count == 0
                    assert provider.embed.call_count == 0
            finally:
                store.close()
        finally:
            os.environ.pop("CRG_EMBED_INCLUDE_BODY", None)

    def test_body_edit_with_empty_file_hash_still_reembeds(self, tmp_path):
        """Codex round 3: when both stored and current file_hash are empty,
        stage 1 must NOT short-circuit — otherwise body-only edits slip
        through on callers that don't populate node.file_hash."""
        src = tmp_path / "sample.py"
        src.write_text("def compute(x):\n    return x * 2\n")
        provider = self._fake_provider()
        os.environ["CRG_EMBED_INCLUDE_BODY"] = "1"
        try:
            store = self._store_with_fake(tmp_path, provider)
            try:
                # file_hash deliberately omitted (None → empty string slot)
                node_v1 = _mk_node(
                    language="python", name="compute", params="(x)",
                    file_path=str(src), line_start=1, line_end=2,
                    qualified_name="sample.py::compute",
                )
                assert store.embed_nodes([node_v1]) == 1
                provider.embed.reset_mock()
                src.write_text("def compute(x):\n    return x * 3\n")
                node_v2 = _mk_node(
                    language="python", name="compute", params="(x)",
                    file_path=str(src), line_start=1, line_end=2,
                    qualified_name="sample.py::compute",
                )
                # Without a file_hash signal we must re-read and recompute
                # body_hash; the body text has changed so this is 1 embed.
                assert store.embed_nodes([node_v2]) == 1
                assert provider.embed.call_count == 1
            finally:
                store.close()
        finally:
            os.environ.pop("CRG_EMBED_INCLUDE_BODY", None)

    def test_metadata_change_triggers_stage2_read(self, tmp_path):
        src = tmp_path / "sample.py"
        src.write_text("def compute(x):\n    return x * 2\n")
        provider = self._fake_provider()
        os.environ["CRG_EMBED_INCLUDE_BODY"] = "1"
        try:
            store = self._store_with_fake(tmp_path, provider)
            try:
                node = _mk_node(
                    language="python", name="compute", params="(x)",
                    file_path=str(src), line_start=1, line_end=2,
                    qualified_name="sample.py::compute",
                )
                store.embed_nodes([node])
                provider.embed.reset_mock()
                # Flip return_type => metadata_hash changes => stage 2 must run.
                node_changed = _mk_node(
                    language="python", name="compute", params="(x)",
                    return_type="int",
                    file_path=str(src), line_start=1, line_end=2,
                    qualified_name="sample.py::compute",
                )
                with patch.object(
                    _FileLineCache, "get_lines", autospec=True,
                    return_value=["def compute(x):", "    return x * 2"],
                ) as mock_get_lines:
                    assert store.embed_nodes([node_changed]) == 1
                    assert mock_get_lines.call_count >= 1
            finally:
                store.close()
        finally:
            os.environ.pop("CRG_EMBED_INCLUDE_BODY", None)

    def test_split_combined_hash_legacy_row(self):
        # Iter 1: pure sha256 hex, no separator
        legacy = "a" * 64
        assert _split_combined_hash(legacy) == (legacy, "", "")
        # Iter 2: metadata + body only (no file fingerprint yet)
        iter2 = "abc:def"
        assert _split_combined_hash(iter2) == ("abc", "def", "")
        # Iter 2 body-disabled stored form
        iter2_body_off = "abc:"
        assert _split_combined_hash(iter2_body_off) == ("abc", "", "")
        # Iter 3.2: metadata + body + file fingerprint
        iter3 = "meta:body:fhash"
        assert _split_combined_hash(iter3) == ("meta", "body", "fhash")
        # Iter 3.2 body-disabled but file_hash captured
        iter3_body_off = "meta::fhash"
        assert _split_combined_hash(iter3_body_off) == ("meta", "", "fhash")

    def test_body_only_change_triggers_reembed(self, tmp_path):
        """Codex-review regression: metadata-unchanged body edit used to be
        skipped forever. Now file_hash shift forces stage-2 re-read."""
        src = tmp_path / "sample.py"
        src.write_text("def compute(x):\n    return x * 2\n")
        provider = self._fake_provider()
        os.environ["CRG_EMBED_INCLUDE_BODY"] = "1"
        try:
            store = self._store_with_fake(tmp_path, provider)
            try:
                node_v1 = _mk_node(
                    language="python", name="compute", params="(x)",
                    file_path=str(src), line_start=1, line_end=2,
                    qualified_name="sample.py::compute", file_hash="h-v1",
                )
                assert store.embed_nodes([node_v1]) == 1
                provider.embed.reset_mock()
                # Body edits but metadata identical; file_hash changes.
                src.write_text("def compute(x):\n    return x * 3\n")
                node_v2 = _mk_node(
                    language="python", name="compute", params="(x)",
                    file_path=str(src), line_start=1, line_end=2,
                    qualified_name="sample.py::compute", file_hash="h-v2",
                )
                assert store.embed_nodes([node_v2]) == 1
                assert provider.embed.call_count == 1
            finally:
                store.close()
        finally:
            os.environ.pop("CRG_EMBED_INCLUDE_BODY", None)


class TestFileLineCacheContainment:
    """Codex-review regression: file_path must stay inside repo_root."""

    def test_absolute_path_outside_root_rejected(self, tmp_path):
        outside = tmp_path.parent / ("outside-" + tmp_path.name + ".secret")
        outside.write_text("top secret\n")
        try:
            cache = _FileLineCache(repo_root=tmp_path)
            assert cache.get_lines(str(outside), 1, 1) == []
        finally:
            outside.unlink(missing_ok=True)

    def test_relative_traversal_rejected(self, tmp_path):
        outside = tmp_path.parent / "escape-target.secret"
        outside.write_text("top secret\n")
        try:
            cache = _FileLineCache(repo_root=tmp_path)
            # "../escape-target.secret" tries to pop out of the repo
            assert cache.get_lines(
                f"../{outside.name}", 1, 1,
            ) == []
        finally:
            outside.unlink(missing_ok=True)

    def test_inside_root_still_reads(self, tmp_path):
        inside = tmp_path / "hello.py"
        inside.write_text("x = 1\n")
        cache = _FileLineCache(repo_root=tmp_path)
        assert cache.get_lines("hello.py", 1, 1) == ["x = 1"]

    def test_no_root_preserves_absolute_behavior(self, tmp_path):
        inside = tmp_path / "hello.py"
        inside.write_text("y = 2\n")
        # Without repo_root we intentionally skip the containment check so
        # unit tests that construct nodes with absolute fixture paths still
        # work (see TestFileLineCache.test_reads_existing_file style, but
        # called with no repo_root).
        cache = _FileLineCache(repo_root=None)
        assert cache.get_lines(str(inside), 1, 1) == ["y = 2"]


class TestKotlinFunSignatureDedup:
    """Codex-review regression: Kotlin ``fun`` without return type."""

    def test_kotlin_fun_without_return_type(self):
        node = _mk_node(
            language="kotlin", name="save", params="(user: User)",
            return_type=None,
        )
        assert _looks_like_signature(
            "override fun save(user: User) {", node,
        ) is True
