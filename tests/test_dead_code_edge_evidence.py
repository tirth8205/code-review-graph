"""Dead-code liveness read from edge evidence collected once per analysis.

PR #1082 (issue #982) replaced the per-candidate edge queries in
``find_dead_code`` (exact-target lookups, ``LIKE '%::name'`` suffix scans and
the ``LIKE '%Name.%'`` member-call scan) with maps built from one pass over
the edges table. These tests pin the evidence rules those queries encoded so
the maps keep answering the same questions: class-qualified callers and test
edges, bare TESTED_BY sources, importers, overrides, language filtering of
bare calls whose source node is missing, ASCII case-insensitive matching (as
SQLite ``LIKE`` did), literal matching of ``_`` in names, dotted class names
(Elixir modules), unicode and spaces in paths, and repeated or updated runs.
"""

from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.parser import CodeParser, EdgeInfo, NodeInfo
from code_review_graph.refactor import find_dead_code, suggest_refactorings


def _node(store, name, kind="Function", file="/repo/app.py", language="python", **extra):
    return store.upsert_node(NodeInfo(
        kind=kind, name=name, file_path=file, line_start=1, line_end=2,
        language=language, **extra,
    ))


def _edge(store, kind, source, target, file):
    store.upsert_edge(EdgeInfo(kind=kind, source=source, target=target, file_path=file, line=1))


def _dead_names(store, **kwargs):
    return {d["name"] for d in find_dead_code(store, **kwargs)}


def _edge_queries(store, **kwargs):
    statements = []
    store._conn.set_trace_callback(statements.append)
    try:
        find_dead_code(store, **kwargs)
    finally:
        store._conn.set_trace_callback(None)
    return [s for s in statements if "FROM EDGES" in s.upper()]


# --- matching rules carried over from the SQL lookups -----------------------


def test_suffix_call_matches_name_case_insensitively(tmp_path):
    # SQLite LIKE is case-insensitive for ASCII: "%::helper" matched "pkg::Helper".
    with GraphStore(tmp_path / "g.db") as store:
        _node(store, "helper")
        _edge(store, "CALLS", "/repo/app.py::caller", "pkg::Helper", "/repo/app.py")
        store.commit()
        assert "helper" not in _dead_names(store)


def test_member_call_matches_class_name_case_insensitively(tmp_path):
    with GraphStore(tmp_path / "g.db") as store:
        _node(store, "Widget", kind="Class")
        _edge(store, "CALLS", "/repo/app.py::caller", "widget.run", "/repo/app.py")
        store.commit()
        assert "Widget" not in _dead_names(store)


def test_underscore_in_function_name_is_literal_for_suffix_calls(tmp_path):
    # "%::my_func" also matched "pkg::myXfunc" because "_" is a LIKE wildcard.
    with GraphStore(tmp_path / "g.db") as store:
        _node(store, "my_func")
        _edge(store, "CALLS", "/repo/app.py::caller", "pkg::myXfunc", "/repo/app.py")
        store.commit()
        assert "my_func" in _dead_names(store)


def test_underscore_in_class_name_is_literal_for_member_calls(tmp_path):
    with GraphStore(tmp_path / "g.db") as store:
        _node(store, "My_Widget", kind="Class")
        _edge(store, "CALLS", "/repo/app.py::caller", "MyXWidget.run", "/repo/app.py")
        store.commit()
        assert "My_Widget" in _dead_names(store)


def test_dotted_class_name_member_call_keeps_class_alive(tmp_path):
    # Elixir modules are stored as Class nodes named "MyApp.Accounts". A call
    # into the module ("...::MyApp.Accounts.normalize") is member-call
    # evidence for the module, as the "%MyApp.Accounts.%" scan reported.
    with GraphStore(tmp_path / "g.db") as store:
        _node(store, "MyApp.Accounts", kind="Class", file="/repo/lib/accounts.ex",
              language="elixir")
        _edge(store, "CALLS", "/repo/lib/accounts.ex::MyApp.Accounts.get_user",
              "/repo/lib/accounts.ex::MyApp.Accounts.normalize", "/repo/lib/accounts.ex")
        store.commit()
        assert "MyApp.Accounts" not in _dead_names(store, kind="Class")


def test_parsed_elixir_modules_with_internal_calls_are_not_dead(tmp_path):
    # Same rule through the real parser: a dotted and an undotted module, each
    # with one function calling another inside it.
    parser = CodeParser()
    sources = {
        "accounts.ex": (
            "defmodule MyApp.Accounts do\n"
            "  def get_user(id) do\n    normalize(id)\n  end\n\n"
            "  defp normalize(id) do\n    id\n  end\nend\n"
        ),
        "calc.ex": (
            "defmodule Calculator do\n"
            "  def add(a, b) do\n    twice(a) + b\n  end\n\n"
            "  defp twice(a) do\n    a * 2\n  end\nend\n"
        ),
    }
    with GraphStore(tmp_path / "g.db") as store:
        for name, text in sources.items():
            path = tmp_path / name
            path.write_text(text, encoding="utf-8")
            nodes, edges = parser.parse_file(path)
            store.store_file_nodes_edges(str(path), nodes, edges, "")
        store.commit()
        dead_classes = _dead_names(store, kind="Class")
        assert "Calculator" not in dead_classes
        assert "MyApp.Accounts" not in dead_classes


