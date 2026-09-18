"""Does an existing user's graph survive the upgrade?

Every other migration test in this suite builds its database with the code
under test and then migrates it, so a migration is only ever exercised against
a database the test just made. That cannot catch the failure that matters: the
graph on a real user's disk was written by a *released* version, weeks ago,
over a *real* repository, and the first thing they run after upgrading is
``status`` and ``update``.

This module closes that gap by testing the released artefact, not a
reconstruction of it:

1. ``uv venv`` + ``uv pip install code-review-graph==<released version>`` into
   a throwaway environment. Nothing from the working tree is on that
   interpreter's path, and the fixture asserts as much.
2. That old release builds a graph over this repository's own tree exported at
   the tag the release was cut from (``git archive v<version>``), so the corpus
   is realistic and pinned to the release rather than drifting with ``main``.
3. The *current* code is then pointed at that database and runs what a user
   runs first: ``status``, an ``update``, and a spread of MCP tool functions.
4. The assertions are about the outcome: migrations apply, the schema version
   lands on ``LATEST_VERSION``, no node or edge is lost, queries answer, and
   nothing raises.

Three releases are covered so a user who skipped a version is covered too, and
the reverse direction (an old release opening a database the new code has
already migrated) is checked as well.

Cost and opt-in
---------------
Each release costs a fresh virtual environment (~430 MB), a full parse of a
~290-file repository, and roughly a minute of wall clock; the whole module is
several minutes and needs network access to PyPI. It is therefore not part of
the normal suite. It is skipped unless ``CRG_UPGRADE_TEST=1`` is set, and it
carries the ``upgrade`` marker so CI can exclude it by name:

    CRG_UPGRADE_TEST=1 pytest -m upgrade tests/test_upgrade_path.py -q

Canaries
--------
A check that passes because it silently did nothing is worse than no check, so
several assertions exist purely to prove this one ran and compared something
real: that the old interpreter imported the released wheel and not the working
tree, that the old build produced a graph of real size, and that the database
it produced was genuinely behind the current schema before the current code
touched it. If any of those stop holding, the module fails rather than passing
vacuously.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import pytest

from code_review_graph.migrations import LATEST_VERSION

REPO_ROOT = Path(__file__).resolve().parents[1]

# The three most recent releases on PyPI, newest first. Pinned rather than
# discovered so the corpus is reproducible; ``test_release_list_tracks_the_
# newest_tag`` fails if a newer release is tagged and this list is not updated,
# which is the only way this check can quietly go stale.
RELEASES = ("2.3.8", "2.3.7", "2.3.6")

OPT_IN_ENV = "CRG_UPGRADE_TEST"

# The file edited to provoke an incremental update. Present in every covered
# tag, parsed as Python, and small enough that the update stays cheap.
EDITED_RELATIVE_PATH = "code_review_graph/hints.py"
NEW_FUNCTION_NAME = "crg_upgrade_path_probe"
REVERSE_FUNCTION_NAME = "crg_downgrade_path_probe"

# A symbol that exists in graph.py in every covered tag. The dotted spelling
# resolves through the ``nodes.symbol`` column that migration v10 introduces,
# so it is the query most likely to break on an upgraded database.
KNOWN_SYMBOL = "upsert_node"
KNOWN_DOTTED_SYMBOL = "GraphStore.upsert_node"
KNOWN_FILE = "code_review_graph/graph.py"

# Floors for the canary that the old release really parsed a real repository.
# The smallest covered tag produced 3282 nodes / 23793 edges / 179 files; these
# sit far enough below that to tolerate parser drift but well above the "the
# build silently did nothing" range.
MIN_BASELINE_NODES = 1000
MIN_BASELINE_EDGES = 5000
MIN_BASELINE_FILES = 50

# How much of the graph the first post-upgrade ``update`` must keep. Re-parsing
# with a newer parser legitimately respells some identities (the C++ overload
# rework between 2.3.6 and now is the largest single example, and moved under
# 1% of nodes), so this is a floor against wholesale loss, not an exact match.
MIN_RETAINED_FRACTION = 0.95

_MISSING_TOOLS = [
    name for name in ("uv", "git") if shutil.which(name) is None
]

pytestmark = [
    pytest.mark.upgrade,
    pytest.mark.skipif(
        not os.environ.get(OPT_IN_ENV),
        reason=(
            f"opt-in: set {OPT_IN_ENV}=1 (installs three releases from PyPI, "
            "several minutes and ~600 MB of disk at a time)"
        ),
    ),
    pytest.mark.skipif(
        bool(_MISSING_TOOLS),
        reason=f"needs {' and '.join(_MISSING_TOOLS)} on PATH",
    ),
    pytest.mark.skipif(
        not (REPO_ROOT / ".git").exists(),
        reason="needs a git checkout to export a tagged corpus from",
    ),
]


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------


def _run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 1800,
) -> subprocess.CompletedProcess[str]:
    """Run *cmd*, capturing output, without ever inheriting stdin."""
    return subprocess.run(
        cmd,
        cwd=None if cwd is None else str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        check=False,
    )


def _check(result: subprocess.CompletedProcess[str], what: str) -> str:
    """Return stdout, or fail the setup with the full output attached."""
    if result.returncode != 0:
        raise AssertionError(
            f"{what} failed with exit code {result.returncode}\n"
            f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
        )
    return result.stdout


def _base_env(crg_home: Path) -> dict[str, str]:
    """Environment shared by both halves: no per-user state, no data-dir override.

    ``CRG_DATA_DIR`` and a registry entry in ``CRG_HOME`` both redirect
    ``get_data_dir``; leaving either at the developer's value would move the
    database out of the corpus and make the whole comparison meaningless.
    """
    env = dict(os.environ)
    env.pop("CRG_DATA_DIR", None)
    env.pop("VIRTUAL_ENV", None)
    env["CRG_HOME"] = str(crg_home)
    env["HERMES_HOME"] = str(crg_home / "hermes")
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _old_env(crg_home: Path) -> dict[str, str]:
    """Environment for the released install: the working tree must not leak in.

    ``PYTHONPATH`` is dropped and ``PYTHONSAFEPATH`` set, because ``python -c``
    otherwise puts the current directory on ``sys.path`` — and the current
    directory during a test run is this checkout, which contains an importable
    ``code_review_graph``. Without this the "old" half would import the code
    under test and the whole module would pass for free.
    """
    env = _base_env(crg_home)
    env.pop("PYTHONPATH", None)
    env["PYTHONSAFEPATH"] = "1"
    return env


def _venv_bin(venv: Path) -> Path:
    return venv / ("Scripts" if os.name == "nt" else "bin")


def _venv_python(venv: Path) -> Path:
    return _venv_bin(venv) / ("python.exe" if os.name == "nt" else "python")


def _venv_script(venv: Path, name: str) -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    return _venv_bin(venv) / f"{name}{suffix}"


def _current_cli(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Drive the CURRENT code's CLI in a subprocess, exactly as a user would.

    ``cli.py`` has no ``__main__`` guard, so ``-m`` cannot be used; importing
    ``main`` and re-pointing ``argv[0]`` gives the same argument parsing and
    exit code as the installed console script.
    """
    bootstrap = (
        "import sys; sys.argv[0] = 'code-review-graph'; "
        "from code_review_graph.cli import main; main()"
    )
    return _run([sys.executable, "-c", bootstrap, *args], env=env)


