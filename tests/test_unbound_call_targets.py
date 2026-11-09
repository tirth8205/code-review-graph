"""An edge that names no node says so, and an answer built on one says so too.

The parser qualifies a call whenever it can guess the file: ``h.hashCode()`` on a
``Helper`` declared in the same Java file became ``One.java::Helper.hashCode``,
and ``Vec::new()`` in a Cargo crate became ``src/lib.rs::new``. Neither names a
node, but a file path in front of a name passes every check a real one does, and
every edge claimed confidence 1.0 whatever had happened to it. See: #984
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, get_db_path, incremental_update
from code_review_graph.parser import EdgeInfo, NodeInfo
from code_review_graph.refactor import find_dead_code, rename_preview
from code_review_graph.tools import query_graph
from code_review_graph.tools.build import build_or_update_graph
from code_review_graph.uncertainty import MAX_CONFIDENCE_CHARS


def _build(root: Path, files: dict[str, str]) -> GraphStore:
    root = root.resolve()
    (root / ".code-review-graph").mkdir(exist_ok=True)
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    result = build_or_update_graph(full_rebuild=True, repo_root=str(root))
    assert result["status"] == "ok", result
    return GraphStore(get_db_path(root))


def _edges(store: GraphStore, kind: str = "CALLS") -> list[dict]:
    rows = store._conn.execute(
        "SELECT source_qualified, target_qualified, line, extra, confidence, "
        "confidence_tier, target_resolution FROM edges WHERE kind = ? ORDER BY line, id",
        (kind,),
    ).fetchall()
    return [
        {
            "source": row["source_qualified"],
            "target": row["target_qualified"],
            "line": row["line"],
            "extra": json.loads(row["extra"] or "{}"),
            "confidence": row["confidence"],
            "tier": row["confidence_tier"],
            "resolution": row["target_resolution"],
        }
        for row in rows
    ]


def _store(tmp_path: Path) -> tuple[Path, GraphStore]:
    root = tmp_path.resolve()
    (root / ".git").mkdir()
    return root, GraphStore(get_db_path(root))


def _node(store: GraphStore, path: Path, name: str, parent: str | None = None) -> str:
    store.upsert_node(NodeInfo(
        kind="Function", name=name, file_path=str(path), line_start=1, line_end=5,
        language="python", parent_name=parent,
    ))
    return f"{path}::{parent}.{name}" if parent else f"{path}::{name}"


def _row(store: GraphStore, kind: str = "CALLS") -> dict:
    rows = _edges(store, kind)
    assert len(rows) == 1
    return rows[0]


JAVA_ONE = (
    "public class One {\n"
    "    public int run() { Helper h = new Helper(); return h.hashCode() + h.build(); }\n"
    "}\n"
    "class Helper { public int build() { return 1; } }\n"
)


def test_java_method_the_type_lacks_is_not_a_qualified_target(tmp_path):
    store = _build(tmp_path, {"One.java": JAVA_ONE})
    try:
        calls = {row["target"].rsplit("/", 1)[-1]: row for row in _edges(store)}
    finally:
        store.close()

    assert "One.java::Helper.hashCode" not in calls
    guess = calls["hashCode"]
    assert guess["extra"]["claimed_endpoint"].endswith("One.java::Helper.hashCode")
    assert guess["tier"] == "UNRESOLVED"
    assert guess["confidence"] < 1.0
    assert guess["resolution"] == "unresolved"
    # The same receiver's real method is untouched.
    real = calls["One.java::Helper.build"]
    assert real["tier"] == "EXTRACTED"
    assert real["confidence"] == 1.0
    assert real["resolution"] == "direct"
    assert "claimed_endpoint" not in real["extra"]


def test_a_settled_target_is_not_bound_to_a_same_named_method_instead(tmp_path):
    """``h.hashCode()`` must not land on ``One.hashCode`` for sharing a file."""
    store = _build(tmp_path, {
        "One.java": (
            "public class One {\n"
            "    public int hashCode() { Helper h = new Helper(); return h.hashCode(); }\n"
            "}\n"
            "class Helper { public int build() { return 1; } }\n"
        ),
    })
    try:
        targets = [row["target"] for row in _edges(store) if row["line"] == 2]
    finally:
        store.close()

    assert "hashCode" in targets
    assert not any(target.endswith("One.hashCode") for target in targets)


CARGO = '[package]\nname = "demo"\nversion = "0.1.0"\nedition = "2021"\n'


def test_rust_external_scoped_call_stays_what_the_source_wrote(tmp_path):
    store = _build(tmp_path, {
        "Cargo.toml": CARGO,
        "src/lib.rs": (
            "pub mod helper;\n"
            "pub fn run() -> usize {\n"
            "    let v: Vec<u8> = Vec::new();\n"
            "    let w = Widget::new();\n"
            "    helper::work() + v.len() + w.size()\n"
            "}\n"
            "pub struct Widget;\n"
            "impl Widget {\n"
            "    pub fn new() -> Self { Widget }\n"
            "    pub fn size(&self) -> usize { 1 }\n"
            "}\n"
        ),
        "src/helper.rs": "pub fn work() -> usize { 1 }\n",
    })
    try:
        targets = {row["target"]: row for row in _edges(store)}
    finally:
        store.close()

    assert "Vec::new" in targets
    assert targets["Vec::new"]["tier"] == "UNRESOLVED"
    assert not any(target.endswith("src/lib.rs::new") for target in targets)
    # Real associated functions and real modules still bind.
    assert any(target.endswith("src/lib.rs::Widget.new") for target in targets)
    assert any(target.endswith("src/helper.rs::work") for target in targets)


def test_rust_inline_module_call_still_binds(tmp_path):
    """``inner::bar()`` names a module in this file, not a type from outside.

    An inline module's items are named after the file, so the call lands on
    ``src/lib.rs::bar``. Refusing every unanchored first segment must not lose it.
    """
    store = _build(tmp_path, {
        "Cargo.toml": CARGO,
        "src/lib.rs": (
            "pub mod helper;\n"
            "mod inner {\n"
            "    pub fn bar() -> usize { 1 }\n"
            "}\n"
            "pub fn run() -> usize { inner::bar() + helper::work() }\n"
        ),
        "src/helper.rs": (
            "mod deep {\n"
            "    pub fn step() -> usize { 1 }\n"
            "}\n"
            "pub fn work() -> usize { deep::step() + Vec::<u8>::new().len() }\n"
        ),
    })
    try:
        targets = {row["target"]: row for row in _edges(store)}
    finally:
        store.close()

    root = tmp_path.resolve()
    assert targets[f"{root}/src/lib.rs::bar"]["tier"] == "EXTRACTED"
    # The module is this file's, wherever the file sits in the crate.
    assert targets[f"{root}/src/helper.rs::step"]["tier"] == "EXTRACTED"
    assert not any(target.endswith("::new") and "/" in target for target in targets)


def test_rust_type_is_not_read_as_a_module_file_of_another_case(tmp_path):
    """``Glob::new()`` is the type ``Glob``, not the module file ``glob.rs``.

    macOS and Windows find ``Glob.rs`` when asked for it and only ``glob.rs``
    exists, which turned the call into ``src/Glob.rs::new``.
    """
    store = _build(tmp_path, {
        "Cargo.toml": CARGO,
        "src/lib.rs": (
            "pub mod glob;\n"
            "pub mod user;\n"
            "pub use glob::Glob;\n"
            "pub fn run() -> usize { Glob::new().size() }\n"
        ),
        "src/user.rs": "use crate::Glob;\npub fn go() -> usize { Glob::new().size() }\n",
        "src/glob.rs": (
            "pub struct Glob;\n"
            "impl Glob {\n"
            "    pub fn new() -> Self { Glob }\n"
            "    pub fn size(&self) -> usize { 1 }\n"
            "}\n"
        ),
    })
    try:
        targets = {row["target"] for row in _edges(store)}
        claims = {row["extra"].get("claimed_endpoint") for row in _edges(store)}
    finally:
        store.close()

    root = tmp_path.resolve()
    assert f"{root}/src/glob.rs::Glob.new" in targets
    assert not any("Glob.rs" in str(name) for name in targets | claims)


def test_rust_tested_by_mirror_does_not_keep_the_invented_source(tmp_path):
    store = _build(tmp_path, {
        "Cargo.toml": CARGO,
        "src/lib.rs": (
            "pub fn run() -> usize { 1 }\n"
            "#[test]\n"
            "fn makes_a_vec() { let v: Vec<u8> = Vec::new(); assert_eq!(v.len() + run(), 1); }\n"
        ),
    })
    try:
        sources = {row["source"] for row in _edges(store, "TESTED_BY")}
    finally:
        store.close()

    assert not any(source.endswith("src/lib.rs::new") for source in sources)


def test_inherited_call_goes_to_its_ancestor_not_another_class(tmp_path):
    """``self.shared()`` named ``Child.shared``, which does not exist.

    ``Base`` is the only ancestor and declares ``shared``, so the call is
    ``Base.shared`` (see ``tests/test_inherited_calls.py``), never
    ``Other.shared``.
    """
    store = _build(tmp_path, {
        "base.py": "class Base:\n    def shared(self):\n        return 1\n",
        "other.py": "class Other:\n    def shared(self):\n        return 2\n",
        "child.py": (
            "from base import Base\n\n\n"
            "class Child(Base):\n"
            "    def run(self):\n"
            "        return self.shared()\n"
        ),
    })
    root = tmp_path.resolve()
    try:
        row = next(row for row in _edges(store) if row["source"].endswith("Child.run"))
    finally:
        store.close()

    assert row["target"] == f"{root}/base.py::Base.shared"
    assert row["extra"]["inherited_endpoint"] == f"{root}/child.py::Child.shared"
    assert row["tier"] == "INFERRED"
    result = query_graph("callers_of", f"{root}/other.py::Other.shared", str(root))
    assert result["results"] == []


def _claimed_repo(tmp_path: Path) -> tuple[Path, str, str]:
    """A call that named ``Helper.ping`` in the wrong file, as a re-export does."""
    root, store = _store(tmp_path)
    try:
        caller = _node(store, root / "a.py", "caller")
        right = _node(store, root / "helper.py", "ping", parent="Helper")
        wrong = _node(store, root / "pool.py", "ping", parent="Pool")
        store.upsert_edge(EdgeInfo(
            kind="CALLS", source=caller, target=f"{root}/lib.py::Helper.ping",
            file_path=str(root / "a.py"), line=3,
        ))
        store.commit()
        store.refresh_target_resolution()
        assert _row(store)["target"] == "ping"
    finally:
        store.close()
    return root, right, wrong


def test_a_guess_keeps_to_the_class_the_call_named(tmp_path):
    root, right, wrong = _claimed_repo(tmp_path)

    named = query_graph("callers_of", right, str(root))
    other = query_graph("callers_of", wrong, str(root))

    assert [row["name"] for row in named["results"]] == ["caller"]
    assert named["results"][0]["target_resolution"] == "unresolved"
    assert "1 of 1" in named["confidence"]
    assert other["results"] == []


def test_settling_restores_a_target_once_its_node_exists(tmp_path):
    root, store = _store(tmp_path)
    source = root / "a.py"
    other = root / "b.py"
    try:
        caller = _node(store, source, "caller")
        claimed = f"{other}::Helper.ping"
        original_extra = {"receiver": "h", "receiver_type": "Helper"}
        store.upsert_edge(EdgeInfo(
            kind="CALLS", source=caller, target=claimed, file_path=str(source), line=3,
            extra=original_extra,
        ))
        original_text = store._conn.execute("SELECT extra FROM edges").fetchone()[0]
        store.commit()

        store.refresh_target_resolution()
        settled = _row(store)
        assert settled["target"] == "ping"
        assert settled["extra"]["claimed_endpoint"] == claimed
        assert settled["tier"] == "UNRESOLVED"

        # A second pass moves nothing.
        store.refresh_target_resolution()
        assert _row(store) == settled

        _node(store, other, "ping", parent="Helper")
        store.commit()
        store.refresh_target_resolution()
        restored = _row(store)
    finally:
        store.close()

    assert restored["target"] == claimed
    assert restored["extra"] == original_extra
    assert restored["tier"] == "EXTRACTED"
    assert restored["confidence"] == 1.0
    assert restored["resolution"] == "direct"
    store = GraphStore(get_db_path(root))
    try:
        # Byte for byte what a fresh parse stores, so full and incremental agree.
        assert store._conn.execute("SELECT extra FROM edges").fetchone()[0] == original_text
    finally:
        store.close()


def test_settling_demotes_a_target_whose_node_went_away(tmp_path):
    root, store = _store(tmp_path)
    source = root / "a.py"
    other = root / "b.py"
    try:
        caller = _node(store, source, "caller")
        real = _node(store, other, "ping")
        store.upsert_edge(EdgeInfo(
            kind="CALLS", source=caller, target=real, file_path=str(source), line=3,
        ))
        store.commit()
        store.refresh_target_resolution()
        assert _row(store)["tier"] == "EXTRACTED"

        store.remove_file_data(str(other))
        store.commit()
        store.refresh_target_resolution()
        row = _row(store)
    finally:
        store.close()

    assert row["target"] == "ping"
    assert row["extra"]["claimed_endpoint"] == real
    assert row["tier"] == "UNRESOLVED"


def test_scope_names_that_are_not_paths_are_left_alone(tmp_path):
    """``Class::method`` is what the scoped resolvers read, not an invention."""
    root, store = _store(tmp_path)
    source = root / "a.py"
    try:
        caller = _node(store, source, "caller")
        store.upsert_edge(EdgeInfo(
            kind="CALLS", source=caller, target="Redis::get", file_path=str(source), line=3,
        ))
        store.commit()
        store.refresh_target_resolution()
        row = _row(store)
    finally:
        store.close()

    assert row["target"] == "Redis::get"
    assert "claimed_endpoint" not in row["extra"]
    assert row["tier"] == "UNRESOLVED"


@pytest.mark.parametrize(
    ("extra", "tier"),
    [
        ({}, "UNRESOLVED"),
        ({"unresolved_targets": ["a.py::x"], "unresolved_target_count": 1}, "UNRESOLVED"),
        ({"ambiguous_targets": ["a.py::x", "b.py::x"], "ambiguous_target_count": 2}, "AMBIGUOUS"),
    ],
)
def test_unbound_edges_do_not_claim_full_confidence(tmp_path, extra, tier):
    root, store = _store(tmp_path)
    source = root / "a.py"
    try:
        caller = _node(store, source, "caller")
        store.upsert_edge(EdgeInfo(
            kind="CALLS", source=caller, target="process", file_path=str(source), line=3,
            extra=extra,
        ))
        store.commit()
        store.refresh_target_resolution()
        row = _row(store)
    finally:
        store.close()

    assert row["target"] == "process"
    assert row["tier"] == tier
    assert 0 < row["confidence"] < 1.0


def test_ambiguous_scores_above_unresolved(tmp_path):
    root, store = _store(tmp_path)
    source = root / "a.py"
    try:
        caller = _node(store, source, "caller")
        store.upsert_edge(EdgeInfo(
            kind="CALLS", source=caller, target="one", file_path=str(source), line=3,
        ))
        store.upsert_edge(EdgeInfo(
            kind="CALLS", source=caller, target="two", file_path=str(source), line=4,
            extra={"ambiguous_targets": ["a.py::two", "b.py::two"]},
        ))
        store.commit()
        store.refresh_target_resolution()
        rows = _edges(store)
    finally:
        store.close()

    assert rows[0]["confidence"] < rows[1]["confidence"]


def test_bare_call_bound_by_evidence_is_inferred_not_extracted(tmp_path):
    root, store = _store(tmp_path)
    source = root / "a.py"
    try:
        caller = _node(store, source, "caller")
        helper = _node(store, source, "helper")
        store.upsert_edge(EdgeInfo(
            kind="CALLS", source=caller, target="helper", file_path=str(source), line=3,
        ))
        store.commit()
        store.resolve_bare_call_targets()
        store.refresh_target_resolution()
        row = _row(store)
    finally:
        store.close()

    assert row["target"] == helper
    assert row["extra"]["bare_call_target"] == "helper"
    assert row["tier"] == "INFERRED"
    assert 0 < row["confidence"] < 1.0


def test_resolver_scores_are_kept(tmp_path):
    """AOP and event edges carry their own score; the refresh must not flatten it."""
    root, store = _store(tmp_path)
    source = root / "a.py"
    try:
        caller = _node(store, source, "caller")
        target = _node(store, source, "advised")
        store.upsert_edge(EdgeInfo(
            kind="CALLS", source=caller, target=target, file_path=str(source), line=3,
            extra={"confidence": 0.7, "confidence_tier": "INFERRED"},
        ))
        store.commit()
        store.refresh_target_resolution()
        row = _row(store)
    finally:
        store.close()

    assert row["tier"] == "INFERRED"
    assert row["confidence"] == 0.7


def test_a_bound_edge_gets_full_confidence_back(tmp_path):
    root, store = _store(tmp_path)
    source = root / "a.py"
    try:
        caller = _node(store, source, "caller")
        store.upsert_edge(EdgeInfo(
            kind="CALLS", source=caller, target="later", file_path=str(source), line=3,
        ))
        store.commit()
        store.refresh_target_resolution()
        assert _row(store)["tier"] == "UNRESOLVED"

        real = _node(store, source, "later")
        store._conn.execute("UPDATE edges SET target_qualified = ?", (real,))
        store.commit()
        store.refresh_target_resolution()
        row = _row(store)
    finally:
        store.close()

    assert row["tier"] == "EXTRACTED"
    assert row["confidence"] == 1.0


def _guess_repo(tmp_path: Path) -> tuple[Path, str]:
    root, store = _store(tmp_path)
    source = root / "a.py"
    try:
        target = _node(store, source, "target")
        certain = _node(store, source, "certain")
        guess = _node(store, root / "b.py", "guess")
        store.upsert_edge(EdgeInfo(
            kind="CALLS", source=certain, target=target, file_path=str(source), line=3,
        ))
        store.upsert_edge(EdgeInfo(
            kind="CALLS", source=guess, target="target", file_path=str(root / "b.py"), line=4,
        ))
        store.commit()
        store.refresh_target_resolution()
    finally:
        store.close()
    return root, target


@pytest.mark.parametrize("detail", ["minimal", "standard"])
def test_answer_with_guesses_says_so(tmp_path, detail):
    root, target = _guess_repo(tmp_path)

    result = query_graph("callers_of", target, str(root), detail_level=detail)

    assert result["resolution_split"] == {"direct": 1, "unresolved": 1}
    assert "1 of 2" in result["confidence"]
    assert "direct" in result["confidence"]
    assert len(result["confidence"]) <= MAX_CONFIDENCE_CHARS


def test_answer_filtered_to_certain_calls_has_no_note(tmp_path):
    root, target = _guess_repo(tmp_path)

    result = query_graph("callers_of", target, str(root), resolution="direct")

    assert result["resolution_split"] == {"direct": 1, "unresolved": 0}
    assert "confidence" not in result


def test_answer_filtered_to_guesses_has_no_note(tmp_path):
    """The caller asked for exactly these, so telling them is noise."""
    root, target = _guess_repo(tmp_path)

    result = query_graph("callers_of", target, str(root), resolution="unresolved")

    assert result["resolution_split"] == {"direct": 0, "unresolved": 1}
    assert "confidence" not in result


def test_certain_answer_has_no_note(tmp_path):
    root, store = _store(tmp_path)
    source = root / "a.py"
    try:
        target = _node(store, source, "target")
        certain = _node(store, source, "certain")
        store.upsert_edge(EdgeInfo(
            kind="CALLS", source=certain, target=target, file_path=str(source), line=3,
        ))
        store.commit()
        store.refresh_target_resolution()
    finally:
        store.close()

    result = query_graph("callers_of", target, str(root))

    assert result["result_count"] == 1
    assert "confidence" not in result


def test_settled_test_edge_is_not_counted_as_testing_a_same_named_symbol(tmp_path):
    """``tests_for`` rows carry no guess label, so a guess must not reach them."""
    root, store = _store(tmp_path)
    test_file = root / "tests" / "test_a.py"
    other = root / "other.py"
    try:
        real = _node(store, other, "ping", parent="Pool")
        store.upsert_node(NodeInfo(
            kind="Test", name="test_it", file_path=str(test_file), line_start=1, line_end=5,
            language="python", is_test=True,
        ))
        test_qn = f"{test_file}::test_it"
        store.upsert_edge(EdgeInfo(
            kind="IMPORTS_FROM", source=str(test_file), target=str(other),
            file_path=str(test_file), line=1,
        ))
        store.upsert_edge(EdgeInfo(
            kind="TESTED_BY", source=f"{test_file}::Helper.ping", target=test_qn,
            file_path=str(test_file), line=2,
        ))
        store.commit()
        store.refresh_target_resolution()
        row = _row(store, "TESTED_BY")
        assert row["source"] == "ping"
        assert row["extra"]["claimed_endpoint"] == f"{test_file}::Helper.ping"
        assert row["tier"] == "UNRESOLVED"
        store.resolve_bare_tested_by_sources()
        assert _row(store, "TESTED_BY")["source"] == "ping"
    finally:
        store.close()

    result = query_graph("tests_for", real, str(root))

    assert result["results"] == []


def test_sharing_an_external_call_with_a_test_is_not_coverage(tmp_path):
    """``String::new`` holds ``::`` but no node: a test using it tests nothing here."""
    store = _build(tmp_path, {
        "Cargo.toml": CARGO,
        "src/lib.rs": (
            "pub fn untested() -> String {\n"
            "    String::new()\n"
            "}\n"
            "#[test]\n"
            "fn other_thing() { let s = String::new(); assert!(s.is_empty()); }\n"
        ),
    })
    root = tmp_path.resolve()
    store.close()

    result = query_graph("tests_for", f"{root}/src/lib.rs::untested", str(root))

    assert result["results"] == []


def test_class_used_only_through_an_inherited_method_stays_alive(tmp_path):
    """The claimed name was the dead-code check's evidence for the class."""
    store = _build(tmp_path, {
        "base.py": "class Base:\n    @staticmethod\n    def make():\n        return 1\n",
        "util.py": "from base import Base\n\n\nclass Util(Base):\n    pass\n",
        "app.py": "from util import Util\n\n\ndef run():\n    return Util.make()\n",
    })
    try:
        rows = _edges(store)
        assert not any(row["target"].endswith("util.py::Util.make") for row in rows)
        dead = {node["name"] for node in find_dead_code(store)}
    finally:
        store.close()

    assert "Util" not in dead


