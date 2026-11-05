"""Python relative imports must resolve to the module FILE, not to a symbol.

``from .graph import GraphStore`` imports from the module ``.graph``. The
module half of a relative import lives in tree-sitter-python's
``relative_import`` node, reachable as the ``module_name`` field; the
``dotted_name`` children that follow ``import`` are the imported SYMBOLS.
Reading the first ``dotted_name`` therefore hands back ``GraphStore`` and
calls it a module, which a walk up the filesystem then "resolves" to
whatever ``GraphStore.py`` it meets first -- on a case-insensitive
filesystem, possibly a real but wrong file.

Every test here pins the exact ``IMPORTS_FROM`` target and asserts that the
target is a file that exists on disk.
"""

import importlib.machinery
from pathlib import Path

import pytest

from code_review_graph.parser import CodeParser
from tests.import_audit import exists_case_exact, looks_like_a_path

# --------------------------------------------------------------------------
# Fixture package
#
#   pkg/__init__.py
#   pkg/graph.py            node_to_dict(), GraphStore
#   pkg/helpers.py
#   pkg/registry.py         lowercase on disk; imported as `Registry`
#   pkg/consumer.py         level-1 importer
#   pkg/sub/__init__.py
#   pkg/sub/deep.py         thing()
#   pkg/sub/consumer.py     level-2 importer
# --------------------------------------------------------------------------


@pytest.fixture
def pkg(tmp_path: Path) -> Path:
    """Build a small two-level package and return the repository root."""
    root = tmp_path / "repo"
    package = root / "pkg"
    sub = package / "sub"
    sub.mkdir(parents=True)

    (package / "__init__.py").write_text(
        "from .graph import GraphStore\n", encoding="utf-8",
    )
    (package / "graph.py").write_text(
        "class GraphStore:\n    pass\n\n\ndef node_to_dict(node):\n    return {}\n",
        encoding="utf-8",
    )
    (package / "helpers.py").write_text(
        "def helper():\n    return 1\n", encoding="utf-8",
    )
    (package / "registry.py").write_text(
        "class Registry:\n    pass\n", encoding="utf-8",
    )
    (sub / "__init__.py").write_text("", encoding="utf-8")
    (sub / "deep.py").write_text(
        "def thing():\n    return 2\n", encoding="utf-8",
    )
    return root


def _parse(pkg_root: Path, relative: str, source: str):
    """Write *source* at *relative* inside *pkg_root* and parse it."""
    target = pkg_root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")
    parser = CodeParser(repo_root=pkg_root)
    return parser.parse_file(target)


def _import_targets(edges) -> list[str]:
    return sorted(e.target for e in edges if e.kind == "IMPORTS_FROM")


def _call_targets(edges) -> set[str]:
    return {e.target for e in edges if e.kind == "CALLS"}


def _posix(path: Path) -> str:
    return path.resolve().as_posix()


# --------------------------------------------------------------------------
# from .m import <symbols>
# --------------------------------------------------------------------------


def test_single_symbol_targets_the_module_file(pkg):
    _, edges = _parse(pkg, "pkg/consumer.py", "from .graph import GraphStore\n")

    assert _import_targets(edges) == [_posix(pkg / "pkg" / "graph.py")]


def test_several_symbols_still_emit_one_edge(pkg):
    """One statement imports from one module, however many names it binds."""
    _, edges = _parse(
        pkg, "pkg/consumer.py", "from .graph import GraphStore, node_to_dict\n",
    )

    assert _import_targets(edges) == [_posix(pkg / "pkg" / "graph.py")]


def test_aliased_symbol_emits_an_edge(pkg):
    """``import a as b`` wraps the name in ``aliased_import`` -- still an import."""
    _, edges = _parse(
        pkg, "pkg/consumer.py", "from .graph import GraphStore as GS\n",
    )

    assert _import_targets(edges) == [_posix(pkg / "pkg" / "graph.py")]


def test_star_import_emits_an_edge(pkg):
    _, edges = _parse(pkg, "pkg/consumer.py", "from .graph import *\n")

    assert _import_targets(edges) == [_posix(pkg / "pkg" / "graph.py")]


def test_symbol_is_never_mistaken_for_a_module_on_a_case_insensitive_fs(pkg):
    """``Registry`` is a class; ``Registry.py`` does not exist anywhere."""
    _, edges = _parse(pkg, "pkg/consumer.py", "from .registry import Registry\n")

    targets = _import_targets(edges)
    assert targets == [_posix(pkg / "pkg" / "registry.py")]
    assert not any(Path(t).name == "Registry.py" for t in targets)