# ---------------------------------------------------------------------------
# Database helpers (read-only, own connection, never the code under test)
# ---------------------------------------------------------------------------


def _db_path(corpus: Path) -> Path:
    return corpus / ".code-review-graph" / "graph.db"


def _snapshot(corpus: Path) -> dict[str, Any]:
    """Read the facts under test straight out of SQLite.

    Deliberately does not go through ``GraphStore``: opening the store runs the
    migrations, which is the very thing being measured.
    """
    conn = sqlite3.connect(str(_db_path(corpus)))
    try:
        row = conn.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone()
        columns = {r[1] for r in conn.execute("PRAGMA table_info(nodes)")}
        snapshot: dict[str, Any] = {
            "schema_version": int(row[0]) if row else None,
            "nodes": conn.execute("SELECT count(*) FROM nodes").fetchone()[0],
            "edges": conn.execute("SELECT count(*) FROM edges").fetchone()[0],
            "files": conn.execute(
                "SELECT count(*) FROM nodes WHERE kind = 'File'"
            ).fetchone()[0],
            "has_symbol_column": "symbol" in columns,
            "qualified_names": frozenset(
                r[0] for r in conn.execute("SELECT qualified_name FROM nodes")
            ),
            "file_names": frozenset(
                r[0] for r in conn.execute(
                    "SELECT qualified_name FROM nodes WHERE kind = 'File'"
                )
            ),
            "indexes": {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                ) if r[0]
            },
        }
        snapshot["null_symbols"] = (
            conn.execute(
                "SELECT count(*) FROM nodes WHERE symbol IS NULL"
            ).fetchone()[0]
            if snapshot["has_symbol_column"]
            else None
        )
        return snapshot
    finally:
        conn.close()


