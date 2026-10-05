"""A Python method call belongs to its receiver, not to the file's imports.

``x.get(...)`` calls the ``get`` that belongs to whatever ``x`` holds. The
bare-target resolver used to accept any same-named node the call-site file
imported, so ``some_dict.get(...)`` was recorded as a call into
``ConnectionPool.get`` — and the better the import edges got, the more of
those it manufactured.

Every test here pins the EXACT target, in both directions: the calls that
must resolve to one specific node, and the calls that must resolve to
nothing at all rather than to a plausible-looking wrong one.
"""

from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.parser import CodeParser

# --------------------------------------------------------------------------
# Fixture repository
#
#   pkg/__init__.py
#   pkg/runner.py       run(), aggregate(), class Runner: start(), get()
#   pkg/other.py        run()            -- same bare name, different module
#   pkg/pool.py         class Pool: get()
# --------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    package = root / "pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "runner.py").write_text(
        "def run(x):\n"
        "    return x\n"
        "\n"
        "\n"
        "def aggregate(rows):\n"
        "    return rows\n"
        "\n"
        "\n"
        "class Runner:\n"
        "    def start(self):\n"
        "        return 1\n"
        "\n"
        "    def get(self, key):\n"
        "        return key\n",
        encoding="utf-8",
    )
    (package / "other.py").write_text(
        "def run(x):\n    return x\n", encoding="utf-8",
    )
    (package / "pool.py").write_text(
        "class Pool:\n"
        "    def get(self, key):\n"
        "        return key\n",
        encoding="utf-8",
    )
    return root


def _write(repo_root: Path, relative: str, source: str) -> Path:
    path = repo_root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def _resolved_store(repo_root: Path, tmp_path: Path) -> GraphStore:
    """Parse every file under *repo_root* and run the bare-target resolver."""
    store = GraphStore(tmp_path / "graph.db")
    parser = CodeParser(repo_root=repo_root)
    for path in sorted(repo_root.rglob("*.py")):
        nodes, edges = parser.parse_file(path)
        for node in nodes:
            store.upsert_node(node)
        for edge in edges:
            store.upsert_edge(edge)
    store.commit()
    # The same resolution the build/postprocess pipeline runs.
    store.resolve_bare_call_targets()
    store.resolve_bare_tested_by_sources()
    return store


def _call_targets(store: GraphStore, caller_file: Path) -> set[str]:
    rows = store._conn.execute(
        "SELECT target_qualified FROM edges "
        "WHERE kind = 'CALLS' AND file_path = ?",
        (caller_file.resolve().as_posix(),),
    ).fetchall()
    return {row["target_qualified"] for row in rows}


def _posix(path: Path) -> str:
    return path.resolve().as_posix()


# --------------------------------------------------------------------------
# The module-receiver rule
# --------------------------------------------------------------------------


def test_absolute_submodule_receiver_resolves_to_that_module(repo, tmp_path):
    """``from pkg import runner`` then ``runner.run(...)`` names one node."""
    caller = _write(
        repo,
        "caller.py",
        "from pkg import runner\n"
        "\n"
        "\n"
        "def go():\n"
        "    return runner.run(1)\n",
    )
    store = _resolved_store(repo, tmp_path)
    try:
        assert _call_targets(store, caller) == {
            f"{_posix(repo / 'pkg' / 'runner.py')}::run",
        }
    finally:
        store.close()


def test_relative_submodule_receiver_resolves_to_that_module(repo, tmp_path):
    """``from . import runner`` inside the package resolves the same way."""
    caller = _write(
        repo,
        "pkg/caller.py",
        "from . import runner\n"
        "\n"
        "\n"
        "def go(rows):\n"
        "    return runner.aggregate(rows)\n",
    )
    store = _resolved_store(repo, tmp_path)
    try:
        assert _call_targets(store, caller) == {
            f"{_posix(repo / 'pkg' / 'runner.py')}::aggregate",
        }
    finally:
        store.close()


