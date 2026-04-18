"""Vector embedding support for semantic code search.

Supports multiple providers:
1. Local (sentence-transformers) - Private, fast, offline.
2. Google Gemini - High-quality, cloud-based. Requires explicit opt-in.
3. MiniMax (embo-01) - High-quality 1536-dim cloud embeddings. Requires MINIMAX_API_KEY.
4. OpenAI-compatible - Any endpoint speaking OpenAI /v1/embeddings (real OpenAI,
   Azure OpenAI, self-hosted gateways like new-api / LiteLLM / vLLM / LocalAI / Ollama).
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import struct
import sys
import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .graph import GraphNode, GraphStore, node_to_dict

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Provider Interface and Implementations
# ---------------------------------------------------------------------------


class EmbeddingProvider(ABC):
    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        pass

    @abstractmethod
    def embed_query(self, text: str) -> list[float]:
        """Embed a search query (may use a different task type than indexing)."""
        pass

    @property
    @abstractmethod
    def dimension(self) -> int:
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        pass


LOCAL_DEFAULT_MODEL = "all-MiniLM-L6-v2"


class LocalEmbeddingProvider(EmbeddingProvider):
    def __init__(self, model_name: str | None = None) -> None:
        self._model_name = model_name or os.environ.get(
            "CRG_EMBEDDING_MODEL", LOCAL_DEFAULT_MODEL
        )
        self._model = None  # Lazy-loaded

    def _get_model(self):
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
                self._model = SentenceTransformer(
                    self._model_name,
                    trust_remote_code=True,
                    model_kwargs={"trust_remote_code": True},
                )
            except ImportError:
                raise ImportError(
                    "sentence-transformers not installed. "
                    "Run: pip install code-review-graph[embeddings]"
                )
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        model = self._get_model()
        vectors = model.encode(texts, show_progress_bar=False)
        return [v.tolist() for v in vectors]

    def embed_query(self, text: str) -> list[float]:
        return self.embed([text])[0]

    @property
    def dimension(self) -> int:
        model = self._get_model()
        return model.get_sentence_embedding_dimension()

    @property
    def name(self) -> str:
        return f"local:{self._model_name}"


class GoogleEmbeddingProvider(EmbeddingProvider):
    def __init__(self, api_key: str, model: str = "gemini-embedding-001") -> None:
        try:
            from google import genai
            self._client = genai.Client(api_key=api_key)
            self.model = model
            self._dimension: int | None = None
        except ImportError:
            raise ImportError(
                "google-generativeai not installed. "
                "Run: pip install code-review-graph[google-embeddings]"
            )

    def embed(self, texts: list[str]) -> list[list[float]]:
        batch_size = 100
        results = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            response = self._call_with_retry(
                lambda b=batch: self._client.models.embed_content(
                    model=self.model,
                    contents=b,
                    config={"task_type": "RETRIEVAL_DOCUMENT"},
                )
            )
            results.extend([e.values for e in response.embeddings])
        if self._dimension is None and results:
            self._dimension = len(results[0])
        return results

    @staticmethod
    def _call_with_retry(fn, max_retries: int = 3):
        """Call fn with exponential backoff on transient API errors."""
        for attempt in range(max_retries):
            try:
                return fn()
            except Exception as e:
                # Retry on rate-limit (429) or server errors (5xx)
                err_str = str(e)
                is_retryable = "429" in err_str or "500" in err_str or "503" in err_str
                if not is_retryable or attempt == max_retries - 1:
                    raise
                wait = 2 ** attempt
                logger.warning("Gemini API error (attempt %d/%d), retrying in %ds: %s",
                               attempt + 1, max_retries, wait, e)
                time.sleep(wait)

    def embed_query(self, text: str) -> list[float]:
        response = self._call_with_retry(
            lambda: self._client.models.embed_content(
                model=self.model,
                contents=[text],
                config={"task_type": "RETRIEVAL_QUERY"},
            )
        )
        vec = response.embeddings[0].values
        if self._dimension is None:
            self._dimension = len(vec)
        return vec

    @property
    def dimension(self) -> int:
        if self._dimension is not None:
            return self._dimension
        # Default for gemini-embedding-001; updated dynamically after first call
        return 768

    @property
    def name(self) -> str:
        return f"google:{self.model}"


class MiniMaxEmbeddingProvider(EmbeddingProvider):
    """MiniMax embo-01 embedding provider (1536 dimensions).

    Uses the MiniMax Embeddings API (https://api.minimax.io/v1/embeddings)
    with the embo-01 model. Requires the MINIMAX_API_KEY environment variable.
    """

    _ENDPOINT = "https://api.minimax.io/v1/embeddings"
    _MODEL = "embo-01"
    _DIMENSION = 1536

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    def _call_api(self, texts: list[str], task_type: str) -> list[list[float]]:
        import json as _json
        import urllib.request

        payload = _json.dumps({
            "model": self._MODEL,
            "texts": texts,
            "type": task_type,
        }).encode("utf-8")

        req = urllib.request.Request(
            self._ENDPOINT,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
        )

        max_retries = 3
        for attempt in range(max_retries):
            try:
                import ssl
                _ssl_ctx = ssl.create_default_context()
                with urllib.request.urlopen(req, timeout=60, context=_ssl_ctx) as resp:  # nosec B310
                    body = _json.loads(resp.read().decode("utf-8"))

                base_resp = body.get("base_resp", {})
                if base_resp.get("status_code", 0) != 0:
                    raise RuntimeError(
                        f"MiniMax API error: {base_resp.get('status_msg', 'unknown')}"
                    )

                return body["vectors"]
            except Exception as e:
                err_str = str(e)
                is_retryable = "429" in err_str or "500" in err_str or "503" in err_str
                if not is_retryable or attempt == max_retries - 1:
                    raise
                wait = 2 ** attempt
                logger.warning(
                    "MiniMax API error (attempt %d/%d), retrying in %ds: %s",
                    attempt + 1, max_retries, wait, e,
                )
                time.sleep(wait)

        return []  # unreachable, but keeps mypy happy

    def embed(self, texts: list[str]) -> list[list[float]]:
        batch_size = 100
        results: list[list[float]] = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            results.extend(self._call_api(batch, "db"))
        return results

    def embed_query(self, text: str) -> list[float]:
        return self._call_api([text], "query")[0]

    @property
    def dimension(self) -> int:
        return self._DIMENSION

    @property
    def name(self) -> str:
        return f"minimax:{self._MODEL}"


class OpenAIEmbeddingProvider(EmbeddingProvider):
    """OpenAI-compatible embedding provider.

    Works with any endpoint that speaks the OpenAI ``/v1/embeddings`` schema:
    - Real OpenAI API (``https://api.openai.com/v1``)
    - Azure OpenAI
    - Self-hosted gateways: new-api, LiteLLM, vLLM, LocalAI, Ollama (openai mode)

    Provider identity in ``name`` includes both the model and the endpoint
    host (``openai:{model}@{host}``), so switching base URL while keeping the
    same model ID re-partitions the embeddings table and forces a clean
    re-embed. This is the only defense against silently mixing vector spaces
    from different backends (e.g. real OpenAI vs. an OpenAI-compatible
    gateway that ships different weights under the same model name).

    Dimension is detected from the first response and frozen; switching the
    ``model`` in the environment also changes ``provider.name`` and triggers
    re-embed via the same isolation key.
    """

    _DEFAULT_BATCH_SIZE = 100

    # Default ports by scheme; stripped from the host_key so the user can't
    # accidentally force a re-embed by toggling an explicit default port.
    _DEFAULT_PORTS = {"http": 80, "https": 443}

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        dimension: int | None = None,
        timeout: int = 120,
        batch_size: int | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._dimension = dimension
        self._timeout = timeout
        self._batch_size = batch_size or self._DEFAULT_BATCH_SIZE
        self._host_key = self._make_host_key(self._base_url)

    @classmethod
    def _make_host_key(cls, base_url: str) -> str:
        """Normalize the identity key used in ``provider.name``.

        Codex review pushed this well past naive ``netloc`` because that
        alone has three leaks:

        1. ``netloc`` preserves ``userinfo`` (``user:pass@host``) — we'd
           persist credentials into the DB's ``embeddings.provider`` column.
           Use ``hostname`` instead.
        2. Default ports (``:80`` for http, ``:443`` for https) are
           semantically identical to omitting the port; keeping them would
           cause spurious re-embeds when the user just spelled the URL
           differently.
        3. Path is part of the backend identity for path-routed gateways:
           ``https://gw/openai/v1`` and ``https://gw/vendor-b/v1`` front
           different models and must not share cached vectors.
        """
        parsed = urlparse(base_url)
        hostname = (parsed.hostname or "").lower()
        scheme = (parsed.scheme or "").lower()
        port = parsed.port
        if port and port != cls._DEFAULT_PORTS.get(scheme):
            # Bracket IPv6 literals when appending a port.
            host_part = f"[{hostname}]:{port}" if ":" in hostname else f"{hostname}:{port}"
        else:
            host_part = hostname
        # Preserve path routing. Trim any trailing slash and any
        # ``/embeddings`` suffix that callers may have included — we append
        # that ourselves when building the request URL.
        path = (parsed.path or "").rstrip("/")
        if path.endswith("/embeddings"):
            path = path[: -len("/embeddings")].rstrip("/")
        # Include scheme: http and https to the same host+path front
        # different endpoints in practice (plaintext vs TLS, dev vs prod
        # gateway), and sharing cached vectors across them is the same
        # silent-mixing failure mode as switching base URL entirely.
        return f"{scheme}://{host_part}{path}" if path else f"{scheme}://{host_part}"

    def _call_api(self, texts: list[str]) -> list[list[float]]:
        import http.client
        import json as _json
        import socket
        import ssl
        import urllib.error
        import urllib.request

        body: dict[str, Any] = {"model": self._model, "input": texts}
        # OpenAI v3 models (text-embedding-3-*) support dimension reduction;
        # only forward the param when the user explicitly pinned one.
        if self._dimension is not None:
            body["dimensions"] = self._dimension

        payload = _json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            f"{self._base_url}/embeddings",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
        )

        max_retries = 3
        for attempt in range(max_retries):
            try:
                _ssl_ctx = ssl.create_default_context()
                try:
                    with urllib.request.urlopen(  # nosec B310
                        req, timeout=self._timeout, context=_ssl_ctx,
                    ) as resp:
                        raw = resp.read().decode("utf-8")
                except urllib.error.HTTPError as http_err:
                    # 429 / 5xx: re-raise and let the outer retry loop handle it.
                    # (We must not convert to RuntimeError here or retry below
                    # can't tell it was a transient HTTP failure.)
                    if http_err.code == 429 or 500 <= http_err.code < 600:
                        raise
                    # Other 4xx: surface the API error body instead of a bare
                    # "400 Bad Request" — gateways like new-api return JSON
                    # with the real reason (batch size limits, invalid model,
                    # etc.) which is far more actionable.
                    try:
                        err_body = http_err.read().decode("utf-8", errors="replace")
                    except Exception:
                        err_body = ""
                    err_msg = err_body or str(http_err)
                    try:
                        parsed = _json.loads(err_body)
                        if isinstance(parsed, dict) and "error" in parsed:
                            err_obj = parsed["error"]
                            err_msg = (
                                err_obj.get("message", err_msg)
                                if isinstance(err_obj, dict) else str(err_obj)
                            )
                    except Exception:  # nosec B110
                        # Non-JSON error body is fine: we already seeded
                        # err_msg with the raw body above, so fall through.
                        pass
                    raise RuntimeError(
                        f"OpenAI API HTTP {http_err.code}: {err_msg}"
                    ) from http_err

                response = _json.loads(raw)

                if "error" in response:
                    err = response["error"]
                    msg = err.get("message", "unknown") if isinstance(err, dict) else str(err)
                    raise RuntimeError(f"OpenAI API error: {msg}")

                data = response.get("data", [])
                if not data:
                    raise RuntimeError("OpenAI API returned empty data")
                # OpenAI spec: data[i].index maps to input[i], but some
                # compatible gateways re-order results or drop entries on
                # partial failure, and others omit `index` entirely. Three
                # disjoint cases:
                #   1. All items have a valid int ``index``: must form a
                #      permutation of 0..N-1, then sort and use.
                #   2. NO item carries an ``index`` field: trust server
                #      order, only verify count matches.
                #   3. Anything in between (partial indices, str indices,
                #      missing on some): refuse. Zipping server order in
                #      that case would happily misalign the indexed items.
                any_has_index = any("index" in item for item in data)
                all_int_index = all(
                    isinstance(item.get("index"), int) for item in data
                )
                if all_int_index:
                    expected = set(range(len(texts)))
                    indices = [int(item["index"]) for item in data]
                    if len(set(indices)) != len(indices) or set(indices) != expected:
                        raise RuntimeError(
                            "OpenAI API returned malformed indices "
                            f"(got {indices}, expected permutation of "
                            f"0..{len(texts) - 1}) — refusing to misalign vectors."
                        )
                    data = sorted(data, key=lambda item: int(item["index"]))
                elif not any_has_index:
                    if len(data) != len(texts):
                        raise RuntimeError(
                            f"OpenAI API returned {len(data)} embeddings for "
                            f"{len(texts)} inputs with no index field — "
                            "refusing to misalign vectors."
                        )
                else:
                    # Mixed: some items have index, others don't (or carry
                    # non-int index). Server order would silently misplace
                    # the indexed items, so we refuse.
                    raise RuntimeError(
                        "OpenAI API returned mixed indexed/unindexed data — "
                        "refusing to misalign vectors."
                    )

                vectors = [item["embedding"] for item in data]
                if vectors and self._dimension is None:
                    self._dimension = len(vectors[0])
                return vectors

            except Exception as e:
                # Retryable = HTTP 429/5xx, network/timeout/TLS issues.
                # Non-retryable = HTTP 4xx (other), malformed responses,
                # misaligned data length — those are caller-side bugs that
                # will keep failing on retry.
                is_retryable = False
                if isinstance(e, urllib.error.HTTPError):
                    is_retryable = e.code == 429 or 500 <= e.code < 600
                elif isinstance(e, (
                    urllib.error.URLError,
                    socket.timeout,
                    TimeoutError,
                    ConnectionError,
                    ssl.SSLError,
                    # Reverse proxies and edge gateways surface transient
                    # disconnects as these stdlib classes. Real incidents
                    # have been observed on Cloudflare-fronted endpoints
                    # and on LiteLLM when upstream providers hiccup.
                    http.client.IncompleteRead,
                    http.client.BadStatusLine,
                    http.client.RemoteDisconnected,
                )):
                    is_retryable = True
                if not is_retryable or attempt == max_retries - 1:
                    raise
                wait = 2 ** attempt
                logger.warning(
                    "OpenAI embeddings API error (attempt %d/%d), retrying in %ds: %s",
                    attempt + 1, max_retries, wait, e,
                )
                time.sleep(wait)

        return []  # unreachable

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        results: list[list[float]] = []
        for i in range(0, len(texts), self._batch_size):
            results.extend(self._call_api(texts[i:i + self._batch_size]))
        return results

    def embed_query(self, text: str) -> list[float]:
        return self._call_api([text])[0]

    @property
    def dimension(self) -> int:
        if self._dimension is not None:
            return self._dimension
        # Default for text-embedding-3-small; updated after first call.
        return 1536

    @property
    def name(self) -> str:
        # Endpoint-aware identity: model alone is NOT enough — two backends
        # can serve the same model ID with different weights or dimensions,
        # and re-using cached embeddings across them silently corrupts
        # semantic ranking. Including the host partitions the embeddings
        # table so switching CRG_OPENAI_BASE_URL triggers a safe re-embed.
        return f"openai:{self._model}@{self._host_key}"


CLOUD_PROVIDERS = {"google", "minimax", "openai"}


def _is_localhost_url(url: str) -> bool:
    """Return True if url points to a localhost host (never treat as cloud egress).

    Uses urlparse.hostname so we compare the actual host, not a substring
    match that could be fooled by e.g. ``https://my-openai.127.0.0.1.nip.io``.
    """
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    # nosec B104: we're *matching* a URL hostname, not binding a listener.
    return host in {"127.0.0.1", "localhost", "0.0.0.0", "::1"}  # nosec B104


def _warn_cloud_egress(provider_name: str) -> None:
    """Print a stderr warning before a cloud embedding provider is used.

    The warning is suppressed when ``CRG_ACCEPT_CLOUD_EMBEDDINGS=1`` is
    set in the environment, so scripted / CI workloads can acknowledge
    once and move on. Use stderr (never stdin/input) to stay compatible
    with the MCP stdio transport — anything we write to stdout would
    corrupt the JSON-RPC stream. See: #174
    """
    if os.environ.get("CRG_ACCEPT_CLOUD_EMBEDDINGS", "").strip() == "1":
        return
    print(
        f"\n⚠️  code-review-graph: about to embed code via the '{provider_name}' "
        "cloud provider.\n"
        "    Your source code (function names, docstrings, file paths) will be "
        "sent to an external API.\n"
        "    This is necessary for semantic search with the cloud provider you "
        "selected.\n"
        "    To skip this warning in future runs, set "
        "CRG_ACCEPT_CLOUD_EMBEDDINGS=1 in your environment.\n"
        "    To stay fully offline, use the default 'local' provider instead "
        "(no API key needed).\n",
        file=sys.stderr,
    )


def get_provider(
    provider: str | None = None,
    model: str | None = None,
) -> EmbeddingProvider | None:
    """Get an embedding provider by name.

    Args:
        provider: Provider name. One of "local", "google", "minimax", "openai",
                  or None for local.
                  Google requires GOOGLE_API_KEY env var and explicit opt-in.
                  MiniMax requires MINIMAX_API_KEY env var and explicit opt-in.
                  OpenAI requires CRG_OPENAI_API_KEY + CRG_OPENAI_BASE_URL +
                  CRG_OPENAI_MODEL env vars (or the ``model`` arg). The egress
                  warning is skipped when the base URL points to localhost.
                  Cloud providers emit a one-time stderr warning before use
                  unless ``CRG_ACCEPT_CLOUD_EMBEDDINGS=1`` is set. See: #174
        model: Model name/path to use. For local provider this is any
               sentence-transformers compatible model. Falls back to
               CRG_EMBEDDING_MODEL env var, then to all-MiniLM-L6-v2.
               For Google provider this is a Gemini model ID.
               For OpenAI provider this overrides CRG_OPENAI_MODEL.
    """
    if provider == "openai":
        api_key = os.environ.get("CRG_OPENAI_API_KEY")
        base_url = os.environ.get("CRG_OPENAI_BASE_URL")
        resolved_model = model or os.environ.get("CRG_OPENAI_MODEL")
        if not api_key or not base_url or not resolved_model:
            missing = [
                name for name, val in [
                    ("CRG_OPENAI_API_KEY", api_key),
                    ("CRG_OPENAI_BASE_URL", base_url),
                    ("CRG_OPENAI_MODEL", resolved_model),
                ] if not val
            ]
            raise ValueError(
                "Missing required environment variable(s) for the OpenAI "
                f"embedding provider: {', '.join(missing)}."
            )
        dim_env = os.environ.get("CRG_OPENAI_DIMENSION")
        dimension = int(dim_env) if dim_env else None
        batch_env = os.environ.get("CRG_OPENAI_BATCH_SIZE")
        batch_size = int(batch_env) if batch_env else None
        if not _is_localhost_url(base_url):
            _warn_cloud_egress("openai")
        return OpenAIEmbeddingProvider(
            api_key=api_key,
            base_url=base_url,
            model=resolved_model,
            dimension=dimension,
            batch_size=batch_size,
        )

    if provider == "minimax":
        api_key = os.environ.get("MINIMAX_API_KEY")
        if not api_key:
            raise ValueError(
                "MINIMAX_API_KEY environment variable is required for "
                "the MiniMax embedding provider."
            )
        _warn_cloud_egress("minimax")
        return MiniMaxEmbeddingProvider(api_key=api_key)

    if provider == "google":
        api_key = os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise ValueError(
                "GOOGLE_API_KEY environment variable is required for "
                "the Google embedding provider."
            )
        _warn_cloud_egress("google")
        try:
            return GoogleEmbeddingProvider(
                api_key=api_key,
                **({"model": model} if model else {}),
            )
        except ImportError:
            return None

    # Default: local
    try:
        return LocalEmbeddingProvider(model_name=model)
    except ImportError:
        return None


def _check_available() -> bool:
    """Check whether local embedding support is available."""
    try:
        import sentence_transformers  # noqa: F401
        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# SQLite vector storage
# ---------------------------------------------------------------------------

_EMBEDDINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS embeddings (
    qualified_name TEXT PRIMARY KEY,
    vector BLOB NOT NULL,
    text_hash TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT 'unknown'
);

CREATE TABLE IF NOT EXISTS embeddings_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _encode_vector(vec: list[float]) -> bytes:
    """Encode a float vector as a compact binary blob."""
    return struct.pack(f"{len(vec)}f", *vec)


def _decode_vector(blob: bytes) -> list[float]:
    """Decode a binary blob back to a float vector."""
    n = len(blob) // 4  # 4 bytes per float32
    return list(struct.unpack(f"{n}f", blob))


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# ---------------------------------------------------------------------------
# Body enrichment helpers (Iter 3 final: ADR-A1+dedup / B4 per-provider / C3 sticky)
# ---------------------------------------------------------------------------

# ADR-B4: per-provider body char budget. Keys are provider-name prefixes
# (the part before ':'). Fallback applies to unknown providers.
_BODY_MAX_CHARS_BY_PROVIDER: dict[str, int] = {
    "local":   700,   # MiniLM ~256 token window; body + metadata must fit together
    "google":  3000,  # gemini-embedding-001 ~2048 token window, conservative 50%
    "minimax": 3000,  # embo-01 ~1024 token window, pay-per-use
    "openai":  6000,  # text-embedding-3-* 8192 token ceiling, safety margin
}
_BODY_MAX_CHARS_FALLBACK = 700
_BODY_MAX_LINES_DEFAULT = 15
_FILE_CACHE_MAX_ENTRIES = 128
_FILE_CACHE_MAX_BYTES = 2 * 1024 * 1024  # skip files larger than 2 MB
_FILE_CACHE_SNIFF_BYTES = 4096           # null-byte probe window for binary detect
_EMBED_META_KEY_BODY_ENABLED = "embed_body_enabled"


def _resolve_body_max_chars(provider_name: str) -> int:
    """Return the per-provider char budget for body-enriched embeddings.

    Precedence:
      1. ``CRG_EMBED_BODY_MAX_CHARS`` env var (positive int) — global override
      2. ``_BODY_MAX_CHARS_BY_PROVIDER[prefix]`` where prefix is
         ``provider_name.split(':', 1)[0].lower()``
      3. ``_BODY_MAX_CHARS_FALLBACK`` for unknown providers
    """
    override = os.environ.get("CRG_EMBED_BODY_MAX_CHARS")
    if override:
        try:
            n = int(override)
            if n > 0:
                return n
        except ValueError:
            pass
    prefix = provider_name.split(":", 1)[0].lower() if provider_name else ""
    return _BODY_MAX_CHARS_BY_PROVIDER.get(prefix, _BODY_MAX_CHARS_FALLBACK)


class _FileLineCache:
    """Per-embed-batch file reader with bounded LRU (``OrderedDict``).

    Returns a slice of file lines for a given 1-indexed inclusive range.
    Returns ``[]`` (empty, never raises) for:
      - missing file / directory / permission / generic ``OSError``
      - binary file (null byte in first ``_FILE_CACHE_SNIFF_BYTES``)
      - oversize file (> ``_FILE_CACHE_MAX_BYTES``)
      - out-of-range line numbers (``line_start <= 0`` or beyond EOF)

    Not thread-safe. ``embed_nodes`` is single-threaded; do not share
    instances across batches.
    """

    def __init__(
        self,
        repo_root: Path | None = None,
        max_entries: int = _FILE_CACHE_MAX_ENTRIES,
    ) -> None:
        self._repo_root = repo_root
        self._max_entries = max_entries
        # value ``None`` marks a known-failed read so we don't re-probe
        self._cache: OrderedDict[str, list[str] | None] = OrderedDict()
        self._read_count = 0
        # Whether the most recent ``get_lines`` call saw a file that
        # loaded at all. ``False`` only when the underlying file
        # read/decode failed (missing / permission / binary / oversized
        # / outside repo). Callers use this to decide whether an empty
        # return reflects a legitimate empty slice or a transient read
        # failure that should be retried on the next run, NOT persisted
        # as an empty-body sentinel (Codex round 5 regression).
        self._last_read_ok = True

    def _resolve(self, file_path: str) -> Path | None:
        """Resolve ``file_path`` to an absolute path within the repo root.

        Returns ``None`` (treated as a read failure) if the resolved path
        escapes the repo root. ``file_path`` comes straight out of the
        graph DB, so an attacker who can write to ``nodes.file_path``
        could otherwise point us at ``/etc/passwd`` or ``../../secret``
        and get the contents embedded into a cloud provider. We only
        allow the escape when ``repo_root`` is unset (e.g. unit tests that
        construct the cache with absolute fixture paths).
        """
        p = Path(file_path)
        if not p.is_absolute() and self._repo_root is not None:
            p = self._repo_root / p
        if self._repo_root is None:
            return p
        try:
            resolved = p.resolve()
            root = self._repo_root.resolve()
        except (OSError, RuntimeError):
            return None
        try:
            resolved.relative_to(root)
        except ValueError:
            return None
        return resolved

    def _load(self, file_path: str) -> list[str] | None:
        """Read and split a file; return ``None`` on any failure."""
        try:
            p = self._resolve(file_path)
            if p is None:
                return None
            st = p.stat()
            if st.st_size > _FILE_CACHE_MAX_BYTES:
                return None
            with p.open("rb") as fh:
                head = fh.read(_FILE_CACHE_SNIFF_BYTES)
                if b"\x00" in head:
                    return None
                rest = fh.read()
            raw = head + rest
        except (OSError, ValueError):
            return None
        try:
            text = raw.decode("utf-8", errors="replace")
        except Exception:
            return None
        return text.splitlines()

    def get_lines(self, file_path: str, line_start: int, line_end: int) -> list[str]:
        if line_start <= 0 or line_end < line_start:
            # Bogus range is a caller-side issue, not a read failure —
            # retrying would hit the same bug, so mark as OK and let the
            # caller persist whatever result they compute.
            self._last_read_ok = True
            return []
        key = file_path
        if key in self._cache:
            self._cache.move_to_end(key)
            lines = self._cache[key]
        else:
            lines = self._load(key)
            self._read_count += 1
            self._cache[key] = lines
            if len(self._cache) > self._max_entries:
                self._cache.popitem(last=False)
        self._last_read_ok = lines is not None
        if lines is None:
            return []
        if line_start > len(lines):
            return []
        return lines[line_start - 1:line_end]

    @property
    def last_read_ok(self) -> bool:
        return self._last_read_ok

    def clear(self) -> None:
        self._cache.clear()
        self._last_read_ok = True


def _looks_like_signature(line: str, node: GraphNode) -> bool:
    """Conservative substring check: is ``line`` the declaration of ``node``?

    Returns True only when ALL of:
      - ``node.name`` appears in the line
      - the line contains a language-specific declaration keyword
      - (if ``node.params`` is set) the first param identifier appears

    Unknown languages return False (do not dedup — keep the line).
    """
    s = line.strip()
    if not s:
        return False
    if node.name not in s:
        return False

    lang = (node.language or "").lower()

    if lang == "python":
        if not ("def " in s or "class " in s):
            return False
    elif lang in ("javascript", "typescript", "jsx", "tsx"):
        if not (
            "function " in s or "=>" in s or "class " in s or "async " in s
        ):
            return False
    elif lang in ("java", "kotlin"):
        has_type_name = bool(
            node.return_type and node.return_type in s and node.name in s
        )
        # Kotlin methods commonly omit an explicit return type (e.g.
        # ``override fun save(user: User) { ... }``); fall back to the
        # ``fun `` keyword so dedup still fires for those.
        has_kotlin_fun = lang == "kotlin" and "fun " in s
        if not (
            has_type_name or "class " in s or "interface " in s or has_kotlin_fun
        ):
            return False
    elif lang == "rust":
        if not any(
            k in s for k in ("fn ", "impl ", "struct ", "enum ", "trait ")
        ):
            return False
    elif lang == "go":
        if "func " not in s:
            return False
    else:
        return False

    if node.params:
        pstripped = node.params.strip("()").strip()
        if pstripped:
            first = pstripped.split(",", 1)[0].split(":", 1)[0].strip()
            # tokens like `*args`, `**kwargs`, `&self`, `self` are safe
            # to skip param gate on (always match): bail only when we have
            # a real identifier we can check.
            bare = first.lstrip("*&")
            if bare and bare != "self" and bare not in s:
                return False
    return True


def _extract_body_text(
    node: GraphNode,
    reader: _FileLineCache,
    *,
    max_chars: int,
    max_lines: int = _BODY_MAX_LINES_DEFAULT,
) -> str:
    """Return a truncated body snippet for semantic embedding.

    Pipeline:
      1. Skip ``File`` nodes and bogus line ranges.
      2. Read the node's ``[line_start, line_end]`` range via ``reader``.
      3. Drop line 0 if it looks like the node's signature (dedup with metadata).
      4. Run a block-comment + triple-quote state machine to skip leading
         docstrings and header comments; leading single-line comments
         (``#`` ``//`` ``--`` ``///`` ``*``) are dropped regardless of length.
      5. Collapse consecutive blank lines.
      6. Truncate by ``max_lines`` then by ``max_chars`` (prefer word boundary,
         append ``' …'`` suffix when cut).
    """
    if node.kind == "File":
        return ""
    if node.line_start is None or node.line_end is None:
        return ""
    if node.line_start <= 0 or node.line_end < node.line_start:
        return ""

    lines = reader.get_lines(node.file_path, node.line_start, node.line_end)
    if not lines:
        return ""

    # Step 3: signature dedup
    if _looks_like_signature(lines[0], node):
        lines = lines[1:]
    if not lines:
        return ""

    # Step 4: block-comment + triple-quote state machine
    kept: list[str] = []
    in_block_comment = False
    in_triple_quote: str | None = None
    drops = 0
    for line in lines:
        s = line.strip()

        # triple-quote state (Python docstring continuation)
        if in_triple_quote is not None:
            drops += 1
            if in_triple_quote in s:
                in_triple_quote = None
            continue

        # block-comment state (C/Java/JS /* ... */)
        if in_block_comment:
            drops += 1
            if "*/" in s:
                in_block_comment = False
            continue

        # leading-only triple-quote open
        if not kept:
            matched_triple = False
            for q in ('"""', "'''"):
                if s.startswith(q):
                    # same-line open+close: """one-liner."""
                    if len(s) >= len(q) * 2 and s.endswith(q):
                        drops += 1
                        matched_triple = True
                        break
                    # multi-line open: enter state (only if no closer on this line)
                    if q not in s[len(q):]:
                        in_triple_quote = q
                        drops += 1
                        matched_triple = True
                        break
            if matched_triple:
                continue

        # block-comment open: /* or /** on its own line
        if s.startswith("/*") or s.startswith("/**"):
            if "*/" not in s:
                # multi-line opener
                in_block_comment = True
                drops += 1
                continue
            # single-line inline block comment: drop only if the line has no
            # trailing code after the closing ``*/``.  Lines like
            # ``/* x */ real_code = 1`` keep flowing into the real-content
            # branch (handled by the leading-``*`` case below + kept).
            after_close = s.split("*/", 1)[1].strip()
            if not kept and not after_close:
                drops += 1
                continue

        # leading single-line comments: drop regardless of length (D-iter3-4)
        if not kept and s.startswith(("#", "//", "--", "///", "*")):
            drops += 1
            continue

        # blank line before any real code
        if not kept and s == "":
            drops += 1
            continue

        kept.append(line)

        # fallback safety cap
        if drops >= 20 and not kept:
            break

    if not kept:
        return ""

    # Step 5: collapse blank-line runs
    collapsed: list[str] = []
    prev_blank = False
    for ln in kept:
        is_blank = ln.strip() == ""
        if is_blank and prev_blank:
            continue
        collapsed.append(ln)
        prev_blank = is_blank

    # Step 6: truncate
    collapsed = collapsed[:max_lines]
    normalised = [ln.replace("\t", "    ").rstrip() for ln in collapsed]
    text = " \n ".join(normalised)
    if len(text) > max_chars:
        cut = text[:max_chars].rsplit(" ", 1)
        text = (cut[0] if len(cut) == 2 and cut[0] else text[:max_chars]) + " …"
    return text


def _split_combined_hash(stored: str) -> tuple[str, str, str]:
    """Parse the combined hash stored in ``embeddings.text_hash``.

    Format evolution (all three forms round-trip through this helper):

      * **Iter 1 legacy**: single sha256 hex, no ``':'``
        → ``(stored, '', '')``
      * **Iter 2 (body flag only)**: ``'meta_hash:body_hash'``
        → ``(meta, body, '')``
      * **Iter 3.2 (body + file fingerprint)**: ``'meta:body:file_hash'``
        → ``(meta, body, file_hash)``

    The third slot lets ``embed_nodes`` detect a body change without
    reading the file again: when metadata is unchanged but the node's
    file-level hash has shifted, the body snippet may have changed
    (Codex-review finding: metadata-only stage-1 filter would otherwise
    leave edits like ``return x * 2`` → ``return x * 3`` permanently
    stale). Rows older than Iter 3.2 report an empty file-hash slot,
    which callers treat as "unknown — must re-verify".
    """
    parts = stored.split(":", 2)
    if len(parts) == 1:
        return parts[0], "", ""
    if len(parts) == 2:
        return parts[0], parts[1], ""
    return parts[0], parts[1], parts[2]


def _node_to_text(
    node: GraphNode,
    reader: _FileLineCache | None = None,
    *,
    max_chars: int | None = None,
) -> str:
    """Build searchable text = metadata + optional body snippet.

    Backward-compatible: callers passing ``reader=None`` get the pre-Iter-2
    metadata-only string exactly as before. Legacy tests that construct a
    node and call ``_node_to_text(node)`` continue to pass unchanged.

    When ``reader`` is provided, ``max_chars`` is required (raises
    ``ValueError`` otherwise — ``assert`` would be stripped under
    ``python -O``). Caller is responsible for gating via ``_body_enabled``
    before passing a reader; this function does not re-check.
    """
    parts = [node.name]
    if node.kind != "File":
        parts.append(node.kind.lower())
    if node.parent_name:
        parts.append(f"in {node.parent_name}")
    if node.params:
        parts.append(node.params)
    if node.return_type:
        parts.append(f"returns {node.return_type}")
    if node.language:
        parts.append(node.language)

    if reader is not None:
        if max_chars is None:
            raise ValueError("max_chars required when reader provided")
        body = _extract_body_text(node, reader, max_chars=max_chars)
        if body:
            parts.append("body:")
            parts.append(body)
    return " ".join(parts)


def _body_enabled(store: "EmbeddingStore") -> bool:
    """ADR-C3 sticky-flag gate for body enrichment (Iter 3 D-iter3-1).

    Decision priority (first match wins; ALL paths write back the flag to
    ``embeddings_meta.embed_body_enabled`` so subsequent runs honour it):

      1. ``CRG_EMBED_INCLUDE_BODY=0`` -> False (write ``"0"``)
      2. ``CRG_EMBED_INCLUDE_BODY=1`` -> True  (write ``"1"``)
      3. Existing flag -> honour stored value
      4. No flag + ``store.count() == 0`` -> True  (auto-on for fresh DB)
      5. No flag + ``store.count()  > 0`` -> False (legacy DB opts out by default)

    The CLI ``--include-body --confirm-reembed`` combo is implemented by
    setting ``CRG_EMBED_INCLUDE_BODY=1`` before invoking embed, so case (2)
    fires and flips ``"0"`` -> ``"1"`` on legacy DBs.
    """
    env = os.environ.get("CRG_EMBED_INCLUDE_BODY")
    if env == "0":
        store._set_meta(_EMBED_META_KEY_BODY_ENABLED, "0")
        return False
    if env == "1":
        store._set_meta(_EMBED_META_KEY_BODY_ENABLED, "1")
        return True
    stored = store._get_meta(_EMBED_META_KEY_BODY_ENABLED)
    if stored == "1":
        return True
    if stored == "0":
        return False
    # Flag absent: decide based on DB emptiness and persist the choice.
    if store.count() == 0:
        store._set_meta(_EMBED_META_KEY_BODY_ENABLED, "1")
        return True
    store._set_meta(_EMBED_META_KEY_BODY_ENABLED, "0")
    return False


class EmbeddingStore:
    """Manages vector embeddings for graph nodes in SQLite."""

    def __init__(
        self,
        db_path: str | Path,
        provider: str | None = None,
        model: str | None = None,
    ) -> None:
        self.provider = get_provider(provider, model=model)
        self.available = self.provider is not None
        self.db_path = Path(db_path)
        self._conn = sqlite3.connect(
            str(self.db_path), timeout=30, check_same_thread=False,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_EMBEDDINGS_SCHEMA)

        # Migration for existing DBs missing the provider column
        try:
            self._conn.execute("SELECT provider FROM embeddings LIMIT 1")
        except sqlite3.OperationalError:
            self._conn.execute(
                "ALTER TABLE embeddings ADD COLUMN provider "
                "TEXT NOT NULL DEFAULT 'unknown'"
            )

        self._conn.commit()

    def __enter__(self) -> "EmbeddingStore":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:  # type: ignore[no-untyped-def]
        self.close()

    def close(self) -> None:
        self._conn.close()

    def _repo_root_guess(self) -> Path | None:
        """Best-effort root inference for the ``_FileLineCache``.

        Graph files live in ``.code-review-graph/graph.db``; walk upward from
        ``db_path`` to find that directory and return its parent. Returns
        ``None`` if we can't pin it down — callers interpret that as
        "use absolute file paths only".
        """
        try:
            parent = self.db_path.resolve().parent
            if parent.name == ".code-review-graph":
                return parent.parent
            # db_path may live directly inside a repo root (tests/tmp_path)
            return parent
        except OSError:
            return None

    # --- embeddings_meta (Iter 3 D-iter3-1 sticky flag storage) ---

    def _get_meta(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM embeddings_meta WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def _set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO embeddings_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._conn.commit()

    def embed_nodes(self, nodes: list[GraphNode], batch_size: int = 64) -> int:
        """Compute and store embeddings for a list of nodes.

        Two-stage filter (Iter 3 Directive 5):

          * **Stage 1 (zero IO)** — compute ``metadata_hash`` from the
            metadata-only ``_node_to_text`` form and compare with the stored
            hash's metadata half. If it matches AND body is disabled OR the
            row already has a body part embedded, skip without reading any
            file.
          * **Stage 2 (with IO)** — only for nodes that pass stage 1's
            escape-hatch branches (metadata changed, or body was just
            enabled on a legacy row): read the file through the
            ``_FileLineCache``, compute ``body_hash``, and store the
            combined ``"{metadata_hash}:{body_hash}"`` in ``text_hash``.
        """
        if not self.provider:
            return 0

        provider_name = self.provider.name
        body_on = _body_enabled(self)
        max_chars = _resolve_body_max_chars(provider_name) if body_on else 0
        reader = _FileLineCache(repo_root=self._repo_root_guess()) if body_on else None

        to_embed: list[tuple[GraphNode, str, str]] = []

        # Sort by file so the LRU cache gets consecutive hits within a file
        sorted_nodes = sorted(
            nodes,
            key=lambda n: (n.file_path or "", n.line_start or 0),
        )

        for node in sorted_nodes:
            if node.kind == "File":
                continue

            # --- Stage 1: metadata-only hash, zero IO ---
            meta_text = _node_to_text(node, reader=None)
            meta_hash = hashlib.sha256(meta_text.encode()).hexdigest()

            existing = self._conn.execute(
                "SELECT text_hash, provider FROM embeddings WHERE qualified_name = ?",
                (node.qualified_name,),
            ).fetchone()

            node_file_hash = node.file_hash or ""
            if existing and existing["provider"] == provider_name:
                stored_meta, stored_body, stored_file_hash = _split_combined_hash(
                    existing["text_hash"]
                )
                if stored_meta == meta_hash:
                    # Metadata identical. Skip when we can PROVE the body
                    # snippet is still fresh too. We require *both* sides
                    # to carry a non-empty file fingerprint that matches;
                    # an empty fingerprint on either side is not proof of
                    # freshness — it just means we don't know — so we
                    # fall through to stage 2 and reread the file. This
                    # is the conservative path Codex round 3 flagged:
                    # metadata-only rows with absent ``node.file_hash``
                    # would otherwise let body-only edits slip through.
                    if not body_on:
                        continue
                    if (stored_body != ""
                            and stored_file_hash != ""
                            and node_file_hash != ""
                            and stored_file_hash == node_file_hash):
                        continue
                    # else: fall through to stage 2 to refresh body_hash

            # --- Stage 2: body-enriched hash (reads file only when needed) ---
            if body_on and reader is not None:
                full_text = _node_to_text(node, reader=reader, max_chars=max_chars)
                body_part = full_text[len(meta_text):]
                if reader.last_read_ok:
                    # Always hash the body slot when body enrichment is
                    # on and the read succeeded, even when ``body_part``
                    # is empty (single-line function whose only line was
                    # the signature, or a body entirely stripped as
                    # docstring/header). Writing ``sha256("")`` here
                    # lets stage 1 distinguish "body ran and produced
                    # nothing" from "body never ran" (legacy / body-off
                    # rows), so empty-body nodes short-circuit too.
                    body_hash = hashlib.sha256(body_part.encode()).hexdigest()
                    combined_hash = f"{meta_hash}:{body_hash}:{node_file_hash}"
                    final_text = full_text
                else:
                    # File read failed (missing / permission / binary /
                    # oversized / outside repo). Do NOT persist the
                    # empty-body sentinel — that would freeze the node
                    # on a transient failure (Codex round 5). Fall back
                    # to the body-off stored form so the next run
                    # retries. We still embed the metadata-only text
                    # now, since we need *some* vector for this node.
                    combined_hash = f"{meta_hash}::{node_file_hash}"
                    final_text = meta_text
            else:
                combined_hash = f"{meta_hash}::{node_file_hash}"
                final_text = meta_text

            if (existing and existing["provider"] == provider_name
                    and existing["text_hash"] == combined_hash):
                continue

            to_embed.append((node, final_text, combined_hash))

        if reader is not None:
            reader.clear()

        if not to_embed:
            return 0

        # Encode in batches
        texts = [t for _, t, _ in to_embed]
        vectors = self.provider.embed(texts)

        for (node, _text, text_hash), vec in zip(to_embed, vectors):
            blob = _encode_vector(vec)
            self._conn.execute(
                """INSERT OR REPLACE INTO embeddings (qualified_name, vector, text_hash, provider)
                   VALUES (?, ?, ?, ?)""",
                (node.qualified_name, blob, text_hash, provider_name),
            )

        self._conn.commit()
        return len(to_embed)

    def search(self, query: str, limit: int = 20) -> list[tuple[str, float]]:
        """Search for nodes by semantic similarity."""
        if not self.provider:
            return []

        provider_name = self.provider.name
        query_vec = self.provider.embed_query(query)

        # Process in chunks, only matching current provider
        scored: list[tuple[str, float]] = []
        cursor = self._conn.execute(
            "SELECT qualified_name, vector FROM embeddings WHERE provider = ?",
            (provider_name,),
        )
        chunk_size = 500
        while True:
            rows = cursor.fetchmany(chunk_size)
            if not rows:
                break
            for row in rows:
                vec = _decode_vector(row["vector"])
                sim = _cosine_similarity(query_vec, vec)
                scored.append((row["qualified_name"], sim))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:limit]

    def remove_node(self, qualified_name: str) -> None:
        self._conn.execute(
            "DELETE FROM embeddings WHERE qualified_name = ?", (qualified_name,)
        )
        self._conn.commit()

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]


def embed_all_nodes(graph_store: GraphStore, embedding_store: EmbeddingStore) -> int:
    """Embed all non-file nodes in the graph."""
    if not embedding_store.available:
        return 0

    all_files = graph_store.get_all_files()
    all_nodes: list[GraphNode] = []
    for f in all_files:
        all_nodes.extend(graph_store.get_nodes_by_file(f))

    return embedding_store.embed_nodes(all_nodes)


def semantic_search(
    query: str,
    graph_store: GraphStore,
    embedding_store: EmbeddingStore,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Search nodes using vector similarity, falling back to keyword search."""
    if embedding_store.available and embedding_store.count() > 0:
        results = embedding_store.search(query, limit=limit)
        output = []
        for qn, score in results:
            node = graph_store.get_node(qn)
            if node:
                d = node_to_dict(node)
                d["similarity_score"] = round(score, 4)
                output.append(d)
        return output

    # Fallback to keyword search
    nodes = graph_store.search_nodes(query, limit=limit)
    return [node_to_dict(n) for n in nodes]