def _symbol_for(corpus: Path, qualified_name: str) -> str | None:
    conn = sqlite3.connect(str(_db_path(corpus)))
    try:
        row = conn.execute(
            "SELECT symbol FROM nodes WHERE qualified_name = ?", (qualified_name,)
        ).fetchone()
        return None if row is None else row[0]
    finally:
        conn.close()


def _find_qualified_name(corpus: Path, suffix: str) -> str | None:
    conn = sqlite3.connect(str(_db_path(corpus)))
    try:
        row = conn.execute(
            "SELECT qualified_name FROM nodes WHERE qualified_name LIKE ? LIMIT 1",
            (f"%::{suffix}",),
        ).fetchone()
        return None if row is None else row[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Corpus construction
# ---------------------------------------------------------------------------


def _export_tag(tag: str, destination: Path, workdir: Path) -> None:
    """Materialise this repository's tree at *tag* as a standalone checkout."""
    archive = workdir / f"{tag}.tar"
    _check(
        _run([
            "git", "-C", str(REPO_ROOT), "archive",
            "--format=tar", "--output", str(archive), tag,
        ]),
        f"git archive {tag}",
    )
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as tar:
        tar.extractall(destination, filter="data")
    archive.unlink()


def _git(corpus: Path, env: dict[str, str]) -> tuple[list[str], dict[str, str]]:
    """A git invocation that ignores the developer's own git configuration.

    Global and system config are pointed at a file that does not exist so a
    hook, commit template or signing key cannot change what gets committed.
    """
    git_env = dict(env)
    git_env["GIT_CONFIG_GLOBAL"] = str(corpus / ".absent-git-config")
    git_env["GIT_CONFIG_SYSTEM"] = str(corpus / ".absent-git-config")
    argv = [
        "git",
        "-c", "init.defaultBranch=main",
        "-c", "user.name=upgrade-path-test",
        "-c", "user.email=upgrade-path-test@invalid",
        "-c", "commit.gpgsign=false",
        "-C", str(corpus),
    ]
    return argv, git_env


def _init_corpus_repo(corpus: Path, env: dict[str, str]) -> None:
    """Make the exported tree a real one-commit git repository.

    ``incremental_update`` and the provenance metadata both read git, so the
    corpus has to be a checkout rather than a loose directory.
    """
    base, git_env = _git(corpus, env)
    _check(_run([*base, "init", "--quiet"], env=git_env), "git init")
    _commit_all(corpus, env, "corpus")


def _commit_all(corpus: Path, env: dict[str, str], message: str) -> None:
    """Commit the working tree.

    Both the current and the released ``update`` default to diffing ``HEAD~1``,
    so an edit has to land in a commit before either of them will see it.
    """
    base, git_env = _git(corpus, env)
    _check(_run([*base, "add", "--all"], env=git_env), "git add")
    _check(
        _run([*base, "commit", "--quiet", "-m", message], env=git_env),
        f"git commit ({message})",
    )


def _append_function(path: Path, name: str) -> None:
    path.write_text(
        path.read_text(encoding="utf-8")
        + f"\n\ndef {name}():\n    return 1\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# The run record
# ---------------------------------------------------------------------------


@dataclass
class UpgradeRun:
    """Everything one released version's upgrade produced, measured once."""

    version: str
    tag: str
    corpus: Path
    venv: Path
    old_dist_version: str
    old_module_file: str
    before: dict[str, Any]
    after_open: dict[str, Any]
    after_update: dict[str, Any]
    # Row-level probes taken at the moment they are meaningful. The corpus is
    # mutated again by the reverse experiment, so reading these back from the
    # database inside a test would measure the wrong state.
    migrated_sample: dict[str, Any]
    updated_sample: dict[str, Any]
    status_result: subprocess.CompletedProcess[str]
    update_result: subprocess.CompletedProcess[str]
    tools: dict[str, dict[str, Any]]
    reverse: dict[str, Any]
    seconds: float


def _install_release(version: str, venv: Path, env: dict[str, str]) -> tuple[str, str]:
    """Install the released wheel into *venv*; return (version, module path)."""
    python_tag = f"{sys.version_info.major}.{sys.version_info.minor}"
    _check(
        _run(["uv", "venv", "--python", python_tag, str(venv)], env=env),
        f"uv venv for {version}",
    )
    _check(
        _run([
            "uv", "pip", "install",
            "--python", str(_venv_python(venv)),
            f"code-review-graph=={version}",
        ], env=env, timeout=1800),
        f"uv pip install code-review-graph=={version}",
    )
    probe = (
        "import json, code_review_graph, importlib.metadata as md; "
        "print(json.dumps({'file': code_review_graph.__file__, "
        "'version': md.version('code-review-graph')}))"
    )
    # cwd is the venv, and PYTHONSAFEPATH is set in the old environment, so
    # neither the current directory nor PYTHONPATH can satisfy this import.
    out = _check(
        _run([str(_venv_python(venv)), "-c", probe], cwd=venv, env=_old_env(
            Path(env["CRG_HOME"]),
        )),
        f"probing the installed {version}",
    )
    import json as _json

    payload = _json.loads(out.strip().splitlines()[-1])
    return payload["version"], payload["file"]


def _call_tools(corpus: Path) -> dict[str, dict[str, Any]]:
    """Run a spread of MCP tool functions against the upgraded database.

    Imported lazily and called in-process: these are the functions the MCP
    server exposes, so calling them directly is what an agent's tool call does
    minus the transport.
    """
    from code_review_graph.tools.community_tools import (
        get_architecture_overview_func,
        list_communities_func,
    )
    from code_review_graph.tools.context import get_minimal_context
    from code_review_graph.tools.query import (
        get_impact_radius,
        list_graph_stats,
        query_graph,
        semantic_search_nodes,
        traverse_graph_func,
    )
    from code_review_graph.tools.refactor_tools import refactor_func
    from code_review_graph.tools.review import (
        get_affected_flows_func,
        get_review_context,
    )

    root = str(corpus)
    calls = {
        "list_graph_stats": lambda: list_graph_stats(repo_root=root),
        "query_graph.callers_of": lambda: query_graph(
            "callers_of", KNOWN_SYMBOL, repo_root=root,
        ),
        "query_graph.dotted_tail": lambda: query_graph(
            "callers_of", KNOWN_DOTTED_SYMBOL, repo_root=root,
        ),
        "query_graph.file_summary": lambda: query_graph(
            "file_summary", KNOWN_FILE, repo_root=root,
        ),
        "semantic_search_nodes": lambda: semantic_search_nodes(
            KNOWN_SYMBOL, repo_root=root, limit=5,
        ),
        "traverse_graph": lambda: traverse_graph_func(
            query=KNOWN_SYMBOL, repo_root=root, depth=2,
        ),
        "get_impact_radius": lambda: get_impact_radius(
            changed_files=[KNOWN_FILE], repo_root=root,
        ),
        "get_review_context": lambda: get_review_context(
            changed_files=[KNOWN_FILE], repo_root=root,
        ),
        "get_affected_flows": lambda: get_affected_flows_func(
            changed_files=[KNOWN_FILE], repo_root=root,
        ),
        "get_minimal_context": lambda: get_minimal_context(
            task="review the graph store", repo_root=root,
        ),
        "list_communities": lambda: list_communities_func(repo_root=root),
        "get_architecture_overview": lambda: get_architecture_overview_func(
            repo_root=root,
        ),
        "refactor.dead_code": lambda: refactor_func(mode="dead_code", repo_root=root),
    }

    results: dict[str, dict[str, Any]] = {}
    for name, call in calls.items():
        try:
            payload = call()
        except Exception as exc:  # noqa: BLE001 - the point is to record any raise
            results[name] = {
                "raised": f"{type(exc).__name__}: {exc}",
                "status": None,
                "payload": None,
            }
            continue
        results[name] = {
            "raised": None,
            "status": payload.get("status") if isinstance(payload, dict) else None,
            "payload": payload if isinstance(payload, dict) else {},
        }
    return results


@pytest.fixture(scope="module", params=RELEASES, ids=[f"from-{v}" for v in RELEASES])
def upgrade_run(request, tmp_path_factory) -> Iterator[UpgradeRun]:
    """Build a graph with one released version, then upgrade it in place.

    Module-scoped so the expensive half runs once per release and every
    assertion below reads the same record. Torn down before the next release
    starts, which keeps peak disk to a single environment plus a single corpus.
    """
    version = request.param
    tag = f"v{version}"
    started = time.monotonic()

    base = tmp_path_factory.mktemp(f"upgrade-{version.replace('.', '-')}")
    crg_home = base / "crg-home"
    crg_home.mkdir()
    env = _base_env(crg_home)
    old_env = _old_env(crg_home)

    tag_exists = _run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "--verify", f"refs/tags/{tag}"],
    )
    if tag_exists.returncode != 0:
        pytest.skip(f"tag {tag} is not present in this checkout (shallow clone?)")

    venv = base / "venv"
    old_dist_version, old_module_file = _install_release(version, venv, env)

    corpus = base / "repo"
    _export_tag(tag, corpus, base)
    _init_corpus_repo(corpus, env)

    # --- the old release builds the graph -----------------------------------
    # No ``-q``: 2.3.6's build subcommand does not accept it, and an upgrade
    # check that only works against the newest old release is not a check.
    _check(
        _run(
            [str(_venv_script(venv, "code-review-graph")), "build", "--repo", str(corpus)],
            cwd=corpus,
            env=old_env,
            timeout=1800,
        ),
        f"{version} build",
    )
    before = _snapshot(corpus)

    # --- the current code opens it (this is what runs the migrations) -------
    from code_review_graph.graph import GraphStore
    from code_review_graph.incremental import get_db_path

    previous_home = os.environ.get("CRG_HOME")
    previous_data_dir = os.environ.get("CRG_DATA_DIR")
    os.environ["CRG_HOME"] = str(crg_home)
    os.environ.pop("CRG_DATA_DIR", None)
    try:
        store = GraphStore(get_db_path(corpus))
        store.close()
        after_open = _snapshot(corpus)
        migrated_qname = _find_qualified_name(corpus, KNOWN_DOTTED_SYMBOL)
        migrated_sample = {
            "qualified_name": migrated_qname,
            "symbol": (
                None if migrated_qname is None
                else _symbol_for(corpus, migrated_qname)
            ),
        }

        status_result = _current_cli(["status", "--repo", str(corpus)], env)

        edited = corpus / EDITED_RELATIVE_PATH
        _append_function(edited, NEW_FUNCTION_NAME)
        _commit_all(corpus, env, "edit before the first update")
        update_result = _current_cli(["update", "--repo", str(corpus)], env)
        after_update = _snapshot(corpus)
        updated_qname = _find_qualified_name(corpus, NEW_FUNCTION_NAME)
        updated_sample = {
            "qualified_name": updated_qname,
            "symbol": (
                None if updated_qname is None
                else _symbol_for(corpus, updated_qname)
            ),
        }

        # Queries run last, against the state a user is actually left in:
        # migrated, then updated once.
        tools = _call_tools(corpus)

        # --- reverse direction: the old release meets the migrated database --
        reverse: dict[str, Any] = {}
        old_cli = str(_venv_script(venv, "code-review-graph"))
        old_status = _run([old_cli, "status", "--repo", str(corpus)], cwd=corpus,
                          env=old_env, timeout=600)
        reverse["status_returncode"] = old_status.returncode
        reverse["status_output"] = old_status.stdout + old_status.stderr

        _append_function(edited, REVERSE_FUNCTION_NAME)
        _commit_all(corpus, env, "edit before the old release's update")
        old_update = _run([old_cli, "update", "--repo", str(corpus)], cwd=corpus,
                          env=old_env, timeout=1800)
        reverse["update_returncode"] = old_update.returncode
        reverse["update_output"] = old_update.stdout + old_update.stderr
        reverse["snapshot"] = _snapshot(corpus)

        reverse_qname = _find_qualified_name(corpus, REVERSE_FUNCTION_NAME)
        reverse["new_node_qualified_name"] = reverse_qname
        reverse["new_node_symbol"] = (
            None if reverse_qname is None else _symbol_for(corpus, reverse_qname)
        )
        # An existing dotted symbol in the file the old release re-parsed: the
        # current code must still be able to resolve it afterwards.
        store = GraphStore(get_db_path(corpus))
        try:
            reverse["dotted_hits_after_old_write"] = len(
                store.search_nodes_by_qualified_tail(f"{REVERSE_FUNCTION_NAME}")
            )
            reverse["schema_version_after_old_write"] = _snapshot(
                corpus
            )["schema_version"]
        finally:
            store.close()
    finally:
        if previous_home is None:
            os.environ.pop("CRG_HOME", None)
        else:
            os.environ["CRG_HOME"] = previous_home
        if previous_data_dir is not None:
            os.environ["CRG_DATA_DIR"] = previous_data_dir

    run = UpgradeRun(
        version=version,
        tag=tag,
        corpus=corpus,
        venv=venv,
        old_dist_version=old_dist_version,
        old_module_file=old_module_file,
        before=before,
        after_open=after_open,
        after_update=after_update,
        migrated_sample=migrated_sample,
        updated_sample=updated_sample,
        status_result=status_result,
        update_result=update_result,
        tools=tools,
        reverse=reverse,
        seconds=time.monotonic() - started,
    )
    try:
        yield run
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ---------------------------------------------------------------------------
# Canaries: prove this check ran and compared something real
# ---------------------------------------------------------------------------


