"""CI guard: a path-shaped IMPORTS_FROM target must name a file that exists.

An import edge target is either a bare module name the resolver could not
place (``os``, ``example.com/m/other`` has a slash but no repository file --
see below) or a claim about the filesystem. When it is a claim, it has to be
true, and true *case-exactly*: ``Path.is_file()`` answers "yes" to
``.../Registry.py`` on APFS when only ``registry.py`` exists, which is how
``from .registry import Registry`` resolving to the class name rather than
the module shipped unnoticed from a macOS workstation.

The guard builds a real graph over a repository laid out with the
import forms that used to break, then asserts:

* every path-shaped target exists case-exactly,
* no target escapes the repository root, and
* every relative import names the file CPython would import.

Which of those bites depends on the filesystem, and the split matters:

* ``test_no_import_target_claims_a_file_that_does_not_exist`` catches the old
  behaviour only where the filesystem is case-INSENSITIVE. Measured on a
  real case-sensitive APFS volume: on ``origin/staging`` the same fixture
  yields the bare target ``Registry`` there instead of ``.../Registry.py``,
  and a bare module name makes no claim about the filesystem, so the guard
  passes. On the default case-insensitive volume the same build produces two
  ``Registry.py`` offenders and the guard fails. CI's matrix is
  ``ubuntu-latest``, so this particular assertion is a macOS/Windows guard,
  not a Linux one.
* ``test_every_relative_import_resolves_to_the_right_file`` compares against
  ``ast``-derived ground truth, so it bites on every platform: 3 of 3
  relative imports wrong on ``origin/staging`` on the case-sensitive volume,
  0 of 3 here. That is the assertion the Linux matrix actually proves.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import _run_python_resolver, full_build
from tests.import_audit import (
    audit_imports,
    exists_case_exact,
    looks_like_a_path,
    missing_path_targets,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def built_repo(tmp_path: Path) -> tuple[Path, Path]:
    """A tiny git repository exercising every Python import form, built."""
    repo = tmp_path / "repo"
    package = repo / "app"
    sub = package / "sub"
    sub.mkdir(parents=True)

    # Bait for the repository-root clamp: real files ABOVE the repository that
    # a level-3 import from `app/` and a level-4 import from `app/sub/` would
    # land on if the walk were not clamped. Without these the escape test has
    # nothing to catch and passes on any implementation.
    (tmp_path / "outside.py").write_text("THING = 1\n", encoding="utf-8")
    (tmp_path / "outside_pkg").mkdir()
    (tmp_path / "outside_pkg" / "__init__.py").write_text(
        "OTHER = 2\n", encoding="utf-8",
    )

    (package / "__init__.py").write_text(
        "from .registry import Registry\n", encoding="utf-8",
    )
    # Lowercase on disk, class name capitalised: the exact shape that a
    # case-insensitive filesystem used to paper over.
    (package / "registry.py").write_text(
        "class Registry:\n    pass\n", encoding="utf-8",
    )
    (package / "graph.py").write_text(
        "def node_to_dict(node):\n    return {}\n", encoding="utf-8",
    )
    (package / "cli.py").write_text(
        "def main():\n    return 0\n", encoding="utf-8",
    )
    # A sibling whose name collides with a symbol imported from cli.py.
    (package / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    (sub / "__init__.py").write_text("", encoding="utf-8")
    (sub / "deep.py").write_text("def thing():\n    return 1\n", encoding="utf-8")

    (package / "consumer.py").write_text(
        "from . import graph\n"
        "from .registry import Registry\n"
        "from .cli import main\n"
        "from .graph import node_to_dict as n2d\n"
        "from .sub import deep\n"
        "from .sub.deep import thing\n"
        "from .graph import *\n"
        "import os\n"
        "from pathlib import Path\n"
        "\n"
        "\n"
        "def run(node):\n"
        "    return n2d(node), thing(), main(), Registry(), os, Path, graph, deep\n",
        encoding="utf-8",
    )
    (sub / "consumer.py").write_text(
        "from ..graph import node_to_dict\n"
        "from ..sub.deep import thing\n"
        "\n"
        "\n"
        "def run(node):\n"
        "    return node_to_dict(node), thing()\n",
        encoding="utf-8",
    )
    # Imports that walk ABOVE the repository root. `app/escape.py` needs three
    # dots to reach `tmp_path`, `app/sub/escape.py` four; both name files that
    # really are there. A resolver without the clamp answers with an absolute
    # path outside the repository, which is what the escape test looks for.
    (package / "escape.py").write_text(
        "from ...outside import THING\n"
        "from ...outside_pkg import OTHER\n"
        "\n"
        "\n"
        "def run():\n"
        "    return THING, OTHER\n",
        encoding="utf-8",
    )
    (sub / "escape.py").write_text(
        "from ....outside import THING\n"
        "\n"
        "\n"
        "def run():\n"
        "    return THING\n",
        encoding="utf-8",
    )

    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)

    db_path = repo / ".code-review-graph" / "graph.db"
    store = GraphStore(db_path)
    try:
        full_build(repo, store)
        _run_python_resolver(store)
    finally:
        store.close()
    return repo, db_path


def test_no_import_target_claims_a_file_that_does_not_exist(built_repo):
    repo, db_path = built_repo
    store = GraphStore(db_path)
    try:
        offenders = missing_path_targets(store._conn)
    finally:
        store.close()

    assert offenders == [], (
        "IMPORTS_FROM targets that look like paths but name no existing file "
        f"(case-exact): {offenders}"
    )


def test_the_fixture_actually_tries_to_escape_the_repository_root(built_repo):
    """Keep the escape test from passing because nothing tried to escape.

    The bait files exist above the repository root and the fixture imports
    them by name, so an unclamped resolver has something real to find.
    """
    repo, _ = built_repo
    above = repo.parent

    assert (above / "outside.py").is_file()
    assert (above / "outside_pkg" / "__init__.py").is_file()
    assert "from ...outside import THING" in (
        repo / "app" / "escape.py"
    ).read_text(encoding="utf-8")
    assert "from ....outside import THING" in (
        repo / "app" / "sub" / "escape.py"
    ).read_text(encoding="utf-8")


def test_no_import_target_escapes_the_repository_root(built_repo):
    repo, db_path = built_repo
    store = GraphStore(db_path)
    try:
        rows = store._conn.execute(
            "SELECT DISTINCT file_path, line, target_qualified FROM edges "
            "WHERE kind = 'IMPORTS_FROM'"
        ).fetchall()
    finally:
        store.close()

    root = repo.resolve()
    escape_lines = {
        (str((root / "app" / "escape.py").resolve()), 1),
        (str((root / "app" / "escape.py").resolve()), 2),
        (str((root / "app" / "sub" / "escape.py").resolve()), 1),
    }
    seen_escape_lines = set()
    for row in rows:
        target = row["target_qualified"]
        seen_escape_lines.add((row["file_path"], row["line"]))
        if not looks_like_a_path(target):
            continue
        path = Path(target.split("::", 1)[0])
        assert path.is_relative_to(root), f"{target} is outside {root}"

    # The escaping statements were parsed (they just resolved to nothing
    # path-shaped), so the loop above really examined them.
    assert escape_lines <= seen_escape_lines, (
        f"escaping imports produced no edge at all: "
        f"{escape_lines - seen_escape_lines}"
    )


def test_every_relative_import_resolves_to_the_right_file(built_repo):
    repo, db_path = built_repo
    store = GraphStore(db_path)
    try:
        stats = audit_imports(store._conn, repo / "app", repo.resolve())
    finally:
        store.close()

    assert stats["relative_total"] >= 12
    assert stats["relative_wrong"] == 0, stats["wrong_examples"]
    assert stats["relative_missing"] == 0
    # The three escaping imports name a file CPython could not reach from
    # inside this repository either, so they have no expected target.
    assert stats["relative_unresolvable_on_disk"] == 3
    assert (
        stats["relative_correct"] + stats["relative_unresolvable_on_disk"]
        == stats["relative_total"]
    )


def test_exists_case_exact_rejects_a_wrong_spelling(tmp_path):
    """The guard's own teeth: this is what fails on a case-insensitive FS."""
    real = tmp_path / "registry.py"
    real.write_text("x = 1\n", encoding="utf-8")

    assert exists_case_exact(real.as_posix())
    assert not exists_case_exact((tmp_path / "Registry.py").as_posix())
    assert not exists_case_exact((tmp_path / "nope.py").as_posix())
    assert not exists_case_exact("registry.py")


