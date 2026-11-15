"""Portable parser facts and their directly observed Git memberships."""

from __future__ import annotations

import logging
import sys
from functools import lru_cache
from pathlib import Path
from sqlite3 import Connection

from .parser import PythonSyntaxFacts, extract_python_syntax_facts
from .repository_identity import repository_scopes

logger = logging.getLogger(__name__)

# This extractor uses CPython's AST. Bump the format when the evidence or
# encoding changes; use the exact interpreter version for AST semantics.
_PROFILE = f"python-ast:1:{sys.implementation.name}:{sys.version.split()[0]}"
_MAX_SOURCE_BYTES = 256 * 1024


class SharedPythonFacts:
    """Read facts before parser dispatch and leave resolution to the worker."""

    def __init__(self, conn: Connection, root: Path) -> None:
        self._conn = conn
        self._root = root
        self._scopes = lru_cache(maxsize=512)(repository_scopes)
        self.reused = 0
        self.extracted = 0
        conn.execute("""
            CREATE TABLE IF NOT EXISTS crg_parser_facts (
                profile TEXT NOT NULL, digest TEXT NOT NULL, facts TEXT NOT NULL,
                PRIMARY KEY (profile, digest)
            ) WITHOUT ROWID
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS crg_parser_fact_scopes (
                profile TEXT NOT NULL, digest TEXT NOT NULL, scope TEXT NOT NULL,
                PRIMARY KEY (profile, digest, scope),
                FOREIGN KEY (profile, digest) REFERENCES crg_parser_facts(profile, digest)
            ) WITHOUT ROWID
        """)

    def _repository(self, path: Path) -> Path:
        directory = path.parent
        while directory != self._root and directory.is_relative_to(self._root):
            if (directory / ".git").exists():
                return directory
            directory = directory.parent
        return self._root

    def get(self, path: Path, source: bytes, digest: str) -> PythonSyntaxFacts | None:
        if len(source) > _MAX_SOURCE_BYTES:
            return None
        scopes = self._scopes(self._repository(path))
        placeholders = ", ".join("?" for _ in scopes)
        row = self._conn.execute(
            "SELECT facts FROM crg_parser_facts JOIN crg_parser_fact_scopes "
            "USING (profile, digest) WHERE profile=? AND digest=? "
            f"AND scope IN ({placeholders}) LIMIT 1",
            (_PROFILE, digest, *scopes),
        ).fetchone()
        if row is not None:
            self.reused += 1
            return PythonSyntaxFacts.decode(row[0])

        facts = extract_python_syntax_facts(source)
        self.extracted += 1
        self._conn.execute(
            "INSERT OR IGNORE INTO crg_parser_facts VALUES (?, ?, ?)",
            (_PROFILE, digest, facts.encode()),
        )
        # A hit never adds a new origin and therefore cannot bridge repos
        # which have no remotes in common with each other.
        self._conn.executemany(
            "INSERT OR IGNORE INTO crg_parser_fact_scopes VALUES (?, ?, ?)",
            ((_PROFILE, digest, scope) for scope in scopes),
        )
        return facts

    def report(self) -> dict[str, int]:
        logger.info("Python syntax facts: %d reused, %d extracted", self.reused, self.extracted)
        return {"python_facts_reused": self.reused, "python_facts_extracted": self.extracted}