def test_the_old_half_is_the_published_artefact(upgrade_run: UpgradeRun) -> None:
    """The graph must have been built by the released wheel, not the working tree.

    If ``uv pip install`` silently resolved to an editable install of this
    checkout, or the environment leaked ``PYTHONPATH``, the whole module would
    be testing the current code against itself and would pass for free.
    """
    assert upgrade_run.old_dist_version == upgrade_run.version, (
        f"asked PyPI for {upgrade_run.version} but the environment reports "
        f"{upgrade_run.old_dist_version}"
    )
    module_file = Path(upgrade_run.old_module_file).resolve()
    assert upgrade_run.venv.resolve() in module_file.parents, (
        f"the old interpreter imported {module_file}, which is outside "
        f"{upgrade_run.venv}: the released wheel is not what built the graph"
    )
    assert REPO_ROOT not in module_file.parents, (
        f"the old interpreter imported the working tree at {module_file}"
    )


def test_the_old_release_built_a_real_graph(upgrade_run: UpgradeRun) -> None:
    """A build that parsed nothing would make every later assertion vacuous."""
    before = upgrade_run.before
    assert before["nodes"] >= MIN_BASELINE_NODES, (
        f"{upgrade_run.tag} produced only {before['nodes']} nodes; the corpus "
        "did not really get parsed"
    )
    assert before["edges"] >= MIN_BASELINE_EDGES, (
        f"{upgrade_run.tag} produced only {before['edges']} edges"
    )
    assert before["files"] >= MIN_BASELINE_FILES, (
        f"{upgrade_run.tag} produced only {before['files']} File nodes"
    )


