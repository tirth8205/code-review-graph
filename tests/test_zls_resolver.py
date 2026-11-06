"""ZLS enrichment: transport framing, paths, resolution side effects.

The knowledge-graph half of the feature (rewriting edges) is tested against a
fake language server so the invariants hold on any machine:

* one transaction per pass, never one autocommit per edge (issue #721 rule);
* ``TESTED_BY`` mirrors follow the ``CALLS`` edge they were minted from;
* every call site on the merged edge must agree before the edge is rewritten;
* no machine-specific absolute path is persisted for an out-of-repo symbol;
* the LSP frame reader does not depend on ``select`` (Windows pipes).
"""

from __future__ import annotations

import io
import json
import queue
import sqlite3
from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.parser import EdgeInfo, NodeInfo
from code_review_graph.zls_resolver import (
    DefinitionResult,
    _extract_frame,
    _frame_reader,
    _portable_module_label,
    count_unresolved_zig_calls,
    enrich_zls_calls,
    uri_to_file_path,
)

REPO = Path("/repo")


def _node(name: str, path: str, *, parent: str | None = None, line: int = 1):
    return NodeInfo(
        kind="Function", name=name, file_path=path, line_start=line,
        line_end=line + 1, language="zig", parent_name=parent,
    )


def _test_node(path: str, name: str, line: int = 10):
    return NodeInfo(
        kind="Test", name=name, file_path=path, line_start=line,
        line_end=line + 2, language="zig", is_test=True,
    )


class _FakeClient:
    """Stands in for ZlsClient: positions map to definition locations."""

    def __init__(self, locations: dict[tuple[str, int, int], tuple[str, int]]):
        self.locations = locations
        self.opened: set[str] = set()
        self.stopped = False

    def open_document(self, file_path: str, source: str) -> None:
        self.opened.add(file_path)

    def definition(self, file_path: str, line: int, character: int) -> DefinitionResult:
        loc = self.locations.get((file_path, line, character))
        if loc is None:
            # No entry in the map means ZLS answered "no definition here".
            from code_review_graph.zls_resolver import _NO_DEFINITION
            return _NO_DEFINITION
        return DefinitionResult(ok=True, location=loc)

    def stop(self) -> None:
        self.stopped = True


@pytest.fixture
def store(tmp_path):
    db = tmp_path / "graph.db"
    handle = GraphStore(str(db))
    yield handle
    handle.close()
    db.unlink(missing_ok=True)


@pytest.fixture
def zig_repo(tmp_path):
    """A two-file Zig project with one unbound-receiver call on line 4."""
    root = tmp_path
    caller = root / "main.zig"
    caller.write_text(
        'const a = @import("./a.zig");\n'
        "pub fn run() void {\n"
        "    var x = a.A{};\n"
        "    x.doIt();\n"
        "}\n"
        'test "covers" {\n'
        "    run();\n"
        "}\n",
        encoding="utf-8",
    )
    target = root / "a.zig"
    target.write_text(
        "pub const A = struct {\n"
        "    pub fn doIt(self: *A) void { _ = self; }\n"
        "};\n",
        encoding="utf-8",
    )
    return root, caller, target


def _seed_unresolved_call(
    store, caller: Path, target: Path, *, line: int = 4, extra=None,
):
    """Index main.zig/a.zig with a bare ``doIt`` call plus its TESTED_BY mirror."""
    store.upsert_node(_node("run", str(caller), line=2))
    store.upsert_node(_test_node(str(caller), 'test:covers', line=6))
    store.upsert_node(_node("doIt", str(target), parent="A", line=2))
    store.upsert_edge(EdgeInfo(
        kind="CALLS", source=f"{caller}::run", target="doIt",
        file_path=str(caller), line=line,
        extra=extra or {"receiver": "x", "receiver_binding": "unknown"},
    ))
    # The parser mirrors a test's CALLS edge; here the test calls run(), and
    # run()'s own call is what ZLS rewrites, so seed the mirror the parser
    # would mint for a test that calls doIt directly.
    store.upsert_edge(EdgeInfo(
        kind="TESTED_BY", source="doIt", target=f"{caller}::test:covers",
        file_path=str(caller), line=line,
        extra={"receiver": "x", "receiver_binding": "unknown"},
    ))
    store.commit()


