"""Text-search quality: FTS5 query construction and docstring indexing.

Two defects are covered here.

1. ``_fts_search`` used to wrap the whole user query in one pair of double
   quotes, turning every multi-word search into an exact-adjacency phrase
   match. ``password hashing`` found nothing unless some node carried those
   two tokens side by side. The query is now built from tokens.

2. ``nodes.extra['docstring']`` was extracted by the parser and fed to the
   embedding text builder but never reached the FTS index, so ~38% of the
   graph's prose was invisible to keyword search.

3. Building the expression from tokens invites questions asked in prose, and
   the first version of it neither deduplicated nor capped the terms, so the
   expression grew one term per word. FTS5 reads one doclist per term and
   scores every term against every matching row, and the OR widening grows
   the matching set with the term count too, so the cost climbed
   superlinearly: on a 120k-node graph the widened expression took 13 ms at
   5 terms and 173 ms at 50.

   On the cap's selection rule: index-measured rarity was tried and measured
   worse than token length. An FTS index over a repository holds every
   node's file path and qualified name, so the project's own vocabulary
   ("graph", "search", "parser") is what the index carries most often, while
   "today" and "whether" are genuinely rare in it. Ranking by index document
   frequency therefore keeps the prose scaffolding and drops the words that
   name the answer: on 18 long questions about this repository it scored MRR
   0.27 against 0.44 for token length, at the same cap.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.migrations import (
    LATEST_VERSION,
    NODES_FTS_COLUMNS,
    get_schema_version,
)
from code_review_graph.parser import NodeInfo
from code_review_graph.search import (
    _MAX_FTS_TERMS,
    build_fts_queries,
    hybrid_search,
    rebuild_fts_index,
    update_fts_index,
)

# ---------------------------------------------------------------------------
# build_fts_queries: pure query construction
# ---------------------------------------------------------------------------


class TestBuildFtsQueries:
    def test_multi_word_query_is_not_one_adjacency_phrase(self):
        """The whole query must not become a single quoted phrase."""
        queries = build_fts_queries("password hashing")
        assert queries, "expected at least one MATCH expression"
        assert '"password hashing"' not in queries[0]
        assert queries[0] == '"password"* AND "hashing"*'

    def test_multi_word_query_widens_to_or_as_a_second_attempt(self):
        """AND keeps precision; the OR widening is the recall fallback."""
        assert build_fts_queries("password hashing") == [
            '"password"* AND "hashing"*',
            '"password"* OR "hashing"*',
        ]

    def test_tokens_get_prefix_matching(self):
        """A partial identifier must still hit via a prefix term."""
        assert build_fts_queries("sanitiz") == ['"sanitiz"*']

    def test_single_character_token_gets_no_prefix(self):
        """``a*`` would match nearly every document; keep it exact."""
        assert build_fts_queries("a") == ['"a"']

    def test_two_character_token_gets_no_prefix(self):
        assert build_fts_queries("io") == ['"io"']

    def test_empty_query_produces_nothing(self):
        assert build_fts_queries("") == []
        assert build_fts_queries("   ") == []

    def test_punctuation_only_query_produces_nothing(self):
        assert build_fts_queries("...") == []
        assert build_fts_queries("-- ** //") == []

    def test_explicit_phrase_is_preserved_and_not_widened(self):
        """A phrase the user typed on purpose stays an adjacency match."""
        assert build_fts_queries('"impact radius"') == ['"impact radius"']

    def test_explicit_phrase_mixed_with_loose_words_is_not_widened(self):
        assert build_fts_queries('"impact radius" graph') == [
            '"impact radius" AND "graph"*'
        ]

    def test_fts5_operator_words_are_quoted_as_terms(self):
        """AND/OR/NOT/NEAR typed by a user are search terms, not operators."""
        assert build_fts_queries("AND") == ['"AND"*']
        assert build_fts_queries("NEAR") == ['"NEAR"*']
        # "OR" is two characters, so it takes no prefix star; it is still a
        # quoted term rather than the FTS5 disjunction operator.
        assert build_fts_queries("cache OR store") == [
            '"cache"* AND "OR" AND "store"*',
            '"cache"* OR "OR" OR "store"*',
        ]

    def test_hyphenated_token_stays_one_term(self):
        assert build_fts_queries("well-known") == ['"well-known"*']

    def test_dotted_token_stays_one_term(self):
        assert build_fts_queries("graph.py") == ['"graph.py"*']

    def test_embedded_double_quote_is_escaped(self):
        """An unbalanced quote must never break out of the quoted term."""
        queries = build_fts_queries('say"hi')
        assert queries == ['"say""hi"*']

    def test_snake_case_identifier_stays_a_single_precise_term(self):
        """``_sanitize_name`` must not widen into ``sanitize OR name``."""
        assert build_fts_queries("_sanitize_name") == ['"_sanitize_name"*']


class TestTermGrowthIsBounded:
    """The expression must not grow one term per word of the question."""

    def test_a_repeated_word_contributes_one_term(self):
        assert build_fts_queries("cache the cache") == [
            '"cache"* AND "the"*',
            '"cache"* OR "the"*',
        ]

    def test_deduplication_ignores_case(self):
        """The index is case-insensitive, so two spellings are one term."""
        assert build_fts_queries("Parser parser PARSER") == ['"Parser"*']

    def test_deduplication_keeps_the_first_spelling_typed(self):
        assert build_fts_queries("PARSER parser") == ['"PARSER"*']

    def test_term_count_is_capped(self):
        query = " ".join(f"token{i:02d}" for i in range(40))
        expression = build_fts_queries(query)[0]
        assert expression.count(" AND ") + 1 == _MAX_FTS_TERMS

    def test_an_or_widening_is_capped_too(self):
        query = " ".join(f"token{i:02d}" for i in range(40))
        widened = build_fts_queries(query)[1]
        assert widened.count(" OR ") + 1 == _MAX_FTS_TERMS

    def test_a_query_at_the_cap_is_untouched(self):
        tokens = [f"token{i:02d}" for i in range(_MAX_FTS_TERMS)]
        expression = build_fts_queries(" ".join(tokens))[0]
        assert expression == " AND ".join(f'"{t}"*' for t in tokens)

    def test_the_cap_keeps_the_rarest_terms_not_the_first_ones(self):
        """The words that say what is being asked about have to survive.

        Truncating to the first N would keep "how does the" and throw away
        "authentication". The kept terms are ranked by length, which across
        natural language tracks rarity.
        """
        question = (
            "how does the code that is run when a user of this app is asked "
            "to prove who they are reach the authentication middleware"
        )
        expression = build_fts_queries(question)[0]
        assert '"authentication"*' in expression
        assert '"middleware"*' in expression
        assert '"how"*' not in expression
        assert '"the"*' not in expression

    def test_capped_terms_stay_in_the_order_they_were_typed(self):
        question = (
            "aa bbbb cc dddd ee ffff gg hhhh ii jjjj kk llll mm nnnn oo pppp"
        )
        expression = build_fts_queries(question)[0]
        kept = [part.strip('"*') for part in expression.split(" AND ")]
        assert kept == sorted(kept, key=question.split().index)

    def test_a_quoted_phrase_survives_the_cap(self):
        """Quotes are an explicit request; the cap must not discard one."""
        question = '"impact radius" ' + " ".join(
            f"token{i:02d}" for i in range(40)
        )
        expression = build_fts_queries(question)[0]
        assert expression.startswith('"impact radius" AND ')
        assert expression.count(" AND ") + 1 == _MAX_FTS_TERMS


# ---------------------------------------------------------------------------
# Search behaviour on a seeded store
# ---------------------------------------------------------------------------


def _seed(store: GraphStore) -> None:
    nodes = [
        NodeInfo(
            kind="Function", name="hashPassword", file_path="auth/crypto.ts",
            line_start=1, line_end=12, language="typescript",
            extra={"docstring": "Derive a salted digest for a credential."},
        ),
        NodeInfo(
            kind="Function", name="verify_token", file_path="auth/session.py",
            line_start=1, line_end=20, language="python",
            extra={"docstring": "Validate a bearer token against the store."},
        ),
        NodeInfo(
            kind="Class", name="PasswordPolicy", file_path="auth/policy.py",
            line_start=1, line_end=40, language="python",
            extra={"docstring": "Rules governing credential strength."},
        ),
        NodeInfo(
            kind="Function", name="_sanitize_name", file_path="util/text.py",
            line_start=1, line_end=8, language="python",
        ),
        NodeInfo(
            kind="Function", name="sanitize_html", file_path="util/text.py",
            line_start=12, line_end=30, language="python",
        ),
        NodeInfo(
            kind="Function", name="compute_impact_radius",
            file_path="graph/impact.py", line_start=1, line_end=50,
            language="python",
            extra={"docstring": "Measure the blast radius of one change."},
        ),
        NodeInfo(
            kind="Class", name="Store", file_path="graph/store.py",
            line_start=1, line_end=90, language="python",
            extra={
                "docstring": (
                    "Every name passes through _sanitize_name and every "
                    "qualified_name is checked before it leaves the store."
                ),
            },
        ),
    ]
    for node in nodes:
        store.upsert_node(node, file_hash="h")
    store.commit()
    rebuild_fts_index(store)


@pytest.fixture
def store():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    graph_store = GraphStore(tmp.name)
    _seed(graph_store)
    yield graph_store
    graph_store.close()
    Path(tmp.name).unlink(missing_ok=True)


def _names(results: list[dict]) -> list[str]:
    return [r["name"] for r in results]


class TestSearchBehaviour:
    def test_multi_word_prose_query_finds_separated_words(self, store):
        """No node holds "password hashing" adjacently; search must still hit."""
        mode: list[str] = []
        results = hybrid_search(store, "password hashing", limit=20, _out_mode=mode)
        assert mode == ["fts"]
        assert "hashPassword" in _names(results)

    def test_partial_identifier_hits_via_prefix(self, store):
        """A phrase-only query for "sanit" matched nothing; a prefix term does."""
        mode: list[str] = []
        results = hybrid_search(store, "sanit", limit=20, _out_mode=mode)
        assert mode == ["fts"]
        assert set(_names(results)) >= {"_sanitize_name", "sanitize_html"}

    def test_truncation_inside_a_stemmed_suffix_still_resolves(self, store):
        """The porter stemmer bounds what a prefix term can reach.

        ``sanitize`` is indexed as its stem ``sanit``, and FTS5 stems the
        prefix term too, so ``sanitiz*`` matches nothing. The LIKE keyword
        path exists for exactly this case and still returns the symbol.
        """
        mode: list[str] = []
        results = hybrid_search(store, "sanitiz", limit=20, _out_mode=mode)
        assert mode == ["keyword"]
        assert "_sanitize_name" in _names(results)

    def test_exact_symbol_name_still_ranks_first(self, store):
        """A docstring that mentions the symbol must not outrank the symbol."""
        results = hybrid_search(store, "_sanitize_name", limit=20)
        assert results, "exact symbol query returned nothing"
        assert results[0]["name"] == "_sanitize_name"

    def test_exact_class_name_ranks_first(self, store):
        results = hybrid_search(store, "Store", limit=20)
        assert results[0]["name"] == "Store"

    def test_docstring_only_word_is_searchable(self, store):
        """"blast" appears in no name, path or signature -- only a docstring."""
        mode: list[str] = []
        results = hybrid_search(store, "blast", limit=20, _out_mode=mode)
        assert mode == ["fts"]
        assert "compute_impact_radius" in _names(results)

    def test_camel_case_inner_word_is_searchable(self, store):
        """unicode61 keeps hashPassword as one token; the split index fixes it.

        The assertion on ``mode`` matters: the LIKE fallback finds this by
        substring even with an empty FTS index, so without it the test would
        pass while the index-time split did nothing.
        """
        mode: list[str] = []
        results = hybrid_search(store, "password", limit=20, _out_mode=mode)
        assert mode == ["fts"]
        assert "hashPassword" in _names(results)

    def test_symbol_named_after_the_query_outranks_a_test_that_repeats_it(self):
        """BM25 alone gets this backwards.

        Every row carries its file path in two indexed columns, so path
        tokens dominate the length normalization, and a test whose class and
        method names both repeat the query words scores higher than the
        function the query describes. The symbol-coverage boost is what puts
        ``full_build`` first.
        """
        base = "/home/ci/workspace/checkout/project"
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        graph_store = GraphStore(tmp.name)
        try:
            graph_store.store_file_nodes_edges(
                f"{base}/pkg/incremental.py",
                [NodeInfo(
                    kind="Function", name="full_build",
                    file_path=f"{base}/pkg/incremental.py",
                    line_start=1, line_end=40, language="python",
                    extra={"docstring": "Full rebuild of the entire graph."},
                )],
                [],
            )
            graph_store.store_file_nodes_edges(
                f"{base}/tests/test_incremental.py",
                [NodeInfo(
                    kind="Test", name=f"test_full_build_{suffix}",
                    file_path=f"{base}/tests/test_incremental.py",
                    line_start=line, line_end=line + 5, language="python",
                    is_test=True, parent_name="TestFullBuild",
                ) for line, suffix in enumerate(
                    ("parses_files", "removes_deleted_dirs", "keeps_nodes",
                     "writes_metadata"), start=1,
                )],
                [],
            )
            rebuild_fts_index(graph_store)
            results = hybrid_search(graph_store, "full build", limit=10)
            assert _names(results)[0] == "full_build"
        finally:
            graph_store.close()
            Path(tmp.name).unlink(missing_ok=True)

    def test_operator_words_do_not_raise_or_act_as_operators(self, store):
        for query in ("AND", "OR", "NOT", "NEAR", "token NEAR store"):
            hybrid_search(store, query, limit=10)

    def test_degenerate_queries_do_not_raise(self, store):
        for query in ("", " ", "a", "...", '"impact radius"', "well-known",
                      "graph.py", 'say"hi', "*", "^", "(", "col:value"):
            hybrid_search(store, query, limit=10)


# ---------------------------------------------------------------------------
# Docstring column + FTS shape
# ---------------------------------------------------------------------------


def _fts_columns(conn: sqlite3.Connection) -> list[str]:
    return [row[1] for row in conn.execute("PRAGMA table_info(nodes_fts)")]


class TestDocstringIndexing:
    def test_upsert_node_stores_the_docstring_column(self, store):
        row = store._conn.execute(
            "SELECT docstring FROM nodes WHERE name = 'compute_impact_radius'"
        ).fetchone()
        assert row["docstring"] == "Measure the blast radius of one change."

    def test_bulk_build_path_stores_the_docstring_column(self, store):
        """A real build never calls upsert_node.

        ``store_file_nodes_edges`` takes the batched ``_replace_file_data``
        path, so a column only written by ``upsert_node`` is populated in
        tests and empty in every graph the CLI produces.
        """
        store.store_file_nodes_edges(
            "svc/mailer.py",
            [NodeInfo(
                kind="Function", name="sendReceipt", file_path="svc/mailer.py",
                line_start=1, line_end=9, language="python",
                extra={"docstring": "Post an invoice acknowledgement."},
            )],
            [],
        )
        row = store._conn.execute(
            "SELECT docstring, name_tokens FROM nodes WHERE name = 'sendReceipt'"
        ).fetchone()
        assert row["docstring"] == "Post an invoice acknowledgement."
        assert "Receipt" in (row["name_tokens"] or "")

    def test_file_node_does_not_fold_its_whole_path_into_the_index(self, store):
        """A File node's name is its path; only the stem may be split.

        Otherwise an absolute checkout path adds its directory words to every
        file in the graph and they all match each other.
        """
        store.store_file_nodes_edges(
            "/home/myUser/CloudDocs/svc/dataLoader.py",
            [NodeInfo(
                kind="File", name="/home/myUser/CloudDocs/svc/dataLoader.py",
                file_path="/home/myUser/CloudDocs/svc/dataLoader.py",
                line_start=1, line_end=1, language="python",
            )],
            [],
        )
        row = store._conn.execute(
            "SELECT name_tokens FROM nodes WHERE kind = 'File'"
        ).fetchone()
        tokens = (row["name_tokens"] or "").split()
        assert "Docs" not in tokens
        assert "User" not in tokens
        assert "Loader" in tokens

    def test_fts_table_indexes_docstring_and_split_tokens(self, store):
        columns = _fts_columns(store._conn)
        assert "docstring" in columns
        assert "name_tokens" in columns

    def test_rebuild_repopulates_docstrings_for_an_existing_graph(self, store):
        conn = store._conn
        conn.execute("DELETE FROM nodes_fts")
        conn.commit()
        rebuild_fts_index(store)
        hits = conn.execute(
            "SELECT count(*) FROM nodes_fts WHERE nodes_fts MATCH ?", ('"blast"',)
        ).fetchone()[0]
        assert hits == 1


class TestDocstringMigration:
    def test_pre_v13_database_gains_docstring_and_token_columns(self):
        """A graph built before this change must migrate without a rebuild."""
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        path = tmp.name
        try:
            store = GraphStore(path)
            conn = store._conn
            # Rewind to the pre-change schema shape.
            conn.execute("ALTER TABLE nodes DROP COLUMN docstring")
            conn.execute("ALTER TABLE nodes DROP COLUMN name_tokens")
            conn.execute("DROP TABLE IF EXISTS nodes_fts")
            conn.execute("""
                CREATE VIRTUAL TABLE nodes_fts USING fts5(
                    name, qualified_name, file_path, signature,
                    content='nodes', content_rowid='rowid',
                    tokenize='porter unicode61'
                )
            """)
            conn.execute(
                "INSERT INTO nodes (kind, name, qualified_name, file_path, "
                "language, extra, updated_at) VALUES "
                "('Function', 'renderPullRequest', 'ci/render.py::renderPullRequest', "
                "'ci/render.py', 'python', ?, 0)",
                ('{"docstring": "Format a review verdict as markdown."}',),
            )
            conn.execute(
                "UPDATE metadata SET value = '10' WHERE key = 'schema_version'"
            )
            store.commit()
            store.close()

            store = GraphStore(path)
            conn = store._conn
            assert get_schema_version(conn) == LATEST_VERSION
            row = conn.execute(
                "SELECT docstring, name_tokens FROM nodes "
                "WHERE name = 'renderPullRequest'"
            ).fetchone()
            assert row["docstring"] == "Format a review verdict as markdown."
            assert "Pull" in (row["name_tokens"] or "")
            assert "docstring" in _fts_columns(conn)

            results = hybrid_search(store, "verdict markdown", limit=10)
            assert [r["name"] for r in results] == ["renderPullRequest"]
            store.close()
        finally:
            Path(path).unlink(missing_ok=True)

    def test_v12_index_mirror_is_widened_to_every_indexed_column(self):
        """The mirror has to carry what an external-content delete replays.

        v12 created ``nodes_fts_state`` with the four columns the index had
        then. v13 adds two more to the index, and a delete that omits them
        leaves the old prose in the index -- SQLite reports the FTS table as
        malformed on the next query. The migration therefore widens the
        mirror and refills it from the rebuild it just ran.
        """
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        path = tmp.name
        try:
            store = GraphStore(path)
            conn = store._conn
            conn.execute("ALTER TABLE nodes DROP COLUMN docstring")
            conn.execute("ALTER TABLE nodes DROP COLUMN name_tokens")
            conn.execute("DROP TABLE IF EXISTS nodes_fts")
            conn.execute("DROP TABLE IF EXISTS nodes_fts_state")
            conn.execute(
                "CREATE TABLE nodes_fts_state (node_id INTEGER PRIMARY KEY, "
                "name TEXT, qualified_name TEXT, file_path TEXT, "
                "signature TEXT)"
            )
            conn.execute("""
                CREATE VIRTUAL TABLE nodes_fts USING fts5(
                    name, qualified_name, file_path, signature,
                    content='nodes', content_rowid='rowid',
                    tokenize='porter unicode61'
                )
            """)
            conn.execute(
                "INSERT INTO nodes (kind, name, qualified_name, file_path, "
                "language, extra, updated_at) VALUES "
                "('Function', 'hashPassword', 'auth.py::hashPassword', "
                "'auth.py', 'python', ?, 0)",
                ('{"docstring": "Derives a zygomorphic digest."}',),
            )
            conn.execute(
                "UPDATE metadata SET value = '12' WHERE key = 'schema_version'"
            )
            store.commit()
            store.close()

            store = GraphStore(path)
            conn = store._conn
            assert get_schema_version(conn) == LATEST_VERSION
            mirror = [
                row[1] for row in conn.execute("PRAGMA table_info(nodes_fts_state)")
            ]
            assert mirror == ["node_id", *NODES_FTS_COLUMNS]
            # The mirror describes the index the migration rebuilt, so the
            # next sync is a delta rather than another full rebuild.
            assert conn.execute(
                "SELECT count(*) FROM nodes_fts_state"
            ).fetchone()[0] == conn.execute(
                "SELECT count(*) FROM nodes"
            ).fetchone()[0]

            # A delta over the widened mirror keeps the index queryable.
            conn.execute(
                "UPDATE nodes SET docstring = 'Derives nothing at all.' "
                "WHERE name = 'hashPassword'"
            )
            store.commit()
            mode: list[str] = []
            update_fts_index(store, ["auth.py"], _out_mode=mode)
            assert mode == ["delta"]
            assert hybrid_search(store, "zygomorphic", limit=10) == []
            assert [r["name"] for r in hybrid_search(store, "password", limit=10)] == [
                "hashPassword"
            ]
            store.close()
        finally:
            Path(path).unlink(missing_ok=True)


class TestWideningDoesNotInventMatches:
    """What the OR widening does for a query that should match nothing."""

    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = GraphStore(self.tmp.name)
        _seed(self.store)
        rebuild_fts_index(self.store)

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def test_a_query_of_words_the_index_does_not_hold_returns_nothing(self):
        """Neither the AND nor the OR may invent a hit out of nothing."""
        assert hybrid_search(self.store, "zygomorphic quixotry", limit=20) == []
        assert hybrid_search(self.store, "zygomorphic", limit=20) == []

    def test_one_indexed_word_is_enough_for_the_widening_to_answer(self):
        """Measured, and deliberate: recall over silence.

        ``zygomorphic password`` cannot match anything as a conjunction, and
        the widening answers it with what ``password`` alone matches. The
        rows are real nodes, not phantoms, but the query as a whole is not
        what they match -- so this is pinned rather than claimed as correct.
        """
        rows = hybrid_search(self.store, "zygomorphic password", limit=20)
        assert rows
        assert all("password" in r["name"].lower()
                   or "password" in (r.get("docstring") or "").lower()
                   or "password" in r["qualified_name"].lower()
                   for r in rows)