def test_sibling_module_never_shadows_the_named_module(pkg):
    """``from .cli import main`` must not land on the sibling ``main.py``."""
    (pkg / "pkg" / "cli.py").write_text(
        "def main():\n    return 0\n", encoding="utf-8",
    )
    (pkg / "pkg" / "main.py").write_text("X = 1\n", encoding="utf-8")

    _, edges = _parse(pkg, "pkg/entry.py", "from .cli import main\n")

    assert _import_targets(edges) == [_posix(pkg / "pkg" / "cli.py")]


# --------------------------------------------------------------------------
# from . import <submodules>  /  from .pkg import <submodule>
# --------------------------------------------------------------------------


def test_from_dot_import_submodule_targets_package_and_submodule(pkg):
    """``.`` is the caller's package; ``graph`` is a module in it."""
    _, edges = _parse(pkg, "pkg/consumer.py", "from . import graph\n")

    assert _import_targets(edges) == [
        _posix(pkg / "pkg" / "__init__.py"),
        _posix(pkg / "pkg" / "graph.py"),
    ]


def test_from_dot_import_several_submodules_emits_one_edge_per_symbol(pkg):
    _, edges = _parse(pkg, "pkg/consumer.py", "from . import graph, helpers\n")

    assert _import_targets(edges) == [
        _posix(pkg / "pkg" / "__init__.py"),
        _posix(pkg / "pkg" / "graph.py"),
        _posix(pkg / "pkg" / "helpers.py"),
    ]


def test_from_subpackage_import_submodule(pkg):
    _, edges = _parse(pkg, "pkg/consumer.py", "from .sub import deep\n")

    assert _import_targets(edges) == [
        _posix(pkg / "pkg" / "sub" / "__init__.py"),
        _posix(pkg / "pkg" / "sub" / "deep.py"),
    ]


def test_from_subpackage_import_plain_symbol_targets_init(pkg):
    """A name that is not a submodule leaves only the package ``__init__``."""
    _, edges = _parse(pkg, "pkg/consumer.py", "from .sub import NOT_A_MODULE\n")

    assert _import_targets(edges) == [_posix(pkg / "pkg" / "sub" / "__init__.py")]


def test_dotted_relative_module(pkg):
    _, edges = _parse(pkg, "pkg/consumer.py", "from .sub.deep import thing\n")

    assert _import_targets(edges) == [_posix(pkg / "pkg" / "sub" / "deep.py")]


# --------------------------------------------------------------------------
# Leading-dot level
# --------------------------------------------------------------------------


def test_level_two_walks_one_package_up(pkg):
    _, edges = _parse(pkg, "pkg/sub/consumer.py", "from ..graph import GraphStore\n")

    assert _import_targets(edges) == [_posix(pkg / "pkg" / "graph.py")]


def test_level_two_with_a_dotted_tail(pkg):
    _, edges = _parse(pkg, "pkg/sub/consumer.py", "from ..sub.deep import thing\n")

    assert _import_targets(edges) == [_posix(pkg / "pkg" / "sub" / "deep.py")]


def test_level_is_not_silently_dropped(pkg):
    """``..graph`` and ``.graph`` name different modules from the same file."""
    (pkg / "pkg" / "sub" / "graph.py").write_text("Y = 1\n", encoding="utf-8")

    _, level_one = _parse(pkg, "pkg/sub/consumer.py", "from .graph import Y\n")
    _, level_two = _parse(
        pkg, "pkg/sub/consumer2.py", "from ..graph import GraphStore\n",
    )

    assert _import_targets(level_one) == [_posix(pkg / "pkg" / "sub" / "graph.py")]
    assert _import_targets(level_two) == [_posix(pkg / "pkg" / "graph.py")]


# --------------------------------------------------------------------------
# Repository-root clamp
# --------------------------------------------------------------------------


def test_level_above_the_repository_root_emits_no_path(pkg, tmp_path):
    """A target outside the repository root must never be emitted."""
    outside = tmp_path / "escape.py"
    outside.write_text("Z = 1\n", encoding="utf-8")

    _, edges = _parse(pkg, "pkg/consumer.py", "from ...escape import Z\n")

    targets = _import_targets(edges)
    assert _posix(outside) not in targets
    for target in targets:
        assert "/" not in target, f"leaked a path outside the repo root: {target}"