def test_the_old_database_was_behind_the_current_schema(
    upgrade_run: UpgradeRun,
) -> None:
    """Nothing is being tested if the released version already wrote v10.

    This is the assertion that keeps the module honest as the schema moves: the
    day a release ships with ``LATEST_VERSION`` equal to the current one, this
    fails and says so rather than reporting a green upgrade check that migrated
    nothing.
    """
    assert upgrade_run.before["schema_version"] is not None, (
        "the released build wrote no schema_version at all"
    )
    assert upgrade_run.before["schema_version"] < LATEST_VERSION, (
        f"{upgrade_run.tag} already writes schema version "
        f"{upgrade_run.before['schema_version']}, which equals the current "
        f"LATEST_VERSION ({LATEST_VERSION}); this run migrated nothing, so "
        "point RELEASES at an older release or drop this version"
    )


def test_release_list_tracks_the_newest_tag() -> None:
    """The newest release under test must be the newest release that exists."""
    listing = _run([
        "git", "-C", str(REPO_ROOT), "tag", "--list", "v*", "--sort=-v:refname",
    ])
    if listing.returncode != 0:
        pytest.skip("cannot list tags in this checkout")
    semver = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
    newest = next(
        (line for line in listing.stdout.split() if semver.match(line)), None,
    )
    if newest is None:
        pytest.skip("no vX.Y.Z tags in this checkout")
    assert newest == f"v{RELEASES[0]}", (
        f"the newest tag is {newest} but this check still upgrades from "
        f"v{RELEASES[0]}; update RELEASES so the release about to ship is the "
        "one being tested"
    )


