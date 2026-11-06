"""Zig receiver evidence, resolver precision, and test-community filtering."""

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from code_review_graph.communities import (
    _qn_is_test,
    _store_repo_root,
    _test_dominated_community_ids,
)
from code_review_graph.graph import GraphStore
from code_review_graph.parser import CodeParser, EdgeInfo, NodeInfo


def _call_edges(edges):
    return [edge for edge in edges if edge.kind == "CALLS"]


class TestZigReceiverEvidence:
    def setup_method(self):
        self.parser = CodeParser()

    def test_self_method_call_is_class_bound(self):
        _, edges = self.parser.parse_bytes(
            Path("self_receiver.zig"),
            b"const T = struct {\n"
            b"    pub fn run(self: *T) void { self.load(); }\n"
            b"    fn load(self: *T) void { _ = self; }\n"
            b"};\n",
        )
        call = next(e for e in _call_edges(edges) if e.target == "load")
        assert call.extra["receiver_binding"] == "class"
        assert call.extra["receiver_class"] == "T"

    def test_module_alias_call_is_module_bound(self, tmp_path):
        util = tmp_path / "util.zig"
        util.write_text("pub fn helper() void {}\n", encoding="utf-8")
        main = tmp_path / "main.zig"
        _, edges = self.parser.parse_bytes(
            main,
            b'const util = @import("./util.zig");\n'
            b"fn run() void { util.helper(); }\n",
        )
        call = next(e for e in _call_edges(edges) if e.target == "helper")
        assert call.extra["receiver_binding"] == "module"
        assert call.extra["receiver_module"].endswith("util.zig")

    def test_nested_container_call_is_module_class_bound(self, tmp_path):
        util = tmp_path / "util.zig"
        util.write_text(
            "pub const time = struct {\n"
            "    pub fn milliTimestamp() void {}\n"
            "};\n",
            encoding="utf-8",
        )
        main = tmp_path / "main.zig"
        _, edges = self.parser.parse_bytes(
            main,
            b'const util = @import("./util.zig");\n'
            b"fn run() void { util.time.milliTimestamp(); }\n",
        )
        call = next(
            e for e in _call_edges(edges) if e.target == "milliTimestamp"
        )
        assert call.extra["receiver_binding"] == "module_class"
        assert call.extra["receiver_class"] == "time"
        assert call.extra["receiver_module"].endswith("util.zig")

    def test_std_call_is_external(self):
        _, edges = self.parser.parse_bytes(
            Path("external.zig"),
            b'const std = @import("std");\n'
            b'fn run() void { _ = std.mem.eql(u8, "a", "b"); }\n',
        )
        call = next(e for e in _call_edges(edges) if e.target == "eql")
        assert call.extra["external"] is True
        assert call.extra["external_module"] == "std"
        # An external call must not be able to answer callers_of for a
        # project symbol that happens to share the name.
        assert call.extra["receiver_binding"] == "external"
        same_named_symbol = SimpleNamespace(
            qualified_name="/repo/src/eql.zig::eql",
            file_path="/repo/src/eql.zig",
            parent_name=None,
        )
        assert not GraphStore.receiver_evidence_admits(
            call.extra, same_named_symbol,
        )

    def test_project_alias_call_keeps_a_std_helper_name(self, tmp_path):
        """``checks.expect()`` is a project call even though std exposes it.

        Judging extern-ness from the callee name is what made valid project
        calls unresolvable: the edge was marked external before any binding
        was consulted, so neither the resolver nor ZLS was ever asked.
        """
        checks = tmp_path / "checks.zig"
        checks.write_text("pub fn expect(ok: bool) void { _ = ok; }\n", encoding="utf-8")
        main = tmp_path / "main.zig"
        _, edges = self.parser.parse_bytes(
            main,
            b'const checks = @import("./checks.zig");\n'
            b"fn run() void { checks.expect(true); }\n",
        )
        call = next(e for e in _call_edges(edges) if e.target == "expect")
        assert "external" not in call.extra
        assert call.extra["receiver_binding"] == "module"

    def test_bare_call_named_like_a_std_helper_is_not_external(self):
        """A name-only match against the std helper list is not evidence.

        Marking it external hid a call the resolver could still answer from
        project evidence, and put it in the bucket ZLS is told to skip.
        """
        _, edges = self.parser.parse_bytes(
            Path("local_expect.zig"),
            b"fn run(ok: bool) void { expect(ok); }\n",
        )
        call = next(e for e in _call_edges(edges) if e.target == "expect")
        assert "external" not in call.extra
        assert "receiver_binding" not in call.extra

    def test_field_receiver_is_marked_unknown(self):
        """``self.map.get()`` has a receiver whose type is not in the file."""
        _, edges = self.parser.parse_bytes(
            Path("field.zig"),
            b"const T = struct {\n"
            b"    map: u8,\n"
            b"    pub fn go(self: *T) void { self.map.foo(); }\n"
            b"};\n",
        )
        call = next(e for e in _call_edges(edges) if e.target == "foo")
        assert call.extra["receiver"] == "self.map"
        assert call.extra["receiver_binding"] == "unknown"

    def test_nested_container_chain_binds_the_innermost_container(self):
        """``T.inner.run()`` belongs to ``inner``, not to ``T``."""
        _, edges = self.parser.parse_bytes(
            Path("nested.zig"),
            b"const T = struct {\n"
            b"    const inner = struct { pub fn run() void {} };\n"
            b"    pub fn run() void {}\n"
            b"    pub fn go() void { T.inner.run(); }\n"
            b"};\n",
        )
        call = next(e for e in _call_edges(edges) if e.target == "run")
        assert call.extra["receiver"] == "T.inner"
        assert call.extra["receiver_binding"] == "class"
        assert call.extra["receiver_class"] == "inner"

    def test_import_bindings_do_not_survive_a_reparse(self, tmp_path):
        """A removed import must not keep binding calls on a reused parser."""
        main = tmp_path / "main.zig"
        with_import = (
            b'const util = @import("./util.zig");\n'
            b"fn run() void { util.helper(); }\n"
        )
        without_import = b"fn run() void { util.helper(); }\n"
        self.parser.parse_bytes(main, with_import)
        _, edges = self.parser.parse_bytes(main, without_import)
        call = next(e for e in _call_edges(edges) if e.target == "helper")
        assert call.extra.get("receiver_binding") == "unknown"
        assert "receiver_module" not in call.extra

    def test_builtin_is_not_a_call(self):
        _, edges = self.parser.parse_bytes(
            Path("builtin.zig"),
            b"fn run(x: u64) u32 { return @intCast(x); }\n",
        )
        assert not any(edge.target.startswith("@") for edge in _call_edges(edges))

    def test_dynamic_receiver_chain_is_marked_unknown(self):
        _, edges = self.parser.parse_bytes(
            Path("dynamic.zig"),
            b"fn run() void { zig_api.runtimeHandle().load(); }\n",
        )
        call = next(e for e in _call_edges(edges) if e.target == "load")
        assert call.extra["receiver"] == "?"
        assert call.extra["receiver_binding"] == "unknown"


