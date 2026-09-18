"""Test-file detection and its effect on the untested-code report.

Test helpers, fixtures and test classes are test code. The parser must mark
every node it emits from a test file with ``is_test``, and ``analyze_changes``
must keep test-file nodes out of ``test_gaps`` even when the stored row says
``is_test = 0`` (a graph built before that parser fix).

Originating PR: #1014, whose review comment listed
``tests/test_upgrade_path.py::_check``, ``_base_env``, ``_venv_python`` and
``_venv_script`` as test gaps.
"""

import tempfile
from pathlib import Path

import pytest

from code_review_graph.changes import analyze_changes
from code_review_graph.graph import GraphStore
from code_review_graph.parser import CodeParser, NodeInfo, is_test_file

# ---------------------------------------------------------------------------
# is_test_file: per-language conventions
# ---------------------------------------------------------------------------

ROOT = "/repo"

TEST_PATHS = [
    # Python
    "/repo/tests/helpers.py",
    "/repo/tests/unit/support.py",
    "/repo/test_upgrade_path.py",
    "/repo/pkg/test_thing.py",
    "/repo/pkg/thing_test.py",
    "/repo/conftest.py",
    "/repo/tests/conftest.py",
    # JavaScript / TypeScript
    "/repo/src/button.test.ts",
    "/repo/src/button.test.tsx",
    "/repo/src/button.spec.ts",
    "/repo/src/button.spec.js",
    "/repo/src/__tests__/button.ts",
    # Go
    "/repo/pkg/server_test.go",
    # Java / Kotlin
    "/repo/src/test/java/com/acme/Thing.java",
    "/repo/src/test/kotlin/com/acme/Thing.kt",
    "/repo/src/main/java/com/acme/ThingTest.java",
    "/repo/src/main/java/com/acme/ThingTests.java",
    "/repo/src/main/kotlin/com/acme/ThingTest.kt",
    # Ruby
    "/repo/spec/models/user_spec.rb",
    "/repo/spec/spec_helper.rb",
    "/repo/lib/user_spec.rb",
    "/repo/lib/user_test.rb",
    # C#
    "/repo/src/OrderTests.cs",
    "/repo/src/OrderTest.cs",
    # PHP
    "/repo/src/OrderServiceTest.php",
]

PRODUCTION_PATHS = [
    "/repo/code_review_graph/changes.py",
    "/repo/src/button.ts",
    "/repo/src/latest/index.ts",
    "/repo/pkg/server.go",
    "/repo/src/main/java/com/acme/Thing.java",
    "/repo/lib/user.rb",
    "/repo/src/Order.cs",
    # "contest" is not "test": the directory pattern must respect path
    # boundaries rather than matching anywhere in the string.
    "/repo/contests/runner.py",
    "/repo/src/greatest.py",
    # An A/B testing feature is not a test suite: the "Test" suffix has to
    # sit on a class-name boundary.
    "/repo/src/ABTest.php",
    "/repo/src/ABTest.cs",
    "/repo/src/ABTest.java",
    # "specs/" holds design documents and generated API specs as often as it
    # holds RSpec files. Only a Ruby source file under it is evidence.
    "/repo/specs/openapi.yaml",
    "/repo/specs/billing-design.md",
    "/repo/spec/openapi.json",
    # A shared library published from test-utils/ is production code. Only
    # dead-code detection opts into that directory.
    "/repo/test-utils/index.ts",
    "/repo/packages/test_utils/render.ts",
]


@pytest.mark.parametrize("path", TEST_PATHS)
def test_is_test_file_recognises_test_conventions(path):
    assert is_test_file(path, ROOT) is True
    assert is_test_file(path.replace("/", "\\"), ROOT) is True


@pytest.mark.parametrize("path", PRODUCTION_PATHS)
def test_is_test_file_rejects_production_paths(path):
    assert is_test_file(path, ROOT) is False
    assert is_test_file(path.replace("/", "\\"), ROOT) is False


@pytest.mark.parametrize("path", TEST_PATHS + PRODUCTION_PATHS)
def test_repo_relative_paths_need_no_root(path):
    """The same answers hold when the caller already has a project path."""
    relative = path[len(ROOT) + 1:]
    assert is_test_file(relative) is is_test_file(path, ROOT)