def test_rename_leaves_a_call_named_on_another_class(tmp_path):
    """``h.build()`` named ``Helper.build``; renaming ``One.build`` must not edit it."""
    store = _build(tmp_path, {
        "One.java": "public class One {\n    public int build() { return 1; }\n}\n",
        "User.java": (
            "class Helper { }\n"
            "class User {\n"
            "    int go() { Helper h = new Helper(); return h.build(); }\n"
            "}\n"
        ),
    })
    root = tmp_path.resolve()
    try:
        settled = [row for row in _edges(store) if row["target"] == "build"]
        assert settled and settled[0]["extra"]["claimed_endpoint"].endswith(
            "User.java::Helper.build",
        )
        preview = rename_preview(store, f"{root}/One.java::One.build", "make")
    finally:
        store.close()

    assert preview is not None
    assert [edit["file"] for edit in preview["edits"]] == [f"{root}/One.java"]


def test_nested_classes_of_the_same_name_are_told_apart(tmp_path):
    """``Alpha.Inner`` and ``Beta.Inner`` share a last name, not a class."""
    store = _build(tmp_path, {
        "a.py": (
            "class Alpha:\n"
            "    class Inner:\n"
            "        def run(self):\n"
            "            return self.shared()\n"
            "\n\n"
            "class Beta:\n"
            "    class Inner:\n"
            "        def shared(self):\n"
            "            return 1\n"
        ),
    })
    root = tmp_path.resolve()
    try:
        row = next(row for row in _edges(store) if row["source"].endswith("Inner.run"))
        assert row["extra"]["claimed_endpoint"] == f"{root}/a.py::Alpha.Inner.shared"
    finally:
        store.close()

    result = query_graph("callers_of", f"{root}/a.py::Beta.Inner.shared", str(root))

    assert result["results"] == []