def _rows(store, kind: str) -> list[sqlite3.Row]:
    return store._conn.execute(
        "SELECT source_qualified, target_qualified, extra FROM edges WHERE kind = ?",
        (kind,),
    ).fetchall()


class TestResolutionSideEffects:
    def test_resolved_call_and_mirror_move_together(
        self, store, zig_repo, tmp_path,
    ):
        root, caller, target = zig_repo
        _seed_unresolved_call(store, caller, target)
        client = _FakeClient({(str(caller), 3, 6): (str(target), 1)})

        stats = enrich_zls_calls(store, root, client=client)

        assert stats["resolved"] == 1
        call = _rows(store, "CALLS")[0]
        assert call["target_qualified"] == f"{target}::A.doIt"
        mirror = _rows(store, "TESTED_BY")[0]
        assert mirror["source_qualified"] == f"{target}::A.doIt", (
            "TESTED_BY must not keep pointing at the unresolved bare name: "
            "a ZLS-resolved function would otherwise report zero covering tests"
        )
        assert "unknown" not in mirror["extra"]

    def test_every_site_on_the_row_must_agree(self, store, zig_repo, tmp_path):
        """Two call sites merged on one line may not resolve to one answer."""
        root, caller, target = zig_repo
        store.upsert_node(_node("run", str(caller), line=2))
        store.upsert_node(_node("doIt", str(target), parent="A", line=2))
        other = root / "b.zig"
        other.write_text(
            "pub const B = struct {\n    pub fn doIt(self: *B) void {}\n};\n",
            encoding="utf-8",
        )
        store.upsert_node(_node("doIt", str(other), parent="B", line=2))
        store.upsert_edge(EdgeInfo(
            kind="CALLS", source=f"{caller}::run", target="doIt",
            file_path=str(caller), line=5,
            extra={"receiver": "x", "receiver_binding": "unknown"},
        ))
        # main.zig line 5 holds two ``doIt()`` sites that ZLS answers with two
        # different definitions (positions are 0-based line / UTF-16 column).
        caller.write_text(
            'const a = @import("./a.zig");\nconst b = @import("./b.zig");\n'
            "pub fn run() void {\n"
            "    var x = a.A{}; var y = b.B{};\n"
            "    x.doIt(); y.doIt();\n"
            "}\n",
            encoding="utf-8",
        )
        store.commit()
        client = _FakeClient({
            (str(caller), 4, 6): (str(target), 1),
            (str(caller), 4, 16): (str(other), 1),
        })

        stats = enrich_zls_calls(store, root, client=client)

        row = _rows(store, "CALLS")[0]
        assert stats["resolved"] == 0
        assert stats["site_conflicts"] == 1
        assert row["target_qualified"] == "doIt", (
            "a conflicting site must stay unresolved instead of guessing"
        )

    def test_out_of_repo_definition_stores_no_absolute_path(
        self, store, zig_repo,
    ):
        root, caller, _target = zig_repo
        _seed_unresolved_call(store, caller, caller)
        sdk = Path("/opt/zig/lib/std/Build.zig")
        client = _FakeClient({(str(caller), 3, 6): (str(sdk), 1)})

        stats = enrich_zls_calls(store, root, client=client)

        assert stats["external"] == 1
        extra = json.loads(_rows(store, "CALLS")[0]["extra"])
        assert extra["external"] is True
        assert extra["external_module"] == "std/Build.zig"
        assert not extra["external_module"].startswith("/")
        assert str(sdk) not in json.dumps(extra)
        # External-only passes also leave the marker: their results are
        # reverted by a re-parse just like resolved ones.
        assert store.get_metadata("zls_enriched_at")

    def test_pass_writes_once_and_rolls_back_on_interrupt(
        self, store, zig_repo,
    ):
        """No edge may be left enriched while its siblings are not (#721 rule)."""
        root, caller, target = zig_repo
        _seed_unresolved_call(store, caller, target)
        store.upsert_node(_node("other", str(caller), line=20))
        store.upsert_edge(EdgeInfo(
            kind="CALLS", source=f"{caller}::other", target="doIt",
            file_path=str(caller), line=4,
            extra={"receiver": "x", "receiver_binding": "unknown"},
        ))
        store.commit()

        class _Boom(_FakeClient):
            def definition(self, file_path, line, character):
                raise KeyboardInterrupt("ctrl-C during a 30-minute pass")

        with pytest.raises(KeyboardInterrupt):
            enrich_zls_calls(store, root, client=_Boom({}))

        rows = store._conn.execute(
            "SELECT extra FROM edges WHERE kind = 'CALLS'",
        ).fetchall()
        assert all("resolution_basis" not in (r["extra"] or "") for r in rows)

    def test_stats_account_for_every_candidate_row(self, store, zig_repo):
        root, caller, target = zig_repo
        _seed_unresolved_call(store, caller, target)
        client = _FakeClient({(str(caller), 3, 6): (str(target), 1)})

        stats = enrich_zls_calls(store, root, client=client)

        accounted = (
            stats["resolved"] + stats["external"] + stats["unmatched"]
            + stats["site_conflicts"] + stats["errors"] + stats["no_definition"]
            + stats.get("skipped", 0)
        )
        assert stats["candidates"] == accounted

    def test_injected_client_is_never_stopped_by_the_pass(
        self, store, zig_repo,
    ):
        """The caller owns a client it supplied; the pass owns its own."""
        root, caller, target = zig_repo
        _seed_unresolved_call(store, caller, target)
        client = _FakeClient({(str(caller), 3, 6): (str(target), 1)})

        enrich_zls_calls(store, root, client=client)

        assert client.stopped is False


