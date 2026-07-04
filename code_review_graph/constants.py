"""Shared constants for code-review-graph."""

from __future__ import annotations

import os

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

# BFS engine: "sql" (SQLite recursive CTE) or "networkx" (Python-side BFS)
BFS_ENGINE = os.environ.get("CRG_BFS_ENGINE", "sql")

# ---------------------------------------------------------------------------
# Impact-radius scoring
# ---------------------------------------------------------------------------
# Change risk does not propagate equally across edge kinds: a direct call is
# a stronger coupling than an import, which is stronger than mere file
# membership. Each traversal hop multiplies the running score by the edge's
# weight and by IMPACT_DEPTH_DECAY; paths whose score falls below
# IMPACT_SCORE_FLOOR are not expanded further. Impacted nodes are ranked
# (and truncated) by their best-path score instead of arbitrary scan order.
#
# These weights model risk propagation for reviews; communities.EDGE_WEIGHTS
# models clustering affinity and intentionally differs (e.g. TESTED_BY is a
# weak clustering signal but a strong "this test is affected" signal).
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
IMPACT_DEPTH_DECAY = float(os.environ.get("CRG_IMPACT_DEPTH_DECAY", "0.6"))
IMPACT_SCORE_FLOOR = float(os.environ.get("CRG_IMPACT_SCORE_FLOOR", "0.05"))