def test_unresolvable_relative_import_never_invents_a_path(pkg):
    _, edges = _parse(pkg, "pkg/consumer.py", "from .nope import Thing\n")

    for target in _import_targets(edges):
        assert "/" not in target
        assert not Path(target).is_absolute()


def test_every_path_shaped_target_exists_on_disk(pkg):
    source = (
        "from .graph import GraphStore, node_to_dict\n"
        "from .graph import GraphStore as GS\n"
        "from .graph import *\n"
        "from . import graph, helpers\n"
        "from .sub import deep\n"
        "from .sub.deep import thing\n"
        "from .registry import Registry\n"
        "from .nope import Missing\n"
        "import os\n"
        "from pathlib import Path\n"
    )
    _, edges = _parse(pkg, "pkg/consumer.py", source)

    for target in _import_targets(edges):
        if looks_like_a_path(target):
            # Case-exact: ``Path.is_file()`` would say yes to ``Registry.py``
            # on APFS, which is exactly how this bug stayed invisible here.
            assert exists_case_exact(target), f"{target} does not exist"


# --------------------------------------------------------------------------
# Absolute imports must not regress
# --------------------------------------------------------------------------


def test_absolute_stdlib_imports_unchanged(pkg):
    _, edges = _parse(
        pkg, "pkg/consumer.py", "import os\nfrom pathlib import Path\n",
    )

    assert _import_targets(edges) == ["os", "pathlib"]


def test_absolute_in_repo_import_resolves_to_the_module_file(pkg):
    _, edges = _parse(pkg, "entry.py", "from pkg.graph import GraphStore\n")

    assert _import_targets(edges) == [_posix(pkg / "pkg" / "graph.py")]


def test_absolute_dotted_module_import_unchanged(pkg):
    _, edges = _parse(
        pkg, "pkg/consumer.py", "import a.b\nimport b as B\nimport x, y as z\n",
    )

    assert _import_targets(edges) == ["a.b", "b", "x", "y"]


# --------------------------------------------------------------------------
# Cross-module CALLS (the import_map half of the same bug)
# --------------------------------------------------------------------------


def test_relative_import_resolves_cross_module_calls(pkg):
    _, edges = _parse(
        pkg,
        "pkg/consumer.py",
        "from .graph import node_to_dict\n\n\ndef run(n):\n    return node_to_dict(n)\n",
    )

    expected = f"{_posix(pkg / 'pkg' / 'graph.py')}::node_to_dict"
    assert expected in _call_targets(edges)


def test_level_two_relative_import_resolves_cross_module_calls(pkg):
    _, edges = _parse(
        pkg,
        "pkg/sub/consumer.py",
        "from ..graph import node_to_dict\n\n\ndef run(n):\n    return node_to_dict(n)\n",
    )

    expected = f"{_posix(pkg / 'pkg' / 'graph.py')}::node_to_dict"
    assert expected in _call_targets(edges)


def test_aliased_relative_import_resolves_cross_module_calls(pkg):
    _, edges = _parse(
        pkg,
        "pkg/consumer.py",
        "from .graph import node_to_dict as n2d\n\n\ndef run(n):\n    return n2d(n)\n",
    )

    graph_file = _posix(pkg / "pkg" / "graph.py")
    assert any(
        target.startswith(f"{graph_file}::") for target in _call_targets(edges)
    ), _call_targets(edges)


def test_star_relative_import_still_resolves_cross_module_calls(pkg):
    _, edges = _parse(
        pkg,
        "pkg/consumer.py",
        "from .graph import *\n\n\ndef run(n):\n    return node_to_dict(n)\n",
    )

    expected = f"{_posix(pkg / 'pkg' / 'graph.py')}::node_to_dict"
    assert expected in _call_targets(edges)


def test_absolute_import_calls_unchanged(pkg):
    _, edges = _parse(
        pkg,
        "entry.py",
        "from pkg.graph import node_to_dict\n\n\ndef run(n):\n"
        "    return node_to_dict(n)\n",
    )

    expected = f"{_posix(pkg / 'pkg' / 'graph.py')}::node_to_dict"
    assert expected in _call_targets(edges)