class TestStaleEnrichmentIsVisible:
    """A re-parse quietly undoes a pass that took minutes to run.

    ``update`` rebuilds the Zig edges it matched from source, which drops the
    targets ZLS settled. Nothing in the graph says "this was enriched before",
    so the user has no way to know the enrichment is stale unless the pass
    leaves a mark and there is a way to measure what is left.
    """

    def test_pass_records_that_the_graph_was_enriched(self, store, zig_repo):
        root, caller, target = zig_repo
        _seed_unresolved_call(store, caller, target)
        client = _FakeClient({(str(caller), 3, 6): (str(target), 1)})

        enrich_zls_calls(store, root, client=client)

        assert store.get_metadata("zls_enriched_at"), (
            "without this marker an update cannot tell the user their ZLS "
            "results were thrown away"
        )

    def test_counting_ignores_a_graph_that_was_never_enriched(self, store, zig_repo):
        root, caller, target = zig_repo
        _seed_unresolved_call(store, caller, target)

        assert count_unresolved_zig_calls(store) == 1
        assert store.get_metadata("zls_enriched_at") is None

        resolved = _FakeClient({(str(caller), 3, 6): (str(target), 1)})
        enrich_zls_calls(store, root, client=resolved)

        assert count_unresolved_zig_calls(store) == 0

    def test_a_pass_that_resolved_nothing_leaves_no_marker(self, store, zig_repo):
        root, caller, target = zig_repo
        _seed_unresolved_call(store, caller, target)

        enrich_zls_calls(store, root, client=_FakeClient({}))

        assert store.get_metadata("zls_enriched_at") is None


