"""Shared constants for code-review-graph."""

from __future__ import annotations

import math
import os
from pathlib import Path


def _bounded_float_env(
    name: str,
    default: float,
    *,
    lower: float,
    upper: float,
) -> float:
    """Read a finite float strictly inside ``(lower, upper)``.

    Invalid environment configuration falls back to the documented default
    instead of making graph traversal unbounded or failing during import.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value) or not lower < value < upper:
        return default
    return value

SECURITY_KEYWORDS: frozenset[str] = frozenset({
    "auth", "login", "password", "token", "session", "crypt", "secret",
    "credential", "permission", "sql", "query", "execute", "connect",
    "socket", "request", "http", "sanitize", "validate", "encrypt",
    "decrypt", "hash", "sign", "verify", "admin", "privilege",
})

# ---------------------------------------------------------------------------
# Configurable limits (override via environment variables)
# ---------------------------------------------------------------------------
MAX_IMPACT_NODES = int(os.environ.get("CRG_MAX_IMPACT_NODES", "500"))
MAX_IMPACT_DEPTH = int(os.environ.get("CRG_MAX_IMPACT_DEPTH", "2"))
MAX_BFS_DEPTH = int(os.environ.get("CRG_MAX_BFS_DEPTH", "15"))
MAX_SEARCH_RESULTS = int(os.environ.get("CRG_MAX_SEARCH_RESULTS", "20"))

# Impact traversal engine: "sql" (bounded SQLite relaxation) or "networkx".
BFS_ENGINE = os.environ.get("CRG_BFS_ENGINE", "sql")

# ---------------------------------------------------------------------------
# Impact-radius scoring
# ---------------------------------------------------------------------------
# Each hop multiplies the best score so strongly coupled nodes rank first.
# These review-risk weights intentionally differ from community-clustering
# affinity weights.
IMPACT_EDGE_WEIGHTS: dict[str, float] = {
    "CALLS": 1.0,
    "INHERITS": 0.9,
    "OVERRIDES": 0.9,
    "IMPLEMENTS": 0.9,
    "TESTED_BY": 0.7,
    "REFERENCES": 0.6,
    "DEPENDS_ON": 0.6,
    "IMPORTS_FROM": 0.5,
    "CONTAINS": 0.3,
}
IMPACT_DEFAULT_EDGE_WEIGHT = 0.5
IMPACT_DEPTH_DECAY = _bounded_float_env(
    "CRG_IMPACT_DEPTH_DECAY", 0.6, lower=0.0, upper=1.0,
)
IMPACT_SCORE_FLOOR = _bounded_float_env(
    "CRG_IMPACT_SCORE_FLOOR", 0.05, lower=0.0, upper=1.0,
)


#: Overrides the per-user state directory that holds ``registry.json``,
#: ``watch.toml``, ``daemon.pid``, ``daemon-state.json`` and ``logs/``.
#: Follows the same convention as CRG_DATA_DIR.
CRG_HOME_ENV = "CRG_HOME"

_DEFAULT_CRG_HOME = Path.home() / ".code-review-graph"


def crg_home() -> Path:
    """Return the per-user state directory for code-review-graph.

    ``$CRG_HOME`` wins when set and non-empty; otherwise
    ``~/.code-review-graph``.

    Resolved per call rather than captured in a module-level constant. An
    import-time constant cannot be redirected afterwards, which is what let
    the test suite write into the real home directory of whoever ran it: by
    the time a fixture set the variable, the value had already been frozen.
    """
    override = os.environ.get(CRG_HOME_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return _DEFAULT_CRG_HOME
