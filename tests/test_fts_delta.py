"""Tests for the per-file FTS5 delta path.

``nodes_fts`` is an external-content FTS5 table, so its entries are keyed by
``nodes.rowid`` and their column values live in ``nodes``.  A delta that
removes the wrong rowid (or forgets one) leaves an index entry that still
matches a query but no longer resolves to a node: a phantom hit.  These
tests assert the delta never produces one, and that its result is entry for
entry identical to what a full rebuild produces.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

from code_review_graph.graph import GraphStore
from code_review_graph.parser import NodeInfo
from code_review_graph.postprocessing import run_post_processing
from code_review_graph.search import (
    _fts_search,
    hybrid_search,
    rebuild_fts_index,
    update_fts_index,
)


def _index_entries(store: GraphStore) -> list[tuple]:
    """Every term occurrence the FTS index actually holds.

    Read through ``fts5vocab`` so the index itself is inspected rather than
    the external content table: a phantom entry is invisible to
    ``SELECT * FROM nodes_fts`` but shows up here.
    """
    conn = store._conn
    conn.execute("DROP TABLE IF EXISTS temp.crg_vocab")
    conn.execute(
        "CREATE VIRTUAL TABLE temp.crg_vocab "
        "USING fts5vocab(main, nodes_fts, instance)"
    )
    rows = conn.execute(
        "SELECT term, doc, col, offset FROM temp.crg_vocab "
        "ORDER BY term, doc, col, offset"
    ).fetchall()
    return [tuple(row) for row in rows]


def _indexed_rowids(store: GraphStore) -> set[int]:
    return {entry[1] for entry in _index_entries(store)}


def _node_ids(store: GraphStore) -> set[int]:
    return {row[0] for row in store._conn.execute("SELECT id FROM nodes")}


def _rebuilt_entries(store: GraphStore) -> list[tuple]:
    """What the index would hold if it were rebuilt from scratch right now."""
    rebuild_fts_index(store)
    return _index_entries(store)


def _function(name: str, file_path: str, line: int = 1) -> NodeInfo:
    return NodeInfo(
        kind="Function",
        name=name,
        file_path=file_path,
        line_start=line,
        line_end=line + 4,
        language="python",
        params="(value: int)",
        return_type="int",
    )


class TestFtsDelta:
    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = GraphStore(self.tmp.name)
        self.store.store_file_nodes_edges(
            "alpha.py",
            [
                _function("vanishing_helper", "alpha.py", 1),
                _function("staying_helper", "alpha.py", 20),
            ],
            [],
            "hash-alpha",
        )
        self.store.store_file_nodes_edges(
            "beta.py",
            [_function("authenticate_request", "beta.py", 1)],
            [],
            "hash-beta",
        )
        self._set_signatures()
        rebuild_fts_index(self.store)

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def _set_signatures(self):
        rows = self.store.get_nodes_without_signature()
        self.store.update_node_signatures(
            [(f"def {row[1]}(value: int) -> int", row[0]) for row in rows]
        )

    # -- the phantom-row hazard ------------------------------------------

    def test_delta_removes_entries_for_a_deleted_node(self):
        """A node dropped from a file leaves no searchable index entry."""
        self.store.store_file_nodes_edges(
            "alpha.py",
            [_function("staying_helper", "alpha.py", 20)],
            [],
            "hash-alpha-2",
        )
        self._set_signatures()

        update_fts_index(self.store, ["alpha.py"])

        assert _fts_search(self.store._conn, "vanishing_helper") == []
        assert hybrid_search(self.store, "vanishing_helper") == []
        # No index entry may point at a rowid that is gone from nodes.
        assert _indexed_rowids(self.store) <= _node_ids(self.store)

    def test_delta_leaves_every_hit_resolvable(self):
        """Every rowid the index can return still resolves to a node row."""
        self.store.store_file_nodes_edges(
            "alpha.py",
            [_function("renamed_helper", "alpha.py", 1)],
            [],
            "hash-alpha-3",
        )
        self.store.remove_file_data("beta.py")
        self.store.commit()
        self._set_signatures()

        update_fts_index(self.store, ["alpha.py", "beta.py"])

        node_ids = _node_ids(self.store)
        for term, doc, _col, _offset in _index_entries(self.store):
            assert doc in node_ids, f"phantom index entry for term {term!r}"
        assert _fts_search(self.store._conn, "authenticate_request") == []

    def test_delta_result_is_identical_to_a_full_rebuild(self):
        """The delta produces the same index entries a full rebuild would."""
        self.store.store_file_nodes_edges(
            "alpha.py",
            [
                _function("staying_helper", "alpha.py", 20),
                _function("brand_new_helper", "alpha.py", 40),
            ],
            [],
            "hash-alpha-4",
        )
        self.store.store_file_nodes_edges(
            "gamma.py",
            [_function("gamma_entry", "gamma.py", 1)],
            [],
            "hash-gamma",
        )
        self._set_signatures()

        update_fts_index(self.store, ["alpha.py", "gamma.py"])
        after_delta = _index_entries(self.store)

        rebuild_fts_index(self.store)
        after_rebuild = _index_entries(self.store)

        assert after_delta == after_rebuild

    def test_delta_indexes_nodes_added_by_a_new_file(self):
        self.store.store_file_nodes_edges(
            "delta.py",
            [_function("freshly_added_symbol", "delta.py", 1)],
            [],
            "hash-delta",
        )
        self._set_signatures()

        update_fts_index(self.store, ["delta.py"])

        names = [r["name"] for r in hybrid_search(self.store, "freshly_added_symbol")]
        assert "freshly_added_symbol" in names

    def test_delta_elsewhere_leaves_unchanged_results_identical(self):
        before = hybrid_search(self.store, "authenticate_request")
        assert before

        self.store.store_file_nodes_edges(
            "alpha.py",
            [_function("staying_helper", "alpha.py", 20)],
            [],
            "hash-alpha-5",
        )
        self._set_signatures()
        update_fts_index(self.store, ["alpha.py"])

        assert hybrid_search(self.store, "authenticate_request") == before

    def test_delta_does_not_fall_back_to_a_full_rebuild(self):
        """A small per-file delta must not drop and repopulate the table."""
        self.store.store_file_nodes_edges(
            "alpha.py",
            [_function("staying_helper", "alpha.py", 20)],
            [],
            "hash-alpha-6",
        )
        self._set_signatures()

        with patch(
            "code_review_graph.search.rebuild_fts_index",
            side_effect=AssertionError("full rebuild taken"),
        ):
            update_fts_index(self.store, ["alpha.py"])

        assert _fts_search(self.store._conn, "vanishing_helper") == []

    def test_delta_removes_the_docstring_terms_of_a_changed_node(self):
        """Every indexed column has to be replayed to delete an entry.

        ``nodes_fts`` grew ``docstring`` and ``name_tokens`` in v13. An
        external-content delete only removes the terms it is handed, so a
        mirror that still carried the four original columns would leave the
        old prose in the index: a query for it would keep matching, now
        pointing at a node whose docstring no longer says it.
        """
        node = _function("authenticate_request", "beta.py", 1)
        node.extra = {"docstring": "verifies the caller's zygomorphic token"}
        self.store.store_file_nodes_edges("beta.py", [node], [], "hash-beta-1")
        self._set_signatures()
        rebuild_fts_index(self.store)
        assert _fts_search(self.store._conn, "zygomorphic")

        replacement = _function("authenticate_request", "beta.py", 1)
        replacement.extra = {"docstring": "verifies nothing in particular"}
        self.store.store_file_nodes_edges(
            "beta.py", [replacement], [], "hash-beta-2"
        )
        self._set_signatures()

        mode: list[str] = []
        update_fts_index(self.store, ["beta.py"], _out_mode=mode)

        assert mode == ["delta"]
        assert _fts_search(self.store._conn, "zygomorphic") == []
        assert _index_entries(self.store) == _rebuilt_entries(self.store)

    def test_delta_removes_the_token_split_of_a_renamed_node(self):
        """``name_tokens`` is derived, so it drifts with the node's name."""
        self.store.store_file_nodes_edges(
            "gamma.py", [_function("zygomorphicHelper", "gamma.py", 1)], [], "g1"
        )
        self._set_signatures()
        rebuild_fts_index(self.store)
        assert _fts_search(self.store._conn, "zygomorphic")

        self.store.store_file_nodes_edges(
            "gamma.py", [_function("plainHelper", "gamma.py", 1)], [], "g2"
        )
        self._set_signatures()

        mode: list[str] = []
        update_fts_index(self.store, ["gamma.py"], _out_mode=mode)

        assert mode == ["delta"]
        assert _fts_search(self.store._conn, "zygomorphic") == []
        assert _index_entries(self.store) == _rebuilt_entries(self.store)

    def test_delta_repairs_drift_without_a_file_hint(self):
        """Rows changed behind the index are resynced even with no hint."""
        self.store.remove_file_data("alpha.py")
        self.store.commit()

        update_fts_index(self.store)

        assert _indexed_rowids(self.store) == _node_ids(self.store)
        assert _fts_search(self.store._conn, "staying_helper") == []

    def test_delta_rebuilds_when_the_state_table_is_missing(self):
        self.store._conn.execute("DROP TABLE IF EXISTS nodes_fts_state")
        self.store.commit()

        count = update_fts_index(self.store, ["alpha.py"])

        assert count == len(_node_ids(self.store))
        assert _indexed_rowids(self.store) == _node_ids(self.store)

    def test_delta_rebuilds_when_the_index_predates_the_mirror(self):
        """A database built before the mirror must not be deleted from."""
        self.store._conn.execute(
            "DELETE FROM metadata WHERE key = 'fts_state_synced'"
        )
        self.store.commit()

        mode: list[str] = []
        update_fts_index(self.store, ["alpha.py"], _out_mode=mode)

        assert mode == ["rebuild"]
        assert _indexed_rowids(self.store) == _node_ids(self.store)

    def test_delta_returns_the_indexed_row_count(self):
        assert update_fts_index(self.store, ["alpha.py"]) == len(_node_ids(self.store))

    def test_delta_reports_its_mode(self):
        mode: list[str] = []
        update_fts_index(self.store, ["alpha.py"], _out_mode=mode)
        assert mode == ["delta"]

        mode.clear()
        self.store._conn.execute("DELETE FROM nodes_fts_state")
        self.store.commit()
        update_fts_index(self.store, ["alpha.py"], _out_mode=mode)
        assert mode == ["rebuild"]