def test_looks_like_a_path_only_flags_resolved_targets():
    assert looks_like_a_path("/repo/app/graph.py")
    assert looks_like_a_path("/repo/app/graph.py::node_to_dict")
    assert looks_like_a_path("C:/repo/app/graph.py")
    # Unresolved module specifiers copied out of the source: not a claim
    # about this filesystem, and not this guard's business.
    assert not looks_like_a_path("os")
    assert not looks_like_a_path("pkg.module")
    assert not looks_like_a_path(".relative")
    assert not looks_like_a_path("./dep")
    assert not looks_like_a_path("vars/common.yml")
    assert not looks_like_a_path("package:flutter/material.dart")
    assert not looks_like_a_path("")


@pytest.fixture(scope="module")
def own_package_graph(tmp_path_factory) -> Path:
    """A graph built from a fresh copy of this project's own package.

    The previous version of this guard read ``.code-review-graph/graph.db``
    from the working tree. That file is gitignored, so the test was inert in
    CI and reported whatever a developer's last local build happened to
    contain -- and it skipped itself whenever the sources were newer. Copying
    the package into ``tmp_path`` and building there makes the guard say the
    same thing on every machine and on every CI run.
    """
    root = tmp_path_factory.mktemp("own-package") / "repo"
    shutil.copytree(
        REPO_ROOT / "code_review_graph",
        root / "code_review_graph",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)

    db_path = root / ".code-review-graph" / "graph.db"
    store = GraphStore(db_path)
    try:
        full_build(root, store)
        _run_python_resolver(store)
    finally:
        store.close()
    return db_path


def test_this_repository_has_no_missing_import_targets(own_package_graph):
    """Every path-shaped import target in our own package names a real file."""
    store = GraphStore(own_package_graph)
    try:
        offenders = missing_path_targets(store._conn)
    finally:
        store.close()

    assert offenders == [], offenders


def test_this_repository_resolves_every_relative_import(own_package_graph):
    """The audit's headline number, pinned so it cannot quietly regress."""
    repo = own_package_graph.parent.parent
    store = GraphStore(own_package_graph)
    try:
        stats = audit_imports(
            store._conn, repo / "code_review_graph", repo.resolve(),
        )
    finally:
        store.close()

    assert stats["relative_total"] > 200, stats
    assert stats["relative_wrong"] == 0, stats["wrong_examples"]
    assert stats["relative_missing"] == 0
    assert stats["relative_correct"] == stats["relative_total"]
