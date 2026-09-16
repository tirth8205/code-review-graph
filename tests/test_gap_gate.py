"""Accuracy tests for the promotion coverage gate.

One test per defect that made the first version of ``scripts/test_gap_gate.py``
unfit to block a promotion:

1. Class nodes never carried ``is_test``, so every ``TestX`` in the suite was
   reported as changed production code with no test.
2. Test-file helpers and fixtures were reported the same way.
3. A ``TESTED_BY`` edge stored under a bare source — the shape a package
   re-export produces — was invisible to the gap scan, so a function with
   three tests read as untested.
4. The report read ``file_path``/``line`` from rows that carry ``file``/
   ``line_start``/``line_end``, printing ``?`` for every location, and paired
   a 50-row table with a footer computed from a 25-row default.
5. With no graph the gate reported zero gaps and exited 0.
6. "A test reaches this function" is also satisfied by a skipped test and by a
   test that asserts nothing.
"""

from __future__ import annotations

import importlib.util
import subprocess
import tempfile
from pathlib import Path

import pytest

from code_review_graph.changes import _nested_in_a_changed_function, analyze_changes
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, get_db_path
from code_review_graph.parser import CodeParser, EdgeInfo, NodeInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "test_gap_gate.py"

_spec = importlib.util.spec_from_file_location("test_gap_gate_module", SCRIPT)
assert _spec is not None and _spec.loader is not None
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


# ---------------------------------------------------------------------------
# 1. the parser marks test classes
# ---------------------------------------------------------------------------


class TestClassTestMarking:
    def _parse(self, tmp_path: Path, rel: str, source: str) -> list[NodeInfo]:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
        nodes, _edges = CodeParser(tmp_path).parse_file(path)
        return nodes

    def test_class_in_test_file_is_marked(self, tmp_path):
        nodes = self._parse(
            tmp_path,
            "tests/test_thing.py",
            "class TestThing:\n"
            "    def test_works(self):\n"
            "        assert True\n",
        )
        classes = [n for n in nodes if n.kind == "Class"]
        assert classes, "expected a Class node"
        assert all(n.is_test for n in classes)

    def test_fixture_class_in_test_file_is_marked(self, tmp_path):
        """Not only ``TestX``: a helper class in a test file is test code too."""
        nodes = self._parse(
            tmp_path,
            "tests/test_thing.py",
            "class FakeClock:\n"
            "    def now(self):\n"
            "        return 0\n",
        )
        clock = next(n for n in nodes if n.kind == "Class" and n.name == "FakeClock")
        assert clock.is_test

    def test_production_class_named_test_something_is_not_marked(self, tmp_path):
        """``^Test`` is not evidence on its own — it would hide real code."""
        nodes = self._parse(
            tmp_path,
            "src/harness.py",
            "class TestHarness:\n"
            "    def run(self):\n"
            "        return 1\n",
        )
        harness = next(n for n in nodes if n.kind == "Class")
        assert harness.name == "TestHarness"
        assert not harness.is_test

    def test_junit_class_outside_a_test_path_is_marked(self, tmp_path):
        """An explicit framework annotation is evidence wherever it lives."""
        nodes = self._parse(
            tmp_path,
            "src/main/java/Sample.java",
            "@Test\n"
            "public class Sample {\n"
            "    void run() {}\n"
            "}\n",
        )
        sample = next(n for n in nodes if n.kind == "Class")
        assert sample.is_test


# ---------------------------------------------------------------------------
# 2 + 3. the gap scan
# ---------------------------------------------------------------------------


