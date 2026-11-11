"""Graphs built before nested members took the full scope chain upgrade once.

PR #1168 (issue #934) renames members of nested classes from
``Outer.java::Inner.m`` to ``Outer.java::Outer.Inner.m``. A graph built before
that still holds the leaf spelling. Re-parsing an importer whose bytes did not
change keeps its node rows and replaces only its edges, so without an upgrade
those edges would name nodes that do not exist.
"""

import json
from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import (
    NESTED_SCOPE_IDENTITY_VERSION,
    full_build,
    incremental_update,
)

FILES = {
    "util/Svc.java": "package util;\npublic class Svc { public static void run() {} }\n",
    "app/Outer.java": (
        "package app;\n"
        "import util.Svc;\n"
        "public class Outer {\n"
        "    public static class Inner {\n"
        "        public void m() { Svc s = new Svc(); s.run(); helper(); }\n"
        "        void helper() {}\n"
        "    }\n"
        "}\n"
    ),
    "other/Caller.java": (
        "package other;\n"
        "import app.Outer.Inner;\n"
        "public class Caller { void go() { Inner i = new Inner(); i.m(); } }\n"
    ),
}


def _write(root: Path) -> None:
    for name, source in FILES.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")


def _as_released(store: GraphStore) -> None:
    """Rewrite the graph to the leaf-scoped spelling releases before #934 stored."""
    conn = store._conn
    conn.execute(
        "UPDATE nodes SET qualified_name = replace(qualified_name, "
        "'::Outer.Inner.', '::Inner.'), parent_name = 'Inner' "
        "WHERE parent_name = 'Outer.Inner'",
    )
    for column in ("source_qualified", "target_qualified"):
        conn.execute(
            f"UPDATE edges SET {column} = replace({column}, "  # nosec B608
            "'::Outer.Inner.', '::Inner.')",
        )
    conn.execute(
        "DELETE FROM metadata WHERE key IN "
        "('nested_scope_identity_version', 'nested_scope_identity_pending_files')",
    )
    conn.commit()


def _dangling(store: GraphStore, root: Path) -> list[tuple[str, str]]:
    nodes = {row[0] for row in store._conn.execute("SELECT qualified_name FROM nodes")}
    prefix = f"{root.as_posix()}/"
    return sorted(
        (kind, name.replace(prefix, ""))
        for kind, source, target in store._conn.execute(
            "SELECT kind, source_qualified, target_qualified FROM edges",
        )
        for name in (source, target)
        if name.startswith(prefix) and "::" in name and name not in nodes
    )


def test_released_graph_upgrades_nested_scopes_without_dangling_edges(tmp_path):
    _write(tmp_path)
    with GraphStore(tmp_path / ".code-review-graph" / "graph.db") as store:
        full_build(tmp_path, store)
        _as_released(store)
        outer = f"{(tmp_path / 'app/Outer.java').as_posix()}"
        assert store.get_node(f"{outer}::Inner.m") is not None
        assert _dangling(store, tmp_path) == []

        # Editing the callee re-parses Outer.java as an importer with
        # unchanged bytes; its members must not keep the released spelling.
        svc = tmp_path / "util/Svc.java"
        svc.write_text(svc.read_text(encoding="utf-8") + "// edited\n", encoding="utf-8")
        incremental_update(tmp_path, store, changed_files=["util/Svc.java"])

        assert store.get_node(f"{outer}::Outer.Inner.m") is not None
        assert store.get_node(f"{outer}::Inner.m") is None
        assert _dangling(store, tmp_path) == []
        version = store.get_metadata("nested_scope_identity_version")
        assert version == NESTED_SCOPE_IDENTITY_VERSION
        assert json.loads(store.get_metadata("nested_scope_identity_pending_files")) == []

        # The upgrade is one-shot.
        again = incremental_update(tmp_path, store, changed_files=[])
        assert again["files_updated"] == 0


def test_upgrade_with_no_changes_re_resolves_callers_of_renamed_members(tmp_path):
    _write(tmp_path)
    with GraphStore(tmp_path / ".code-review-graph" / "graph.db") as store:
        full_build(tmp_path, store)
        outer = f"{(tmp_path / 'app/Outer.java').as_posix()}"
        caller = f"{(tmp_path / 'other/Caller.java').as_posix()}::Caller.go"
        callers = {
            e.source_qualified for e in store.get_edges_by_target(f"{outer}::Outer.Inner.m")
        }
        assert caller in callers
        _as_released(store)
        assert caller in {
            e.source_qualified for e in store.get_edges_by_target(f"{outer}::Inner.m")
        }
        incremental_update(tmp_path, store, changed_files=[])
        # Caller.java's bytes did not change, but its call into the renamed
        # member must follow the new name rather than dangle.
        assert _dangling(store, tmp_path) == []
        assert caller in {
            e.source_qualified for e in store.get_edges_by_target(f"{outer}::Outer.Inner.m")
        }


def test_graph_without_nested_classes_records_the_version_without_reparsing(tmp_path):
    (tmp_path / "Svc.java").write_text(FILES["util/Svc.java"], encoding="utf-8")
    with GraphStore(tmp_path / ".code-review-graph" / "graph.db") as store:
        full_build(tmp_path, store)
        _as_released(store)
        result = incremental_update(tmp_path, store, changed_files=[])
        assert result["files_updated"] == 0
        version = store.get_metadata("nested_scope_identity_version")
        assert version == NESTED_SCOPE_IDENTITY_VERSION