# ---------------------------------------------------------------------------
# Forward direction: the upgrade a user actually performs
# ---------------------------------------------------------------------------


def test_opening_migrates_to_the_current_schema_version(
    upgrade_run: UpgradeRun,
) -> None:
    assert upgrade_run.after_open["schema_version"] == LATEST_VERSION
    assert upgrade_run.after_open["has_symbol_column"], (
        "migration v10 did not add nodes.symbol to a real pre-v10 database"
    )


def test_opening_loses_no_nodes_or_edges(upgrade_run: UpgradeRun) -> None:
    """Opening only migrates; it re-parses nothing, so the counts must be equal."""
    before, after = upgrade_run.before, upgrade_run.after_open
    assert after["nodes"] == before["nodes"], (
        f"node count changed on open: {before['nodes']} -> {after['nodes']}"
    )
    assert after["edges"] == before["edges"], (
        f"edge count changed on open: {before['edges']} -> {after['edges']}"
    )
    lost = before["qualified_names"] - after["qualified_names"]
    assert not lost, (
        f"{len(lost)} node(s) disappeared during migration, e.g. "
        f"{sorted(lost)[:5]}"
    )


def test_the_v10_backfill_covers_every_pre_existing_row(
    upgrade_run: UpgradeRun,
) -> None:
    """Every migrated row needs ``symbol`` set, or dotted lookups miss it."""
    after = upgrade_run.after_open
    assert after["null_symbols"] == 0, (
        f"{after['null_symbols']} of {after['nodes']} migrated rows still have "
        "a NULL symbol; search_nodes_by_qualified_tail cannot find them"
    )
    assert "idx_nodes_symbol" in after["indexes"], (
        "migration v10 left nodes.symbol unindexed"
    )
    sample = upgrade_run.migrated_sample
    assert sample["qualified_name"] is not None, (
        f"{KNOWN_DOTTED_SYMBOL} is missing from the {upgrade_run.tag} corpus; "
        "pick a symbol that exists in every covered tag"
    )
    assert sample["symbol"] == KNOWN_DOTTED_SYMBOL, (
        "the backfilled symbol does not match the tail of the qualified name: "
        f"{sample}"
    )