class TestPostProcessingFtsDelta:
    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = GraphStore(self.tmp.name)
        self.store.store_file_nodes_edges(
            "alpha.py",
            [
                _function("doomed_symbol", "alpha.py", 1),
                _function("kept_symbol", "alpha.py", 20),
            ],
            [],
            "hash-alpha",
        )
        run_post_processing(self.store)

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def test_post_processing_delta_removes_the_deleted_symbol(self):
        self.store.store_file_nodes_edges(
            "alpha.py",
            [_function("kept_symbol", "alpha.py", 20)],
            [],
            "hash-alpha-2",
        )

        result = run_post_processing(self.store, changed_files=["alpha.py"])

        assert result["fts_indexed"] == len(_node_ids(self.store))
        assert _fts_search(self.store._conn, "doomed_symbol") == []
        assert _indexed_rowids(self.store) <= _node_ids(self.store)

    def test_post_processing_delta_matches_a_full_rebuild(self):
        self.store.store_file_nodes_edges(
            "alpha.py",
            [_function("kept_symbol", "alpha.py", 20)],
            [],
            "hash-alpha-2",
        )
        self.store.store_file_nodes_edges(
            "omega.py",
            [_function("omega_symbol", "omega.py", 1)],
            [],
            "hash-omega",
        )

        run_post_processing(self.store, changed_files=["alpha.py", "omega.py"])
        after_delta = _index_entries(self.store)

        rebuild_fts_index(self.store)
        assert _index_entries(self.store) == after_delta


class TestFtsFileHint:
    def test_hint_uses_the_spelling_the_graph_stores(self, tmp_path):
        """The delta hint must match ``nodes.file_path``, or it narrows nothing."""
        from unittest.mock import patch as _patch

        from code_review_graph.incremental import full_build
        from code_review_graph.tools.build import _fts_file_hint

        (tmp_path / "pkg").mkdir()
        (tmp_path / "pkg" / "mod.py").write_text("def hinted():\n    pass\n")
        (tmp_path / ".git").mkdir()

        store = GraphStore(tmp_path / "graph.db")
        try:
            with _patch(
                "code_review_graph.incremental.get_all_tracked_files",
                return_value=["pkg/mod.py"],
            ):
                full_build(tmp_path, store)
            stored = {
                row[0]
                for row in store._conn.execute("SELECT DISTINCT file_path FROM nodes")
            }
        finally:
            store.close()

        assert set(_fts_file_hint(str(tmp_path), ["pkg/mod.py"])) <= stored

    def test_hint_is_absent_when_nothing_changed(self):
        from code_review_graph.tools.build import _fts_file_hint

        assert _fts_file_hint("/repo", None) is None
        assert _fts_file_hint("/repo", []) is None
