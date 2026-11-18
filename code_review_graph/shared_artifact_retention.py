"""Retention for parser evidence and verified input manifests."""

from __future__ import annotations

import json
import re
import time
from sqlite3 import Connection

from .build_state import REFRESH_REQUEST_KEY

_Key = tuple[str, str, str]


def prune(
    conn: Connection, *, days: int, percent: int, max_bytes: int, apply: bool,
) -> tuple[int, int, int, int]:
    """Delete aged or over-budget evidence only when no graph still uses it."""
    tables = {name for (name,) in conn.execute(
        "SELECT name FROM sqlite_schema WHERE type='table'",
    )}
    facts_in_use: set[str] = set()
    manifests_in_use: set[str] = set()
    # Protect the legacy native materializations and cooperative receipts too:
    # an edit during refresh may have prevented a new verified publication.
    for table in tables:
        if re.fullmatch(r"crg_[0-9a-f]{64}_nodes", table):
            facts_in_use.update(row[0] for row in conn.execute(
                f"SELECT DISTINCT file_hash FROM {table} "
                "WHERE kind='File' AND file_hash IS NOT NULL",
            ))
        elif re.fullmatch(r"crg_[0-9a-f]{64}_metadata", table):
            for (value,) in conn.execute(
                f"SELECT value FROM {table} WHERE key=? AND value<>''", (REFRESH_REQUEST_KEY,),
            ):
                if digest := json.loads(value).get("result", {}).get("input_manifest_sha256"):
                    manifests_in_use.add(digest)

    if "crg_graph_generations" in tables:
        facts_in_use.update(row[0] for row in conn.execute("""
            SELECT DISTINCT json_extract(p.data, '$[1][11]')
            FROM crg_generation_payloads p JOIN crg_generation_members m ON m.payload_id=p.id
            JOIN crg_generation_bindings b USING (generation_id)
            WHERE json_extract(p.data, '$[0]')='nodes'
              AND json_extract(p.data, '$[1][0]')='File'
              AND json_extract(p.data, '$[1][11]') IS NOT NULL
        """))
    if "crg_graph_input_bindings" in tables:
        manifests_in_use.update(row[0] for row in conn.execute(
            "SELECT DISTINCT digest FROM crg_graph_input_bindings",
        ))

    now = time.time()
    marks = {
        (kind, profile, digest): since for kind, profile, digest, since in conn.execute(
            "SELECT kind, profile, digest, since FROM crg_artifact_unbound",
        )
    } if "crg_artifact_unbound" in tables else {}
    orphans: dict[_Key, tuple[float, int]] = {}
    active = 0
    if "crg_parser_facts" in tables:
        for profile, digest, size in conn.execute(
            "SELECT profile, digest, length(CAST(facts AS BLOB)) FROM crg_parser_facts",
        ):
            if digest in facts_in_use:
                active += size
            else:
                key = ("parser", profile, digest)
                orphans[key] = (marks.get(key, now), size)
    if "crg_graph_inputs" in tables:
        for digest, size in conn.execute(
            "SELECT digest, length(CAST(digest AS BLOB)) + 8 FROM crg_graph_inputs",
        ):
            if digest in manifests_in_use:
                active += size
            else:
                key = ("manifest", "", digest)
                orphans[key] = (marks.get(key, now), size)

    budget = min(max_bytes, active * percent // 100) if days else 0
    remaining = sum(size for _, size in orphans.values())
    expiry = now - days * 86400
    facts = []
    manifests = []
    reclaimed = 0
    for key, (since, size) in sorted(orphans.items(), key=lambda item: (item[1][0], item[0])):
        if days and percent and max_bytes and since > expiry and remaining <= budget:
            break
        kind, profile, digest = key
        if kind == "parser":
            facts.append((profile, digest))
        else:
            manifests.append((digest,))
        remaining -= size
        reclaimed += size
        del orphans[key]

    if apply:
        if facts:
            for table in ("crg_parser_fact_scopes", "crg_parser_facts"):
                conn.executemany(f"DELETE FROM {table} WHERE profile=? AND digest=?", facts)
        if manifests:
            conn.executemany("DELETE FROM crg_graph_inputs WHERE digest=?", manifests)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS crg_artifact_unbound (
                kind TEXT NOT NULL, profile TEXT NOT NULL, digest TEXT NOT NULL,
                since REAL NOT NULL, PRIMARY KEY (kind, profile, digest)
            ) WITHOUT ROWID
        """)
        conn.executemany(
            "DELETE FROM crg_artifact_unbound WHERE kind=? AND profile=? AND digest=?",
            (key for key in marks if key not in orphans),
        )
        conn.executemany(
            "INSERT OR IGNORE INTO crg_artifact_unbound VALUES (?, ?, ?, ?)",
            ((*key, since) for key, (since, _) in orphans.items() if key not in marks),
        )
    return len(facts), len(manifests), reclaimed, budget