def test_status_succeeds_on_the_upgraded_database(upgrade_run: UpgradeRun) -> None:
    result = upgrade_run.status_result
    assert result.returncode == 0, (
        f"status exited {result.returncode}\n{result.stdout}\n{result.stderr}"
    )
    assert f"Nodes: {upgrade_run.after_open['nodes']}" in result.stdout, (
        f"status did not report the migrated node count\n{result.stdout}"
    )


def test_update_succeeds_and_does_not_shrink_the_graph(
    upgrade_run: UpgradeRun,
) -> None:
    """The first ``update`` after upgrading re-parses; it must not lose the graph.

    Re-parsing legitimately changes individual identities: between 2.3.6 and
    now, for example, C++ members gained their overload signature, so
    ``sample.cpp::Animal.speak`` becomes ``sample.cpp::Animal.speak()``. What
    cannot happen is the graph getting materially smaller, or whole files
    vanishing, which is what silent data loss would look like.
    """
    result = upgrade_run.update_result
    assert result.returncode == 0, (
        f"update exited {result.returncode}\n{result.stdout}\n{result.stderr}"
    )
    before, after = upgrade_run.after_open, upgrade_run.after_update
    assert after["schema_version"] == LATEST_VERSION
    assert after["nodes"] >= before["nodes"] * MIN_RETAINED_FRACTION, (
        f"the first update dropped the graph from {before['nodes']} to "
        f"{after['nodes']} nodes\n{result.stdout}"
    )
    assert after["edges"] >= before["edges"] * MIN_RETAINED_FRACTION, (
        f"the first update dropped the graph from {before['edges']} to "
        f"{after['edges']} edges\n{result.stdout}"
    )
    lost_names = before["qualified_names"] - after["qualified_names"]
    assert len(lost_names) <= before["nodes"] * (1 - MIN_RETAINED_FRACTION), (
        f"{len(lost_names)} of {before['nodes']} node identities did not "
        f"survive the first update, e.g. {sorted(lost_names)[:5]}"
    )


def test_update_keeps_every_file_in_the_graph(upgrade_run: UpgradeRun) -> None:
    """File nodes are stable identities: a path either exists or it does not.

    Unlike function and class names, which re-parsing may legitimately respell,
    a File node disappearing means the update stopped covering that file.
    """
    lost_files = (
        upgrade_run.after_open["file_names"]
        - upgrade_run.after_update["file_names"]
    )
    assert not lost_files, (
        f"{len(lost_files)} file(s) left the graph during the first update "
        f"after upgrading, e.g. {sorted(lost_files)[:5]}"
    )


def test_update_maintains_the_symbol_column(upgrade_run: UpgradeRun) -> None:
    """Rows written after the migration must carry ``symbol`` like migrated ones."""
    assert upgrade_run.after_update["null_symbols"] == 0, (
        f"{upgrade_run.after_update['null_symbols']} row(s) written by the "
        "current update have a NULL symbol"
    )
    sample = upgrade_run.updated_sample
    assert sample["qualified_name"] is not None, (
        f"the function added before the update never reached the graph; the "
        f"update did nothing\n{upgrade_run.update_result.stdout}"
    )
    assert sample["symbol"] == NEW_FUNCTION_NAME, (
        f"a row written by the current update has symbol {sample['symbol']!r}"
    )


