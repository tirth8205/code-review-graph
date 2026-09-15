"""Reconciliation must reject graph roots that overlap only partly (#909)."""

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, incremental_update


def test_909_partial_root_overlap_is_refused_before_purge(tmp_path):
    root = tmp_path / "repo"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (root / "outer.py").write_text("def outer(): return 1\n")
    (nested / "inner.py").write_text("def inner(): return 2\n")
    with GraphStore(tmp_path / "graph.db") as store:
        full_build(root, store)
        before = list(store._conn.iterdump())
        with pytest.raises(RuntimeError, match="different repository root"):
            incremental_update(nested, store, changed_files=[])
        assert list(store._conn.iterdump()) == before
