"""Tests for graph-powered refactoring operations."""

import tempfile
import threading
import time
from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.parser import EdgeInfo, NodeInfo
from code_review_graph.refactor import (
    REFACTOR_EXPIRY_SECONDS,
    _pending_refactors,
    _refactor_lock,
    apply_refactor,
    find_dead_code,
    rename_preview,
    suggest_refactorings,
)


class TestRenamePreview:
    """Tests for rename_preview."""

    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.store = GraphStore(self.tmp.name)
        self._seed()

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)
        # Clean up pending refactors.
        with _refactor_lock:
            _pending_refactors.clear()

    def _seed(self):
        """Seed the store with test data for rename tests."""
        # File nodes
        self.store.upsert_node(NodeInfo(
            kind="File", name="/repo/utils.py", file_path="/repo/utils.py",
            line_start=1, line_end=50, language="python",
        ))
        self.store.upsert_node(NodeInfo(
            kind="File", name="/repo/main.py", file_path="/repo/main.py",
            line_start=1, line_end=30, language="python",
        ))
        # Function to rename
        self.store.upsert_node(NodeInfo(
            kind="Function", name="helper", file_path="/repo/utils.py",
            line_start=10, line_end=20, language="python",
        ))
        # Caller function
        self.store.upsert_node(NodeInfo(
            kind="Function", name="run", file_path="/repo/main.py",
            line_start=5, line_end=15, language="python",
        ))
        # CALLS edge: run -> helper
        self.store.upsert_edge(EdgeInfo(
            kind="CALLS", source="/repo/main.py::run",
            target="/repo/utils.py::helper", file_path="/repo/main.py", line=10,
        ))
        # IMPORTS_FROM edge: main.py imports helper
        self.store.upsert_edge(EdgeInfo(
            kind="IMPORTS_FROM", source="/repo/main.py",
            target="/repo/utils.py::helper", file_path="/repo/main.py", line=1,
        ))
        self.store.commit()

    def test_rename_preview_returns_edits_with_refactor_id(self):
        """rename_preview returns a dict with refactor_id and edits."""
        result = rename_preview(self.store, "helper", "new_helper")
        assert result is not None
        assert "refactor_id" in result
        assert len(result["refactor_id"]) == 8
        assert result["type"] == "rename"
        assert result["old_name"] == "helper"
        assert result["new_name"] == "new_helper"
        assert isinstance(result["edits"], list)
        assert len(result["edits"]) > 0
        assert "stats" in result
        assert result["stats"]["high"] > 0

    def test_rename_finds_callers(self):
        """rename_preview finds definition + call sites."""
        result = rename_preview(self.store, "helper", "new_helper")
        assert result is not None
        edits = result["edits"]
        # Should have at least: 1 definition + 1 call + 1 import = 3
        assert len(edits) >= 3
        files = {e["file"] for e in edits}
        assert "/repo/utils.py" in files  # definition
        assert "/repo/main.py" in files   # call site + import site

    def test_rename_not_found(self):
        """rename_preview returns None if symbol not found."""
        result = rename_preview(self.store, "nonexistent_function", "new_name")
        assert result is None

    def test_rename_stores_in_pending(self):
        """rename_preview stores the preview in _pending_refactors."""
        result = rename_preview(self.store, "helper", "new_helper")
        assert result is not None
        rid = result["refactor_id"]
        with _refactor_lock:
            assert rid in _pending_refactors


class TestFindDeadCode:
    """Tests for find_dead_code."""

    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.store = GraphStore(self.tmp.name)
        self._seed()

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def _seed(self):
        """Seed with a mix of used and unused functions."""
        # File
        self.store.upsert_node(NodeInfo(
            kind="File", name="/repo/app.py", file_path="/repo/app.py",
            line_start=1, line_end=100, language="python",
        ))
        # A function that IS called
        self.store.upsert_node(NodeInfo(
            kind="Function", name="used_func", file_path="/repo/app.py",
            line_start=10, line_end=20, language="python",
        ))
        # A function that is NOT called (dead code)
        self.store.upsert_node(NodeInfo(
            kind="Function", name="dead_func", file_path="/repo/app.py",
            line_start=30, line_end=40, language="python",
        ))
        # An entry point function (should be excluded)
        self.store.upsert_node(NodeInfo(
            kind="Function", name="main", file_path="/repo/app.py",
            line_start=50, line_end=60, language="python",
        ))
        # A test function (should be excluded)
        self.store.upsert_node(NodeInfo(
            kind="Test", name="test_something", file_path="/repo/test_app.py",
            line_start=1, line_end=10, language="python", is_test=True,
        ))

        # Caller for used_func
        self.store.upsert_node(NodeInfo(
            kind="Function", name="caller", file_path="/repo/app.py",
            line_start=70, line_end=80, language="python",
        ))
        self.store.upsert_edge(EdgeInfo(
            kind="CALLS", source="/repo/app.py::caller",
            target="/repo/app.py::used_func", file_path="/repo/app.py", line=75,
        ))
        self.store.commit()

    def test_find_dead_code(self):
        """find_dead_code detects unreferenced functions."""
        dead = find_dead_code(self.store)
        dead_names = {d["name"] for d in dead}
        assert "dead_func" in dead_names

    def test_find_dead_code_excludes_called(self):
        """find_dead_code does NOT include functions with callers."""
        dead = find_dead_code(self.store)
        dead_names = {d["name"] for d in dead}
        assert "used_func" not in dead_names

    def test_find_dead_code_excludes_entry_points(self):
        """Entry points (like 'main') are not flagged as dead code."""
        dead = find_dead_code(self.store)
        dead_names = {d["name"] for d in dead}
        assert "main" not in dead_names

    def test_find_dead_code_excludes_tests(self):
        """Test nodes are not flagged as dead code."""
        dead = find_dead_code(self.store)
        dead_names = {d["name"] for d in dead}
        assert "test_something" not in dead_names

    def test_find_dead_code_kind_filter(self):
        """kind filter restricts results."""
        dead = find_dead_code(self.store, kind="Class")
        # We have no Class nodes, so should be empty
        assert len(dead) == 0

    def test_find_dead_code_file_pattern(self):
        """file_pattern filter works."""
        dead = find_dead_code(self.store, file_pattern="nonexistent")
        assert len(dead) == 0


