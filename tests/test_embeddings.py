"""Tests for the embeddings module."""

import json
import os
import time
from unittest.mock import MagicMock, patch

import pytest

from code_review_graph.embeddings import (
    LOCAL_DEFAULT_MODEL,
    EmbeddingStore,
    LocalEmbeddingProvider,
    MiniMaxEmbeddingProvider,
    OpenAIEmbeddingProvider,
    _cosine_similarity,
    _decode_vector,
    _encode_vector,
    _is_localhost_url,
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

    def test_embed_sends_user_agent_header(self):
        # urllib's default UA ("Python-urllib/X.Y") is rejected by some
        # Cloudflare-fronted gateways with HTTP 403 / error 1010. CRG must
        # send an explicit User-Agent so requests get through.
        provider = MiniMaxEmbeddingProvider(api_key="test-key")
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
            provider.embed_query("hello")

        req = mock_urlopen.call_args[0][0]
        ua = req.headers.get("User-agent", "")
        assert ua.startswith("code-review-graph/")
        assert "github.com/tirth8205/code-review-graph" in ua


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
        # Cloudflare-fronted gateways (e.g. Fireworks) reject the urllib
        # default UA with HTTP 403 / error 1010. See _USER_AGENT in
        # embeddings.py.
        ua = req.headers.get("User-agent", "")
        assert ua.startswith("code-review-graph/")
        assert "github.com/tirth8205/code-review-graph" in ua
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
