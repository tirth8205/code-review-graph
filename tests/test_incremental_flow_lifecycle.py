"""Incremental flow and community state must match a fresh full rebuild.

``git diff --name-only`` hands the incremental path repo-relative files while
the graph stores absolute ``file_path`` values, and replacing a file's nodes
gives them new IDs. Before PR #673 (issue #569, also #1058) an incremental
update therefore never traced a new entry point, left flows pointing at
deleted node IDs, and kept community rows whose members had all been
deleted. These tests drive the real build tool (the path ``code-review-graph
update`` and ``build_or_update_graph_tool`` take) through additions,
replacements, callee-only edits, deletions, renames and no-op updates, and
compare the result with a fresh full rebuild of the same tree. Lower-level
cases pin the path expansion the fix relies on: empty input, duplicates,
Windows separators, symlinked roots, unicode and spaces, and batches larger
than one SQL parameter batch.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import (
    full_build,
    get_db_path,
    incremental_update,
)
from code_review_graph.parser import NodeInfo
from code_review_graph.tools.build import build_or_update_graph, run_postprocess

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git_ok(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        cwd=str(repo),
        timeout=30,
    )
    assert result.returncode == 0, f"git {' '.join(args)}: {result.stderr.strip()}"
    return result.stdout.strip()


def _commit_all(repo: Path, message: str) -> str:
    _git_ok(repo, "add", "-A")
    _git_ok(repo, "commit", "-q", "--allow-empty", "-m", message)
    return _git_ok(repo, "rev-parse", "HEAD")


APP_V1 = "def helper():\n    return 1\n\n\ndef main():\n    return helper()\n"
LONELY = "def solo_a():\n    return solo_b()\n\n\ndef solo_b():\n    return 2\n"
TOOL = "def run_tool():\n    return tool_helper()\n\n\ndef tool_helper():\n    return 3\n"


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A git repo with two independent flows (``main`` and ``solo_a``)."""
    monkeypatch.delenv("CRG_DATA_DIR", raising=False)
    monkeypatch.setenv("CRG_SERIAL_PARSE", "1")
    root = tmp_path / "repo"
    root.mkdir()
    _git_ok(root, "init", "-q", "-b", "main")
    _git_ok(root, "config", "user.email", "test@example.invalid")
    _git_ok(root, "config", "user.name", "Test")
    (root / "app.py").write_text(APP_V1)
    (root / "lonely.py").write_text(LONELY)
    _commit_all(root, "init")
    return root


def _build(root: Path, *, full: bool, postprocess: str = "full") -> dict:
    result = build_or_update_graph(
        full_rebuild=full,
        repo_root=str(root),
        postprocess=postprocess,
    )
    assert result["status"] in {"ok", "partial"}, result
    return result


_MISSING = "<missing node>"


def _flow_state(store: GraphStore) -> tuple[list, list]:
    """Flows and memberships keyed by qualified name (node IDs churn).

    A row pointing at a deleted node shows up as ``<missing node>``.
    """
    conn = store._conn
    flows = sorted(
        tuple(row)
        for row in conn.execute(
            "SELECT f.name, COALESCE(n.qualified_name, ?), f.depth, f.node_count, "
            "f.file_count FROM flows f LEFT JOIN nodes n ON n.id = f.entry_point_id",
            (_MISSING,),
        ).fetchall()
    )
    members = sorted(
        tuple(row)
        for row in conn.execute(
            "SELECT f.name, COALESCE(n.qualified_name, ?) FROM flow_memberships fm "
            "JOIN flows f ON f.id = fm.flow_id "
            "LEFT JOIN nodes n ON n.id = fm.node_id",
            (_MISSING,),
        ).fetchall()
    )
    return flows, members


def _community_state(store: GraphStore) -> list:
    """Community rows with their live members, independent of row IDs."""
    conn = store._conn
    state = []
    for cid, name, size in conn.execute(
        "SELECT id, name, size FROM communities"
    ).fetchall():
        members = tuple(sorted(
            r[0] for r in conn.execute(
                "SELECT qualified_name FROM nodes WHERE community_id = ?", (cid,),
            ).fetchall()
        ))
        state.append((name, size, members))
    return sorted(state)


