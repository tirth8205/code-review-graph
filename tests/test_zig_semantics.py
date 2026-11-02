"""Zig receiver evidence, resolver precision, and test-community filtering."""

import tempfile
from pathlib import Path

from code_review_graph.communities import (
    _qn_is_test,
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

    def test_builtin_is_not_a_call(self):
        _, edges = self.parser.parse_bytes(
            Path("builtin.zig"),
            b"fn run(x: u64) u32 { return @intCast(x); }\n",
        )
        assert not any(edge.target.startswith("@") for edge in _call_edges(edges))

    def test_dynamic_receiver_chain_is_not_bound(self):
        _, edges = self.parser.parse_bytes(
            Path("dynamic.zig"),
            b"fn run() void { zig_api.runtimeHandle().load(); }\n",
        )
        call = next(e for e in _call_edges(edges) if e.target == "load")
        assert call.extra["receiver"] == "?"
        assert "receiver_binding" not in call.extra


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


class TestTestCommunityDetection:
    def test_test_files_are_detected_by_path(self):
        assert _qn_is_test("/repo/src/tests/app_binding_test.zig::test:x")
        assert _qn_is_test("/repo/src/core/engine_test.zig::engine")
        assert not _qn_is_test(
            "/repo/src/io/buffer_lease.zig::test:buffer lease rejects stale release",
        )

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

        assert _test_dominated_community_ids(communities) == {1}