def test_module_receiver_ignores_a_same_named_function_elsewhere(repo, tmp_path):
    """``pkg.other`` also defines ``run``; the receiver decides which."""
    caller = _write(
        repo,
        "caller.py",
        "from pkg import other, runner\n"
        "\n"
        "\n"
        "def go():\n"
        "    return other.run(1)\n",
    )
    store = _resolved_store(repo, tmp_path)
    try:
        assert _call_targets(store, caller) == {
            f"{_posix(repo / 'pkg' / 'other.py')}::run",
        }
    finally:
        store.close()


def test_module_receiver_is_found_when_the_import_is_inside_a_function(
    repo, tmp_path,
):
    """Test files import inside the test; the file-scope map never sees it."""
    caller = _write(
        repo,
        "caller.py",
        "def go():\n"
        "    from pkg import runner\n"
        "\n"
        "    return runner.run(1)\n",
    )
    store = _resolved_store(repo, tmp_path)
    try:
        assert _call_targets(store, caller) == {
            f"{_posix(repo / 'pkg' / 'runner.py')}::run",
        }
    finally:
        store.close()


def test_module_receiver_never_reaches_into_a_class(repo, tmp_path):
    """``runner.start()`` is a module attribute, not ``Runner.start``."""
    caller = _write(
        repo,
        "caller.py",
        "from pkg import runner\n"
        "\n"
        "\n"
        "def go():\n"
        "    return runner.start()\n",
    )
    store = _resolved_store(repo, tmp_path)
    try:
        assert _call_targets(store, caller) == {"start"}
    finally:
        store.close()


# --------------------------------------------------------------------------
# The do-not-attribute rule
# --------------------------------------------------------------------------


def test_dict_literal_receiver_is_not_attributed(repo, tmp_path):
    """``cfg = {}`` then ``cfg.get(...)`` is builtin dict.get, and nothing else."""
    caller = _write(
        repo,
        "caller.py",
        "from pkg.pool import Pool\n"
        "\n"
        "\n"
        "def go():\n"
        "    cfg = {}\n"
        "    return cfg.get('k')\n",
    )
    store = _resolved_store(repo, tmp_path)
    try:
        assert _call_targets(store, caller) == {"get"}
    finally:
        store.close()


def test_list_literal_receiver_is_not_attributed(repo, tmp_path):
    """The same holds for every container literal, without naming a method."""
    caller = _write(
        repo,
        "caller.py",
        "from pkg.runner import Runner\n"
        "\n"
        "\n"
        "def go():\n"
        "    rows = []\n"
        "    return rows.get(0)\n",
    )
    store = _resolved_store(repo, tmp_path)
    try:
        assert _call_targets(store, caller) == {"get"}
    finally:
        store.close()


def test_unknown_local_receiver_is_not_attributed(repo, tmp_path):
    """A factory's return type is not visible, so nothing may be claimed."""
    caller = _write(
        repo,
        "caller.py",
        "from pkg.runner import Runner\n"
        "\n"
        "\n"
        "def make():\n"
        "    return Runner()\n"
        "\n"
        "\n"
        "def go():\n"
        "    handler = make()\n"
        "    return handler.start()\n",
    )
    store = _resolved_store(repo, tmp_path)
    try:
        targets = _call_targets(store, caller)
        assert "start" in targets
        assert f"{_posix(repo / 'pkg' / 'runner.py')}::Runner.start" not in targets
    finally:
        store.close()


def test_expression_receiver_is_not_attributed(repo, tmp_path):
    """``os.environ.get(...)`` must not become this file's own ``get``."""
    caller = _write(
        repo,
        "caller.py",
        "import os\n"
        "\n"
        "\n"
        "def get(key):\n"
        "    return key\n"
        "\n"
        "\n"
        "def go():\n"
        "    return os.environ.get('HOME')\n",
    )
    store = _resolved_store(repo, tmp_path)
    try:
        assert _call_targets(store, caller) == {"get"}
    finally:
        store.close()