# ---------------------------------------------------------------------------
# The repository root decides, never the directories above the checkout
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("workspace", ["test", "tests", "spec", "specs"])
@pytest.mark.parametrize("path", PRODUCTION_PATHS + [
    "/code_review_graph/parser.py",
    "/src/index.ts",
])
def test_checkout_under_a_test_named_directory_stays_production(workspace, path):
    """A repository cloned into a directory named "test" is not test code.

    This is the failure the whole feature turns on: ``file_path`` values in
    the graph are absolute, so matching ``tests/`` against them also matches
    a CI workspace or job directory above the checkout. Every production
    symbol in the repository would be suppressed and the untested-code report
    would go silent (#1023).
    """
    relative = path.lstrip("/")
    if relative.startswith("repo/"):
        relative = relative[len("repo/"):]
    root = f"/ci/{workspace}/checkout"
    assert is_test_file(f"{root}/{relative}", root) is False


@pytest.mark.parametrize("workspace", ["test", "tests", "spec", "specs"])
def test_checkout_under_a_test_named_directory_still_finds_its_own_tests(workspace):
    root = f"/ci/{workspace}/checkout"
    assert is_test_file(f"{root}/tests/helpers.py", root) is True
    assert is_test_file(f"{root}/src/__tests__/button.ts", root) is True


def test_absolute_path_without_a_root_keeps_filename_rules_only():
    """With no root, directory conventions are dropped rather than guessed.

    Dropping them under-reports test files; guessing them would hide
    production gaps for an entire repository, so the safe direction is the
    only one taken.
    """
    assert is_test_file("/unknown/checkout/tests/test_x.py") is True
    assert is_test_file("/unknown/checkout/tests/helpers.py") is False


def test_path_outside_the_repository_root_keeps_filename_rules_only():
    assert is_test_file("/elsewhere/tests/helpers.py", "/repo") is False
    assert is_test_file("/elsewhere/tests/test_x.py", "/repo") is True


def test_root_spelling_differences_are_tolerated():
    """A root given through a symlink or in another case still matches."""
    assert is_test_file("/repo/tests/helpers.py", "/repo/") is True
    assert is_test_file("/Repo/tests/helpers.py", "/repo") is True


# ---------------------------------------------------------------------------
# test-utils/: opt-in, because a shared library is published from there
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [
    "/repo/test-utils/render.ts",
    "/repo/test_utils/render.ts",
    "/repo/packages/core/test-util/render.ts",
])
def test_test_helper_dirs_are_opt_in(path):
    assert is_test_file(path, ROOT) is False
    assert is_test_file(path, ROOT, include_helper_dirs=True) is True


# ---------------------------------------------------------------------------
# A shipped test/ package is production code (the django/test/client.py shape)
# ---------------------------------------------------------------------------


def _make_tree(base: Path, files: list[str]) -> None:
    for rel in files:
        target = base / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")


def test_shipped_test_subpackage_is_production_code():
    """``django/test/client.py`` is imported by users as ``django.test``."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _make_tree(root, [
            "django/__init__.py",
            "django/test/__init__.py",
            "django/test/client.py",
        ])
        assert is_test_file(str(root / "django/test/client.py"), str(root)) is False


def test_shipped_test_subpackage_still_yields_to_a_test_filename():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _make_tree(root, [
            "django/__init__.py",
            "django/test/__init__.py",
            "django/test/test_client.py",
        ])
        assert is_test_file(str(root / "django/test/test_client.py"), str(root)) is True


def test_top_level_test_package_is_still_test_code():
    """A top-level ``test/`` package is an application's own test suite."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _make_tree(root, ["test/__init__.py", "test/helpers.py"])
        assert is_test_file(str(root / "test/helpers.py"), str(root)) is True


def test_test_directory_without_an_init_is_test_code():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _make_tree(root, ["pkg/__init__.py", "pkg/test/helpers.py"])
        assert is_test_file(str(root / "pkg/test/helpers.py"), str(root)) is True