def _dangling(store: GraphStore) -> tuple[int, int]:
    conn = store._conn
    memberships = conn.execute(
        "SELECT COUNT(*) FROM flow_memberships "
        "WHERE node_id NOT IN (SELECT id FROM nodes)"
    ).fetchone()[0]
    entry_points = conn.execute(
        "SELECT COUNT(*) FROM flows WHERE entry_point_id NOT IN (SELECT id FROM nodes)"
    ).fetchone()[0]
    return memberships, entry_points


def _snapshot(root: Path) -> tuple:
    with GraphStore(get_db_path(root)) as store:
        return _flow_state(store), _community_state(store), _dangling(store)


def _assert_matches_full_rebuild(root: Path, *, communities: bool = True) -> tuple:
    """Snapshot the incremental graph, rebuild in place, and compare."""
    incremental = _snapshot(root)
    assert incremental[2] == (0, 0), f"dangling flow rows: {incremental[2]}"
    _build(root, full=True)
    clean = _snapshot(root)
    assert incremental[0] == clean[0], "flows differ from a fresh full rebuild"
    if communities:
        assert incremental[1] == clean[1], "communities differ from a full rebuild"
    return incremental


def _flow_names(root: Path) -> list[str]:
    with GraphStore(get_db_path(root)) as store:
        return sorted(
            r[0] for r in store._conn.execute("SELECT name FROM flows").fetchall()
        )


# ---------------------------------------------------------------------------
# The real update path: build tool over git history
# ---------------------------------------------------------------------------