# --------------------------------------------------------------------------
# Which file CPython would actually bind
#
# Ground truth here is taken from the interpreter, never from the audit
# script and never from prose: ``PathFinder.find_spec`` runs the same
# ``FileFinder`` that ``import`` runs, without executing the module. The audit
# script's own ``_module_file`` had the module-before-package order wrong, so
# a test written from it would have agreed with the bug.
# --------------------------------------------------------------------------


def _cpython_origin(directory: Path, name: str) -> str | None:
    """``__file__`` CPython would give ``directory/name``, or None for a
    namespace package (which has no file)."""
    spec = importlib.machinery.PathFinder.find_spec(name, [str(directory)])
    assert spec is not None, f"CPython finds no module {name} in {directory}"
    return spec.origin


def test_package_beats_a_same_named_module_file(pkg):
    """``pkg/m/__init__.py`` and ``pkg/m.py`` both exist: the package wins."""
    package = pkg / "pkg"
    (package / "m.py").write_text("WHICH = 'module'\n", encoding="utf-8")
    (package / "m").mkdir()
    (package / "m" / "__init__.py").write_text(
        "WHICH = 'package'\n", encoding="utf-8",
    )

    expected = _cpython_origin(package, "m")
    assert expected == _posix(package / "m" / "__init__.py")

    _, edges = _parse(pkg, "pkg/consumer.py", "from .m import WHICH\n")

    assert _import_targets(edges) == [expected]


def test_absolute_import_also_prefers_the_package(pkg):
    """The same precedence on the absolute walk-up, not only the dot form."""
    package = pkg / "pkg"
    (package / "m.py").write_text("WHICH = 'module'\n", encoding="utf-8")
    (package / "m").mkdir()
    (package / "m" / "__init__.py").write_text(
        "WHICH = 'package'\n", encoding="utf-8",
    )

    _, edges = _parse(pkg, "entry.py", "from pkg.m import WHICH\n")

    assert _import_targets(edges) == [_cpython_origin(package, "m")]


def test_module_file_beats_a_same_named_namespace_directory(pkg):
    """A bare directory is only the fallback, so ``both.py`` still wins.

    ``FileFinder`` records the namespace candidate but keeps looking for a
    loader; it returns the namespace spec only when no file matched.
    """
    package = pkg / "pkg"
    (package / "both.py").write_text("WHICH = 'module'\n", encoding="utf-8")
    (package / "both").mkdir()
    (package / "both" / "x.py").write_text("Y = 1\n", encoding="utf-8")

    expected = _cpython_origin(package, "both")
    assert expected == _posix(package / "both.py")

    _, edges = _parse(pkg, "pkg/consumer.py", "from .both import WHICH\n")

    assert _import_targets(edges) == [expected]


# --------------------------------------------------------------------------
# PEP 420 namespace packages
# --------------------------------------------------------------------------


def test_namespace_package_submodule_resolves_to_its_file(pkg):
    """``pkg/ns/`` has no ``__init__.py``; ``pkg/ns/leaf.py`` is still a file."""
    package = pkg / "pkg"
    (package / "ns").mkdir()
    (package / "ns" / "leaf.py").write_text(
        "def thing():\n    return 1\n", encoding="utf-8",
    )

    # CPython: `pkg.ns` is a namespace package (no origin), `pkg.ns.leaf` is
    # an ordinary module file.
    assert _cpython_origin(package, "ns") is None
    assert _cpython_origin(package / "ns", "leaf") == _posix(
        package / "ns" / "leaf.py",
    )

    _, edges = _parse(pkg, "pkg/consumer.py", "from .ns import leaf\n")

    assert _import_targets(edges) == [_posix(package / "ns" / "leaf.py")]


def test_namespace_package_emits_no_target_for_itself(pkg):
    """It has no file, so there is nothing honest to point an edge at.

    The previous behaviour emitted the raw specifier ``.ns`` -- a target no
    node in any graph can answer to.
    """
    package = pkg / "pkg"
    (package / "ns").mkdir()
    (package / "ns" / "leaf.py").write_text("X = 1\n", encoding="utf-8")

    _, edges = _parse(pkg, "pkg/consumer.py", "from .ns import leaf\n")

    assert ".ns" not in _import_targets(edges)


def test_namespace_subpackage_is_still_walked_through(pkg):
    """``from .ns.leaf import thing`` never needed an ``__init__.py``."""
    package = pkg / "pkg"
    (package / "ns").mkdir()
    (package / "ns" / "leaf.py").write_text(
        "def thing():\n    return 1\n", encoding="utf-8",
    )

    _, edges = _parse(pkg, "pkg/consumer.py", "from .ns.leaf import thing\n")

    assert _import_targets(edges) == [_posix(package / "ns" / "leaf.py")]


