"""A module that replaces itself in ``sys.modules`` is not the module importers get.

Flattened-import compatibility shims look like::

    import importlib, sys
    sys.modules[__name__] = importlib.import_module("app.services.billing")

``import billing`` then hands the caller ``app/services/billing.py``. The
parser used to stop at the five-line shim: the import edge named the shim,
``billing.charge(...)`` was recorded against a module where ``charge`` is not
defined and so resolved to nothing, and ``from billing import charge``
qualified as ``billing.py::charge``, a node that does not exist. On a backend
with dozens of such shims the real modules looked almost unused.

These tests pin the exact targets: every call through a shim lands on the
definition, following re-exports, and a name the real module does not have
stays unresolved.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, incremental_update
from code_review_graph.parser import CodeParser
from code_review_graph.postprocessing import run_post_processing

SHIM = (
    "import importlib\n"
    "import sys\n"
    "\n"
    'sys.modules[__name__] = importlib.import_module("{module}")\n'
)

# --------------------------------------------------------------------------
# Fixture repository
#
#   app/services/engine.py    run_model()
#   app/services/billing.py   charge(); re-exports run_model from engine
#   billing.py                shim -> app.services.billing
#   legacy_billing.py         shim -> billing (a shim of a shim)
# --------------------------------------------------------------------------


def _write(root: Path, relative: str, source: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    _write(root, "app/__init__.py", "")
    _write(root, "app/services/__init__.py", "")
    _write(root, "app/services/engine.py", "def run_model(x):\n    return x\n")
    _write(
        root,
        "app/services/billing.py",
        "from app.services.engine import run_model\n"
        "\n"
        "\n"
        "def charge(amount):\n"
        "    return run_model(amount)\n",
    )
    _write(root, "billing.py", SHIM.format(module="app.services.billing"))
    _write(root, "legacy_billing.py", SHIM.format(module="billing"))
    return root


def _paths(repo_root: Path) -> dict[str, str]:
    resolved = repo_root.resolve()
    return {
        "billing": (resolved / "app/services/billing.py").as_posix(),
        "engine": (resolved / "app/services/engine.py").as_posix(),
        "shim": (resolved / "billing.py").as_posix(),
    }


def _resolved_store(repo_root: Path, tmp_path: Path) -> GraphStore:
    """Parse every file under *repo_root* and run the bare-endpoint resolvers."""
    store = GraphStore(tmp_path / "graph.db")
    parser = CodeParser(repo_root=repo_root)
    for path in sorted(repo_root.rglob("*.py")):
        nodes, edges = parser.parse_file(path)
        for node in nodes:
            store.upsert_node(node)
        for edge in edges:
            store.upsert_edge(edge)
    store.commit()
    store.resolve_bare_call_targets()
    store.resolve_bare_tested_by_sources()
    return store


def _edges(store: GraphStore, kind: str, file_path: Path) -> list:
    wanted = str(file_path.resolve().as_posix())
    return [
        e for e in store.get_all_edges()
        if e.kind == kind and e.file_path == wanted
    ]


def _call_targets(store: GraphStore, caller: Path) -> set[str]:
    return {e.target_qualified for e in _edges(store, "CALLS", caller)}


# -- import edges ---------------------------------------------------------


def test_import_of_a_shim_targets_the_real_module(repo, tmp_path):
    caller = _write(repo, "caller.py", "import billing\nfrom billing import charge\n")
    store = _resolved_store(repo, tmp_path)
    targets = {e.target_qualified for e in _edges(store, "IMPORTS_FROM", caller)}
    assert targets == {_paths(repo)["billing"]}


def test_shim_of_a_shim_is_followed_to_the_end(repo, tmp_path):
    caller = _write(
        repo, "caller.py",
        "import legacy_billing\n\n\ndef go():\n    legacy_billing.charge(1)\n",
    )
    store = _resolved_store(repo, tmp_path)
    paths = _paths(repo)
    assert {
        e.target_qualified for e in _edges(store, "IMPORTS_FROM", caller)
    } == {paths["billing"]}
    assert _call_targets(store, caller) == {f"{paths['billing']}::charge"}


# -- calls ---------------------------------------------------------------


def test_module_call_through_a_shim_resolves(repo, tmp_path):
    caller = _write(
        repo, "caller.py",
        "import billing\n\n\ndef go():\n    billing.charge(1)\n",
    )
    store = _resolved_store(repo, tmp_path)
    assert _call_targets(store, caller) == {f"{_paths(repo)['billing']}::charge"}


def test_module_call_follows_a_reexport_to_the_definition(repo, tmp_path):
    caller = _write(
        repo, "caller.py",
        "import billing\n\n\ndef go():\n    billing.run_model(1)\n",
    )
    store = _resolved_store(repo, tmp_path)
    assert _call_targets(store, caller) == {f"{_paths(repo)['engine']}::run_model"}


def test_module_call_follows_a_reexport_without_any_shim(repo, tmp_path):
    caller = _write(
        repo, "caller.py",
        "import app.services.billing as real\n\n\n"
        "def go():\n    real.run_model(1)\n",
    )
    store = _resolved_store(repo, tmp_path)
    assert _call_targets(store, caller) == {f"{_paths(repo)['engine']}::run_model"}


def test_from_shim_import_resolves(repo, tmp_path):
    caller = _write(
        repo, "caller.py",
        "from billing import charge, run_model\n\n\n"
        "def go():\n    charge(1)\n    run_model(2)\n",
    )
    store = _resolved_store(repo, tmp_path)
    paths = _paths(repo)
    assert _call_targets(store, caller) == {
        f"{paths['billing']}::charge",
        f"{paths['engine']}::run_model",
    }


def test_from_shim_import_as_alias_resolves_to_the_original_name(repo, tmp_path):
    caller = _write(
        repo, "caller.py",
        "from billing import charge as pay\n"
        "from billing import run_model as infer\n\n\n"
        "def go():\n    pay(1)\n    infer(2)\n",
    )
    store = _resolved_store(repo, tmp_path)
    paths = _paths(repo)
    assert _call_targets(store, caller) == {
        f"{paths['billing']}::charge",
        f"{paths['engine']}::run_model",
    }


def test_from_import_as_alias_resolves_without_any_shim(repo, tmp_path):
    caller = _write(
        repo, "caller.py",
        "from app.services.billing import charge as pay\n\n\n"
        "def go():\n    pay(1)\n",
    )
    store = _resolved_store(repo, tmp_path)
    assert _call_targets(store, caller) == {f"{_paths(repo)['billing']}::charge"}


def test_name_the_real_module_lacks_stays_unresolved(repo, tmp_path):
    caller = _write(
        repo, "caller.py",
        "import billing\n\n\ndef go():\n    billing.refund(1)\n",
    )
    store = _resolved_store(repo, tmp_path)
    assert _call_targets(store, caller) == {"refund"}
    assert not any(
        "::refund" in e.target_qualified for e in store.get_all_edges()
    )


# -- tests ----------------------------------------------------------------


def test_tested_by_through_a_shim_names_the_real_function(repo, tmp_path):
    _write(
        repo, "tests/test_billing.py",
        "import billing\n\n\n"
        "def test_charge():\n    assert billing.charge(1) == 1\n",
    )
    store = _resolved_store(repo, tmp_path)
    sources = {
        e.source_qualified for e in store.get_all_edges() if e.kind == "TESTED_BY"
    }
    assert sources == {f"{_paths(repo)['billing']}::charge"}


# -- what is not a shim ---------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        # computed module name
        "import importlib, sys\nname = 'app.services.billing'\n"
        "sys.modules[__name__] = importlib.import_module(name)\n",
        # relative module name needs a package argument
        "import importlib, sys\n"
        "sys.modules[__name__] = importlib.import_module('.billing')\n",
        # conditional swap
        "import importlib, sys\nif True:\n"
        "    sys.modules[__name__] = importlib.import_module("
        "'app.services.billing')\n",
        # target outside the repository
        "import importlib, sys\n"
        "sys.modules[__name__] = importlib.import_module('json')\n",
    ],
    ids=["computed", "relative", "conditional", "outside-repo"],
)
def test_unfollowable_swap_keeps_the_file_as_the_module(repo, tmp_path, source):
    shim = _write(repo, "odd.py", source)
    caller = _write(repo, "caller.py", "import odd\n")
    store = _resolved_store(repo, tmp_path)
    assert {
        e.target_qualified for e in _edges(store, "IMPORTS_FROM", caller)
    } == {str(shim.resolve().as_posix())}


def test_shim_cycle_terminates(tmp_path):
    root = tmp_path / "repo"
    _write(root, "a.py", SHIM.format(module="b"))
    _write(root, "b.py", SHIM.format(module="a"))
    caller = _write(root, "caller.py", "import a\n")
    store = _resolved_store(root, tmp_path)
    targets = {e.target_qualified for e in _edges(store, "IMPORTS_FROM", caller)}
    assert len(targets) == 1
    assert targets <= {
        (root / "a.py").resolve().as_posix(),
        (root / "b.py").resolve().as_posix(),
    }


# -- build / update parity -----------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _edge_rows(store: GraphStore) -> list[tuple]:
    return sorted(
        (e.kind, e.source_qualified, e.target_qualified, e.file_path)
        for e in store.get_all_edges()
    )


def test_incremental_update_matches_a_rebuild(repo, tmp_path):
    _write(
        repo, "caller.py",
        "import billing\n\n\n"
        "def go():\n    billing.charge(1)\n    billing.refund(2)\n",
    )
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "initial")

    store = GraphStore(repo / ".code-review-graph" / "graph.db")
    full_build(repo, store)
    run_post_processing(store)
    first = _edge_rows(store)
    # Running the post-processing pass again changes nothing.
    run_post_processing(store)
    assert _edge_rows(store) == first

    base = _git(repo, "rev-parse", "HEAD")
    real = repo / "app/services/billing.py"
    real.write_text(
        real.read_text(encoding="utf-8")
        + "\n\ndef refund(amount):\n    return -amount\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add refund")
    incremental_update(repo, store, base=base)
    run_post_processing(store)
    updated = _edge_rows(store)
    store.close()

    paths = _paths(repo)
    caller = str((repo / "caller.py").resolve().as_posix())
    assert ("CALLS", f"{caller}::go", f"{paths['billing']}::refund", caller) in updated

    rebuilt = GraphStore(tmp_path / "rebuild.db")
    try:
        full_build(repo, rebuilt)
        run_post_processing(rebuilt)
        assert _edge_rows(rebuilt) == updated
    finally:
        rebuilt.close()


# -- review follow-ups ------------------------------------------------------


def test_last_of_several_swaps_wins(repo, tmp_path):
    _write(
        repo, "twice.py",
        "import importlib\nimport sys\n\n"
        'sys.modules[__name__] = importlib.import_module("app.services.engine")\n'
        'sys.modules[__name__] = importlib.import_module("app.services.billing")\n',
    )
    caller = _write(repo, "caller.py", "import twice\n")
    store = _resolved_store(repo, tmp_path)
    assert {
        e.target_qualified for e in _edges(store, "IMPORTS_FROM", caller)
    } == {_paths(repo)["billing"]}


def test_literal_swap_overwritten_by_an_unfollowable_one_is_declined(repo, tmp_path):
    shim = _write(
        repo, "overwritten.py",
        "import importlib\nimport sys\n\n"
        'sys.modules[__name__] = importlib.import_module("app.services.billing")\n'
        "name = 'app.services.engine'\n"
        "sys.modules[__name__] = importlib.import_module(name)\n",
    )
    caller = _write(repo, "caller.py", "import overwritten\n")
    store = _resolved_store(repo, tmp_path)
    assert {
        e.target_qualified for e in _edges(store, "IMPORTS_FROM", caller)
    } == {str(shim.resolve().as_posix())}


def test_star_import_through_a_shim_resolves(repo, tmp_path):
    caller = _write(
        repo, "caller.py",
        "from billing import *\n\n\ndef go():\n    charge(1)\n    run_model(2)\n",
    )
    store = _resolved_store(repo, tmp_path)
    paths = _paths(repo)
    assert _call_targets(store, caller) == {
        f"{paths['billing']}::charge",
        f"{paths['engine']}::run_model",
    }


def test_recursive_star_import_through_a_shim_resolves(repo, tmp_path):
    _write(repo, "facade.py", "from billing import *\n")
    caller = _write(
        repo, "caller.py",
        "from facade import *\n\n\ndef go():\n    charge(1)\n    run_model(2)\n",
    )
    store = _resolved_store(repo, tmp_path)
    paths = _paths(repo)
    assert _call_targets(store, caller) == {
        f"{paths['billing']}::charge",
        f"{paths['engine']}::run_model",
    }


def test_renamed_reexport_is_never_sent_to_an_unrelated_same_named_function(tmp_path):
    root = tmp_path / "repo"
    _write(
        root, "pkg/impl.py",
        "def charge(x):\n    return x\n\n\ndef pay(x):\n    return -x\n",
    )
    _write(root, "pkg/__init__.py", "from .impl import charge as pay\n")
    caller = _write(
        root, "caller.py",
        "import pkg\nfrom pkg import pay\n\n\n"
        "def go():\n    pkg.pay(1)\n    pay(2)\n",
    )
    store = _resolved_store(root, tmp_path)
    unrelated = f"{(root / 'pkg/impl.py').resolve().as_posix()}::pay"
    assert unrelated not in _call_targets(store, caller)


def test_later_unaliased_import_drops_the_earlier_alias(tmp_path):
    root = tmp_path / "repo"
    _write(root, "a.py", "def foo():\n    return 1\n")
    _write(root, "b.py", "def bar():\n    return 2\n\n\ndef foo():\n    return 3\n")
    caller = _write(
        root, "caller.py",
        "from a import foo as bar\nfrom b import bar\n\n\ndef go():\n    bar()\n",
    )
    store = _resolved_store(root, tmp_path)
    assert _call_targets(store, caller) == {f"{(root / 'b.py').resolve().as_posix()}::bar"}


@pytest.mark.parametrize(
    "source",
    [
        # a local function shadows importlib's
        "import sys\n\n\ndef import_module(name):\n    return sys.modules[__name__]\n\n\n"
        "sys.modules[__name__] = import_module('app.services.billing')\n",
        # `importlib` rebound to something else
        "import sys\nimport importlib\nimportlib = object()\n"
        "sys.modules[__name__] = importlib.import_module('app.services.billing')\n",
        # bare call with no import at all
        "import sys\n"
        "sys.modules[__name__] = import_module('app.services.billing')\n",
    ],
    ids=["local-def", "rebound-importlib", "unbound"],
)
def test_import_module_that_is_not_importlibs_is_declined(repo, tmp_path, source):
    shim = _write(repo, "fake.py", source)
    caller = _write(repo, "caller.py", "import fake\n")
    store = _resolved_store(repo, tmp_path)
    assert {
        e.target_qualified for e in _edges(store, "IMPORTS_FROM", caller)
    } == {str(shim.resolve().as_posix())}


@pytest.mark.parametrize(
    "header",
    [
        "from importlib import import_module\nimport sys\n",
        "import importlib.util\nimport sys\n",
    ],
    ids=["from-importlib", "dotted-import"],
)
def test_importlib_spellings_are_followed(repo, tmp_path, header):
    call = (
        "import_module" if "from importlib" in header else "importlib.import_module"
    )
    _write(
        repo, "spelled.py",
        header + f"sys.modules[__name__] = {call}('app.services.billing')\n",
    )
    caller = _write(repo, "caller.py", "import spelled\n")
    store = _resolved_store(repo, tmp_path)
    assert {
        e.target_qualified for e in _edges(store, "IMPORTS_FROM", caller)
    } == {_paths(repo)["billing"]}


@pytest.mark.parametrize(
    "header",
    [
        "import importlib\nimport fake_sys as sys\n",
        "import importlib\nimport sys\nsys = object()\n",
    ],
    ids=["aliased-sys", "rebound-sys"],
)
def test_swap_on_something_that_is_not_sys_is_declined(repo, tmp_path, header):
    shim = _write(
        repo, "notsys.py",
        header + "sys.modules[__name__] = importlib.import_module('app.services.billing')\n",
    )
    caller = _write(repo, "caller.py", "import notsys\n")
    store = _resolved_store(repo, tmp_path)
    assert {
        e.target_qualified for e in _edges(store, "IMPORTS_FROM", caller)
    } == {shim.resolve().as_posix()}


def test_submodule_of_a_package_installed_by_a_shim(repo, tmp_path):
    _write(repo, "app/services/child.py", "def helper():\n    return 1\n")
    _write(repo, "services.py", SHIM.format(module="app.services"))
    caller = _write(
        repo, "caller.py", "from services import child\n\n\ndef go():\n    child.helper()\n",
    )
    store = _resolved_store(repo, tmp_path)
    child = (repo / "app/services/child.py").resolve().as_posix()
    assert child in {e.target_qualified for e in _edges(store, "IMPORTS_FROM", caller)}
    assert _call_targets(store, caller) == {f"{child}::helper"}


def test_symlink_outside_the_repository_is_never_read(tmp_path, monkeypatch):
    outside = _write(
        tmp_path / "elsewhere", "linked.py",
        "import importlib\nimport sys\n\n"
        "sys.modules[__name__] = importlib.import_module('pkg.real')\n",
    )
    root = tmp_path / "repo"
    _write(root, "pkg/__init__.py", "")
    _write(root, "pkg/real.py", "def f():\n    return 1\n")
    try:
        (root / "pkg/linked.py").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    _write(root, "caller.py", "from pkg import linked\n")

    reads: list[Path] = []
    original_read_bytes = Path.read_bytes

    def counting_read_bytes(path: Path) -> bytes:
        if path.resolve() == outside.resolve():
            reads.append(path)
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", counting_read_bytes)
    parser = CodeParser(repo_root=root)
    parser.parse_file(root / "caller.py")
    assert reads == []
