"""Vector embedding support for semantic code search.

Supports multiple providers with auto-detection based on API key presence:
1. Voyage AI (voyage-code-3) - Code-optimized cloud embeddings. Requires VOYAGEAI_API_KEY.
2. Google Gemini - High-quality, cloud-based. Requires GOOGLE_API_KEY.
3. MiniMax (embo-01) - High-quality 1536-dim cloud embeddings. Requires MINIMAX_API_KEY.
4. Local (sentence-transformers) - Private, fast, offline. Default fallback.

When no provider is explicitly requested, the first available API key wins
(checked in the order above). Set EMBEDDING_MODEL to override the default
model for whichever provider is active.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import struct
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

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
            "EMBEDDING_MODEL",
            os.environ.get("CRG_EMBEDDING_MODEL", LOCAL_DEFAULT_MODEL),
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


class VoyageAIEmbeddingProvider(EmbeddingProvider):
    """Voyage AI embedding provider.

    Uses the Voyage AI Python SDK with the voyage-code-3 model (1024 dimensions,
    optimized for code). Requires the VOYAGEAI_API_KEY environment variable.
    """

    _DEFAULT_MODEL = "voyage-code-3"
    _DIMENSION = 1024

    def __init__(self, api_key: str, model: str | None = None) -> None:
        self.model = model or self._DEFAULT_MODEL
        try:
            import voyageai
            self._client = voyageai.Client(api_key=api_key)
        except ImportError:
            raise ImportError(
                "voyageai not installed. "
                "Run: pip install code-review-graph[voyage-embeddings]"
            )

    @staticmethod
    def _call_with_retry(fn, max_retries: int = 3):
        """Call fn with exponential backoff on transient API errors."""
        for attempt in range(max_retries):
            try:
                return fn()
            except Exception as e:
                err_str = str(e)
                is_retryable = "429" in err_str or "500" in err_str or "503" in err_str
                if not is_retryable or attempt == max_retries - 1:
                    raise
                wait = 2 ** attempt
                logger.warning(
                    "Voyage AI API error (attempt %d/%d), retrying in %ds: %s",
                    attempt + 1, max_retries, wait, e,
                )
                time.sleep(wait)

    def embed(self, texts: list[str]) -> list[list[float]]:
        batch_size = 128
        results: list[list[float]] = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            response = self._call_with_retry(
                lambda b=batch: self._client.embed(
                    texts=b, model=self.model, input_type="document",
                )
            )
            results.extend([list(e) for e in response.embeddings])
        return results

    def embed_query(self, text: str) -> list[float]:
        response = self._call_with_retry(
            lambda: self._client.embed(
                texts=[text], model=self.model, input_type="query",
            )
        )
        return list(response.embeddings[0])

    @property
    def dimension(self) -> int:
        return self._DIMENSION

    @property
    def name(self) -> str:
        return f"voyageai:{self.model}"


def get_provider(
    provider: str | None = None,
    model: str | None = None,
) -> EmbeddingProvider | None:
    """Get an embedding provider by name or auto-detect from environment.

    When ``provider`` is explicitly set, the corresponding API key env var
    is required (raises ``ValueError`` if missing).

    When ``provider`` is ``None``, providers are tried in order by checking
    for API keys: ``VOYAGEAI_API_KEY`` -> ``GOOGLE_API_KEY`` ->
    ``MINIMAX_API_KEY`` -> local fallback.

    Args:
        provider: Provider name. One of ``"voyage"``, ``"google"``,
                  ``"minimax"``, ``"local"``, or ``None`` for auto-detect.
        model: Model name override. Falls back to ``EMBEDDING_MODEL`` env
               var, then to each provider's default.
    """
    # Resolve model: explicit param > EMBEDDING_MODEL env var > provider default
    if model is None:
        model = os.environ.get("EMBEDDING_MODEL")

    # --- Explicit provider requested ---
    if provider == "voyage":
        api_key = os.environ.get("VOYAGEAI_API_KEY")
        if not api_key:
            raise ValueError(
                "VOYAGEAI_API_KEY environment variable is required for "
                "the Voyage AI embedding provider."
            )
        try:
            return VoyageAIEmbeddingProvider(
                api_key=api_key,
                **({"model": model} if model else {}),
            )
        except ImportError:
            return None

    if provider == "minimax":
        api_key = os.environ.get("MINIMAX_API_KEY")
        if not api_key:
            raise ValueError(
                "MINIMAX_API_KEY environment variable is required for "
                "the MiniMax embedding provider."
            )
        return MiniMaxEmbeddingProvider(api_key=api_key)

    if provider == "google":
        api_key = os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise ValueError(
                "GOOGLE_API_KEY environment variable is required for "
                "the Google embedding provider."
            )
        try:
            return GoogleEmbeddingProvider(
                api_key=api_key,
                **({"model": model} if model else {}),
            )
        except ImportError:
            return None

    if provider == "local":
        try:
            return LocalEmbeddingProvider(model_name=model)
        except ImportError:
            return None

    if provider is not None:
        logger.warning("Unknown embedding provider '%s', falling back to local", provider)
        try:
            return LocalEmbeddingProvider(model_name=model)
        except ImportError:
            return None

    # --- Auto-detect: check API keys in priority order ---
    voyageai_key = os.environ.get("VOYAGEAI_API_KEY")
    if voyageai_key:
        try:
            return VoyageAIEmbeddingProvider(
                api_key=voyageai_key,
                **({"model": model} if model else {}),
            )
        except ImportError:
            logger.warning(
                "VOYAGEAI_API_KEY is set but voyageai is not installed. "
                "Install with: pip install code-review-graph[voyage-embeddings]"
            )

    google_key = os.environ.get("GOOGLE_API_KEY")
    if google_key:
        try:
            return GoogleEmbeddingProvider(
                api_key=google_key,
                **({"model": model} if model else {}),
            )
        except ImportError:
            logger.warning(
                "GOOGLE_API_KEY is set but google-generativeai is not installed. "
                "Install with: pip install code-review-graph[google-embeddings]"
            )

    minimax_key = os.environ.get("MINIMAX_API_KEY")
    if minimax_key:
        return MiniMaxEmbeddingProvider(api_key=minimax_key)

    # Fall back to local
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


def _node_to_text(node: GraphNode) -> str:
    """Convert a node to a searchable text representation."""
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
    return " ".join(parts)


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
        self._conn = sqlite3.connect(str(self.db_path), timeout=30, check_same_thread=False)
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

    def close(self) -> None:
        self._conn.close()

    def embed_nodes(self, nodes: list[GraphNode], batch_size: int = 64) -> int:
        """Compute and store embeddings for a list of nodes."""
        if not self.provider:
            return 0

        # Filter to nodes that need embedding
        to_embed: list[tuple[GraphNode, str, str]] = []
        provider_name = self.provider.name

        for node in nodes:
            if node.kind == "File":
                continue
            text = _node_to_text(node)
            text_hash = hashlib.sha256(text.encode()).hexdigest()

            existing = self._conn.execute(
                "SELECT text_hash, provider FROM embeddings WHERE qualified_name = ?",
                (node.qualified_name,),
            ).fetchone()

            # Re-embed if text changed OR provider changed
            if (existing and existing["text_hash"] == text_hash
                    and existing["provider"] == provider_name):
                continue
            to_embed.append((node, text, text_hash))

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