class TestBuildToolParity:
    def test_new_entry_point_from_git_diff_is_traced(self, repo: Path) -> None:
        """#569: a brand-new file's entry point gets a flow on ``update``."""
        _build(repo, full=True)
        (repo / "tool.py").write_text(TOOL)
        _commit_all(repo, "tool")

        result = _build(repo, full=False)
        assert result["build_type"] == "incremental"
        assert result["flows_detected"] >= 1
        assert "run_tool" in _flow_names(repo)
        # Communities of a brand-new isolated file are only assigned once an
        # existing community is touched (pre-existing design), so compare flows.
        _assert_matches_full_rebuild(repo, communities=False)

    def test_replacement_keeps_one_live_flow(self, repo: Path) -> None:
        """Re-parsing the entry point's file rematches its flow by name."""
        _build(repo, full=True)
        (repo / "app.py").write_text(
            "def helper():\n    return 1\n\n\ndef extra():\n    return 5\n\n\n"
            "def main():\n    return helper() + extra()\n"
        )
        _commit_all(repo, "modify")

        _build(repo, full=False)
        flows, _members = _snapshot(repo)[0]
        main_flows = [f for f in flows if f[0] == "main"]
        assert len(main_flows) == 1
        assert main_flows[0][3] == 3, "main should now reach helper and extra"
        _assert_matches_full_rebuild(repo)

    def test_callee_only_edit_retraces_caller_flow(self, repo: Path) -> None:
        """A flow whose entry point is unchanged is re-traced when a callee changes."""
        (repo / "app.py").write_text(
            "from util import helper\n\n\ndef main():\n    return helper()\n"
        )
        (repo / "util.py").write_text("def helper():\n    return 1\n")
        _commit_all(repo, "split")
        _build(repo, full=True)
        assert "main" in _flow_names(repo)

        (repo / "util.py").write_text(
            "def deeper():\n    return 1\n\n\ndef helper():\n    return deeper()\n"
        )
        _commit_all(repo, "callee")
        _build(repo, full=False)
        flows, _members = _snapshot(repo)[0]
        assert [f[3] for f in flows if f[0] == "main"] == [3]
        # util.py's nodes sit in no community (one-node file groups fall under
        # min_size), so incremental community detection is skipped by design.
        _assert_matches_full_rebuild(repo, communities=False)

    def test_deleted_file_leaves_no_flow_or_empty_community(self, repo: Path) -> None:
        _build(repo, full=True)
        assert "solo_a" in _flow_names(repo)
        _git_ok(repo, "rm", "-q", "lonely.py")
        _commit_all(repo, "delete")

        _build(repo, full=False)
        assert "solo_a" not in _flow_names(repo)
        with GraphStore(get_db_path(repo)) as store:
            for _name, size, members in _community_state(store):
                assert members, "community row kept with no members"
                assert size == len(members)
        _assert_matches_full_rebuild(repo)

    def test_rename_matches_full_rebuild(self, repo: Path) -> None:
        _build(repo, full=True)
        _git_ok(repo, "mv", "lonely.py", "renamed.py")
        _commit_all(repo, "rename")

        _build(repo, full=False)
        with GraphStore(get_db_path(repo)) as store:
            assert not store.get_nodes_by_file(str((repo / "lonely.py").resolve()))
        _assert_matches_full_rebuild(repo)

    def test_former_entry_point_that_gains_a_caller_loses_its_flow(
        self, repo: Path,
    ) -> None:
        """A preserved entry-point name must not resurrect a flow it no longer heads."""
        _build(repo, full=True)
        assert "solo_a" in _flow_names(repo)
        # ``solo_a`` has no conventional entry name, so it is an entry point
        # only while nothing calls it.
        (repo / "lonely.py").write_text(
            LONELY + "\n\ndef kickoff():\n    return solo_a()\n"
        )
        _commit_all(repo, "caller")

        _build(repo, full=False)
        names = _flow_names(repo)
        assert "kickoff" in names
        assert "solo_a" not in names
        _assert_matches_full_rebuild(repo)

    def test_same_function_name_in_two_files_is_not_cross_wired(
        self, repo: Path,
    ) -> None:
        for name in ("a.py", "b.py"):
            (repo / name).write_text(
                "def handler():\n    return work()\n\n\ndef work():\n    return 1\n"
            )
        _commit_all(repo, "twins")
        _build(repo, full=True)
        with GraphStore(get_db_path(repo)) as store:
            b_flow_before = store._conn.execute(
                "SELECT f.id, f.entry_point_id FROM flows f JOIN nodes n "
                "ON n.id = f.entry_point_id WHERE n.qualified_name LIKE ?",
                ("%/b.py::handler",),
            ).fetchall()
        assert len(b_flow_before) == 1

        (repo / "a.py").write_text(
            "def handler():\n    return work() + more()\n\n\n"
            "def work():\n    return 1\n\n\ndef more():\n    return 2\n"
        )
        _commit_all(repo, "edit a")
        _build(repo, full=False)
        with GraphStore(get_db_path(repo)) as store:
            b_flow_after = store._conn.execute(
                "SELECT f.id, f.entry_point_id FROM flows f JOIN nodes n "
                "ON n.id = f.entry_point_id WHERE n.qualified_name LIKE ?",
                ("%/b.py::handler",),
            ).fetchall()
            handler_flows = store._conn.execute(
                "SELECT COUNT(*) FROM flows WHERE name = 'handler'"
            ).fetchone()[0]
        assert b_flow_after == b_flow_before, "unchanged b.py flow was rewritten"
        assert handler_flows == 2
        _assert_matches_full_rebuild(repo)

    def test_second_update_without_changes_is_idempotent(self, repo: Path) -> None:
        _build(repo, full=True)
        (repo / "app.py").write_text(APP_V1 + "\n\ndef extra():\n    return 3\n")
        _commit_all(repo, "modify")
        _build(repo, full=False)
        first = _snapshot(repo)
        with GraphStore(get_db_path(repo)) as store:
            ids_first = store._conn.execute(
                "SELECT id, entry_point_id FROM flows ORDER BY id"
            ).fetchall()

        _build(repo, full=False)
        assert _snapshot(repo) == first
        with GraphStore(get_db_path(repo)) as store:
            ids_second = store._conn.execute(
                "SELECT id, entry_point_id FROM flows ORDER BY id"
            ).fetchall()
        assert [tuple(r) for r in ids_second] == [tuple(r) for r in ids_first]

    @pytest.mark.parametrize("level", ["none", "minimal"])
    def test_postprocess_skipped_leaves_no_dangling_rows(
        self, repo: Path, level: str,
    ) -> None:
        """Without flow tracing, changed files' flows are dropped, not left dangling."""
        _build(repo, full=True)
        with GraphStore(get_db_path(repo)) as store:
            solo_before = store._conn.execute(
                "SELECT id, entry_point_id FROM flows WHERE name = 'solo_a'"
            ).fetchall()
        (repo / "app.py").write_text(APP_V1 + "\n\ndef extra():\n    return 3\n")
        _commit_all(repo, "modify")

        _build(repo, full=False, postprocess=level)
        with GraphStore(get_db_path(repo)) as store:
            assert _dangling(store) == (0, 0)
            solo_after = store._conn.execute(
                "SELECT id, entry_point_id FROM flows WHERE name = 'solo_a'"
            ).fetchall()
        assert [tuple(r) for r in solo_after] == [tuple(r) for r in solo_before]

        # An explicit postprocess afterwards restores full-rebuild parity.
        assert run_postprocess(repo_root=str(repo))["status"] == "ok"
        _assert_matches_full_rebuild(repo)

    def test_graph_damaged_by_earlier_release_is_cleaned(self, repo: Path) -> None:
        """Rows left dangling by pre-#673 updates are purged on the next update."""
        _build(repo, full=True)
        # What earlier releases did: replace a file's nodes without touching
        # the flows or communities that referenced the old node IDs.
        app = repo / "app.py"
        app.write_text(APP_V1 + "\n\ndef extra():\n    return 3\n")
        with GraphStore(get_db_path(repo)) as store:
            from code_review_graph.parser import CodeParser

            source = app.read_bytes()
            nodes, edges = CodeParser(repo).parse_bytes(app.resolve(), source)
            import hashlib

            store.store_file_nodes_edges(
                str(app.resolve()), nodes, edges, hashlib.sha256(source).hexdigest(),
            )
            store._conn.execute(
                "UPDATE nodes SET community_id = NULL WHERE file_path = ?",
                (str((repo / "lonely.py").resolve()),),
            )
            store.commit()
            assert _dangling(store) != (0, 0)

        (repo / "tool.py").write_text(TOOL)
        _commit_all(repo, "upgrade")
        _build(repo, full=False)
        with GraphStore(get_db_path(repo)) as store:
            assert _dangling(store) == (0, 0)
            for _name, size, members in _community_state(store):
                assert members and size == len(members)