class TestGapScanAccuracy:
    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = GraphStore(self.tmp.name)

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def _node(self, kind: str, name: str, path: str, *, is_test: bool = False,
              parent: str | None = None, start: int = 1, end: int = 10) -> None:
        self.store.upsert_node(
            NodeInfo(
                kind=kind,
                name=name,
                file_path=path,
                line_start=start,
                line_end=end,
                language="python",
                parent_name=parent,
                is_test=is_test,
            ),
            file_hash="h",
        )
        self.store.commit()

    def _edge(self, kind: str, source: str, target: str, path: str) -> None:
        self.store.upsert_edge(
            EdgeInfo(kind=kind, source=source, target=target, file_path=path, line=1)
        )
        self.store.commit()

    def _gaps(self, changed_files: list[str], ranges: dict) -> set[str]:
        result = analyze_changes(
            self.store, changed_files=changed_files, changed_ranges=ranges,
        )
        return {g["name"] for g in result["test_gaps"]}

    def test_helpers_in_a_test_file_are_not_gaps(self):
        """A conftest fixture is not changed production code with no test."""
        self._node("Function", "built_repo", "tests/conftest.py", start=1, end=10)
        self._node("Function", "_git", "tests/conftest.py", start=12, end=20)
        self._node("Function", "handler", "app.py", start=1, end=10)

        gaps = self._gaps(
            ["tests/conftest.py", "app.py"],
            {"tests/conftest.py": [(1, 20)], "app.py": [(1, 10)]},
        )
        assert gaps == {"handler"}

    def test_unmarked_test_class_is_not_a_gap(self):
        """Belt and braces: even an old graph row with is_test=0 is skipped."""
        self._node("Class", "TestChanges", "tests/test_changes.py", is_test=False)
        gaps = self._gaps(
            ["tests/test_changes.py"], {"tests/test_changes.py": [(1, 10)]},
        )
        assert gaps == set()

    def test_bare_source_tested_by_edge_counts_as_coverage(self):
        """The shape a package re-export produces must not read as untested.

        ``from pkg import detect_changes_func`` records an import of
        ``pkg/__init__.py`` while the definition lives in ``pkg/review.py``,
        so endpoint resolution leaves the ``TESTED_BY`` edge under the bare
        name. Looking it up by qualified name alone finds nothing.
        """
        self._node("Function", "detect_changes_func", "pkg/review.py")
        self._node("Test", "test_detect", "tests/test_review.py", is_test=True)
        # The test file imports the package, which re-exports from review.py.
        self._edge("IMPORTS_FROM", "tests/test_review.py", "pkg/__init__.py",
                   "tests/test_review.py")
        self._edge("IMPORTS_FROM", "pkg/__init__.py", "pkg/review.py",
                   "pkg/__init__.py")
        self._edge("TESTED_BY", "detect_changes_func",
                   "tests/test_review.py::test_detect", "tests/test_review.py")

        gaps = self._gaps(["pkg/review.py"], {"pkg/review.py": [(1, 10)]})
        assert gaps == set()

    def test_bare_source_without_import_evidence_is_still_a_gap(self):
        """The fallback stays evidence-gated: a name match alone proves nothing."""
        self._node("Function", "detect_changes_func", "pkg/review.py")
        self._node("Test", "test_detect", "tests/test_review.py", is_test=True)
        self._edge("TESTED_BY", "detect_changes_func",
                   "tests/test_review.py::test_detect", "tests/test_review.py")

        gaps = self._gaps(["pkg/review.py"], {"pkg/review.py": [(1, 10)]})
        assert gaps == {"detect_changes_func"}

    def test_closure_is_reported_once_against_its_enclosing_function(self):
        """Nothing outside ``outer`` can call ``walk``; one row, not two."""
        self._node("Function", "outer", "app.py", start=10, end=60)
        self._node("Function", "walk", "app.py", start=20, end=30)

        gaps = self._gaps(["app.py"], {"app.py": [(10, 60)]})
        assert gaps == {"outer"}

    def test_a_method_is_not_treated_as_nested_in_its_class(self):
        """Only a *function* encloses; a class must not swallow its methods."""
        self._node("Class", "Service", "app.py", start=1, end=40)
        self._node("Function", "handle", "app.py", parent="Service", start=5, end=12)

        gaps = self._gaps(["app.py"], {"app.py": [(1, 40)]})
        assert gaps == {"Service", "handle"}

    def test_closure_stays_when_its_parent_is_not_in_the_change(self):
        """Suppression only applies when the parent is itself being reported."""
        self._node("Function", "outer", "app.py", start=10, end=60)
        self._node("Function", "walk", "app.py", start=20, end=30)
        nodes = self.store.get_nodes_by_file("app.py")
        walk = next(n for n in nodes if n.name == "walk")

        assert _nested_in_a_changed_function(nodes) == {walk.qualified_name}
        assert _nested_in_a_changed_function([walk]) == set()

    def test_untested_function_is_still_reported(self):
        self._node("Function", "lonely", "app.py")
        gaps = self._gaps(["app.py"], {"app.py": [(1, 10)]})
        assert gaps == {"lonely"}

    def test_gap_rows_carry_file_and_line_span(self):
        self._node("Function", "lonely", "app.py", start=7, end=19)
        result = analyze_changes(
            self.store, changed_files=["app.py"], changed_ranges={"app.py": [(7, 19)]},
        )
        row = result["test_gaps"][0]
        assert row["file"] == "app.py"
        assert (row["line_start"], row["line_end"]) == (7, 19)


# ---------------------------------------------------------------------------
# 4. the report renders what the rows actually contain
# ---------------------------------------------------------------------------


class TestReportRendering:
    def _rows(self, count: int) -> list[dict]:
        return [
            {
                "name": f"f{i}",
                "qualified_name": f"app.py::f{i}",
                "file": "app.py",
                "line_start": i + 1,
                "line_end": i + 5,
            }
            for i in range(count)
        ]

    def test_location_columns_are_filled(self, tmp_path):
        report = gate._render(
            self._rows(1), total=1, changed=1, base="origin/main",
            repo=tmp_path, max_rows=50, weak=[],
        )
        assert "| `f0` | `app.py` | 1-5 | not at all |" in report
        assert "?" not in report

    def test_footer_counts_the_rows_it_did_not_render(self, tmp_path):
        report = gate._render(
            self._rows(50), total=118, changed=9, base="origin/main",
            repo=tmp_path, max_rows=50, weak=[],
        )
        assert "_68 more not listed_" in report
        rendered = sum(1 for line in report.splitlines()
                       if line.startswith("| `f"))
        assert rendered == 50
        assert rendered + 68 == 118

    def test_no_footer_when_everything_is_shown(self, tmp_path):
        report = gate._render(
            self._rows(3), total=3, changed=1, base="origin/main",
            repo=tmp_path, max_rows=50, weak=[],
        )
        assert "more not listed" not in report

    def test_weak_coverage_is_a_separate_section(self, tmp_path):
        report = gate._render(
            self._rows(1), total=1, changed=1, base="origin/main",
            repo=tmp_path, max_rows=50,
            weak=[{"name": "quiet", "file": "app.py", "reason": "no assertion"}],
        )
        assert "Covered, but by a test that checks nothing" in report
        assert "| `quiet` | `app.py` | no assertion |" in report


