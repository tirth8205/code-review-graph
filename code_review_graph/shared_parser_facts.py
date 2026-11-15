"""Portable parser facts and their directly observed Git memberships."""

from __future__ import annotations

import json
import logging
import sys
from functools import lru_cache
from importlib.metadata import version
from pathlib import Path
from sqlite3 import Connection

from .parser import CodeParser, PreparedPythonFacts, PythonSyntaxFacts, extract_python_syntax_facts
from .repository_identity import repository_scopes

logger = logging.getLogger(__name__)

# This extractor uses CPython's AST. Bump the format when the evidence or
# encoding changes; use the exact interpreter version for AST semantics.
_PROFILE = f"python-ast:1:{sys.implementation.name}:{sys.version.split()[0]}"
_MAX_SOURCE_BYTES = 256 * 1024


class SharedPythonFacts:
    """Read facts before parser dispatch and leave resolution to the worker."""

    def __init__(self, conn: Connection, root: Path, parser: CodeParser) -> None:
        self._conn = conn
        self._root = root
        self._scopes = lru_cache(maxsize=512)(repository_scopes)
        self._method_profile = json.dumps((
            "tree-sitter-python-method-scopes", 1, version("tree-sitter"),
            version("tree-sitter-language-pack"), parser.python_method_scope_profile(),
        ), separators=(",", ":"))
        self.structural_reused = 0
        self.structural_extracted = 0
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

    def _read(self, profile: str, digest: str, scopes: tuple[str, ...]) -> str | None:
        placeholders = ", ".join("?" for _ in scopes)
        row = self._conn.execute(
            "SELECT facts FROM crg_parser_facts JOIN crg_parser_fact_scopes "
            "USING (profile, digest) WHERE profile=? AND digest=? "
            f"AND scope IN ({placeholders}) LIMIT 1",
            (profile, digest, *scopes),
        ).fetchone()
        return row[0] if row is not None else None

    def _write(self, profile: str, digest: str, scopes: tuple[str, ...], data: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO crg_parser_facts VALUES (?, ?, ?)",
            (profile, digest, data),
        )
        # A hit never adds a new origin and therefore cannot bridge repos
        # which have no remotes in common with each other.
        self._conn.executemany(
            "INSERT OR IGNORE INTO crg_parser_fact_scopes VALUES (?, ?, ?)",
            ((profile, digest, scope) for scope in scopes),
        )
    def get(self, path: Path, source: bytes, digest: str) -> PreparedPythonFacts | None:
        if len(source) > _MAX_SOURCE_BYTES:
            return None
        scopes = self._scopes(self._repository(path))
        raw = self._read(_PROFILE, digest, scopes)
        if raw is None:
            facts = extract_python_syntax_facts(source)
            self._write(_PROFILE, digest, scopes, facts.encode())
            self.extracted += 1
        else:
            facts = PythonSyntaxFacts.decode(raw)
            self.reused += 1
        structural = self._read(self._method_profile, digest, scopes)
        if structural is not None:
            self.structural_reused += 1
            method_scopes = dict(json.loads(structural))
        else:
            method_scopes = None
        return PreparedPythonFacts(facts, method_scopes)

    def remember(self, path: Path, digest: str, method_scopes: dict[int, str]) -> None:
        scopes = self._scopes(self._repository(path))
        self._write(self._method_profile, digest, scopes, json.dumps(sorted(method_scopes.items())))
        self.structural_extracted += 1

    def report(self) -> dict[str, int]:
        logger.info("Python syntax facts: %d reused, %d extracted", self.reused, self.extracted)
        logger.info("Tree-sitter method scopes: %d reused, %d extracted",
                    self.structural_reused, self.structural_extracted)
        return {"python_facts_reused": self.reused, "python_facts_extracted": self.extracted,
                "structural_facts_reused": self.structural_reused,
                "structural_facts_extracted": self.structural_extracted}