def test_a_claim_on_the_last_scope_name_still_matches_the_full_one(tmp_path):
    """A claim that knew only ``Inner`` is no evidence against ``Outer.Inner``."""
    root, store = _store(tmp_path)
    try:
        caller = _node(store, root / "a.py", "caller")
        real = _node(store, root / "b.py", "ping", parent="Outer.Inner")
        store.upsert_edge(EdgeInfo(
            kind="CALLS", source=caller, target=f"{root}/a.py::Inner.ping",
            file_path=str(root / "a.py"), line=3,
        ))
        store.commit()
        store.refresh_target_resolution()
    finally:
        store.close()

    result = query_graph("callers_of", real, str(root))

    assert [row["name"] for row in result["results"]] == ["caller"]


def test_cpp_scoped_call_bound_across_files_is_inferred(tmp_path):
    store = _build(tmp_path, {
        "callee.cpp": "void A::unique() {}\n",
        "caller.cpp": "void test_run() { A::unique(); }\n",
    })
    try:
        call = next(row for row in _edges(store) if row["target"].endswith("A.unique()"))
        mirror = next(
            row for row in _edges(store, "TESTED_BY") if row["source"].endswith("A.unique()")
        )
    finally:
        store.close()

    assert call["extra"]["cpp_scoped_target"] == "A::unique"
    for row in (call, mirror):
        assert row["tier"] == "INFERRED"
        assert 0 < row["confidence"] < 1.0


