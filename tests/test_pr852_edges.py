"""Edge-case tests for the changed_files absolute-path remap in analyze_changes (#848).

Stresses the remap beyond the PR's regression test: absolute passthrough
(the MCP path), mixed relative/absolute input, backslash separators,
dot segments, trailing-slash repo_root, unicode file names, the
no-ranges node fallback, empty input, caller-list immutability, and
IN-clause batching with >450 changed files.
"""

import tempfile
from pathlib import Path

from code_review_graph.changes import analyze_changes
from code_review_graph.flows import store_flows, trace_flows
from code_review_graph.graph import GraphStore
from code_review_graph.parser import EdgeInfo, NodeInfo


def _repo_root() -> str:
    """A fake absolute repo root valid on the current OS."""
    return "/repo" if not Path("C:/").exists() else "C:/repo"


class TestPR852Edges:
    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = GraphStore(self.tmp.name)
        self.root = _repo_root()

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def _add_func(self, name: str, path: str, line_start: int = 1, line_end: int = 10) -> int:
        node = NodeInfo(
            kind="Function",
            name=name,
            file_path=path,
            line_start=line_start,
            line_end=line_end,
            language="python",
        )
        nid = self.store.upsert_node(node, file_hash="abc")
        self.store.commit()
        return nid

    def _add_call(self, source_qn: str, target_qn: str, path: str) -> None:
        edge = EdgeInfo(
            kind="CALLS", source=source_qn, target=target_qn, file_path=path, line=5,
        )
        self.store.upsert_edge(edge)
        self.store.commit()

    def _build_flow(self, sub: str = "services.py") -> str:
        """One handler -> service flow stored under absolute paths."""
        routes = f"{self.root}/routes.py"
        service = f"{self.root}/{sub}"
        self._add_func("handler", routes)
        self._add_func("service", service)
        self._add_call(f"{routes}::handler", f"{service}::service", routes)
        store_flows(self.store, trace_flows(self.store))
        return service

    # -- absolute passthrough (the MCP path) --

    def test_absolute_changed_files_pass_through_unchanged(self):
        service = self._build_flow()
        result = analyze_changes(
            self.store,
            changed_files=[service],
            changed_ranges={service: [(1, 10)]},
            repo_root=self.root,
        )
        assert len(result["affected_flows"]) >= 1
        assert "1 changed file(s)" in result["summary"]

    def test_mixed_relative_and_absolute_inputs(self):
        service = self._build_flow()
        routes = f"{self.root}/routes.py"
        result = analyze_changes(
            self.store,
            changed_files=["services.py", routes],
            changed_ranges={service: [(1, 10)], routes: [(1, 10)]},
            repo_root=self.root,
        )
        assert result["affected_flows"]
        assert "2 changed file(s)" in result["summary"]

    # -- separator and segment forms --

    def test_backslash_relative_path_matches(self):
        """A Windows-style relative path still hits the POSIX graph identity."""
        service = self._build_flow(sub="pkg/services.py")
        result = analyze_changes(
            self.store,
            changed_files=["pkg\\services.py"],
            changed_ranges={service: [(1, 10)]},
            repo_root=self.root,
        )
        assert result["affected_flows"]

    def test_leading_dot_segment_is_collapsed(self):
        service = self._build_flow()
        result = analyze_changes(
            self.store,
            changed_files=["./services.py"],
            changed_ranges={service: [(1, 10)]},
            repo_root=self.root,
        )
        assert result["affected_flows"]

    def test_trailing_slash_repo_root(self):
        service = self._build_flow()
        result = analyze_changes(
            self.store,
            changed_files=["services.py"],
            changed_ranges={service: [(1, 10)]},
            repo_root=self.root + "/",
        )
        assert result["affected_flows"]

    def test_unicode_file_names(self):
        service = self._build_flow(sub="ünïcode/файл.py")
        result = analyze_changes(
            self.store,
            changed_files=["ünïcode/файл.py"],
            changed_ranges={service: [(1, 10)]},
            repo_root=self.root,
        )
        assert result["affected_flows"]

    # -- the no-ranges node fallback (covered by #852, not #837) --

    def test_no_ranges_fallback_finds_nodes_with_relative_paths(self):
        """With empty changed_ranges the per-file node fallback must also remap."""
        self._build_flow()
        result = analyze_changes(
            self.store,
            changed_files=["services.py"],
            changed_ranges={},
            repo_root=self.root,
        )
        assert len(result["changed_functions"]) == 1
        assert result["changed_functions"][0]["name"] == "service"
        assert result["affected_flows"]

    # -- degenerate and hostile input --

    def test_empty_changed_files(self):
        self._build_flow()
        result = analyze_changes(
            self.store, changed_files=[], changed_ranges={}, repo_root=self.root,
        )
        assert result["affected_flows"] == []
        assert result["changed_functions"] == []

    def test_caller_list_is_not_mutated(self):
        """CLI reuses its relative list after the call (estimate_file_tokens)."""
        service = self._build_flow()
        changed = ["services.py"]
        analyze_changes(
            self.store,
            changed_files=changed,
            changed_ranges={service: [(1, 10)]},
            repo_root=self.root,
        )
        assert changed == ["services.py"]

    def test_missing_files_do_not_crash_or_match(self):
        self._build_flow()
        result = analyze_changes(
            self.store,
            changed_files=["nonexistent.py", "also/missing.py"],
            changed_ranges={},
            repo_root=self.root,
        )
        assert result["changed_functions"] == []
        assert result["affected_flows"] == []

    # -- scale: exercise the 450-item IN-clause batching --

    def test_flow_found_when_match_lands_in_second_batch(self):
        service = self._build_flow()
        filler = [f"filler/mod_{i:04d}.py" for i in range(500)]
        # The only real file sorts after the filler so it lands in batch 2.
        changed = filler + ["services.py"]
        result = analyze_changes(
            self.store,
            changed_files=changed,
            changed_ranges={service: [(1, 10)]},
            repo_root=self.root,
        )
        assert result["affected_flows"]
        assert "501 changed file(s)" in result["summary"]
