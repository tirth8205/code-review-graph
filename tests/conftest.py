"""Shared test fixtures.

Keeps code-review-graph's own per-user state, and every user-level config
the installers in ``skills.py`` write (``~/.codex``, ``~/.cursor``,
``~/.config/opencode`` and the rest), out of the developer's real home
directory: each test runs with a temporary home.
"""

from __future__ import annotations

import importlib
import os
import pkgutil
import shutil
import tempfile
from pathlib import Path
from tempfile import mkdtemp

import pytest

import code_review_graph.tools

# The tool modules load on first use (#1139). A test that patches a name such
# as ``code_review_graph.graph.GraphStore`` while one of them loads for the
# first time leaves its mock bound there for every later test. Load them before
# any test runs, as importing the package used to.
for _module in ("incremental", "postprocessing"):
    importlib.import_module(f"code_review_graph.{_module}")
for _info in pkgutil.iter_modules(code_review_graph.tools.__path__):
    importlib.import_module(f"code_review_graph.tools.{_info.name}")


@pytest.fixture(scope="session", autouse=True)
def contained_tempdir(tmp_path_factory):
    """Hold every raw ``tempfile`` directory the suite makes, and delete them.

    Many tests call ``tempfile.mkdtemp()`` and never remove the result, and
    subprocesses inherit ``TMPDIR``. Without this, each run left hundreds of
    MB in the caller's temp folder; with ``TMPDIR`` on ``/dev/shm`` that was
    RAM, and repeated runs filled memory and swap.
    """
    root = tmp_path_factory.mktemp("tempfile")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(tempfile, "tempdir", str(root))
        patch.setenv("TMPDIR", str(root))
        yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(scope="session")
def crg_home_parent(tmp_path_factory):
    return tmp_path_factory.mktemp("crg-isolation")


@pytest.fixture(autouse=True)
def isolated_crg_home(crg_home_parent, monkeypatch):
    """Redirect the per-user state directory into a temporary directory.

    ``~/.code-review-graph`` holds ``registry.json``, ``watch.toml``,
    ``daemon.pid``, ``daemon-state.json`` and ``logs/``. Two paths reached
    the real one:

    * ``Registry()`` defaults there, and ``incremental.get_data_dir()``
      constructs one internally — so any test touching data-dir resolution
      both read and wrote the registry of whoever ran the suite. That put
      pytest tmp paths into a developer's home directory, and made those
      tests depend on machine state: a developer with a registered repo
      could get different results from one without.
    * ``daemon`` built its config/PID/state paths from ``Path.home()``.

    Autouse and unconditional: an opt-in fixture would silently stop
    protecting a test the day someone forgets to request it.
    """
    # The numbered pytest allocator scans its parent on every call. Keep
    # these directories under one bucket so they also stay out of tmp_path's
    # numbered-directory scans.
    root = Path(mkdtemp(dir=crg_home_parent))
    # Pin explicit installer overrides even when a test patches Path.home.
    homes = {
        "CRG_HOME": root / "crg-home",
        "HERMES_HOME": root / "hermes-home",
        "DSH_HOME": root / "dsh-home",
        "HOME": root / "user-home",
    }
    for variable, path in homes.items():
        path.mkdir(mode=0o700)
        monkeypatch.setenv(variable, str(path))
    # Tests that exercise the Codex default patch Path.home() to a temporary
    # directory; clear an external override so it cannot escape that scope.
    monkeypatch.delenv("CODEX_HOME", raising=False)
    # The OpenCode plugin and MiMo config follow ``XDG_CONFIG_HOME`` over a
    # patched ``Path.home()``, so a developer's own setting must not leak in.
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    # A developer who records their own context history (#813) must not
    # have the suite append lines to every scratch repository it builds.
    monkeypatch.delenv("CRG_CONTEXT_HISTORY", raising=False)
    # Every non-interactive bash a test spawns sources ``BASH_ENV``; a
    # developer's hook there (direnv, for one) writes to the script's stderr.
    monkeypatch.delenv("BASH_ENV", raising=False)
    # The installers write user-level configs under the home directory
    # (``~/.codex``, ``~/.continue``, ``~/.qwen`` and more). Point the home
    # at a temp directory, so a test that forgets to patch ``Path.home()``
    # writes there and not into the real one (crg-2lp). ``Path.home()``
    # reads ``HOME`` on POSIX and ``USERPROFILE`` on Windows.
    user_home = homes["HOME"]
    monkeypatch.setenv("USERPROFILE", str(user_home))
    # These win over the home when set, so clear them: the folders they
    # name then follow the temp home too.
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "MIMOCODE_HOME", "CLAUDE_CONFIG_DIR"):
        monkeypatch.delenv(name, raising=False)
    # Git reads its global config from the home as well. Give the temp one
    # an identity, so a test commit works the same on every machine.
    (user_home / ".gitconfig").write_text(
        "[user]\n\tname = CRG Test\n\temail = crg-test@example.com\n",
        encoding="utf-8",
    )
    return homes["CRG_HOME"]


# Opt-in markers: suites slow or invasive enough that the ordinary run must not
# pay for them. A suite listed here is collected but skipped unless the run's
# ``-m`` expression names its marker, so ``pytest tests/`` stays fast while
# ``pytest -m <marker>`` still finds the tests. Add a marker name to the set
# (and to the ``markers`` list in pyproject.toml) to gate another suite.
#
# One hook, not one per suite: pytest looks the name up on the module, so a
# second ``pytest_collection_modifyitems`` here would silently shadow the
# first and every gate defined above it would stop running.
_OPT_IN_MARKERS = frozenset({
    "cli_surface", "corpus", "determinism", "packaging", "platform_lifecycle",
})

# Environment escape hatches, for suites that also need to be switchable on
# without a ``-m`` expression (CI runs them alongside the ordinary suite).
_OPT_IN_ENV_OVERRIDES = {"packaging": "CRG_RUN_PACKAGING_TESTS"}


def pytest_configure(config):
    # Parent runners (e.g. CLI wrappers) may inject GIT_CONFIG_* variables
    # (such as diff.external="" or empty core.hooksPath) that break subprocess
    # Git invocations in test fixtures.
    for key in list(os.environ):
        if key.startswith("GIT_CONFIG_"):
            del os.environ[key]


def pytest_collection_modifyitems(config, items):
    """Skip opt-in suites unless the run explicitly selects their marker."""
    expression = config.getoption("-m", default="") or ""
    gated = set()
    for name in _OPT_IN_MARKERS:
        if name in expression:
            continue
        override = _OPT_IN_ENV_OVERRIDES.get(name)
        if override and os.environ.get(override) == "1":
            continue
        gated.add(name)
    if not gated:
        return
    for item in items:
        for name in gated:
            if item.get_closest_marker(name) is not None:
                item.add_marker(
                    pytest.mark.skip(reason=f"opt-in suite; run with -m {name}")
                )
                break