class TestPassOrdering:
    """Flows, criticality and communities are all derived from ``CALLS``.

    A pass that rewrites those targets therefore invalidates everything built
    from them, so it has to run before those stages and not after the build.
    """

    @staticmethod
    def _record(monkeypatch, calls: list[str]) -> None:
        def _enrich(store_, root, **kwargs):
            calls.append("zls")
            return {"candidates": 0, "resolved": 0, "skipped": True, "reason": "none"}

        def _trace(store_):
            calls.append("flows")
            return []

        def _detect(store_):
            calls.append("communities")
            return []

        monkeypatch.setattr("code_review_graph.zls_resolver.enrich_zls_calls", _enrich)
        monkeypatch.setattr("code_review_graph.flows.trace_flows", _trace)
        monkeypatch.setattr("code_review_graph.flows.store_flows", lambda s, f: 0)
        monkeypatch.setattr(
            "code_review_graph.communities.detect_communities", _detect,
        )
        monkeypatch.setattr(
            "code_review_graph.communities.store_communities", lambda s, c: 0,
        )
        monkeypatch.setattr(
            "code_review_graph.tools.build._compute_summaries", lambda s: None,
        )

    def test_zls_runs_before_the_stages_that_read_its_edges(
        self, store, tmp_path, monkeypatch,
    ):
        from code_review_graph.tools import build as build_tool

        calls: list[str] = []
        self._record(monkeypatch, calls)

        warnings = build_tool._run_postprocess(
            store, {}, "full", repo_root=str(tmp_path), zls=True,
        )

        assert calls == ["zls", "flows", "communities"], (
            "deriving flows from the pre-resolution edges and only then "
            "resolving them leaves the report describing a graph that no "
            "longer exists"
        )
        assert not [w for w in warnings if "not recomputed" in w]

    def test_zls_without_postprocess_warns_that_derived_data_is_stale(
        self, store, tmp_path, monkeypatch,
    ):
        from code_review_graph.tools import build as build_tool

        calls: list[str] = []
        self._record(monkeypatch, calls)

        warnings = build_tool._run_postprocess(
            store, {}, "none", repo_root=str(tmp_path), zls=True,
        )

        assert calls == ["zls"], "--skip-postprocess must not derive anything"
        assert any("not recomputed" in w for w in warnings), (
            "the caller still has to learn that the derived data is stale"
        )


class TestBrokenBuildIsNotSilent:
    def test_all_sites_failing_reports_a_diagnostic(self, store, zig_repo):
        root, caller, target = zig_repo
        _seed_unresolved_call(store, caller, target)

        stats = enrich_zls_calls(store, root, client=_FakeClient({}))

        assert stats["resolved"] == 0
        assert stats["no_definition"] == 1
        assert stats["errors"] == 0
        assert stats.get("warning"), (
            "ZLS answering nothing usually means 'zig build' fails; saying so "
            "is the difference between a skipped pass and a broken one"
        )
        assert "no definition" in stats["warning"].lower()
        assert "does not build" in stats["warning"]

    def test_requests_that_never_answered_are_not_called_a_stale_build(
        self, store, zig_repo,
    ):
        """A silent server and an unbuildable workspace need different fixes."""
        root, caller, target = zig_repo
        _seed_unresolved_call(store, caller, target)

        class _Timeouts(_FakeClient):
            def definition(self, file_path, line, character):
                raise TimeoutError("no response within 8s")

        stats = enrich_zls_calls(store, root, client=_Timeouts({}))

        assert stats["errors"] == 1
        assert stats["no_definition"] == 0
        assert "requests failed" in stats["warning"].lower(), (
            "telling the user to run 'zig build' when the server never "
            "answered sends them to the wrong thing to fix"
        )

    def test_a_receiver_named_external_does_not_hide_the_call(self, store, zig_repo):
        """The skip rule reads the metadata, not the JSON text.

        Already-known-external rows must not be asked again, but matching the
        ``external`` key inside the serialized blob also matched a receiver
        variable called ``external``, leaving a resolvable call unasked.
        """
        root, caller, target = zig_repo
        _seed_unresolved_call(
            store, caller, target,
            extra={"receiver": "external", "receiver_binding": "unknown"},
        )
        client = _FakeClient({(str(caller), 3, 6): (str(target), 1)})

        stats = enrich_zls_calls(store, root, client=client)

        assert stats["candidates"] == 1
        assert stats["resolved"] == 1
        assert _rows(store, "CALLS")[0]["target_qualified"] == f"{target}::A.doIt"

    def test_a_row_already_known_to_be_external_is_not_asked_again(
        self, store, zig_repo,
    ):
        root, caller, target = zig_repo
        _seed_unresolved_call(
            store, caller, target,
            extra={"receiver": "std", "external": True,
                   "external_module": "std/Build.zig"},
        )
        client = _FakeClient({(str(caller), 3, 6): (str(target), 1)})

        stats = enrich_zls_calls(store, root, client=client)

        assert stats == {"skipped": True, "reason": "no unresolved Zig calls"}
        assert client.opened == set()