def _node(
    kind: str,
    name: str,
    path: Path,
    *,
    parent_name: str | None = None,
    line: int = 1,
) -> NodeInfo:
    return NodeInfo(
        kind=kind,
        name=name,
        file_path=str(path),
        line_start=line,
        line_end=line + 1,
        language="zig",
        parent_name=parent_name,
    )


class TestZigResolverPrecision:
    def setup_method(self):
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        handle.close()
        self._db_path = Path(handle.name)
        self.store = GraphStore(str(self._db_path))

    def teardown_method(self):
        self.store.close()
        self._db_path.unlink(missing_ok=True)

    def test_same_file_candidate_wins_over_imported_name_twin(self):
        path_a = Path("/repo/src/io/buffer_lease.zig")
        path_b = Path("/repo/src/io/lease_state.zig")
        for path, line in ((path_a, 1), (path_b, 3)):
            self.store.upsert_node(_node("Class", "BufferLease", path, line=line))
            self.store.upsert_node(
                _node("Function", "init", path, parent_name="BufferLease", line=line),
            )
        self.store.upsert_node(_node("Function", "caller", path_a, line=10))
        self.store.upsert_edge(EdgeInfo(
            kind="IMPORTS_FROM",
            source=str(path_a),
            target=str(path_b),
            file_path=str(path_a),
        ))
        self.store.upsert_edge(EdgeInfo(
            kind="CALLS",
            source=f"{path_a}::caller",
            target="init",
            file_path=str(path_a),
            line=11,
            extra={
                "receiver_binding": "class",
                "receiver_class": "BufferLease",
            },
        ))
        self.store.commit()

        assert self.store.resolve_bare_call_targets() == 1
        row = self.store._conn.execute(
            "SELECT target_qualified FROM edges WHERE kind = 'CALLS'",
        ).fetchone()
        assert row["target_qualified"] == f"{path_a}::BufferLease.init"

    def test_module_class_binding_resolves_container_method(self):
        path_a = Path("/repo/src/abi/main.zig")
        path_util = Path("/repo/src/common/os_compat.zig")
        self.store.upsert_node(_node("Class", "time", path_util, line=1))
        self.store.upsert_node(
            _node("Function", "milliTimestamp", path_util, parent_name="time", line=2),
        )
        self.store.upsert_node(_node("Function", "caller", path_a, line=10))
        self.store.upsert_edge(EdgeInfo(
            kind="IMPORTS_FROM",
            source=str(path_a),
            target=str(path_util),
            file_path=str(path_a),
        ))
        self.store.upsert_edge(EdgeInfo(
            kind="CALLS",
            source=f"{path_a}::caller",
            target="milliTimestamp",
            file_path=str(path_a),
            line=11,
            extra={
                "receiver_binding": "module_class",
                "receiver_module": str(path_util),
                "receiver_class": "time",
            },
        ))
        self.store.commit()

        assert self.store.resolve_bare_call_targets() == 1
        row = self.store._conn.execute(
            "SELECT target_qualified FROM edges WHERE kind = 'CALLS'",
        ).fetchone()
        assert row["target_qualified"] == f"{path_util}::time.milliTimestamp"

    def test_external_target_is_never_resolved_by_bare_name(self):
        path_a = Path("/repo/src/project.zig")
        path_b = Path("/repo/src/other.zig")
        self.store.upsert_node(_node("Function", "fetchAdd", path_b, line=1))
        self.store.upsert_node(_node("Function", "caller", path_a, line=10))
        self.store.upsert_edge(EdgeInfo(
            kind="IMPORTS_FROM",
            source=str(path_a),
            target=str(path_b),
            file_path=str(path_a),
        ))
        self.store.upsert_edge(EdgeInfo(
            kind="CALLS",
            source=f"{path_a}::caller",
            target="fetchAdd",
            file_path=str(path_a),
            line=11,
            extra={"external": True},
        ))
        self.store.commit()

        assert self.store.resolve_bare_call_targets() == 0
        row = self.store._conn.execute(
            "SELECT target_qualified FROM edges WHERE kind = 'CALLS'",
        ).fetchone()
        assert row["target_qualified"] == "fetchAdd"

    def test_unknown_receiver_is_not_resolved_by_import_evidence(self):
        """A receiver the file cannot bind is evidence of nothing.

        The import list says which files this one talks to, not what ``t`` is.
        Resolving ``t.go()`` to the one imported ``go`` invented an edge the
        source does not support, and ZLS was then told to skip the row.
        """
        path_a = Path("/repo/src/main.zig")
        path_util = Path("/repo/src/util.zig")
        self.store.upsert_node(_node("Function", "go", path_util, line=1))
        self.store.upsert_node(_node("Function", "caller", path_a, line=10))
        self.store.upsert_edge(EdgeInfo(
            kind="IMPORTS_FROM",
            source=str(path_a),
            target=str(path_util),
            file_path=str(path_a),
        ))
        self.store.upsert_edge(EdgeInfo(
            kind="CALLS",
            source=f"{path_a}::caller",
            target="go",
            file_path=str(path_a),
            line=11,
            extra={"receiver": "t", "receiver_binding": "unknown"},
        ))
        self.store.commit()

        assert self.store.resolve_bare_call_targets() == 0
        row = self.store._conn.execute(
            "SELECT target_qualified, extra FROM edges WHERE kind = 'CALLS'",
        ).fetchone()
        assert row["target_qualified"] == "go"
        # The row stays a ZLS candidate: the pass is how this call gets an
        # answer, so the resolver must not have consumed it already.
        assert "external" not in json.loads(row["extra"])