def test_no_mcp_tool_raises_on_the_upgraded_database(
    upgrade_run: UpgradeRun,
) -> None:
    raised = {
        name: result["raised"]
        for name, result in upgrade_run.tools.items()
        if result["raised"]
    }
    assert not raised, f"tool functions raised on an upgraded graph: {raised}"
    errored = {
        name: str(result["payload"].get("error"))[:200]
        for name, result in upgrade_run.tools.items()
        if result["status"] == "error"
    }
    assert not errored, f"tool functions returned errors: {errored}"


def test_mcp_tools_return_sane_results(upgrade_run: UpgradeRun) -> None:
    """A tool that answers ``nothing found`` for everything is not a working graph."""
    tools = upgrade_run.tools

    stats = tools["list_graph_stats"]["payload"]
    assert stats.get("total_nodes") == upgrade_run.after_update["nodes"], (
        "list_graph_stats disagrees with the database it just read: "
        f"{stats.get('total_nodes')} vs {upgrade_run.after_update['nodes']}"
    )

    callers = tools["query_graph.callers_of"]["payload"]
    assert callers.get("status") == "ok"
    assert callers.get("result_count", 0) >= 1, (
        f"callers_of({KNOWN_SYMBOL!r}) found nothing on a graph of "
        f"{upgrade_run.after_update['nodes']} nodes"
    )

    dotted = tools["query_graph.dotted_tail"]["payload"]
    assert dotted.get("status") == "ok", (
        f"the dotted target {KNOWN_DOTTED_SYMBOL!r} did not resolve after the "
        f"v10 upgrade: {str(dotted.get('summary'))[:200]}"
    )

    file_summary = tools["query_graph.file_summary"]["payload"]
    assert file_summary.get("result_count", 0) >= 1

    assert tools["semantic_search_nodes"]["payload"].get("results"), (
        "hybrid search returned nothing; the FTS index did not survive"
    )
    assert tools["get_minimal_context"]["payload"].get("status") == "ok", (
        "get_minimal_context is not ready on a freshly upgraded, freshly "
        f"updated graph: {tools['get_minimal_context']['payload']}"
    )
    assert tools["list_communities"]["payload"].get("communities"), (
        "community data written by the old release did not survive the upgrade"
    )


# ---------------------------------------------------------------------------
# Reverse direction: an old release meeting a newer database
# ---------------------------------------------------------------------------


def test_the_reverse_experiment_actually_ran(upgrade_run: UpgradeRun) -> None:
    """Canary for the two expectations below, which are currently xfail.

    Without this, a reverse half that crashed during setup would leave the
    xfails passing as xfail and report nothing.
    """
    reverse = upgrade_run.reverse
    assert reverse["schema_version_after_old_write"] == LATEST_VERSION, (
        "the reverse experiment did not run against a current-schema database"
    )
    assert reverse["update_returncode"] is not None
    assert reverse["new_node_qualified_name"] is not None, (
        f"the old release's update never parsed the new function; the reverse "
        f"experiment measured nothing\n{reverse['update_output'][-2000:]}"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "known defect: run_migrations returns early when the stored schema "
        "version is ahead of LATEST_VERSION, and nothing else compares the "
        "two, so an older release opens a newer database silently"
    ),
)
def test_an_old_release_refuses_a_newer_database(upgrade_run: UpgradeRun) -> None:
    """Opening a database from the future should fail loudly, not proceed."""
    reverse = upgrade_run.reverse
    assert reverse["status_returncode"] != 0, (
        "the old release read a newer database and exited 0"
    )
    assert "schema" in reverse["status_output"].lower(), (
        "the refusal does not mention the schema version"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "known defect: an older release writes rows without nodes.symbol into "
        "a v10 database and leaves schema_version at 10, so the backfill never "
        "runs again and dotted-tail queries answer not_found for nodes that "
        "are present"
    ),
)
def test_an_old_release_write_leaves_the_graph_queryable(
    upgrade_run: UpgradeRun,
) -> None:
    """If the old release does write, it must not silently break lookups."""
    reverse = upgrade_run.reverse
    assert reverse["snapshot"]["null_symbols"] == 0, (
        f"{reverse['snapshot']['null_symbols']} row(s) lost their symbol when "
        "the old release re-parsed a file"
    )
    assert reverse["new_node_symbol"] == REVERSE_FUNCTION_NAME
    assert reverse["dotted_hits_after_old_write"] >= 1, (
        "the current code cannot resolve a node the old release just wrote"
    )