# ---------------------------------------------------------------------------
# 6. weak coverage detection
# ---------------------------------------------------------------------------


class TestWeakCoverageDetection:
    def _make(self, tmp_path: Path, source: str, decorators=None):
        path = tmp_path / "test_sample.py"
        path.write_text(source, encoding="utf-8")
        extra = {"decorators": decorators} if decorators else {}
        node = NodeInfo(
            kind="Test",
            name="test_x",
            file_path=str(path),
            line_start=1,
            line_end=len(source.splitlines()),
            language="python",
            is_test=True,
            extra=extra,
        )
        tmp_db = tmp_path / "g.db"
        store = GraphStore(str(tmp_db))
        try:
            store.upsert_node(node, file_hash="h")
            store.commit()
            return store.get_node(f"{path}::test_x") or store.get_all_nodes()[0]
        finally:
            store.close()

    def test_assertionless_test_is_weak(self, tmp_path):
        node = self._make(tmp_path, "def test_x():\n    my_func()\n")
        assert gate._test_is_weak(node, {}) == "no assertion"

    def test_asserting_test_is_not_weak(self, tmp_path):
        node = self._make(tmp_path, "def test_x():\n    assert my_func() == 1\n")
        assert gate._test_is_weak(node, {}) is None

    def test_unittest_style_assertion_counts(self, tmp_path):
        node = self._make(
            tmp_path, "def test_x(self):\n    self.assertEqual(my_func(), 1)\n",
        )
        assert gate._test_is_weak(node, {}) is None

    def test_skipped_test_is_weak(self, tmp_path):
        node = self._make(
            tmp_path,
            "def test_x():\n    assert my_func() == 1\n",
            decorators=['pytest.mark.skip(reason="broken")'],
        )
        assert gate._test_is_weak(node, {}) == "skipped"

    def test_conditional_skip_is_not_called_skipped(self, tmp_path):
        """``skipif`` runs wherever its condition is false — not dead."""
        node = self._make(
            tmp_path,
            "def test_x():\n    assert my_func() == 1\n",
            decorators=['pytest.mark.skipif(not IGRAPH, reason="no igraph")'],
        )
        assert gate._test_is_weak(node, {}) is None


# ---------------------------------------------------------------------------
# 5. the gate fails closed
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=repo, check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


@pytest.fixture()
def gated_repo(tmp_path):
    """A two-commit git repo: a base commit, then one changed source file."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "app.py").write_text("def handler():\n    return 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "branch", "base-ref")
    (repo / "app.py").write_text(
        "def handler():\n    return 1\n\n\ndef added():\n    return 2\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "change")
    return repo


def _run_gate(repo: Path, tmp_path: Path, *extra: str) -> int:
    return gate.main([
        "--repo", str(repo),
        "--base", "base-ref",
        "--out", str(tmp_path / "gaps.md"),
        *extra,
    ])


def test_gate_fails_closed_when_the_graph_is_missing(gated_repo, tmp_path, capsys):
    """Before: 135 changed files, zero gaps reported, exit 0."""
    assert not get_db_path(gated_repo, read_only=True).exists()
    assert _run_gate(gated_repo, tmp_path) == 2
    assert "cannot answer" in capsys.readouterr().err


def test_gate_fails_closed_when_the_graph_is_stale(gated_repo, tmp_path, capsys):
    store = GraphStore(get_db_path(gated_repo))
    try:
        full_build(gated_repo, store)
    finally:
        store.close()
    assert _run_gate(gated_repo, tmp_path) == 1  # graph current: finds the gap

    (gated_repo / "app.py").write_text(
        "def handler():\n    return 99\n", encoding="utf-8",
    )
    assert _run_gate(gated_repo, tmp_path) == 2
    assert "different revision" in capsys.readouterr().err


def test_gate_reports_nothing_to_analyse_without_changes(gated_repo, tmp_path):
    assert gate.main([
        "--repo", str(gated_repo), "--base", "HEAD",
        "--out", str(tmp_path / "gaps.md"),
    ]) == 0


def test_allow_missing_graph_opt_out_still_warns(gated_repo, tmp_path, capsys):
    assert _run_gate(gated_repo, tmp_path, "--allow-missing-graph") == 0
    assert "warning:" in capsys.readouterr().err