class TestLspTransport:
    def test_frames_are_cut_on_content_length(self):
        buffer = bytearray(
            b'Content-Length: 17\r\n\r\n{"id": 1, "ok":1}',
        )
        assert _extract_frame(buffer) == b'{"id": 1, "ok":1}'
        assert buffer == bytearray()

    def test_partial_frame_is_left_in_the_buffer(self):
        buffer = bytearray(b"Content-Length: 40\r\n\r\n{\"a\":")
        assert _extract_frame(buffer) is None
        assert buffer.startswith(b"Content-Length: 40")

    def test_reader_threads_deliver_messages_without_select(self, monkeypatch):
        """Windows pipes cannot be selected on; reading must be blocking I/O."""
        import code_review_graph.zls_resolver as transport

        assert not hasattr(transport, "select"), (
            "select() only supports sockets on Windows"
        )
        stream = io.BytesIO(
            b'Content-Length: 16\r\n\r\n{"id": 1, "x":1}'
            b'Content-Length: 16\r\n\r\n{"id": 2, "x":2}',
        )
        out: queue.Queue = queue.Queue()

        _frame_reader(stream, out)

        assert out.get_nowait()["id"] == 1
        assert out.get_nowait()["id"] == 2
        assert out.get_nowait() is None  # EOF sentinel

    def test_windows_drive_uri_loses_its_leading_slash(self):
        assert uri_to_file_path(
            "file:///C:/Users/me/proj/main.zig", windows=True,
        ) == "C:/Users/me/proj/main.zig"

    def test_unc_uri_keeps_its_authority(self):
        assert uri_to_file_path(
            "file://build-server/share/zig/main.zig", windows=True,
        ) == "//build-server/share/zig/main.zig"

    def test_percent_escapes_are_decoded(self):
        assert uri_to_file_path("file:///repo/a%20b/main.zig") == "/repo/a b/main.zig"


class TestPortableModuleLabel:
    def test_tail_of_the_package_path_is_used(self):
        assert _portable_module_label("/opt/zig/lib/std/mem/Allocator.zig") == "mem/Allocator.zig"

    def test_relative_paths_keep_their_shape(self):
        assert _portable_module_label("src/io/pool.zig") == "src/io/pool.zig"

    def test_windows_drive_letter_path_is_truncated(self):
        """``C:/Users/me/zig/lib/std/Build.zig`` must not land in the graph."""
        label = _portable_module_label("C:/Users/me/zig/lib/std/Build.zig")
        assert label == "std/Build.zig"
        assert "Users" not in label
        assert "C:" not in label

    def test_windows_backslash_path_is_truncated(self):
        label = _portable_module_label("C:\\Users\\me\\zig\\lib\\std\\Build.zig")
        assert label == "std/Build.zig"


class TestCrossFileMirrorIsolation:
    """A TESTED_BY mirror must not follow a bare name into another file.

    The mirror the parser mints from a test's CALLS edge lives in the same
    file as the test.  Matching ``source_qualified`` globally used to drag a
    ``TESTED_BY`` in ``b.zig`` along when ``a.zig`` resolved a call to the
    same bare name, crediting the resolved target with a test it never had.
    """

    def test_same_bare_name_in_another_file_is_not_moved(self, store, zig_repo):
        root, caller, target = zig_repo
        _seed_unresolved_call(store, caller, target)
        # A second file with its own ``doIt`` and its own TESTED_BY mirror.
        other = root / "other.zig"
        other.write_text(
            "pub const B = struct {\n"
            "    pub fn doIt(self: *B) void { _ = self; }\n"
            "};\n",
            encoding="utf-8",
        )
        store.upsert_node(_node("doIt", str(other), parent="B", line=2))
        store.upsert_node(_test_node(str(other), "test:b", line=10))
        store.upsert_edge(EdgeInfo(
            kind="TESTED_BY", source="doIt", target=f"{other}::test:b",
            file_path=str(other), line=4,
            extra={"receiver_binding": "unknown"},
        ))
        store.commit()

        client = _FakeClient({(str(caller), 3, 6): (str(target), 1)})
        enrich_zls_calls(store, root, client=client)

        # The mirror in caller.zig moved to the resolved target.
        mirrors_in_caller = store._conn.execute(
            "SELECT source_qualified, target_qualified, extra, file_path "
            "FROM edges WHERE kind = 'TESTED_BY' AND file_path = ?",
            (str(caller),),
        ).fetchall()
        assert all(
            r["source_qualified"] == f"{target}::A.doIt"
            for r in mirrors_in_caller
        )
        # The mirror in other.zig stayed on the bare name — it belongs to
        # a different file's test of a different ``doIt``.
        mirrors_in_other = store._conn.execute(
            "SELECT source_qualified, target_qualified, extra, file_path "
            "FROM edges WHERE kind = 'TESTED_BY' AND file_path = ?",
            (str(other),),
        ).fetchall()
        assert all(
            r["source_qualified"] == "doIt"
            for r in mirrors_in_other
        ), "a TESTED_BY in another file must not follow a bare name"