# --- evidence sources the per-candidate queries consulted -------------------


def test_class_qualified_call_keeps_method_alive(tmp_path):
    with GraphStore(tmp_path / "g.db") as store:
        _node(store, "Widget", kind="Class")
        _node(store, "spin", parent_name="Widget")
        _edge(store, "CALLS", "/repo/other.py::caller", "Widget::spin", "/repo/other.py")
        store.commit()
        assert "spin" not in _dead_names(store, kind="Function")


def test_class_qualified_tested_by_keeps_method_alive(tmp_path):
    with GraphStore(tmp_path / "g.db") as store:
        _node(store, "Widget", kind="Class")
        _node(store, "spin", parent_name="Widget")
        _edge(store, "TESTED_BY", "Widget::spin", "/repo/tests/test_w.py::test_spin",
              "/repo/tests/test_w.py")
        store.commit()
        assert "spin" not in _dead_names(store, kind="Function")


def test_bare_tested_by_source_respects_import_ambiguity(tmp_path):
    with GraphStore(tmp_path / "g.db") as store:
        _node(store, "helper", file="/repo/one.py")
        _node(store, "helper", file="/repo/two.py")
        _edge(store, "TESTED_BY", "helper", "/repo/check.py::check_helper", "/repo/check.py")
        _edge(store, "IMPORTS_FROM", "/repo/check.py", "/repo/one.py", "/repo/check.py")
        store.commit()
        dead = find_dead_code(store)
        assert {d["file_path"] for d in dead if d["name"] == "helper"} == {"/repo/two.py"}


def test_reversed_tested_by_edge_is_not_test_evidence(tmp_path):
    # Only the outgoing direction (production -> test) counts; see #515.
    with GraphStore(tmp_path / "g.db") as store:
        _node(store, "helper")
        _edge(store, "TESTED_BY", "/repo/tests/test_a.py::test_helper", "/repo/app.py::helper",
              "/repo/tests/test_a.py")
        store.commit()
        assert "helper" in _dead_names(store)


def test_direct_importer_keeps_symbol_alive(tmp_path):
    with GraphStore(tmp_path / "g.db") as store:
        _node(store, "helper")
        _edge(store, "IMPORTS_FROM", "/repo/use.py", "/repo/app.py::helper", "/repo/use.py")
        store.commit()
        assert "helper" not in _dead_names(store)


def test_override_of_abstract_base_method_is_skipped(tmp_path):
    with GraphStore(tmp_path / "g.db") as store:
        _node(store, "Base", kind="Class")
        _node(store, "run", parent_name="Base", extra={"decorators": ["abstractmethod"]})
        _node(store, "Child", kind="Class")
        _node(store, "run", parent_name="Child")
        _edge(store, "INHERITS", "/repo/app.py::Child", "Base", "/repo/app.py")
        store.commit()
        dead = find_dead_code(store, kind="Function")
        assert "/repo/app.py::Child.run" not in {d["qualified_name"] for d in dead}


def test_override_of_called_base_method_is_alive(tmp_path):
    with GraphStore(tmp_path / "g.db") as store:
        _node(store, "Base", kind="Class")
        _node(store, "spin", parent_name="Base")
        _node(store, "Child", kind="Class", file="/repo/child.py")
        _node(store, "spin", parent_name="Child", file="/repo/child.py")
        _edge(store, "INHERITS", "/repo/child.py::Child", "/repo/app.py::Base", "/repo/child.py")
        _edge(store, "CALLS", "/repo/use.py::caller", "/repo/app.py::Base.spin", "/repo/use.py")
        store.commit()
        dead = {d["qualified_name"] for d in find_dead_code(store, kind="Function")}
        assert "/repo/app.py::Base.spin" not in dead
        assert "/repo/child.py::Child.spin" not in dead


def test_bare_call_from_unindexed_source_follows_language_filter(tmp_path):
    # The caller's language comes from its node. With no node, a typed
    # candidate cannot be matched by language (as the inner join did), while a
    # candidate without a language accepts any bare caller.
    with GraphStore(tmp_path / "g.db") as store:
        _node(store, "typed_helper")
        _node(store, "untyped_helper", file="/repo/raw.txt", language="")
        _edge(store, "CALLS", "/repo/ghost.py::caller", "typed_helper", "/repo/ghost.py")
        _edge(store, "CALLS", "/repo/ghost.py::caller", "untyped_helper", "/repo/ghost.py")
        store.commit()
        dead = _dead_names(store)
        assert "typed_helper" in dead
        assert "untyped_helper" not in dead