def test_contradictory_bindings_are_not_attributed(repo, tmp_path):
    """A name that is a Pool here and a dict there says nothing at either."""
    caller = _write(
        repo,
        "caller.py",
        "from pkg.pool import Pool\n"
        "\n"
        "\n"
        "def go():\n"
        "    thing = Pool()\n"
        "    thing = {}\n"
        "    return thing.get('k')\n",
    )
    store = _resolved_store(repo, tmp_path)
    try:
        assert _call_targets(store, caller) == {
            "get",
            f"{_posix(repo / 'pkg' / 'pool.py')}::Pool",
        }
    finally:
        store.close()


# --------------------------------------------------------------------------
# Evidence that DOES justify an attribution
# --------------------------------------------------------------------------


def test_constructed_receiver_resolves_to_that_class(repo, tmp_path):
    """``pool = Pool()`` then ``pool.get(...)`` is ``Pool.get`` and only that."""
    caller = _write(
        repo,
        "caller.py",
        "from pkg.pool import Pool\n"
        "from pkg.runner import Runner\n"
        "\n"
        "\n"
        "def go():\n"
        "    pool = Pool()\n"
        "    return pool.get('k')\n",
    )
    store = _resolved_store(repo, tmp_path)
    try:
        assert _call_targets(store, caller) == {
            f"{_posix(repo / 'pkg' / 'pool.py')}::Pool",
            f"{_posix(repo / 'pkg' / 'pool.py')}::Pool.get",
        }
    finally:
        store.close()


def test_annotated_receiver_resolves_to_that_class(repo, tmp_path):
    """An annotation is evidence even when the value comes from elsewhere."""
    caller = _write(
        repo,
        "caller.py",
        "from pkg.pool import Pool\n"
        "from pkg.runner import Runner\n"
        "\n"
        "\n"
        "def go(pool: Pool):\n"
        "    return pool.get('k')\n",
    )
    store = _resolved_store(repo, tmp_path)
    try:
        assert _call_targets(store, caller) == {
            f"{_posix(repo / 'pkg' / 'pool.py')}::Pool.get",
        }
    finally:
        store.close()


def test_self_field_constructed_in_another_method_resolves(repo, tmp_path):
    """``self.pool = Pool()`` in setup, ``self.pool.get()`` in a test."""
    caller = _write(
        repo,
        "caller.py",
        "from pkg.pool import Pool\n"
        "\n"
        "\n"
        "class TestPool:\n"
        "    def setup_method(self):\n"
        "        self.pool = Pool()\n"
        "\n"
        "    def test_get(self):\n"
        "        return self.pool.get('k')\n",
    )
    store = _resolved_store(repo, tmp_path)
    try:
        assert f"{_posix(repo / 'pkg' / 'pool.py')}::Pool.get" in _call_targets(
            store, caller,
        )
    finally:
        store.close()


def test_receiver_evidence_is_scoped_to_its_function(repo, tmp_path):
    """One name, two functions, two values: only the justified one resolves."""
    caller = _write(
        repo,
        "caller.py",
        "from pkg.pool import Pool\n"
        "\n"
        "\n"
        "def real():\n"
        "    thing = Pool()\n"
        "    return thing.get('k')\n"
        "\n"
        "\n"
        "def fake():\n"
        "    thing = {}\n"
        "    return thing.get('k')\n",
    )
    store = _resolved_store(repo, tmp_path)
    try:
        rows = store._conn.execute(
            "SELECT line, target_qualified FROM edges "
            "WHERE kind = 'CALLS' AND file_path = ? ORDER BY line",
            (_posix(caller),),
        ).fetchall()
        by_line = {row["line"]: row["target_qualified"] for row in rows}
        assert by_line[6] == f"{_posix(repo / 'pkg' / 'pool.py')}::Pool.get"
        assert by_line[11] == "get"
    finally:
        store.close()