def test_rescript_call_bound_through_open_is_inferred(tmp_path):
    store = _build(tmp_path, {
        "CurrencyFormatUtils.res": "let format = (x) => x\n",
        "Caller.res": "open CurrencyFormatUtils\nlet run = () => format(12.0)\n",
    })
    try:
        row = next(
            row for row in _edges(store)
            if row["target"].endswith("CurrencyFormatUtils.res::format")
        )
    finally:
        store.close()

    assert row["tier"] == "INFERRED"
    assert 0 < row["confidence"] < 1.0


def test_terraform_reference_binds_when_its_declaration_arrives_later(tmp_path):
    """An update that adds ``variable "region"`` must link what a rebuild links."""
    root = tmp_path.resolve()
    (root / "outputs.tf").write_text(
        'output "where" {\n  value = var.region\n}\n', encoding="utf-8",
    )
    store = GraphStore(root / "graph.db")
    try:
        full_build(root, store)
        (root / "variables.tf").write_text('variable "region" {}\n', encoding="utf-8")
        incremental_update(root, store, changed_files=["variables.tf"])
        updated = _edges(store, "REFERENCES")
        full_build(root, store)
        rebuilt = _edges(store, "REFERENCES")
    finally:
        store.close()

    assert [row["target"] for row in rebuilt] == [f"{root}/variables.tf::var.region"]
    assert [(row["target"], row["extra"]) for row in updated] == [
        (row["target"], row["extra"]) for row in rebuilt
    ]


def test_rust_inline_module_call_needs_the_item_in_that_module(tmp_path):
    """``inner::bar()`` re-exported from elsewhere is not this file's own ``bar``."""
    store = _build(tmp_path, {
        "Cargo.toml": CARGO,
        "src/lib.rs": "pub mod actual;\npub mod helper;\n",
        "src/actual.rs": "pub fn bar() -> usize { 2 }\n",
        "src/helper.rs": (
            "mod inner {\n"
            "    pub use crate::actual::bar;\n"
            "}\n"
            "pub fn bar() -> usize { 1 }\n"
            "pub fn run() -> usize { inner::bar() }\n"
        ),
    })
    root = tmp_path.resolve()
    try:
        targets = [row["target"] for row in _edges(store) if row["source"].endswith("::run")]
    finally:
        store.close()

    assert f"{root}/src/helper.rs::bar" not in targets