def test_plural_tests_package_is_never_treated_as_shipped():
    """``tests/`` with an ``__init__.py`` is the unittest convention."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _make_tree(root, [
            "pkg/__init__.py",
            "pkg/tests/__init__.py",
            "pkg/tests/helpers.py",
        ])
        assert is_test_file(str(root / "pkg/tests/helpers.py"), str(root)) is True


# ---------------------------------------------------------------------------
# Parser: every node emitted from a test file is test code
# ---------------------------------------------------------------------------

_PY_TEST_SOURCE = '''\
import pytest


@pytest.fixture
def corpus_repo():
    return 1


def _check(value):
    """Private helper, not a test."""
    return value


class TestUpgradePath:
    def test_one(self):
        assert _check(1) == 1

    def helper(self):
        return 2
'''


@pytest.fixture
def parsed_python_test_file():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "tests" / "test_upgrade_path.py"
        path.parent.mkdir(parents=True)
        path.write_text(_PY_TEST_SOURCE, encoding="utf-8")
        nodes, _edges = CodeParser(Path(tmp)).parse_file(path)
        yield {n.name: n for n in nodes}


def test_class_in_test_file_is_marked_as_test(parsed_python_test_file):
    """Class nodes from a test file carry is_test (the #1014 regression)."""
    assert parsed_python_test_file["TestUpgradePath"].is_test is True


def test_private_helper_in_test_file_is_marked_as_test(parsed_python_test_file):
    assert parsed_python_test_file["_check"].is_test is True


def test_fixture_in_test_file_is_marked_as_test(parsed_python_test_file):
    assert parsed_python_test_file["corpus_repo"].is_test is True


def test_non_test_method_in_test_class_is_marked_as_test(parsed_python_test_file):
    assert parsed_python_test_file["helper"].is_test is True


def test_every_node_from_a_test_file_is_marked(parsed_python_test_file):
    unmarked = [n.name for n in parsed_python_test_file.values() if not n.is_test]
    assert unmarked == []


def test_production_file_nodes_are_not_marked_as_test():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "app.py"
        path.write_text(
            "class Service:\n"
            "    def handle(self):\n"
            "        return 1\n"
            "\n"
            "def _private():\n"
            "    return 2\n",
            encoding="utf-8",
        )
        nodes, _ = CodeParser().parse_file(path)
    by_name = {n.name: n for n in nodes}
    assert by_name["Service"].is_test is False
    assert by_name["handle"].is_test is False
    assert by_name["_private"].is_test is False


def test_typescript_spec_file_class_is_marked_as_test():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "button.spec.ts"
        path.write_text(
            "export class Fixture {\n"
            "  build(): number { return 1; }\n"
            "}\n",
            encoding="utf-8",
        )
        nodes, _ = CodeParser().parse_file(path)
    by_name = {n.name: n for n in nodes}
    assert by_name["Fixture"].is_test is True


# ---------------------------------------------------------------------------
# analyze_changes: test-file nodes never reach test_gaps
# ---------------------------------------------------------------------------


class TestGapsExcludeTestFiles:
    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = GraphStore(self.tmp.name)

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def _add(self, kind, name, path, is_test=False, line_start=1, line_end=10):
        self.store.upsert_node(
            NodeInfo(
                kind=kind,
                name=name,
                file_path=path,
                line_start=line_start,
                line_end=line_end,
                language="python",
                is_test=is_test,
            ),
            file_hash="h",
        )
        self.store.commit()

    def test_stale_graph_test_file_rows_are_not_reported(self):
        """A graph built before the parser fix stores is_test = 0 on test
        classes and helpers. The report must still exclude them."""
        self._add("Class", "TestUpgradePath", "tests/test_upgrade_path.py",
                  line_start=1, line_end=20)
        self._add("Function", "_check", "tests/test_upgrade_path.py",
                  line_start=21, line_end=30)
        self._add("Function", "_base_env", "tests/test_upgrade_path.py",
                  line_start=31, line_end=40)
        self._add("Function", "_venv_python", "tests/test_upgrade_path.py",
                  line_start=41, line_end=50)

        result = analyze_changes(
            self.store,
            changed_files=["tests/test_upgrade_path.py"],
            changed_ranges={"tests/test_upgrade_path.py": [(1, 50)]},
        )
        assert result["test_gaps"] == []

    def test_production_gaps_are_still_reported(self):
        self._add("Function", "analyze_changes", "code_review_graph/changes.py",
                  line_start=1, line_end=10)
        self._add("Class", "GraphStore", "code_review_graph/graph.py",
                  line_start=1, line_end=10)

        result = analyze_changes(
            self.store,
            changed_files=[
                "code_review_graph/changes.py",
                "code_review_graph/graph.py",
            ],
            changed_ranges={
                "code_review_graph/changes.py": [(1, 10)],
                "code_review_graph/graph.py": [(1, 10)],
            },
        )
        names = {g["name"] for g in result["test_gaps"]}
        assert names == {"analyze_changes", "GraphStore"}

    def test_mixed_diff_keeps_production_and_drops_test_files(self):
        self._add("Function", "compute_risk_score",
                  "code_review_graph/changes.py", line_start=1, line_end=10)
        self._add("Class", "TestChanges", "tests/test_changes.py",
                  line_start=1, line_end=10)
        self._add("Function", "_add_func", "tests/test_changes.py",
                  line_start=11, line_end=20)

        result = analyze_changes(
            self.store,
            changed_files=[
                "code_review_graph/changes.py",
                "tests/test_changes.py",
            ],
            changed_ranges={
                "code_review_graph/changes.py": [(1, 10)],
                "tests/test_changes.py": [(1, 20)],
            },
        )
        names = {g["name"] for g in result["test_gaps"]}
        assert names == {"compute_risk_score"}


# ---------------------------------------------------------------------------
# review guidance: same rule, same stale-graph guard
# ---------------------------------------------------------------------------


def _graph_node(node_id, kind, name, qualified_name, file_path, is_test=False):
    from code_review_graph.graph import GraphNode

    return GraphNode(
        id=node_id,
        kind=kind,
        name=name,
        qualified_name=qualified_name,
        file_path=file_path,
        line_start=1,
        line_end=10,
        language="python",
        parent_name=None,
        params=None,
        return_type=None,
        is_test=is_test,
        file_hash=None,
        extra={},
    )


def test_review_guidance_ignores_test_file_helpers():
    """Review guidance must not ask for tests for the tests (#1014)."""
    from code_review_graph.tools.review import _generate_review_guidance

    impact = {
        "changed_nodes": [
            _graph_node(1, "Function", "_check",
                        "tests/test_upgrade_path.py::_check",
                        "tests/test_upgrade_path.py"),
            _graph_node(2, "Function", "analyze_changes",
                        "code_review_graph/changes.py::analyze_changes",
                        "code_review_graph/changes.py"),
        ],
        "edges": [],
        "impacted_nodes": [],
        "impacted_files": [],
    }
    guidance = _generate_review_guidance(impact, ["tests/test_upgrade_path.py"])
    assert "_check" not in guidance
    assert "analyze_changes" in guidance
    assert "1 changed function(s) lack test coverage" in guidance


def test_review_guidance_under_a_test_named_checkout_still_warns():
    """The coverage warning must survive a checkout under a "test" directory."""
    from code_review_graph.tools.review import _generate_review_guidance

    root = "/ci/test/checkout"
    impact = {
        "changed_nodes": [
            _graph_node(1, "Function", "_check",
                        f"{root}/tests/test_upgrade_path.py::_check",
                        f"{root}/tests/test_upgrade_path.py"),
            _graph_node(2, "Function", "analyze_changes",
                        f"{root}/code_review_graph/changes.py::analyze_changes",
                        f"{root}/code_review_graph/changes.py"),
        ],
        "edges": [],
        "impacted_nodes": [],
        "impacted_files": [],
    }
    guidance = _generate_review_guidance(
        impact, [f"{root}/code_review_graph/changes.py"], root,
    )
    assert "_check" not in guidance
    assert "1 changed function(s) lack test coverage: analyze_changes" in guidance


# ---------------------------------------------------------------------------
# Every consumer reads the root, not the absolute path
# ---------------------------------------------------------------------------


class TestCheckoutUnderATestDirectory:
    """The end-to-end shape of #1023, through each consumer of the graph.

    A repository whose absolute path happens to contain a directory named
    "test" must produce exactly the same report as the same tree checked out
    anywhere else.
    """

    def setup_method(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "test" / "checkout"
        (self.root / ".code-review-graph").mkdir(parents=True)
        self.store = GraphStore(self.root / ".code-review-graph" / "graph.db")
        self.store.set_metadata("repo_root", str(self.root))

    def teardown_method(self):
        self.store.close()
        self.tmp.cleanup()

    def _add(self, kind, name, rel_path, is_test=False, line_start=1, line_end=10):
        self.store.upsert_node(
            NodeInfo(
                kind=kind,
                name=name,
                file_path=str(self.root / rel_path),
                line_start=line_start,
                line_end=line_end,
                language="python",
                is_test=is_test,
            ),
            file_hash="h",
        )
        self.store.commit()

    def test_store_reports_the_recorded_repo_root(self):
        assert self.store.get_repo_root() == str(self.root)

    def test_store_falls_back_to_the_default_database_location(self):
        """A graph built before builds recorded the root still answers."""
        self.store._conn.execute("DELETE FROM metadata WHERE key = 'repo_root'")
        self.store._conn.commit()
        assert self.store.get_repo_root() == str(self.root)

    def test_production_gaps_survive(self):
        self._add("Function", "analyze_changes", "code_review_graph/changes.py")
        self._add("Function", "_check", "tests/test_upgrade_path.py")
        result = analyze_changes(
            self.store,
            changed_files=["code_review_graph/changes.py",
                           "tests/test_upgrade_path.py"],
            repo_root=str(self.root),
        )
        assert {g["name"] for g in result["test_gaps"]} == {"analyze_changes"}

    def test_production_gaps_survive_without_an_explicit_repo_root(self):
        """MCP callers pass absolute paths and no root; the graph knows it."""
        self._add("Function", "analyze_changes", "code_review_graph/changes.py")
        result = analyze_changes(
            self.store,
            changed_files=[str(self.root / "code_review_graph/changes.py")],
        )
        assert {g["name"] for g in result["test_gaps"]} == {"analyze_changes"}

    def test_entry_points_survive(self):
        from code_review_graph.flows import detect_entry_points

        self._add("Function", "main", "code_review_graph/cli.py")
        self._add("Function", "helper", "tests/support.py")
        names = {n.name for n in detect_entry_points(self.store)}
        assert names == {"main"}

    def test_dead_code_candidates_survive(self):
        from code_review_graph.refactor import find_dead_code

        self._add("Function", "orphaned_helper", "code_review_graph/util.py")
        self._add("Function", "test_only_helper", "tests/support.py")
        names = {d["name"] for d in find_dead_code(self.store)}
        assert names == {"orphaned_helper"}

    def test_dead_code_ignores_directories_above_the_checkout(self):
        """The package-alias heuristic must not read the workspace name.

        ``find_dead_code`` accepts a bare-name caller when a directory of the
        callee's path appears in the caller's import specifier. Read from the
        absolute path, the workspace directory ("test" here) joins that set
        and matches any import mentioning it, so the candidate disappears in
        one checkout and not in another.
        """
        from code_review_graph.graph import EdgeInfo
        from code_review_graph.refactor import find_dead_code

        self._add("Function", "orphaned_helper", "packages/core/util.ts")
        # A second definition, so the bare-name CALLS edge below is ambiguous
        # and has to be resolved through the import graph.
        self._add("Function", "orphaned_helper", "packages/data/util.ts")
        self._add("Function", "caller", "packages/app/main.ts")
        self.store.upsert_edge(
            EdgeInfo(
                kind="IMPORTS_FROM",
                source=str(self.root / "packages/app/main.ts"),
                target="@acme/latest-test-kit",
                file_path=str(self.root / "packages/app/main.ts"),
                line=1,
            ),
        )
        self.store.upsert_edge(
            EdgeInfo(
                kind="CALLS",
                source=str(self.root / "packages/app/main.ts") + "::caller",
                target="orphaned_helper",
                file_path=str(self.root / "packages/app/main.ts"),
                line=2,
            ),
        )
        self.store.commit()
        dead_paths = {
            d["relative_path"] for d in find_dead_code(self.store, root=str(self.root))
            if d["name"] == "orphaned_helper"
        }
        assert "packages/core/util.ts" in dead_paths


def test_shipped_test_subpackage_needs_a_filesystem_path():
    """A relative path with no root cannot be probed, so it stays test code.

    Probing it against the working directory would make the answer depend on
    where the process was started.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _make_tree(root, [
            "django/__init__.py",
            "django/test/__init__.py",
            "django/test/client.py",
        ])
        assert is_test_file("django/test/client.py") is True
        assert is_test_file("django/test/client.py", str(root)) is False