class TestSuggestRefactorings:
    """Tests for suggest_refactorings."""

    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.store = GraphStore(self.tmp.name)
        self._seed()

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def _seed(self):
        """Seed with dead code to generate suggestions."""
        self.store.upsert_node(NodeInfo(
            kind="File", name="/repo/lib.py", file_path="/repo/lib.py",
            line_start=1, line_end=50, language="python",
        ))
        # Unreferenced function -> removal suggestion
        self.store.upsert_node(NodeInfo(
            kind="Function", name="orphan_func", file_path="/repo/lib.py",
            line_start=10, line_end=20, language="python",
        ))
        self.store.commit()

    def test_suggest_refactorings(self):
        """suggest_refactorings returns a list of suggestions."""
        suggestions = suggest_refactorings(self.store)
        assert isinstance(suggestions, list)
        # Should have at least the dead-code removal suggestion
        assert len(suggestions) >= 1
        types = {s["type"] for s in suggestions}
        assert "remove" in types

    def test_suggestion_structure(self):
        """Each suggestion has the required fields."""
        suggestions = suggest_refactorings(self.store)
        for s in suggestions:
            assert "type" in s
            assert "description" in s
            assert "symbols" in s
            assert "rationale" in s
            assert s["type"] in ("move", "remove")


class TestApplyRefactor:
    """Tests for apply_refactor."""

    def setup_method(self):
        with _refactor_lock:
            _pending_refactors.clear()

    def teardown_method(self):
        with _refactor_lock:
            _pending_refactors.clear()

    def test_apply_refactor_validates_id(self):
        """apply_refactor rejects nonexistent refactor_id."""
        # Use a real temp dir as repo_root (needs .git or .code-review-graph)
        tmp_dir = Path(tempfile.mkdtemp())
        (tmp_dir / ".git").mkdir()
        try:
            result = apply_refactor("nonexistent_id", tmp_dir)
            assert result["status"] == "error"
            assert "not found" in result["error"].lower() or "expired" in result["error"].lower()
        finally:
            (tmp_dir / ".git").rmdir()
            tmp_dir.rmdir()

    def test_apply_refactor_expiry(self):
        """apply_refactor rejects expired previews."""
        tmp_dir = Path(tempfile.mkdtemp())
        (tmp_dir / ".git").mkdir()
        try:
            # Insert a preview that is already expired.
            rid = "expired1"
            with _refactor_lock:
                _pending_refactors[rid] = {
                    "refactor_id": rid,
                    "type": "rename",
                    "old_name": "old",
                    "new_name": "new",
                    "edits": [],
                    "stats": {"high": 0, "medium": 0, "low": 0},
                    "created_at": time.time() - REFACTOR_EXPIRY_SECONDS - 10,
                }
            result = apply_refactor(rid, tmp_dir)
            assert result["status"] == "error"
            assert "expired" in result["error"].lower()
        finally:
            (tmp_dir / ".git").rmdir()
            tmp_dir.rmdir()

    def test_apply_refactor_path_traversal(self):
        """apply_refactor blocks edits outside repo root."""
        tmp_dir = Path(tempfile.mkdtemp())
        (tmp_dir / ".git").mkdir()
        try:
            rid = "traversal"
            with _refactor_lock:
                _pending_refactors[rid] = {
                    "refactor_id": rid,
                    "type": "rename",
                    "old_name": "old",
                    "new_name": "new",
                    "edits": [{
                        "file": "/etc/passwd",
                        "line": 1,
                        "old": "old",
                        "new": "new",
                        "confidence": "high",
                    }],
                    "stats": {"high": 1, "medium": 0, "low": 0},
                    "created_at": time.time(),
                }
            result = apply_refactor(rid, tmp_dir)
            assert result["status"] == "error"
            assert "outside repo root" in result["error"].lower()
        finally:
            (tmp_dir / ".git").rmdir()
            tmp_dir.rmdir()

    def test_apply_refactor_success(self):
        """apply_refactor applies string replacement to a real file."""
        tmp_dir = Path(tempfile.mkdtemp())
        (tmp_dir / ".git").mkdir()
        target_file = tmp_dir / "example.py"
        target_file.write_text("def old_func():\n    pass\n", encoding="utf-8")
        try:
            rid = "success1"
            with _refactor_lock:
                _pending_refactors[rid] = {
                    "refactor_id": rid,
                    "type": "rename",
                    "old_name": "old_func",
                    "new_name": "new_func",
                    "edits": [{
                        "file": str(target_file),
                        "line": 1,
                        "old": "old_func",
                        "new": "new_func",
                        "confidence": "high",
                    }],
                    "stats": {"high": 1, "medium": 0, "low": 0},
                    "created_at": time.time(),
                }
            result = apply_refactor(rid, tmp_dir)
            assert result["status"] == "ok"
            assert result["edits_applied"] == 1
            assert len(result["files_modified"]) == 1
            # Verify file content was changed.
            content = target_file.read_text(encoding="utf-8")
            assert "new_func" in content
            assert "old_func" not in content
        finally:
            target_file.unlink(missing_ok=True)
            (tmp_dir / ".git").rmdir()
            tmp_dir.rmdir()


