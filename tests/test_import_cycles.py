"""Files that import each other, reported as a knowledge gap.

Part of #434. A cycle only counts when the imports run: an import under
``if TYPE_CHECKING`` or written ``import type`` never does, and ``from . import
sibling`` names the package the file is already inside. An import inside a
function does run, but late, so those loops are kept and marked as not failing
at load time.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from code_review_graph.analysis import find_import_cycles, find_knowledge_gaps
from code_review_graph.constants import (
    IMPORT_CONTEXT_DEFERRED,
    IMPORT_CONTEXT_KEY,
    IMPORT_CONTEXT_PARENT_PACKAGE,
    IMPORT_CONTEXT_TYPE_ONLY,
)
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, get_db_path
from code_review_graph.parser import CodeParser, EdgeInfo, NodeInfo
from code_review_graph.tools import get_knowledge_gaps_func


def _write(root: Path, files: dict[str, str]) -> None:
    for rel_path, content in files.items():
        path = root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _build(root: Path, files: dict[str, str]) -> None:
    _write(root, files)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    store = GraphStore(get_db_path(root))
    try:
        full_build(root, store)
    finally:
        store.close()


def _cycles(root: Path) -> list[dict]:
    store = GraphStore(get_db_path(root))
    try:
        return find_import_cycles(store, repo_root=root)
    finally:
        store.close()


def _import_edges(path: Path) -> list[EdgeInfo]:
    _, edges = CodeParser().parse_file(path)
    return [edge for edge in edges if edge.kind == "IMPORTS_FROM"]


def _context_by_target(path: Path) -> dict[str, str | None]:
    return {
        Path(edge.target).name: edge.extra.get(IMPORT_CONTEXT_KEY)
        for edge in _import_edges(path)
    }


class TestParserMarksImportsThatDoNotRunAtLoad:
    def test_module_level_import_has_no_mark(self, tmp_path):
        _write(tmp_path, {"a.py": "from b import x\n", "b.py": "x = 1\n"})
        assert _context_by_target(tmp_path / "a.py") == {"b.py": None}

    @pytest.mark.parametrize("guard", [
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:",
        "import typing\nif typing.TYPE_CHECKING:",
        "import typing as t\nif t.TYPE_CHECKING:",
    ])
    def test_python_type_checking_import(self, tmp_path, guard):
        _write(tmp_path, {
            "a.py": f"{guard}\n    from b import x\n",
            "b.py": "x = 1\n",
        })
        assert _context_by_target(tmp_path / "a.py")["b.py"] == IMPORT_CONTEXT_TYPE_ONLY

    def test_else_of_type_checking_runs(self, tmp_path):
        _write(tmp_path, {
            "a.py": (
                "from typing import TYPE_CHECKING\n"
                "if TYPE_CHECKING:\n    pass\nelse:\n    from b import x\n"
            ),
            "b.py": "x = 1\n",
        })
        assert _context_by_target(tmp_path / "a.py")["b.py"] is None

    def test_python_import_inside_a_function(self, tmp_path):
        _write(tmp_path, {
            "a.py": "def f():\n    from b import x\n    return x\n",
            "b.py": "x = 1\n",
        })
        assert _context_by_target(tmp_path / "a.py")["b.py"] == IMPORT_CONTEXT_DEFERRED

    def test_sibling_through_own_package_marks_only_the_package(self, tmp_path):
        _write(tmp_path, {
            "pkg/__init__.py": "",
            "pkg/a.py": "from . import b\n",
            "pkg/b.py": "x = 1\n",
        })
        assert _context_by_target(tmp_path / "pkg" / "a.py") == {
            "__init__.py": IMPORT_CONTEXT_PARENT_PACKAGE,
            "b.py": None,
        }

    def test_symbol_from_own_package_is_a_real_import(self, tmp_path):
        """``from . import app`` where ``app`` is a variable reads __init__.py."""
        _write(tmp_path, {
            "pkg/__init__.py": "app = 1\n",
            "pkg/a.py": "from . import app\n",
        })
        assert _context_by_target(tmp_path / "pkg" / "a.py") == {"__init__.py": None}

    def test_sibling_mixed_with_symbol_keeps_the_package_real(self, tmp_path):
        _write(tmp_path, {
            "pkg/__init__.py": "app = 1\n",
            "pkg/a.py": "from . import app, b\n",
            "pkg/b.py": "x = 1\n",
        })
        assert _context_by_target(tmp_path / "pkg" / "a.py")["__init__.py"] is None

    def test_other_package_is_a_real_import(self, tmp_path):
        """Importing a submodule of another package runs its __init__.py."""
        _write(tmp_path, {
            "other/__init__.py": "",
            "other/b.py": "x = 1\n",
            "a.py": "from other import b\n",
        })
        assert _context_by_target(tmp_path / "a.py")["__init__.py"] is None

    @pytest.mark.parametrize("statement", [
        "import type { X } from './b';",
        "import { type X, type Y } from './b';",
    ])
    def test_typescript_type_import(self, tmp_path, statement):
        _write(tmp_path, {
            "a.ts": f"{statement}\n",
            "b.ts": "export type X = number;\nexport type Y = string;\n",
        })
        assert _context_by_target(tmp_path / "a.ts")["b.ts"] == IMPORT_CONTEXT_TYPE_ONLY

    def test_typescript_value_import_mixed_with_a_type(self, tmp_path):
        _write(tmp_path, {
            "a.ts": "import { type X, y } from './b';\n",
            "b.ts": "export type X = number;\nexport const y = 1;\n",
        })
        assert _context_by_target(tmp_path / "a.ts")["b.ts"] is None

    def test_javascript_require_inside_a_function(self, tmp_path):
        _write(tmp_path, {
            "a.js": "function f() {\n  return require('./b');\n}\n",
            "b.js": "module.exports = 1;\n",
        })
        assert _context_by_target(tmp_path / "a.js")["b.js"] == IMPORT_CONTEXT_DEFERRED

    def test_javascript_dynamic_import_at_the_top(self, tmp_path):
        _write(tmp_path, {
            "a.js": "import('./b').then(console.log);\n",
            "b.js": "export default 1;\n",
        })
        assert _context_by_target(tmp_path / "a.js")["b.js"] == IMPORT_CONTEXT_DEFERRED

    def test_javascript_top_level_require_beats_an_earlier_lazy_one(self, tmp_path):
        _write(tmp_path, {
            "a.js": "function f() {\n  return require('./b');\n}\nconst b = require('./b');\n",
            "b.js": "module.exports = 1;\n",
        })
        assert _context_by_target(tmp_path / "a.js")["b.js"] is None


    def test_ruby_require_inside_a_method(self, tmp_path):
        _write(tmp_path, {
            "lib/a.rb": "class A\n  def self.load!\n    require_relative 'b'\n  end\nend\n",
            "lib/b.rb": "class B\nend\n",
        })
        assert _context_by_target(tmp_path / "lib" / "a.rb")["b.rb"] == IMPORT_CONTEXT_DEFERRED

    def test_ruby_top_level_require_has_no_mark(self, tmp_path):
        _write(tmp_path, {
            "lib/a.rb": "require_relative 'b'\n",
            "lib/b.rb": "class B\nend\n",
        })
        assert _context_by_target(tmp_path / "lib" / "a.rb") == {"b.rb": None}


class TestPackageFileDoesNotShadowTheStandardLibrary:
    """Flask's ``import json`` in ``flask/json/provider.py`` is the stdlib one.

    Python 3 looks on ``sys.path``, and a package's own folder is not on it. The
    graph read those imports as ``flask/json/__init__.py`` and ``flask/typing.py``,
    which reported a cycle that does not exist.
    """

    FLASK_SHAPE = {
        "src/pkg/__init__.py": "",
        "src/pkg/typing.py": "x = 1\n",
        "src/pkg/json/__init__.py": "from .provider import y\n",
        "src/pkg/json/provider.py": "import json\nimport typing as t\ny = 1\n",
        "src/pkg/other.py": "import json\nimport typing\n",
        "scripts/tool.py": "import json\nimport typing\n",
    }

    def test_no_cycle_is_reported(self, tmp_path):
        _build(tmp_path, self.FLASK_SHAPE)
        assert _cycles(tmp_path) == []

    @pytest.mark.parametrize("rel_path", [
        "src/pkg/json/provider.py", "src/pkg/other.py", "scripts/tool.py",
    ])
    def test_imports_stay_external_after_the_full_build(self, tmp_path, rel_path):
        _build(tmp_path, self.FLASK_SHAPE)
        store = GraphStore(get_db_path(tmp_path))
        try:
            targets = {
                edge.target_qualified
                for edge in store.get_edges_by_source(str(tmp_path / rel_path))
                if edge.kind == "IMPORTS_FROM"
            }
        finally:
            store.close()
        assert {"json", "typing"} <= targets
        assert not any("/src/pkg/" in target for target in targets)

    def test_top_level_file_can_still_shadow(self, tmp_path):
        """A ``typing.py`` beside a script really is what that script imports."""
        _write(tmp_path, {"tool.py": "import typing\n", "typing.py": "x = 1\n"})
        assert _context_by_target(tmp_path / "tool.py") == {"typing.py": None}

    def test_top_level_file_found_by_suffix_is_kept(self, tmp_path):
        """A ``src/`` layout: ``src`` is not a package, so ``typing`` can live there."""
        _build(tmp_path, {
            "src/typing.py": "x = 1\n",
            "scripts/tool.py": "import typing\n",
        })
        store = GraphStore(get_db_path(tmp_path))
        try:
            targets = {
                edge.target_qualified
                for edge in store.get_edges_by_source(str(tmp_path / "scripts" / "tool.py"))
                if edge.kind == "IMPORTS_FROM"
            }
        finally:
            store.close()
        assert targets == {str(tmp_path / "src" / "typing.py")}

    def test_other_names_still_resolve_from_a_package(self, tmp_path):
        _write(tmp_path, {
            "pkg/__init__.py": "",
            "pkg/helpers.py": "x = 1\n",
            "pkg/a.py": "import helpers\n",
        })
        assert _context_by_target(tmp_path / "pkg" / "a.py") == {"helpers.py": None}


class TestFindImportCycles:
    def test_two_files_importing_each_other(self, tmp_path):
        _build(tmp_path, {
            "a.py": "from b import y\nx = 1\n",
            "b.py": "\n\nfrom a import x\ny = 1\n",
        })

        cycles = _cycles(tmp_path)

        assert len(cycles) == 1
        cycle = cycles[0]
        assert cycle["size"] == 2
        assert cycle["load_time"] is True
        assert cycle["files"] == ["a.py", "b.py"]
        assert cycle["name"] == "a.py -> b.py -> a.py"
        assert cycle["cycle"] == [
            {"file": "a.py", "line": 1, "imports": "b.py"},
            {"file": "b.py", "line": 3, "imports": "a.py"},
        ]

    def test_no_cycle_in_a_plain_chain(self, tmp_path):
        _build(tmp_path, {
            "a.py": "from b import y\n",
            "b.py": "from c import z\ny = 1\n",
            "c.py": "z = 1\n",
        })
        assert _cycles(tmp_path) == []

    def test_type_only_import_does_not_make_a_cycle(self, tmp_path):
        _build(tmp_path, {
            "a.py": "from b import y\nx = 1\n",
            "b.py": (
                "from typing import TYPE_CHECKING\n"
                "if TYPE_CHECKING:\n    from a import x\ny = 1\n"
            ),
        })
        assert _cycles(tmp_path) == []

    def test_sibling_import_through_the_package_does_not_make_a_cycle(self, tmp_path):
        """The shape of Flask's celery example, which is not a cycle at all."""
        _build(tmp_path, {
            "pkg/__init__.py": "def create():\n    from . import views\n    return views\n",
            "pkg/views.py": "from . import tasks\n",
            "pkg/tasks.py": "x = 1\n",
        })
        assert _cycles(tmp_path) == []

    def test_package_and_module_sharing_a_variable_is_a_cycle(self, tmp_path):
        """The shape of Flask's javascript example, which is a real cycle."""
        _build(tmp_path, {
            "pkg/__init__.py": "app = 1\nfrom pkg import views\n",
            "pkg/views.py": "from . import app\n",
        })

        cycles = _cycles(tmp_path)

        assert len(cycles) == 1
        assert cycles[0]["load_time"] is True
        assert cycles[0]["files"] == ["pkg/__init__.py", "pkg/views.py"]

    def test_deferred_loop_is_kept_but_not_called_load_time(self, tmp_path):
        _build(tmp_path, {
            "a.py": "from b import y\nx = 1\n",
            "b.py": "y = 1\ndef f():\n    from a import x\n    return x\n",
        })

        cycles = _cycles(tmp_path)

        assert len(cycles) == 1
        assert cycles[0]["load_time"] is False

    def test_load_time_loop_is_shown_inside_a_larger_group(self, tmp_path):
        _build(tmp_path, {
            # a <-> b at load time; b -> c -> a only because c imports late.
            "a.py": "from b import y\nx = 1\n",
            "b.py": "from a import x\nfrom c import z\ny = 1\n",
            "c.py": "z = 1\ndef f():\n    from a import x\n    return x\n",
        })

        cycles = _cycles(tmp_path)

        assert len(cycles) == 1
        assert cycles[0]["size"] == 3
        assert cycles[0]["load_time"] is True
        assert cycles[0]["name"] == "a.py -> b.py -> a.py"

    def test_load_time_cycles_come_first(self, tmp_path):
        _build(tmp_path, {
            "late/a.py": "from late.b import y\nx = 1\n",
            "late/b.py": "y = 1\ndef f():\n    from late.a import x\n    return x\n",
            "now/a.py": "from now.b import y\nx = 1\n",
            "now/b.py": "from now.a import x\ny = 1\n",
        })

        cycles = _cycles(tmp_path)

        assert [cycle["load_time"] for cycle in cycles] == [True, False]

    def test_self_import_is_not_a_cycle(self, tmp_path):
        _build(tmp_path, {"pkg/__init__.py": "from . import __name__ as n\n"})
        assert _cycles(tmp_path) == []

    def test_import_that_names_no_file_is_ignored(self, tmp_path):
        store = GraphStore(tmp_path / "graph.db")
        try:
            path = str(tmp_path / "a.py")
            store.upsert_node(NodeInfo(
                kind="File", name=path, file_path=path,
                line_start=1, line_end=1, language="python",
            ))
            store.upsert_edge(EdgeInfo(
                kind="IMPORTS_FROM", source=path, target="json", file_path=path, line=1,
            ))
            store.upsert_edge(EdgeInfo(
                kind="IMPORTS_FROM", source="json", target=path, file_path=path, line=1,
            ))
            store.commit()
            assert find_import_cycles(store) == []
        finally:
            store.close()

    def test_long_loop_is_cut_but_its_length_is_told(self, tmp_path):
        count = 15
        _build(tmp_path, {
            f"m{index:02}.py": f"from m{(index + 1) % count:02} import x\nx = 1\n"
            for index in range(count)
        })

        cycle = _cycles(tmp_path)[0]

        assert cycle["size"] == count
        assert cycle["loop_length"] == count
        assert len(cycle["cycle"]) == 10
        assert len(cycle["files"]) == 10

    def test_order_is_the_same_on_every_run(self, tmp_path):
        _build(tmp_path, {
            "c.py": "from a import x\nz = 1\n",
            "a.py": "from b import y\nx = 1\n",
            "b.py": "from c import z\ny = 1\n",
        })
        first = _cycles(tmp_path)
        assert first == _cycles(tmp_path)
        assert first[0]["name"] == "a.py -> b.py -> c.py -> a.py"