# --- paths, names and repeated runs -----------------------------------------


def test_unicode_names_and_spaces_in_paths(tmp_path):
    with GraphStore(tmp_path / "g.db") as store:
        _node(store, "helper", file="/repo/my dir/módulo ü.py")
        _node(store, "helper", file="/repo/other dir/x.py")
        _node(store, "Ölçer", kind="Class", file="/repo/my dir/módulo ü.py")
        _edge(store, "CALLS", "/repo/my dir/use me.py::caller", "pkg::helper",
              "/repo/my dir/use me.py")
        _edge(store, "IMPORTS_FROM", "/repo/my dir/use me.py", "/repo/my dir/módulo ü.py",
              "/repo/my dir/use me.py")
        _edge(store, "CALLS", "/repo/my dir/use me.py::caller", "Ölçer.ölç",
              "/repo/my dir/use me.py")
        store.commit()
        dead = find_dead_code(store)
        assert {(d["name"], d["file_path"]) for d in dead} == {
            ("helper", "/repo/other dir/x.py"),
        }


def test_windows_style_paths_are_matched_after_normalisation(tmp_path):
    with GraphStore(tmp_path / "g.db") as store:
        _node(store, "helper", file="C:\\repo\\one.py")
        _node(store, "helper", file="C:\\repo\\two.py")
        _node(store, "caller", file="C:\\repo\\use.py")
        _edge(store, "CALLS", "C:/repo/use.py::caller", "helper", "C:\\repo\\use.py")
        _edge(store, "IMPORTS_FROM", "C:/repo/use.py", "C:/repo/one.py", "C:\\repo\\use.py")
        store.commit()
        dead = find_dead_code(store, root="C:\\repo")
        assert {d["file_path"] for d in dead if d["name"] == "helper"} == {"C:/repo/two.py"}


def test_empty_graph_has_no_dead_code(tmp_path):
    with GraphStore(tmp_path / "g.db") as store:
        assert find_dead_code(store) == []
        assert suggest_refactorings(store) == []


def test_repeat_runs_agree_and_follow_graph_updates(tmp_path):
    # Evidence is rebuilt per call: nothing is cached between analyses.
    with GraphStore(tmp_path / "g.db") as store:
        _node(store, "helper")
        for line in (1, 2):  # two call sites, same target
            store.upsert_edge(EdgeInfo(
                kind="CALLS", source="/repo/use.py::caller", target="pkg::helper",
                file_path="/repo/use.py", line=line,
            ))
        store.commit()
        first = find_dead_code(store)
        assert first == find_dead_code(store) == []
        store.remove_file_data("/repo/use.py")
        store.commit()
        assert _dead_names(store) == {"helper"}
        assert [s["symbols"] for s in suggest_refactorings(store) if s["type"] == "remove"] == [
            ["/repo/app.py::helper"],
        ]


def test_edge_query_count_is_constant_with_classes_methods_and_overrides(tmp_path):
    def build(count):
        store = GraphStore(tmp_path / f"g-{count}.db")
        _node(store, "Base", kind="Class")
        _node(store, "spin", parent_name="Base")
        for i in range(count):
            file = f"/repo/m{i}.py"
            _node(store, f"Child{i}", kind="Class", file=file)
            _node(store, "spin", parent_name=f"Child{i}", file=file)
            _node(store, f"loose_{i}", file=file)
            _edge(store, "INHERITS", f"{file}::Child{i}", "/repo/app.py::Base", file)
            _edge(store, "TESTED_BY", f"{file}::loose_{i}", f"/repo/tests/t{i}.py::test_x",
                  f"/repo/tests/t{i}.py")
        store.commit()
        return store

    small, large = build(5), build(80)
    try:
        small_q, large_q = _edge_queries(small), _edge_queries(large)
    finally:
        small.close()
        large.close()
    assert len(large_q) == len(small_q)
    assert not any("LIKE" in s.upper() for s in large_q)


def test_parsed_attribute_call_keeps_function_alive(tmp_path):
    # A parsed Python file whose function is only called through a qualified
    # attribute ("mod.helper") from another file stays alive.
    lib = tmp_path / "pkg" / "lib.py"
    use = tmp_path / "pkg" / "use.py"
    lib.parent.mkdir()
    lib.write_text("def helper():\n    return 1\n\n\ndef orphan():\n    return 2\n")
    use.write_text("from pkg import lib\n\n\ndef caller():\n    return lib.helper()\n")
    parser = CodeParser()
    with GraphStore(tmp_path / "g.db") as store:
        for path in (lib, use):
            nodes, edges = parser.parse_file(Path(path))
            store.store_file_nodes_edges(str(path), nodes, edges, "")
        store.commit()
        dead = _dead_names(store, root=tmp_path)
        assert "orphan" in dead
        assert "helper" not in dead
