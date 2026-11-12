"""A method call on a receiver of unknown type does not bind to a same-file method.

In ripgrep's ``lib.rs``, ``v.map(...)`` in a free function was bound to
``lib.rs::Match.map`` only because both sit in one file. The parser records no
type for ``v`` in Rust, so nothing says ``v`` is a ``Match``. See: crg-1la
"""

from __future__ import annotations

import json
from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import get_db_path
from code_review_graph.tools.build import build_or_update_graph

CARGO = '[package]\nname = "demo"\nversion = "0.1.0"\nedition = "2021"\n'

LIB_RS = (
    "pub struct Match {\n"
    "    start: usize,\n"
    "    end: usize,\n"
    "}\n"
    "\n"
    "impl Match {\n"
    "    pub fn new(start: usize, end: usize) -> Match {\n"
    "        Match { start, end }\n"
    "    }\n"
    "\n"
    "    pub fn map(&self, shift: usize) -> Match {\n"
    "        Match::new(self.start + shift, self.end + shift)\n"
    "    }\n"
    "\n"
    "    pub fn moved(&self) -> Match {\n"
    "        self.map(1)\n"
    "    }\n"
    "\n"
    "    pub fn other(&self, v: Option<usize>) -> Option<usize> {\n"
    "        v.map(|x| x + self.start)\n"
    "    }\n"
    "}\n"
    "\n"
    "pub fn shift(v: Option<usize>) -> Option<usize> {\n"
    "    v.map(|x| x + 1)\n"
    "}\n"
)


def _calls(tmp_path: Path) -> list[dict]:
    root = tmp_path.resolve()
    (root / ".git").mkdir()
    (root / "Cargo.toml").write_text(CARGO, encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "lib.rs").write_text(LIB_RS, encoding="utf-8")
    result = build_or_update_graph(full_rebuild=True, repo_root=str(root))
    assert result["status"] == "ok", result
    store = GraphStore(get_db_path(root))
    try:
        rows = store._conn.execute(
            "SELECT source_qualified, target_qualified, extra FROM edges "
            "WHERE kind = 'CALLS' ORDER BY line"
        ).fetchall()
    finally:
        store.close()
    return [
        {
            "source": row["source_qualified"].rpartition("::")[2],
            "target": row["target_qualified"],
            "extra": json.loads(row["extra"] or "{}"),
        }
        for row in rows
    ]


def test_free_function_call_on_untyped_receiver_stays_unbound(tmp_path):
    calls = _calls(tmp_path)
    [row] = [call for call in calls if call["source"] == "shift"]

    assert row["target"] == "map"
    assert row["extra"].get("receiver") == "v"


def test_method_call_on_another_untyped_receiver_stays_unbound(tmp_path):
    calls = _calls(tmp_path)
    [row] = [call for call in calls if call["source"] == "Match.other"]

    assert row["target"] == "map"


def test_self_and_type_calls_still_bind(tmp_path):
    calls = _calls(tmp_path)
    by_source = {call["source"]: call["target"] for call in calls}

    assert by_source["Match.moved"].endswith("src/lib.rs::Match.map")
    assert by_source["Match.map"].endswith("src/lib.rs::Match.new")