class TestPartialNoneIsRejected:
    """When some call sites answer and others fail, the row stays unresolved.

    A site that timed out is not confirmed: accepting the others would mark
    an unconfirmed call as resolved, which is the same guess the pass exists
    to replace.
    """

    def test_one_site_error_and_one_ok_stays_unresolved(self, store, zig_repo):
        root, caller, target = zig_repo
        _seed_unresolved_call(store, caller, target, line=4)
        # Two ``doIt()`` calls on line 4: one resolves, one errors.
        caller.write_text(
            'const a = @import("./a.zig");\n'
            "pub fn run() void {\n"
            "    var x = a.A{};\n"
            "    x.doIt(); x.doIt();\n"
            "}\n",
            encoding="utf-8",
        )
        store.commit()

        class _Mixed(_FakeClient):
            _call_count = 0

            def definition(self, file_path, line, character):
                self._call_count += 1
                if self._call_count == 1:
                    raise TimeoutError("simulated timeout")
                loc = self.locations.get((file_path, line, character))
                if loc is None:
                    from code_review_graph.zls_resolver import _NO_DEFINITION
                    return _NO_DEFINITION
                return DefinitionResult(ok=True, location=loc)

        client = _Mixed({
            (str(caller), 3, 6): (str(target), 1),
            (str(caller), 3, 15): (str(target), 1),
        })
        stats = enrich_zls_calls(store, root, client=client)

        assert stats["resolved"] == 0
        assert stats["errors"] >= 1
        row = _rows(store, "CALLS")[0]
        assert row["target_qualified"] == "doIt", (
            "a row with an unconfirmed site must not be rewritten"
        )


class TestMetadataPreservation:
    """Unrelated metadata in the original edge extra survives enrichment."""

    def test_unrelated_keys_are_preserved_on_resolution(self, store, zig_repo):
        root, caller, target = zig_repo
        _seed_unresolved_call(
            store, caller, target,
            extra={
                "receiver": "x",
                "receiver_binding": "unknown",
                "source_context": "caller_run",
                "call_kind": "method",
            },
        )
        client = _FakeClient({(str(caller), 3, 6): (str(target), 1)})

        enrich_zls_calls(store, root, client=client)

        extra = json.loads(_rows(store, "CALLS")[0]["extra"])
        assert extra["source_context"] == "caller_run"
        assert extra["call_kind"] == "method"
        assert extra["resolution_basis"] == "zls"

    def test_unrelated_keys_are_preserved_on_external(self, store, zig_repo):
        root, caller, _target = zig_repo
        _seed_unresolved_call(
            store, caller, caller,
            extra={
                "receiver": "std",
                "receiver_binding": "unknown",
                "source_context": "stdlib_call",
            },
        )
        sdk = Path("/opt/zig/lib/std/Build.zig")
        client = _FakeClient({(str(caller), 3, 6): (str(sdk), 1)})

        enrich_zls_calls(store, root, client=client)

        extra = json.loads(_rows(store, "CALLS")[0]["extra"])
        assert extra["source_context"] == "stdlib_call"
        assert extra["external"] is True
        assert extra["resolution_basis"] == "zls"
