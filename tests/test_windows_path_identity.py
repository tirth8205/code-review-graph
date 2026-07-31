"""Regression tests for issue #774: Windows path separators in node identity.

Qualified names and ``file_path`` values are graph identity. They must be
separator-stable across operating systems: a graph built on Windows has to
produce the same identifiers as one built on Linux/macOS, and consumers that
reconstruct identifiers from ``Path`` objects must agree with the parser.

These tests simulate Windows behaviour on POSIX hosts by feeding
``pathlib.PureWindowsPath`` objects (whose ``str()`` uses backslashes) into
code paths that accept ``Path``-like values.
"""

from pathlib import Path, PurePosixPath, PureWindowsPath

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import _reconcile_stale_files
from code_review_graph.parser import CodeParser, EdgeInfo, NodeInfo, normalize_file_path

# ---------------------------------------------------------------------------
# The normalization helper itself
# ---------------------------------------------------------------------------


def test_normalize_file_path_windows_path_object():
    assert normalize_file_path(PureWindowsPath(r"C:\repo\src\app.py")) == "C:/repo/src/app.py"


def test_normalize_file_path_backslash_string():
    assert normalize_file_path("C:\\repo\\src\\app.py") == "C:/repo/src/app.py"


def test_normalize_file_path_posix_inputs_unchanged():
    assert normalize_file_path("/repo/src/app.py") == "/repo/src/app.py"
    assert normalize_file_path(PurePosixPath("/repo/src/app.py")) == "/repo/src/app.py"
    assert normalize_file_path(Path("infra") / "locals.tf") == "infra/locals.tf"


def test_normalize_file_path_relative_windows_path():
    assert normalize_file_path(PureWindowsPath("infra") / "locals.tf") == "infra/locals.tf"


# ---------------------------------------------------------------------------
# Parser identity: qualified names, node file_path, edge endpoints
# ---------------------------------------------------------------------------


def test_julia_identity_uses_forward_slashes_for_windows_paths():
    """The exact failure from issue #774: '\\repo\\case.jl::Demo.greet'."""
    nodes, edges = CodeParser().parse_bytes(
        PureWindowsPath(r"\repo\case.jl"),
        b"module Demo\ngreet() = 1\ndelegate() = greet()\nend\n",
    )

    assert all(n.file_path == "/repo/case.jl" for n in nodes)
    file_node = next(n for n in nodes if n.kind == "File")
    assert file_node.name == "/repo/case.jl"

    calls = [e for e in edges if e.kind == "CALLS"]
    assert any(
        e.source == "/repo/case.jl::Demo.delegate"
        and e.target == "/repo/case.jl::Demo.greet"
        for e in calls
    )
    assert all(e.file_path == "/repo/case.jl" for e in edges)


def test_hcl_references_use_forward_slashes_for_windows_paths():
    """The test_hcl_parser.py failure from issue #774, driven via a Windows path."""
    source = b"""\
variable "items" {}
variable "enabled" {}

locals {
  selected = [
    for item in var.items : item.name
    if var.enabled
  ]
}
"""
    _, edges = CodeParser().parse_bytes(PureWindowsPath("infra") / "locals.tf", source)

    targets = {
        e.target
        for e in edges
        if e.kind == "REFERENCES" and e.source == "infra/locals.tf::local.selected"
    }
    assert targets == {
        "infra/locals.tf::var.items",
        "infra/locals.tf::var.enabled",
    }


def test_python_identity_uses_forward_slashes_for_windows_paths():
    nodes, edges = CodeParser().parse_bytes(
        PureWindowsPath(r"C:\repo\pkg\mod.py"),
        b"class Greeter:\n    def greet(self):\n        return 1\n",
    )

    assert all(n.file_path == "C:/repo/pkg/mod.py" for n in nodes)
    contains = {(e.source, e.target) for e in edges if e.kind == "CONTAINS"}
    assert ("C:/repo/pkg/mod.py::Greeter", "C:/repo/pkg/mod.py::Greeter.greet") in contains


def test_qualify_normalizes_file_path_component():
    parser = CodeParser()
    assert parser._qualify("greet", "\\repo\\case.jl", "Demo") == "/repo/case.jl::Demo.greet"
    assert parser._qualify("greet", "/repo/case.jl", None) == "/repo/case.jl::greet"