class TestPendingRefactorsThreadSafe:
    """Tests for thread-safety of the pending refactors storage."""

    def test_pending_refactors_thread_safe(self):
        """The _refactor_lock is a threading.Lock instance."""
        assert isinstance(_refactor_lock, type(threading.Lock()))

    def test_concurrent_access(self):
        """Multiple threads can safely access _pending_refactors."""
        results = []

        def writer(rid: str):
            with _refactor_lock:
                _pending_refactors[rid] = {
                    "refactor_id": rid,
                    "created_at": time.time(),
                }
                results.append(rid)

        threads = [threading.Thread(target=writer, args=(f"t{i}",)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        with _refactor_lock:
            assert len(results) == 10
            assert len(_pending_refactors) >= 10
            # Clean up
            _pending_refactors.clear()


class TestFindDeadCodeWithReferences:
    """Tests for REFERENCES-aware dead code detection."""

    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.store = GraphStore(self.tmp.name)
        self._seed()

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def _seed(self):
        """Seed with functions that have REFERENCES edges (map dispatch pattern)."""
        # File
        self.store.upsert_node(NodeInfo(
            kind="File", name="/repo/handlers.ts", file_path="/repo/handlers.ts",
            line_start=1, line_end=100, language="typescript",
        ))
        # A function referenced in a map (should NOT be dead)
        self.store.upsert_node(NodeInfo(
            kind="Function", name="handleCreate", file_path="/repo/handlers.ts",
            line_start=10, line_end=20, language="typescript",
        ))
        # A function with CALLS edge (should NOT be dead)
        self.store.upsert_node(NodeInfo(
            kind="Function", name="calledFunc", file_path="/repo/handlers.ts",
            line_start=30, line_end=40, language="typescript",
        ))
        # A truly dead function (no edges at all)
        self.store.upsert_node(NodeInfo(
            kind="Function", name="deadFunc", file_path="/repo/handlers.ts",
            line_start=50, line_end=60, language="typescript",
        ))
        # Caller
        self.store.upsert_node(NodeInfo(
            kind="Function", name="dispatch", file_path="/repo/handlers.ts",
            line_start=70, line_end=80, language="typescript",
        ))
        # REFERENCES edge: dispatch -> handleCreate (map dispatch pattern)
        self.store.upsert_edge(EdgeInfo(
            kind="REFERENCES", source="/repo/handlers.ts::dispatch",
            target="/repo/handlers.ts::handleCreate",
            file_path="/repo/handlers.ts", line=75,
        ))
        # CALLS edge: dispatch -> calledFunc
        self.store.upsert_edge(EdgeInfo(
            kind="CALLS", source="/repo/handlers.ts::dispatch",
            target="/repo/handlers.ts::calledFunc",
            file_path="/repo/handlers.ts", line=76,
        ))
        self.store.commit()

    def test_referenced_function_not_dead(self):
        """Functions with REFERENCES edges should NOT be flagged as dead code."""
        dead = find_dead_code(self.store)
        dead_names = {d["name"] for d in dead}
        assert "handleCreate" not in dead_names

    def test_called_function_not_dead(self):
        """Functions with CALLS edges remain excluded (existing behavior)."""
        dead = find_dead_code(self.store)
        dead_names = {d["name"] for d in dead}
        assert "calledFunc" not in dead_names

    def test_truly_dead_function_still_reported(self):
        """Functions with no edges at all should still be flagged as dead code."""
        dead = find_dead_code(self.store)
        dead_names = {d["name"] for d in dead}
        assert "deadFunc" in dead_names

    def test_only_references_edge_sufficient(self):
        """A function with ONLY a REFERENCES edge (no CALLS/IMPORTS) is not dead."""
        dead = find_dead_code(self.store)
        dead_names = {d["name"] for d in dead}
        # handleCreate has only a REFERENCES edge, no CALLS targeting it
        assert "handleCreate" not in dead_names