def test_namespace_package_with_no_submodule_keeps_the_raw_specifier(pkg):
    """The one case with nothing better to say, pinned so it stays explicit."""
    package = pkg / "pkg"
    (package / "ns").mkdir()
    (package / "ns" / "leaf.py").write_text("X = 1\n", encoding="utf-8")

    _, edges = _parse(pkg, "pkg/consumer.py", "from .ns import NOT_A_MODULE\n")

    assert _import_targets(edges) == [".ns"]


# --------------------------------------------------------------------------
# The two halves of the fix must agree
#
# `_extract_import` writes the IMPORTS_FROM edge; `_collect_import_names`
# fills the import_map that CALLS and REFERENCES resolve through. A name
# bound to a MODULE must mean the same file in both.
# --------------------------------------------------------------------------


def _import_map(root: Path, file_path: Path) -> dict[str, str]:
    parser = CodeParser(repo_root=root)
    source = file_path.read_bytes()
    tree = parser._get_parser("python").parse(source)
    import_map, _ = parser._collect_file_scope(
        tree.root_node, "python", source, str(file_path),
    )
    return import_map


def test_import_map_and_import_edge_agree_on_from_dot_import(pkg):
    consumer = pkg / "pkg" / "consumer.py"
    _, edges = _parse(pkg, "pkg/consumer.py", "from . import graph\n")

    import_map = _import_map(pkg, consumer)

    assert import_map["graph"] == _posix(pkg / "pkg" / "graph.py")
    assert import_map["graph"] in _import_targets(edges)


def test_import_map_and_import_edge_agree_on_a_namespace_submodule(pkg):
    package = pkg / "pkg"
    (package / "ns").mkdir()
    (package / "ns" / "leaf.py").write_text("X = 1\n", encoding="utf-8")
    consumer = package / "consumer.py"
    _, edges = _parse(pkg, "pkg/consumer.py", "from .ns import leaf\n")

    import_map = _import_map(pkg, consumer)

    assert import_map["leaf"] == _posix(package / "ns" / "leaf.py")
    assert import_map["leaf"] in _import_targets(edges)


def test_import_map_keeps_a_plain_symbol_on_the_module_file(pkg):
    """Only submodules move; ``GraphStore`` still belongs to ``graph.py``."""
    consumer = pkg / "pkg" / "consumer.py"
    _parse(pkg, "pkg/consumer.py", "from .graph import GraphStore\n")

    import_map = _import_map(pkg, consumer)

    assert import_map["GraphStore"] == _posix(pkg / "pkg" / "graph.py")


def test_reference_to_an_imported_module_names_its_file_not_a_symbol(pkg):
    """``from . import graph`` then ``f(graph)`` refers to the MODULE.

    This used to emit ``<pkg>/__init__.py::graph``, a qualified name that
    matches no node in any graph, because ``graph`` is not defined in the
    package ``__init__``.
    """
    _, edges = _parse(
        pkg,
        "pkg/consumer.py",
        "from . import graph\n"
        "\n"
        "\n"
        "def register(fn):\n"
        "    return fn\n"
        "\n"
        "\n"
        "def run():\n"
        "    return register(graph)\n",
    )

    references = {e.target for e in edges if e.kind == "REFERENCES"}
    assert _posix(pkg / "pkg" / "graph.py") in references
    assert not any("__init__.py::graph" in target for target in references)


def test_reference_to_an_imported_subpackage_names_its_init(pkg):
    """A subpackage's file is its ``__init__.py``, not ``<parent>::sub``."""
    _, edges = _parse(
        pkg,
        "pkg/consumer.py",
        "from . import sub\n"
        "\n"
        "\n"
        "def register(fn):\n"
        "    return fn\n"
        "\n"
        "\n"
        "def run():\n"
        "    return register(sub)\n",
    )

    references = {e.target for e in edges if e.kind == "REFERENCES"}
    assert _posix(pkg / "pkg" / "sub" / "__init__.py") in references
    assert f"{_posix(pkg / 'pkg' / '__init__.py')}::sub" not in references