class TestTestCommunityDetection:
    def test_test_files_are_detected_by_path(self):
        assert _qn_is_test("/repo/src/tests/app_binding_test.zig::test:x")
        assert _qn_is_test("/repo/src/core/engine_test.zig::engine")
        assert not _qn_is_test(
            "/repo/src/io/buffer_lease.zig::test:buffer lease rejects stale release",
        )

    def test_directory_conventions_are_judged_inside_the_repository(self):
        """The checkout's own parent directories are not project structure.

        A CI workspace that happens to be named ``tests`` used to make every
        symbol in the repository look like test code, which silently emptied
        the untested-production report (#1023).
        """
        qualified = "/home/ci/tests/myrepo/src/core/engine.py::run"
        assert _qn_is_test(qualified, repo_root="/home/ci/tests/myrepo") is False
        # The same path with no known root is judged by its filename alone,
        # which under-reports test files rather than call the whole
        # repository test code. This is the rule every other caller uses.
        assert _qn_is_test(qualified) is False
        # A directory that really is inside the project still counts.
        assert _qn_is_test(
            "/home/ci/tests/myrepo/src/tests/engine.py::run",
            repo_root="/home/ci/tests/myrepo",
        ) is True

    def test_a_test_sounding_producer_is_not_a_test_file(self):
        """Each ecosystem names its test files its own way.

        The unanchored filename rule matched ``foo_testdata.py`` and
        ``my_test_utils.go``, so a module that merely helps tests was counted
        as one and its production callers disappeared from the report. The
        prefix convention is pytest's and minitest's; Go, Zig and C put the
        word at the end, and a file named the other way compiles as
        production.
        """
        assert _qn_is_test("/repo/src/foo_testdata.py::load") is False
        assert _qn_is_test("/repo/src/testing.py::runner") is False
        assert _qn_is_test("/repo/src/test_utils.go::helper") is False
        assert _qn_is_test("/repo/src/test_engine.zig::case") is False
        assert _qn_is_test("/repo/src/engine_tests.zig::case") is True
        assert _qn_is_test("/repo/src/test_engine.py::case") is True

    def test_empty_community_does_not_abort_the_detection(self):
        """No members means no ratio, not a division by zero."""
        communities = [{"id": 7, "name": "orphan", "members": []}]

        assert _test_dominated_community_ids(communities) == set()

    def test_membership_and_warning_use_the_same_test_rule(self, tmp_path):
        """One question, one answer.

        The edge count skipped communities the membership rule called
        test-dominated while the warning re-judged them from the generated
        name, so a community could be both filtered and warned about.
        """
        store = GraphStore(str(tmp_path / "graph.db"))
        try:
            store.set_metadata("repo_root", str(tmp_path))
            members = [
                f"{tmp_path}/src/tests/{name}.zig::test:x" for name in ("a", "b")
            ]
            community = {"id": 1, "name": "support-tcp", "members": members}
            ids = _test_dominated_community_ids(
                [community], _store_repo_root(store),
            )
            assert ids == {1}
        finally:
            store.close()

    def test_inline_tests_do_not_make_production_community_test_dominated(self):
        communities = [
            {
                "id": 1,
                "name": "tests-runtime",
                "members": [
                    "/repo/src/tests/a.zig::test:x",
                    "/repo/src/tests/a.zig::helper",
                ],
            },
            {
                "id": 2,
                "name": "io-claim",
                "members": [
                    "/repo/src/io/a.zig::run",
                    "/repo/src/io/a.zig::test:run rejects stale",
                    "/repo/src/io/b.zig::helper",
                ],
            },
        ]

        assert _test_dominated_community_ids(communities, "/repo") == {1}

    def test_a_test_directory_is_only_evidence_inside_the_repository(self):
        """The membership rule needs to know where the project starts.

        ``/home/ci/tests/job/src/app.py`` is a production file in a workspace
        whose parent happens to be named ``tests``, and the graph stores
        absolute paths. Without the root the directory half of the question
        cannot be answered, so the conservative answer is taken: a test
        community stays in the report (noise) rather than production code
        being filtered out of it (#1023).
        """
        communities = [
            {
                "id": 1,
                "name": "runtime",
                "members": [
                    "/repo/src/tests/a.zig::test:x",
                    "/repo/src/tests/a.zig::helper",
                ],
            },
        ]

        assert _test_dominated_community_ids(communities, "/repo") == {1}
        assert _test_dominated_community_ids(communities) == set()