class TestKnowledgeGapsReportCycles:
    FILES = {
        "a.py": "from b import y\nx = 1\n",
        "b.py": "from a import x\ny = 1\n",
    }

    def test_category_is_present(self, tmp_path):
        _build(tmp_path, self.FILES)
        store = GraphStore(get_db_path(tmp_path))
        try:
            assert len(find_knowledge_gaps(store)["import_cycles"]) == 1
        finally:
            store.close()

    def test_tool_counts_and_shows_the_cycle(self, tmp_path):
        _build(tmp_path, self.FILES)

        result = get_knowledge_gaps_func(repo_root=str(tmp_path))

        assert result["summary"]["import_cycles"] == 1
        assert result["gaps"]["import_cycles"][0]["cycle"][0]["file"] == "a.py"

    def test_minimal_keeps_the_loop_and_drops_the_rows(self, tmp_path):
        _build(tmp_path, self.FILES)

        result = get_knowledge_gaps_func(repo_root=str(tmp_path), detail_level="minimal")

        assert result["gaps"]["import_cycles"] == [{
            "name": "a.py -> b.py -> a.py", "size": 2, "load_time": True,
        }]

    def test_cycle_count_survives_the_cap(self, tmp_path):
        _build(tmp_path, {
            f"p{index}/{name}.py": (
                f"from p{index}.{other} import v\nv = 1\n"
            )
            for index in range(3)
            for name, other in (("a", "b"), ("b", "a"))
        })

        result = get_knowledge_gaps_func(repo_root=str(tmp_path), max_per_category=1)

        assert result["summary"]["import_cycles"] == 3
        assert len(result["gaps"]["import_cycles"]) == 1
        assert result["truncated"] is True