def test_call_through_a_package_reexport_lands_on_the_defining_file(pkg):
    """``from .sub import thing`` where ``sub/__init__`` re-exports it.

    Qualifying the symbol against the package file produced
    ``sub/__init__.py::thing`` -- path-shaped, confident, and matching no
    node. The package's export map knows where the name came from.
    """
    (pkg / "pkg" / "sub" / "__init__.py").write_text(
        "from .deep import thing\n\n__all__ = ['thing']\n", encoding="utf-8",
    )

    _, edges = _parse(
        pkg,
        "pkg/consumer.py",
        "from .sub import thing\n\n\ndef run():\n    return thing()\n",
    )

    expected = f"{_posix(pkg / 'pkg' / 'sub' / 'deep.py')}::thing"
    assert expected in _call_targets(edges)


# --------------------------------------------------------------------------
# Other languages share _extract_import / _collect_file_scope / _do_resolve_module
# --------------------------------------------------------------------------


def test_typescript_relative_imports_unchanged(tmp_path):
    root = tmp_path / "ts"
    (root / "src").mkdir(parents=True)
    dep = root / "src" / "dep.ts"
    dep.write_text("export function helper() { return 1; }\n", encoding="utf-8")
    caller = root / "src" / "main.ts"
    caller.write_text(
        "import { helper } from './dep';\nexport function run() { return helper(); }\n",
        encoding="utf-8",
    )

    parser = CodeParser(repo_root=root)
    _, edges = parser.parse_file(caller)

    assert _import_targets(edges) == [_posix(dep)]
    assert f"{_posix(dep)}::helper" in _call_targets(edges)


def test_javascript_relative_imports_unchanged(tmp_path):
    root = tmp_path / "js"
    root.mkdir()
    dep = root / "dep.js"
    dep.write_text("export function helper() { return 1; }\n", encoding="utf-8")
    caller = root / "main.js"
    caller.write_text("import { helper } from './dep';\n", encoding="utf-8")

    parser = CodeParser(repo_root=root)
    _, edges = parser.parse_file(caller)

    assert _import_targets(edges) == [_posix(dep)]


def test_java_imports_unchanged(tmp_path):
    root = tmp_path / "java"
    pkg_dir = root / "com" / "ex"
    pkg_dir.mkdir(parents=True)
    helper = pkg_dir / "Helper.java"
    helper.write_text(
        "package com.ex;\npublic class Helper {}\n", encoding="utf-8",
    )
    caller = root / "Main.java"
    caller.write_text(
        "import com.ex.Helper;\npublic class Main {}\n", encoding="utf-8",
    )

    parser = CodeParser(repo_root=root)
    _, edges = parser.parse_file(caller)

    assert _import_targets(edges) == [_posix(helper)]


def test_kotlin_imports_unchanged(tmp_path):
    root = tmp_path / "kt"
    pkg_dir = root / "app"
    pkg_dir.mkdir(parents=True)
    helper = pkg_dir / "Helper.kt"
    helper.write_text("package app\nclass Helper\n", encoding="utf-8")
    caller = root / "Main.kt"
    caller.write_text("import app.Helper\nfun main() {}\n", encoding="utf-8")

    parser = CodeParser(repo_root=root)
    _, edges = parser.parse_file(caller)

    assert _import_targets(edges) == [_posix(helper)]


def test_go_imports_unchanged(tmp_path):
    root = tmp_path / "go"
    root.mkdir()
    caller = root / "main.go"
    caller.write_text(
        'package main\n\nimport (\n\t"fmt"\n\t"example.com/m/other"\n)\n',
        encoding="utf-8",
    )

    parser = CodeParser(repo_root=root)
    _, edges = parser.parse_file(caller)

    assert _import_targets(edges) == ["example.com/m/other", "fmt"]


def test_rust_imports_unchanged(tmp_path):
    root = tmp_path / "rs"
    src = root / "src"
    src.mkdir(parents=True)
    (root / "Cargo.toml").write_text(
        '[package]\nname = "demo"\nversion = "0.1.0"\n', encoding="utf-8",
    )
    helper = src / "helper.rs"
    helper.write_text("pub fn thing() {}\n", encoding="utf-8")
    caller = src / "main.rs"
    caller.write_text(
        "mod helper;\nuse crate::helper::thing;\n\nfn main() { thing(); }\n",
        encoding="utf-8",
    )

    parser = CodeParser(repo_root=root)
    _, edges = parser.parse_file(caller)

    assert _posix(helper) in _import_targets(edges)