def test_php_namespace_backslashes_survive_normalization(tmp_path):
    """Only the path component is normalized; PHP FQN identifiers keep '\\'."""
    php = tmp_path / "service.php"
    php.write_text(
        "<?php\nnamespace App\\Service;\nuse App\\Domain\\Entity\\Job;\n"
        "class Handler { function run() { return new Job(); } }\n",
        encoding="utf-8",
    )
    nodes, edges = CodeParser().parse_file(php)

    # The unresolved import target must keep its namespace backslashes.
    imports = [e for e in edges if e.kind == "IMPORTS_FROM"]
    assert any("App\\Domain\\Entity\\Job" == e.target for e in imports), (
        [e.target for e in imports]
    )
    assert all(e.file_path == php.as_posix() for e in edges)


def test_dataclass_file_path_is_normalized_defensively():
    node = NodeInfo(
        kind="Function",
        name="greet",
        file_path="C:\\repo\\mod.py",
        line_start=1,
        line_end=2,
    )
    assert node.file_path == "C:/repo/mod.py"

    edge = EdgeInfo(
        kind="CALLS",
        source="a",
        target="b",
        file_path="C:\\repo\\mod.py",
    )
    assert edge.file_path == "C:/repo/mod.py"


def test_file_node_name_is_normalized():
    node = NodeInfo(
        kind="File",
        name="C:\\repo\\mod.py",
        file_path="C:\\repo\\mod.py",
        line_start=1,
        line_end=1,
    )
    assert node.name == "C:/repo/mod.py"
    assert node.file_path == "C:/repo/mod.py"


# ---------------------------------------------------------------------------
# GraphStore boundary: file-keyed lookups accept either separator spelling
# ---------------------------------------------------------------------------


def _store_with_windows_file(tmp_path):
    store = GraphStore(tmp_path / "graph.db")
    nodes = [
        NodeInfo(
            kind="File",
            name="C:/repo/src/app.py",
            file_path="C:/repo/src/app.py",
            line_start=1,
            line_end=3,
        ),
        NodeInfo(
            kind="Function",
            name="run",
            file_path="C:/repo/src/app.py",
            line_start=1,
            line_end=3,
        ),
    ]
    store.store_file_nodes_edges("C:\\repo\\src\\app.py", nodes, [], "hash")
    return store


def test_store_and_lookup_bridge_separator_spellings(tmp_path):
    store = _store_with_windows_file(tmp_path)
    try:
        assert store.get_all_files() == ["C:/repo/src/app.py"]
        # Native-Windows spelling of the same file must find the same rows.
        assert len(store.get_nodes_by_file("C:\\repo\\src\\app.py")) == 2
        assert len(store.get_nodes_by_file("C:/repo/src/app.py")) == 2
    finally:
        store.close()


def test_remove_files_permanently_bridges_separator_spellings(tmp_path):
    store = _store_with_windows_file(tmp_path)
    try:
        removed = store.remove_files_permanently(["C:\\repo\\src\\app.py"])
        assert removed == 1
        assert store.get_all_files() == []
    finally:
        store.close()


def test_store_file_batch_normalizes_file_key(tmp_path):
    store = GraphStore(tmp_path / "graph.db")
    try:
        node = NodeInfo(
            kind="File",
            name="C:/repo/src/app.py",
            file_path="C:/repo/src/app.py",
            line_start=1,
            line_end=1,
        )
        store.store_file_nodes_edges("C:/repo/src/app.py", [node], [], "old")
        # Re-storing under the native-Windows spelling must replace, not duplicate.
        store.store_file_batch([("C:\\repo\\src\\app.py", [node], [], "new")])
        assert len(store.get_nodes_by_file("C:/repo/src/app.py")) == 1
    finally:
        store.close()


# ---------------------------------------------------------------------------
# incremental.py reconciliation: native-separator joins must not orphan files
# ---------------------------------------------------------------------------


def test_reconcile_does_not_remove_files_present_under_windows_separators(tmp_path):
    store = GraphStore(tmp_path / "graph.db")
    try:
        node = NodeInfo(
            kind="File",
            name="C:/repo/src/app.py",
            file_path="C:/repo/src/app.py",
            line_start=1,
            line_end=1,
        )
        store.store_file_nodes_edges("C:/repo/src/app.py", [node], [], "h")

        # On Windows, repo_root / rel yields backslashes. The reconciliation
        # must still recognise the stored POSIX identity as present.
        stale = _reconcile_stale_files(
            PureWindowsPath("C:/repo"),
            store,
            current_files=["src/app.py"],
        )
        assert stale == []
        assert store.get_all_files() == ["C:/repo/src/app.py"]
    finally:
        store.close()