class TestPathSpellings:
    def test_unicode_and_spaces_in_paths(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("CRG_DATA_DIR", raising=False)
        root = tmp_path / "my repo ü"
        root.mkdir()
        _git_ok(root, "init", "-q", "-b", "main")
        _git_ok(root, "config", "user.email", "test@example.invalid")
        _git_ok(root, "config", "user.name", "Test")
        (root / "app.py").write_text(APP_V1)
        _commit_all(root, "init")
        _build(root, full=True)

        pkg = root / "pkg dir"
        pkg.mkdir()
        (pkg / "módulo ñ.py").write_text(TOOL)
        _commit_all(root, "unicode")
        _build(root, full=False)
        assert "run_tool" in _flow_names(root)
        _assert_matches_full_rebuild(root, communities=False)

    def test_symlinked_repo_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Stored paths are canonical; a symlinked root must still match them."""
        monkeypatch.delenv("CRG_DATA_DIR", raising=False)
        real = tmp_path / "real"
        real.mkdir()
        _git_ok(real, "init", "-q", "-b", "main")
        _git_ok(real, "config", "user.email", "test@example.invalid")
        _git_ok(real, "config", "user.name", "Test")
        (real / "app.py").write_text(APP_V1)
        _commit_all(real, "init")
        link = tmp_path / "link"
        try:
            link.symlink_to(real, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable")

        _build(link, full=True)
        (real / "tool.py").write_text(TOOL)
        _commit_all(real, "tool")
        _build(link, full=False)
        assert "run_tool" in _flow_names(real)

        from code_review_graph.flows import expand_changed_file_paths

        with GraphStore(get_db_path(real)) as store:
            expanded = expand_changed_file_paths(store, ["tool.py"], repo_root=link)
        assert str((real / "tool.py").resolve()) in expanded

    def test_windows_separator_input_matches_posix_identity(
        self, tmp_path: Path,
    ) -> None:
        from code_review_graph.flows import expand_changed_file_paths

        store = GraphStore(tmp_path / "g.db")
        try:
            target = (tmp_path / "pkg" / "b.py").resolve()
            store.upsert_node(NodeInfo("Function", "f", str(target), 1, 2, "python"))
            store.commit()
            expanded = expand_changed_file_paths(
                store, ["pkg\\b.py"], repo_root=tmp_path,
            )
            assert normalize(target) in expanded
        finally:
            store.close()

    def test_legacy_backslash_rows_match_only_exact_basename(
        self, tmp_path: Path,
    ) -> None:
        """Without a root, ``b.py`` matches ``C:\\repo\\b.py`` but not ``ab.py``."""
        from code_review_graph.flows import expand_changed_file_paths

        store = GraphStore(tmp_path / "g.db")
        try:
            for i, path in enumerate(("C:\\repo\\b.py", "C:\\repo\\ab.py")):
                store._conn.execute(
                    "INSERT INTO nodes (kind, name, qualified_name, file_path, "
                    "line_start, line_end, language, updated_at) "
                    "VALUES ('Function', ?, ?, ?, 1, 2, 'python', 0)",
                    (f"f{i}", f"{path}::f{i}", path),
                )
            store.commit()
            expanded = expand_changed_file_paths(store, ["b.py"])
            assert "C:\\repo\\b.py" in expanded
            assert "C:\\repo\\ab.py" not in expanded
        finally:
            store.close()

    def test_get_files_matching_escapes_wildcards_in_windows_input(
        self, tmp_path: Path,
    ) -> None:
        store = GraphStore(tmp_path / "g.db")
        try:
            for name in ("a_b.py", "axb.py"):
                path = str((tmp_path / "pkg" / name).resolve())
                store.upsert_node(NodeInfo("Function", "f", path, 1, 2, "python"))
            store.commit()
            matched = store.get_files_matching("pkg\\a_b.py")
            assert matched == [normalize(tmp_path / "pkg" / "a_b.py")]
        finally:
            store.close()


def normalize(path: Path) -> str:
    return path.resolve().as_posix()


# ---------------------------------------------------------------------------
# Lifecycle helpers: empty, duplicate and large inputs
# ---------------------------------------------------------------------------


def _store_two_file_flows(tmp_path: Path) -> GraphStore:
    from code_review_graph.flows import store_flows, trace_flows

    for name in ("a.py", "b.py"):
        (tmp_path / name).write_text(
            f"def entry_{name[0]}():\n    return work_{name[0]}()\n\n\n"
            f"def work_{name[0]}():\n    return 1\n"
        )
    store = GraphStore(tmp_path / "g.db")
    full_build(tmp_path, store)
    store_flows(store, trace_flows(store))
    return store


class TestLifecycleHelpers:
    def test_empty_inputs_are_no_ops(self, tmp_path: Path) -> None:
        from code_review_graph.communities import (
            capture_community_assignments,
            incremental_detect_communities,
            purge_empty_communities,
            remap_community_assignments,
        )
        from code_review_graph.flows import (
            clear_flows_for_files,
            expand_changed_file_paths,
            incremental_trace_flows,
        )

        store = _store_two_file_flows(tmp_path)
        try:
            before = _flow_state(store)
            assert expand_changed_file_paths(store, []) == []
            assert expand_changed_file_paths(store, [], repo_root=tmp_path) == []
            assert incremental_trace_flows(store, []) == 0
            assert incremental_trace_flows(store, [], entry_point_qns=[]) == 0
            assert incremental_trace_flows(store, [], entry_point_qns=None) == 0
            # A captured entry point that no longer exists is ignored.
            assert incremental_trace_flows(
                store, [], entry_point_qns=["/gone.py::vanished"],
            ) == 0
            assert clear_flows_for_files(store, []) == set()
            assert capture_community_assignments(store, []) == {}
            assert remap_community_assignments(store, {}) == 0
            assert incremental_detect_communities(store, []) == 0
            assert _flow_state(store) == before
        finally:
            store.close()

        empty = GraphStore(tmp_path / "empty.db")
        try:
            assert purge_empty_communities(empty) == 0
            assert expand_changed_file_paths(empty, ["a.py"], repo_root=tmp_path) == [
                "a.py", normalize(tmp_path / "a.py"),
            ]
        finally:
            empty.close()

    def test_duplicate_changed_files_trace_one_flow(self, tmp_path: Path) -> None:
        from code_review_graph.flows import incremental_trace_flows

        store = _store_two_file_flows(tmp_path)
        try:
            abs_b = str((tmp_path / "b.py").resolve())
            count = incremental_trace_flows(
                store, ["b.py", "b.py", abs_b, "b.py"], repo_root=tmp_path,
            )
            assert count == 1
            names = [
                r[0] for r in store._conn.execute("SELECT name FROM flows").fetchall()
            ]
            assert sorted(names) == ["entry_a", "entry_b"]
        finally:
            store.close()

    def test_batches_larger_than_one_sql_batch(self, tmp_path: Path) -> None:
        """Inputs above the 450-parameter batch size are all matched and cleared."""
        from code_review_graph.communities import capture_community_assignments
        from code_review_graph.flows import (
            clear_flows_for_files,
            expand_changed_file_paths,
            incremental_trace_flows,
            store_flows,
            trace_flows,
        )
        from code_review_graph.parser import EdgeInfo

        count = 1000
        store = GraphStore(tmp_path / "g.db")
        try:
            paths = []
            for i in range(count):
                path = normalize(tmp_path / "pkg" / f"m{i}.py")
                paths.append(path)
                store.upsert_node(NodeInfo("Function", "entry", path, 1, 2, "python"))
                store.upsert_node(NodeInfo("Function", "work", path, 3, 4, "python"))
                store.upsert_edge(EdgeInfo(
                    "CALLS", f"{path}::entry", f"{path}::work", path, 1,
                ))
            store.commit()
            store_flows(store, trace_flows(store, max_depth=3))
            assert store._conn.execute("SELECT COUNT(*) FROM flows").fetchone()[0] == count
            store._conn.execute("INSERT INTO communities (name, size) VALUES ('all', ?)",
                                (2 * count,))
            store._conn.execute("UPDATE nodes SET community_id = 1")
            store.commit()

            relative = [f"pkg/m{i}.py" for i in range(count)]
            expanded = expand_changed_file_paths(store, relative, repo_root=tmp_path)
            assert set(paths) <= set(expanded)

            captured = capture_community_assignments(store, paths)
            assert len(captured) == 2 * count

            entry_qns = clear_flows_for_files(store, paths)
            assert len(entry_qns) == count
            assert store._conn.execute("SELECT COUNT(*) FROM flows").fetchone()[0] == 0

            retraced = incremental_trace_flows(
                store, relative, repo_root=tmp_path, entry_point_qns=entry_qns,
            )
            assert retraced == count
        finally:
            store.close()


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_failed_parse_in_parallel_path_keeps_flows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The executor branch (8+ files) must also keep flows of a file that failed."""
    import code_review_graph.incremental as incremental_module
    from code_review_graph.flows import store_flows, trace_flows

    monkeypatch.delenv("CRG_SERIAL_PARSE", raising=False)
    monkeypatch.setenv("CRG_PARSE_EXECUTOR", "thread")
    names = [f"m{i}.py" for i in range(9)]
    for name in names:
        stem = name[:-3]
        (tmp_path / name).write_text(
            f"def entry_{stem}():\n    return work_{stem}()\n\n\n"
            f"def work_{stem}():\n    return 1\n"
        )
    store = GraphStore(tmp_path / "g.db")
    try:
        full_build(tmp_path, store)
        store_flows(store, trace_flows(store))
        before = _flow_state(store)
        assert len(before[0]) == len(names)

        for name in names:
            stem = name[:-3]
            (tmp_path / name).write_text(
                f"def entry_{stem}():\n    return work_{stem}()\n\n\n"
                f"def work_{stem}():\n    return 2\n"
            )
        real = incremental_module._parse_single_file

        def failing(args):  # noqa: ANN001
            if args[0] == "m0.py":
                return args[0], [], [], "simulated parse failure", None
            return real(args)

        monkeypatch.setattr(incremental_module, "_parse_single_file", failing)
        result = incremental_update(tmp_path, store, changed_files=names)
        assert [e["file"] for e in result["errors"]] == ["m0.py"]
        assert result["files_updated"] == len(names) - 1

        flows, _members = _flow_state(store)
        m0 = [f for f in flows if f[0] == "entry_m0"]
        assert len(m0) == 1 and m0[0][1] != _MISSING, "failed file lost its flow"
        assert _dangling(store) == (0, 0)
        assert sorted(result["flow_entry_point_qns"]) == sorted(
            f"{normalize(tmp_path / n)}::entry_{n[:-3]}" for n in names[1:]
        )
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Freshness: the lifecycle work must not change what a no-op update records
# ---------------------------------------------------------------------------


def test_authoritative_noop_update_advances_freshness_anchor(repo: Path) -> None:
    """An empty diff from the last-synced commit still records the new HEAD.

    Guard for staging behaviour (#952): ``incremental_update`` with
    ``base`` equal to the stored ``git_head_sha`` is authoritative, so an
    empty commit must move the anchor even though nothing was re-parsed.
    """
    with GraphStore(get_db_path(repo)) as store:
        full_build(repo, store)
        anchor = store.get_metadata("git_head_sha")
        head = _commit_all(repo, "empty")
        assert head != anchor

        result = incremental_update(repo, store, base=anchor)
        assert result["files_updated"] == 0
        assert result.get("freshness_advanced") is True
        assert store.get_metadata("git_head_sha") == head


def test_noop_update_reports_graph_up_to_date(repo: Path) -> None:
    _build(repo, full=True)
    result = _build(repo, full=False)
    assert result["files_updated"] == 0
    assert result["summary"] == "No changes detected. Graph is up to date."


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_unreadable_changed_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A file that cannot be read keeps its flows unless its rows are removed.

    Watch batches (``reconcile_stale=False``) keep the previous rows and the
    flows over them. A full reconciliation treats an unreadable file as gone
    and removes its rows (unchanged behaviour); its flows must go with them
    instead of pointing at deleted nodes.
    """
    from code_review_graph.flows import store_flows, trace_flows

    monkeypatch.setenv("CRG_SERIAL_PARSE", "1")
    app = tmp_path / "app.py"
    app.write_text(APP_V1)
    store = GraphStore(tmp_path / "g.db")
    try:
        full_build(tmp_path, store)
        store_flows(store, trace_flows(store))
        before = _flow_state(store)
        assert before[0]
        app.write_text(APP_V1 + "\n\ndef extra():\n    return 3\n")
        app.chmod(0)
        try:
            if os.access(app, os.R_OK):
                pytest.skip("running with permission to read mode-000 files")
            watch = incremental_update(
                tmp_path, store, changed_files=["app.py"], reconcile_stale=False,
            )
            assert [e["file"] for e in watch["errors"]] == ["app.py"]
            assert watch["flow_entry_point_qns"] == []
            assert _flow_state(store) == before

            incremental_update(tmp_path, store, changed_files=["app.py"])
        finally:
            app.chmod(0o644)
        assert store.get_nodes_by_file(str(app.resolve())) == []
        assert _flow_state(store) == ([], [])
        assert _dangling(store) == (0, 0)
    finally:
        store.close()
