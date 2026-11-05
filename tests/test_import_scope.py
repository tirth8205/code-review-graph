"""Directory-scoped import targets: one edge per import, expanded on read.

A Go import names a PACKAGE -- a directory of files -- and Ruby's
``require_all`` names a directory tree. Resolving either one by emitting an
edge per member file makes the edge count grow with imports times package
size. Measured on kubernetes/kubernetes that was 73,507 edges for a single
imported package, and it also made an incremental update disagree with a
clean rebuild, because an edge's target set then depended on which files
were in the package at the moment the importing file happened to be parsed.

So the parser emits one edge naming the directory, and the read path expands
a directory to its member files:

* ``importers_of(<file>)`` also matches edges targeting the file's package;
* the impact traversal follows a package target at every hop, without
  spending one;
* ``imports_of`` labels the target so nobody mistakes a directory for a file.

These tests pin all three, plus the two properties the shape exists for:
the edge count does not grow with package size, and a build equals an
update.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from code_review_graph.graph import (
    IMPACT_FRONTIER_DIRS_SQL,
    GraphStore,
    _impact_candidate_sql,
    import_scope_ancestors,
)
from code_review_graph.incremental import full_build, incremental_update
from code_review_graph.tools.query import get_impact_radius, query_graph


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _git(repo: Path, *args: str) -> None:
    subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    )


def _init_repo(repo: Path) -> None:
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "initial")


def _go_repo(root: Path, members: int = 4, importers: int = 3) -> Path:
    """A Go module whose ``pkg/core`` package has *members* files."""
    _write(root, "go.mod", "module example.com/app\n\ngo 1.22\n")
    for i in range(members):
        _write(root, f"pkg/core/core{i}.go", f"package core\n\nfunc Core{i}() {{}}\n")
    for i in range(importers):
        _write(
            root, f"cmd/app{i}/main.go",
            "package main\n\n"
            'import "example.com/app/pkg/core"\n\n'
            f"func main{i}() {{ core.Core0() }}\n",
        )
    return root


def _build(repo: Path) -> GraphStore:
    store = GraphStore(repo / ".code-review-graph" / "graph.db")
    full_build(repo, store)
    store.commit()
    return store


def _import_edges(store: GraphStore) -> list:
    return [
        e for e in store.get_all_edges()
        if e.kind == "IMPORTS_FROM"
    ]


class TestPackageEdgeShape:
    def test_one_edge_per_import_not_per_package_file(self, tmp_path):
        """The whole point: edge count tracks imports, not package size.

        Three importers of a four-file package is three edges. Fanning out
        would be twelve, and on kubernetes' busiest package it was 73,507.
        """
        repo = _go_repo(tmp_path, members=4, importers=3)
        _init_repo(repo)
        store = _build(repo)
        try:
            in_repo = [
                e for e in _import_edges(store)
                if e.target_qualified.startswith(str(repo))
            ]
            assert len(in_repo) == 3
            assert {e.target_qualified for e in in_repo} == {
                str(repo / "pkg" / "core"),
            }
        finally:
            store.close()

    def test_edge_count_is_flat_in_package_size(self, tmp_path):
        """Growing the package from 2 to 20 files adds no import edges."""
        counts = []
        for index, members in enumerate((2, 20)):
            repo = _go_repo(tmp_path / f"r{index}", members=members, importers=3)
            _init_repo(repo)
            store = _build(repo)
            try:
                counts.append(len([
                    e for e in _import_edges(store)
                    if e.target_qualified.startswith(str(repo))
                ]))
            finally:
                store.close()
        assert counts[0] == counts[1] == 3


class TestReadPathExpansion:
    def test_importers_of_answers_for_every_file_in_the_package(self, tmp_path):
        """The original win, kept: every member file has importers.

        Before the resolver, Go answered 0 here. Under a fan-out it answered
        only for the files that happened to be in the package when the
        importer was parsed.
        """
        repo = _go_repo(tmp_path, members=4, importers=3)
        _init_repo(repo)
        store = _build(repo)
        store.close()
        for i in range(4):
            member = repo / "pkg" / "core" / f"core{i}.go"
            result = query_graph("importers_of", str(member), repo_root=str(repo))
            files = {row["file"] for row in result["results"]}
            assert files == {
                str(repo / "cmd" / f"app{j}" / "main.go") for j in range(3)
            }, member
            assert all(
                row["via_package"] == str(repo / "pkg" / "core")
                for row in result["results"]
            )

    def test_imports_of_says_the_target_is_a_package(self, tmp_path):
        repo = _go_repo(tmp_path, members=2, importers=1)
        _init_repo(repo)
        store = _build(repo)
        store.close()
        result = query_graph(
            "imports_of", str(repo / "cmd" / "app0" / "main.go"),
            repo_root=str(repo),
        )
        rows = [
            row for row in result["results"]
            if row.get("import_target_kind")
        ]
        assert [row["import_target"] for row in rows] == [
            str(repo / "pkg" / "core"),
        ]
        assert rows[0]["import_target_kind"] == "package"

    def test_impact_radius_reaches_importers_of_a_changed_member(self, tmp_path):
        repo = _go_repo(tmp_path, members=4, importers=3)
        _init_repo(repo)
        store = _build(repo)
        try:
            impact = store.get_impact_radius(
                [str(repo / "pkg" / "core" / "core3.go")],
            )
            assert set(impact["impacted_files"]) == {
                str(repo / "cmd" / f"app{j}" / "main.go") for j in range(3)
            }
        finally:
            store.close()

    def test_both_traversal_engines_agree(self, tmp_path):
        repo = _go_repo(tmp_path, members=3, importers=2)
        _init_repo(repo)
        store = _build(repo)
        try:
            changed = [str(repo / "pkg" / "core" / "core1.go")]
            sql = store.get_impact_radius_sql(changed)
            nx = store._get_impact_radius_networkx(changed)
            assert set(sql["impacted_files"]) == set(nx["impacted_files"])
            assert sql["impact_scores"] == nx["impact_scores"]
        finally:
            store.close()

    def test_a_package_target_does_not_reach_a_subdirectory(self, tmp_path):
        """A Go package is exactly one directory; a subdirectory is another.

        Expanding a package target to everything below it would report the
        importers of ``pkg/core`` as impacted by a change to
        ``pkg/core/inner``, which is a different package entirely.
        """
        repo = _go_repo(tmp_path, members=2, importers=2)
        _write(
            repo, "pkg/core/inner/inner.go",
            "package inner\n\nfunc Inner() {}\n",
        )
        _init_repo(repo)
        store = _build(repo)
        try:
            impact = store.get_impact_radius(
                [str(repo / "pkg" / "core" / "inner" / "inner.go")],
            )
            assert impact["impacted_files"] == []
        finally:
            store.close()

    def test_the_connecting_edge_is_in_the_answer(self, tmp_path):
        """A directory target matches neither endpoint set, so it needs
        fetching explicitly or the radius rests on an invisible edge."""
        repo = _go_repo(tmp_path, members=2, importers=1)
        _init_repo(repo)
        store = _build(repo)
        store.close()
        result = get_impact_radius(
            changed_files=["pkg/core/core0.go"], repo_root=str(repo),
        )
        targets = {
            e["target"] for e in result["edges"] if e["kind"] == "IMPORTS_FROM"
        }
        assert str(repo / "pkg" / "core") in targets


class TestRubyTreeScope:
    def test_require_all_importers_reach_a_nested_file(self, tmp_path):
        """``require_all`` loads a TREE, so a file two levels down counts."""
        _write(
            tmp_path, "thing.gemspec",
            'Gem::Specification.new do |s|\n'
            '  s.name = "thing"\n'
            '  s.require_paths = ["lib"]\n'
            'end\n',
        )
        _write(tmp_path, "lib/thing/commands/build.rb", "module Build; end\n")
        _write(
            tmp_path, "lib/thing/commands/serve/servlet.rb",
            "module Servlet; end\n",
        )
        _write(tmp_path, "lib/thing.rb", 'require_all "thing/commands"\n')
        _init_repo(tmp_path)
        store = _build(tmp_path)
        try:
            nested = tmp_path / "lib" / "thing" / "commands" / "serve" / "servlet.rb"
            result = query_graph(
                "importers_of", str(nested), repo_root=str(tmp_path),
            )
            assert {row["file"] for row in result["results"]} == {
                str(tmp_path / "lib" / "thing.rb"),
            }
            impact = store.get_impact_radius([str(nested)])
            assert str(tmp_path / "lib" / "thing.rb") in impact["impacted_files"]
        finally:
            store.close()


class TestBuildEqualsUpdate:
    def test_adding_a_file_to_a_package_changes_no_import_edge(self, tmp_path):
        """The defect a fan-out cannot avoid, and this shape cannot have.

        A new file in ``pkg/core`` belongs to the package every importer
        already imports. Under a fan-out the rebuild gains one edge per
        importer and the incremental update gains none, because no importer
        is re-parsed. Naming the directory means there is nothing to gain.
        """
        repo = _go_repo(tmp_path / "repo", members=3, importers=4)
        _init_repo(repo)

        incremental_db = repo / ".code-review-graph" / "graph.db"
        store = GraphStore(incremental_db)
        full_build(repo, store)
        store.commit()
        base = subprocess.run(  # noqa: S603
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        before = sorted(
            (e.source_qualified, e.target_qualified)
            for e in _import_edges(store)
        )

        _write(repo, "pkg/core/added.go", "package core\n\nfunc Added() {}\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "add a package member")
        incremental_update(repo, store, base=base)
        store.commit()
        after_incremental = sorted(
            (e.source_qualified, e.target_qualified)
            for e in _import_edges(store)
        )
        store.close()

        rebuilt = GraphStore(tmp_path / "rebuild.db")
        try:
            full_build(repo, rebuilt)
            rebuilt.commit()
            after_rebuild = sorted(
                (e.source_qualified, e.target_qualified)
                for e in _import_edges(rebuilt)
            )
        finally:
            rebuilt.close()

        assert after_incremental == after_rebuild
        # And the new file changed nothing about anyone else's imports.
        assert after_incremental == before


class TestUnindexedTargets:
    def test_no_import_edge_names_a_path_that_is_not_a_node(self, tmp_path):
        """``vendor/`` is a default ignore, so it must never be a target."""
        repo = _go_repo(tmp_path, members=2, importers=1)
        _write(
            repo, "vendor/example.com/dep/dep.go",
            "package dep\n\nfunc Dep() {}\n",
        )
        _write(
            repo, "cmd/app0/main.go",
            "package main\n\n"
            "import (\n"
            '\t"example.com/app/pkg/core"\n'
            '\t"example.com/dep"\n'
            ")\n\n"
            "func main0() { core.Core0(); dep.Dep() }\n",
        )
        _init_repo(repo)
        store = _build(repo)
        try:
            known = {n.qualified_name for n in store.get_all_nodes(exclude_files=False)}
            known |= {
                str(repo / "pkg" / "core"),
            }
            for edge in _import_edges(store):
                target = edge.target_qualified
                if not target.startswith(str(repo)):
                    continue  # a bare module string, visibly external
                assert target in known, target
            assert "example.com/dep" in {
                e.target_qualified for e in _import_edges(store)
            }
        finally:
            store.close()


class TestScopeAncestors:
    @pytest.mark.parametrize("name", ["", "noslash", "ACME.Core", "fmt"])
    def test_a_name_that_is_not_a_path_has_no_ancestors(self, name):
        """A C# namespace bridge and a bare module string are targets too.

        Neither may ever join a directory expansion by accident.
        """
        assert import_scope_ancestors(name) == []

    def test_own_directory_serves_both_scopes(self):
        entries = import_scope_ancestors("/repo/pkg/core/core.go")
        assert entries[0] == ("/repo/pkg/core", ("package", "tree"))
        assert all(scopes == ("tree",) for _dir, scopes in entries[1:])


class TestTraversalQueryPlan:
    """The directory branch must be an index seek, not a rescan.

    Left to itself SQLite drove this branch from the covering index on
    ``kind`` alone and rescanned every IMPORTS_FROM row once per frontier
    directory: 18 seconds of a 19-second kubernetes traversal, against 1.5
    with the plan pinned by CROSS JOIN and INDEXED BY. A token budget cannot
    catch that, and neither can a small fixture -- only the plan can.
    """

    def _plan(self, store: GraphStore, sql: str) -> list[str]:
        conn = store._conn
        conn.execute(
            "CREATE TEMP TABLE IF NOT EXISTS _impact_frontier "
            "(node_qn TEXT PRIMARY KEY, score REAL NOT NULL)"
        )
        conn.execute(
            "CREATE TEMP TABLE IF NOT EXISTS _impact_next "
            "(node_qn TEXT PRIMARY KEY, score REAL NOT NULL)"
        )
        conn.execute(
            "CREATE TEMP TABLE IF NOT EXISTS _impact_frontier_dirs "
            "(dir TEXT PRIMARY KEY, score REAL NOT NULL)"
        )
        conn.execute(
            "CREATE TEMP TABLE IF NOT EXISTS _impact_policies "
            "(kind TEXT PRIMARY KEY, weight REAL NOT NULL, "
            "direction TEXT NOT NULL)"
        )
        params = (
            (0.5, 0.6, "incoming", "outgoing", 0.5, 0.6, "incoming",
             "incoming", 0.5, 0.6, "incoming", "incoming", 0.05)
            if "?" in sql else ()
        )
        return [
            row["detail"]
            for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params)
        ]

    def test_the_directory_branch_seeks_on_the_target(self, tmp_path):
        repo = _go_repo(tmp_path, members=2, importers=2)
        _init_repo(repo)
        store = _build(repo)
        try:
            plan = self._plan(store, _impact_candidate_sql(""))
        finally:
            store.close()
        seeks = [
            line for line in plan
            if "idx_edges_target_kind" in line and "target_qualified=?" in line
        ]
        assert seeks, f"directory branch is not an index seek: {plan}"

    def test_the_frontier_directories_are_driven_by_the_frontier(self, tmp_path):
        """Not by every File node in the graph, which was the same defect."""
        repo = _go_repo(tmp_path, members=2, importers=2)
        _init_repo(repo)
        store = _build(repo)
        try:
            plan = self._plan(store, IMPACT_FRONTIER_DIRS_SQL)
        finally:
            store.close()
        assert not any(
            "SCAN n" in line or "idx_nodes_kind" in line for line in plan
        ), f"frontier directories scan the nodes table: {plan}"
