"""Release gate: the whole CLI surface, and what it does when things go wrong.

Every other CLI test in this suite pins one command or one helper. This
module takes the opposite approach: it enumerates the command tree **from
the live argparse parser** and the environment variables **from the package
source**, so neither list can drift away from the code the way a docs-derived
list would. Adding a command or an ``os.environ`` read without covering it
here fails a completeness assertion rather than quietly widening an
uncovered gap.

Three things are checked.

1. *Surface.* Every subcommand and subcommand flag argparse knows about is
   enumerated; the ones that terminate are run in a realistic scratch
   repository (git history, packages, a test file, a built graph) and must
   exit 0 and print something. Where a flag changes behaviour, the behaviour
   is compared against the same command without it, so a flag that silently
   stopped doing anything is caught.

2. *Failure modes.* A predictable problem must produce ONE clear line and a
   non-zero exit, never a traceback. The house style is set by ``update`` and
   ``watch`` (``print(f"Error: {exc}", file=sys.stderr)`` then ``sys.exit(1)``)
   and by the missing-matplotlib branch of ``visualize --format svg``. Cases
   that do not yet meet it are ``xfail(strict=True)``: the suite stays green,
   the bug stays recorded, and the day someone fixes it the XPASS forces this
   file to be updated rather than letting the coverage rot.

3. *Environment variables.* Defaults, valid values, and — the interesting
   half — invalid ones. ``int(os.environ.get(...))`` at module scope turns a
   typo in a shell profile into an import-time ``ValueError`` with a
   traceback and no mention of which variable was wrong (#912). The unguarded
   sites are frozen into a baseline so a *new* one fails immediately.

Nothing here touches the developer's machine: ``HOME``, ``CRG_HOME`` and
``HERMES_HOME`` are redirected into ``tmp_path`` the way ``tests/conftest.py``
already does, every repository is built under ``tmp_path``, and no test ever
starts a background daemon.

Run just this module::

    uv run --python 3.13 python -m pytest tests/test_cli_surface.py -m cli_surface -q

and the rest of the suite with ``-m "not cli_surface"``.
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pytest

from code_review_graph import cli

pytestmark = pytest.mark.cli_surface

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = REPO_ROOT / "code_review_graph"

# One CLI run is ~0.2s; anything blocking (serve, watch) is killed well
# before this. A generous ceiling keeps a loaded CI box from flaking.
RUN_TIMEOUT = 120.0


# ---------------------------------------------------------------------------
# Enumerating the command tree from the parser itself
# ---------------------------------------------------------------------------


def _subparser_choices(parser: argparse.ArgumentParser) -> dict:
    """Return the ``{name: parser}`` map of *parser*'s subcommands."""
    holder = parser._subparsers
    if holder is None:
        return {}
    for action in holder._group_actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def _build_real_parser() -> argparse.ArgumentParser:
    """Capture the parser ``cli.main`` builds, without running any command.

    ``main`` constructs the parser inline and immediately calls
    ``parse_args``. Intercepting that call is the only way to read the real
    tree; re-declaring it here would be exactly the drift this module exists
    to prevent.
    """

    class _CapturedError(Exception):
        def __init__(self, parser: argparse.ArgumentParser) -> None:
            super().__init__("captured")
            self.parser = parser

    def _intercept(self, *args, **kwargs):  # noqa: ANN001 - argparse signature
        raise _CapturedError(self)

    original = argparse.ArgumentParser.parse_args
    argparse.ArgumentParser.parse_args = _intercept  # type: ignore[method-assign]
    original_argv = sys.argv
    sys.argv = ["code-review-graph"]
    try:
        cli.main()
    except _CapturedError as captured:
        return captured.parser
    finally:
        argparse.ArgumentParser.parse_args = original  # type: ignore[method-assign]
        sys.argv = original_argv
    raise AssertionError("cli.main() did not reach parse_args; enumeration is broken")


_PARSER = _build_real_parser()
_TOP_LEVEL = _subparser_choices(_PARSER)

#: ``{"daemon start": parser}`` alongside ``{"status": parser}``.
COMMAND_PARSERS: dict[str, argparse.ArgumentParser] = {}
for _name, _sub in _TOP_LEVEL.items():
    COMMAND_PARSERS[_name] = _sub
    for _nested_name, _nested in _subparser_choices(_sub).items():
        COMMAND_PARSERS[f"{_name} {_nested_name}"] = _nested

COMMANDS: tuple[str, ...] = tuple(sorted(COMMAND_PARSERS))


def _option_strings(parser: argparse.ArgumentParser) -> set[str]:
    return {
        opt
        for action in parser._actions
        for opt in action.option_strings
        if opt not in ("-h", "--help")
    }


#: ``{"status": {"--json", ...}}`` — help excluded, it is covered separately.
COMMAND_FLAGS: dict[str, set[str]] = {
    name: _option_strings(parser) for name, parser in COMMAND_PARSERS.items()
}
ALL_FLAGS: set[tuple[str, str]] = {
    (command, flag) for command, flags in COMMAND_FLAGS.items() for flag in flags
}


# ---------------------------------------------------------------------------
# Enumerating environment variables from the package source
# ---------------------------------------------------------------------------

#: Two spellings count as reading an environment variable: the raw
#: ``os.environ.get("NAME")`` and the package's own guarded numeric readers,
#: ``constants.env_int("NAME", default)`` / ``env_float``. Matching only the
#: raw one would make this whole module blind to every setting the #912 fix
#: moved behind the helper.
_ENV_READ_RE = re.compile(
    r"""(?:os\.(?:environ\.get|getenv)|env_int|env_float)\(\s*["']([A-Z][A-Z0-9_]*)["']"""
)

#: Helper names that parse an environment variable as a number *and* fall
#: back to the documented default instead of raising.
_GUARDED_NUMERIC_HELPERS = frozenset({"env_int", "env_float", "_bounded_float_env"})


def _python_sources() -> Iterable[Path]:
    for path in sorted(PACKAGE_DIR.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path


def _scan_env_reads() -> dict[str, set[str]]:
    """``{"CRG_GIT_TIMEOUT": {"changes.py", "incremental.py"}}``."""
    found: dict[str, set[str]] = {}
    for path in _python_sources():
        text = path.read_text(encoding="utf-8", errors="replace")
        for name in _ENV_READ_RE.findall(text):
            found.setdefault(name, set()).add(path.name)
        # ``os.environ["X"]`` subscript reads are a different shape.
        for name in re.findall(r"""os\.environ\[\s*["']([A-Z][A-Z0-9_]*)["']""", text):
            found.setdefault(name, set()).add(path.name)
    return found


ENV_READS = _scan_env_reads()
CRG_ENV_VARS = tuple(sorted(name for name in ENV_READS if name.startswith("CRG_")))


# ---------------------------------------------------------------------------
# Running the CLI
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CliResult:
    """One completed (or killed) CLI run."""

    argv: tuple[str, ...]
    returncode: int | None
    output: str

    @property
    def traceback(self) -> bool:
        return "Traceback (most recent call last)" in self.output

    @property
    def timed_out(self) -> bool:
        return self.returncode is None

    def describe(self) -> str:
        return f"$ code-review-graph {' '.join(self.argv)}\nrc={self.returncode}\n{self.output}"


def run_cli(
    *argv: str,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: float = RUN_TIMEOUT,
) -> CliResult:
    """Run the CLI as a subprocess and capture the merged streams.

    A subprocess rather than ``cli.main()`` in-process on purpose: this module
    is about exit codes, tracebacks and import-time failures, none of which
    survive an in-process call faithfully.
    """
    command = [sys.executable, "-m", "code_review_graph", *argv]
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        output, _ = process.communicate(timeout=timeout)
        returncode: int | None = process.returncode
    except subprocess.TimeoutExpired:
        process.kill()
        output, _ = process.communicate()
        returncode = None
    return CliResult(tuple(argv), returncode, output or "")


def assert_clean_failure(result: CliResult, *, expected_code: int = 1) -> None:
    """The house style: one clear line on stderr, non-zero exit, no traceback.

    Set by ``update``/``watch`` (``print(f"Error: {exc}", file=sys.stderr)``
    then ``sys.exit(1)``) and by the missing-matplotlib branch of
    ``visualize --format svg``.
    """
    assert not result.traceback, "leaked a Python traceback:\n" + result.describe()
    assert result.returncode == expected_code, result.describe()
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert lines, "failed silently, with nothing for the user to act on:\n" + result.describe()
    assert len(lines) <= 3, "more than a short message:\n" + result.describe()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def init_repo(path: Path) -> Path:
    """Create a git repository with identity configured and no commits yet."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "cli-surface@example.invalid")
    _git(path, "config", "user.name", "CLI Surface Test")
    _git(path, "config", "commit.gpgsign", "false")
    return path


def write_sample_project(repo: Path) -> None:
    """A small but realistic tree: a package, a caller, a test, a README."""
    (repo / "pkg").mkdir(parents=True, exist_ok=True)
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "core.py").write_text(
        '"""Core module."""\n'
        "\n"
        "\n"
        "def helper(value):\n"
        "    return value * 2\n"
        "\n"
        "\n"
        "def entry(value):\n"
        "    return helper(value) + 1\n"
        "\n"
        "\n"
        "class Widget:\n"
        '    """A widget."""\n'
        "\n"
        "    def render(self):\n"
        "        return entry(3)\n",
        encoding="utf-8",
    )
    (repo / "pkg" / "util.py").write_text(
        "from pkg.core import helper\n"
        "\n"
        "\n"
        "def double_all(items):\n"
        "    return [helper(item) for item in items]\n",
        encoding="utf-8",
    )
    (repo / "tests").mkdir(exist_ok=True)
    (repo / "tests" / "test_core.py").write_text(
        "from pkg.core import entry\n"
        "\n"
        "\n"
        "def test_entry():\n"
        "    assert entry(1) == 3\n",
        encoding="utf-8",
    )
    (repo / "README.md").write_text("# sample\n", encoding="utf-8")


@pytest.fixture(scope="session")
def cli_env(tmp_path_factory) -> dict[str, str]:
    """A process environment whose per-user state lives under ``tmp_path``.

    Same contract as ``tests/conftest.py``'s ``isolated_crg_home``, but as an
    environment mapping because everything here runs in a subprocess and does
    not inherit a monkeypatched parent.
    """
    home = tmp_path_factory.mktemp("cli-surface-home")
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["CRG_HOME"] = str(home / ".code-review-graph")
    env["HERMES_HOME"] = str(home / ".hermes")
    env["NO_COLOR"] = "1"
    # A stray override in the developer's shell must not reach a subprocess.
    for stale in ("CRG_DATA_DIR", "CRG_REPO_ROOT", "CRG_TOOLS", "VIRTUAL_ENV"):
        env.pop(stale, None)
    for name in CRG_ENV_VARS:
        env.pop(name, None)
    env["CRG_HOME"] = str(home / ".code-review-graph")
    env["HERMES_HOME"] = str(home / ".hermes")
    return env


@pytest.fixture(scope="session")
def built_repo(tmp_path_factory, cli_env) -> Path:
    """A repository with two commits and a freshly built graph."""
    repo = init_repo(tmp_path_factory.mktemp("cli-surface-built") / "repo")
    write_sample_project(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "initial")
    (repo / "pkg" / "util.py").write_text(
        (repo / "pkg" / "util.py").read_text(encoding="utf-8")
        + "\n\ndef triple_all(items):\n    return [helper(item) * 3 for item in items]\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "second")
    built = run_cli("build", "-q", cwd=repo, env=cli_env)
    assert built.returncode == 0, built.describe()
    return repo


@pytest.fixture()
def fresh_built_repo(tmp_path, cli_env) -> Path:
    """A throwaway built repository for tests that damage the data dir."""
    repo = init_repo(tmp_path / "repo")
    write_sample_project(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "initial")
    # A second commit so ``--base HEAD~1`` has a real diff to report; without
    # it every detect-changes assertion would compare two empty answers.
    (repo / "pkg" / "util.py").write_text(
        (repo / "pkg" / "util.py").read_text(encoding="utf-8")
        + "\n\ndef triple_all(items):\n    return [helper(item) * 3 for item in items]\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "second")
    built = run_cli("build", "-q", cwd=repo, env=cli_env)
    assert built.returncode == 0, built.describe()
    return repo


@pytest.fixture()
def blocked_imports(tmp_path) -> Path:
    """A ``PYTHONPATH`` entry that makes optional imports fail deterministically.

    Shadowing beats uninstalling: the test then behaves identically whether or
    not the developer happens to have matplotlib in their virtualenv.
    """
    shim = tmp_path / "import-shim"
    shim.mkdir()
    (shim / "matplotlib.py").write_text(
        'raise ImportError("matplotlib blocked by test shim")\n', encoding="utf-8"
    )
    package = shim / "sentence_transformers"
    package.mkdir()
    (package / "__init__.py").write_text(
        'raise ImportError("sentence-transformers blocked by test shim")\n',
        encoding="utf-8",
    )
    return shim


# ---------------------------------------------------------------------------
# Canaries: prove the harness really ran and really compared something
# ---------------------------------------------------------------------------


def test_canary_parser_enumeration_found_the_real_tree():
    """If this drops to nothing, every parameterized test below is vacuous."""
    assert len(_TOP_LEVEL) >= 30, f"only found {len(_TOP_LEVEL)} subcommands"
    for expected in ("build", "update", "status", "watch", "serve", "daemon", "wiki"):
        assert expected in _TOP_LEVEL, f"{expected} missing from the parser walk"
    assert "daemon start" in COMMAND_PARSERS, "nested daemon subcommands were not walked"
    assert len(COMMAND_PARSERS) >= 37, COMMANDS
    assert len(ALL_FLAGS) >= 100, f"only found {len(ALL_FLAGS)} flags"
    assert ("status", "--json") in ALL_FLAGS
    assert ("visualize", "--format") in ALL_FLAGS


def test_canary_env_scan_found_the_real_variables():
    """The env-var half is only as good as the scan that feeds it."""
    assert len(CRG_ENV_VARS) >= 35, f"only found {len(CRG_ENV_VARS)}: {CRG_ENV_VARS}"
    for expected in (
        "CRG_DATA_DIR",
        "CRG_REPO_ROOT",
        "CRG_GIT_TIMEOUT",
        "CRG_MAX_IMPACT_NODES",
        "CRG_PARSE_WORKERS",
    ):
        assert expected in ENV_READS, f"{expected} not found by the source scan"


def test_canary_harness_actually_runs_the_cli(built_repo, cli_env):
    """A runner that silently did nothing would make every assertion pass."""
    result = run_cli("-v", cwd=built_repo, env=cli_env)
    assert result.returncode == 0, result.describe()
    assert result.output.startswith("code-review-graph "), result.describe()
    assert re.search(r"\d+\.\d+", result.output), result.describe()


def test_canary_harness_detects_a_traceback(built_repo, cli_env, tmp_path):
    """Prove the traceback detector fires on a run that really does crash.

    This used to use ``CRG_MAX_IMPACT_NODES=``, the #912 import-time failure.
    That is fixed, and so is every other failure this module knows how to
    name, so the canary needs a crash of its own: a ``PYTHONPATH`` shim that
    shadows ``sqlite3`` with a module that raises ``RuntimeError``. Nothing
    catches that, which is the point — an unforeseen bug must still show its
    traceback. If this stops producing one, every ``assert not
    result.traceback`` below is vacuous.
    """
    shim = tmp_path / "crash-shim"
    shim.mkdir()
    (shim / "sqlite3.py").write_text(
        'raise RuntimeError("sqlite3 shadowed by test shim")\n', encoding="utf-8"
    )
    env = {**cli_env, "PYTHONPATH": str(shim)}
    result = run_cli("status", cwd=built_repo, env=env)
    assert result.traceback, "the detector missed a real traceback:\n" + result.describe()
    assert result.returncode not in (0, None), result.describe()


def test_canary_scratch_repo_graph_is_not_empty(built_repo, cli_env):
    """Commands must not be passing over an empty graph."""
    import json

    result = run_cli("status", "--json", cwd=built_repo, env=cli_env)
    assert result.returncode == 0, result.describe()
    stats = json.loads(result.output.strip().splitlines()[-1])
    assert stats["nodes"] > 5, stats
    assert stats["edges"] > 5, stats
    assert stats["files"] >= 4, stats


# ---------------------------------------------------------------------------
# 1. Every command, every flag
# ---------------------------------------------------------------------------

#: How to invoke each enumerated command in ``built_repo``. Commands that
#: never return (servers, watchers) or that would start a background process
#: on the developer's machine are listed in ``LONG_RUNNING`` / ``HELP_ONLY``
#: instead; the completeness test below proves the three lists together cover
#: every command argparse knows about.
INVOCATIONS: dict[str, list[str]] = {
    "install": ["install", "--dry-run", "--platform", "claude"],
    "init": ["init", "--dry-run", "--platform", "codex"],
    "uninstall": ["uninstall", "--dry-run", "--yes", "--platform", "claude"],
    "build": ["build"],
    "update": ["update"],
    "postprocess": ["postprocess"],
    "status": ["status"],
    "forget": ["forget", "pkg/util.py", "--dry-run"],
    "visualize": ["visualize"],
    "wiki": ["wiki"],
    "register": ["register", "."],
    "unregister": ["unregister", "."],
    "repos": ["repos"],
    "eval": ["eval", "--report"],
    "detect-changes": ["detect-changes", "--brief"],
    "dead-code": ["dead-code"],
    "query": ["query", "callers_of", "helper"],
    "impact": ["impact", "--files", "pkg/core.py"],
    "search": ["search", "helper"],
    "flows": ["flows"],
    "flow": ["flow", "--id", "1"],
    "communities": ["communities"],
    "community": ["community", "--id", "1"],
    "architecture": ["architecture"],
    "large-functions": ["large-functions", "--min-lines", "1"],
    "refactor": ["refactor", "dead_code"],
    "daemon": ["daemon"],
    "daemon status": ["daemon", "status"],
    "daemon add": ["daemon", "add", ".", "--alias", "surface"],
    "daemon remove": ["daemon", "remove", "surface"],
    "daemon logs": ["daemon", "logs"],
}

#: Blocking commands: started, given a moment, then killed. Success is
#: "reached a steady state without tracebacking", not an exit code.
LONG_RUNNING: dict[str, list[str]] = {
    "watch": ["watch"],
    "serve": ["serve"],
    "mcp": ["mcp"],
}

#: Covered by ``--help`` only. ``daemon start``/``restart`` fork a real
#: background watcher and ``daemon stop`` reaches for a PID file, so running
#: them for real is machine state this suite refuses to create.
HELP_ONLY: tuple[str, ...] = ("daemon start", "daemon restart", "daemon stop", "enrich")

#: ``embed`` succeeds only where the optional embedding stack is installed and
#: would otherwise download a model, so it gets its own test that accepts
#: either a clean success or a clean missing-dependency failure.
CONDITIONAL: tuple[str, ...] = ("embed",)

#: Commands whose "nothing to show" case is a user error, not a success:
#: ``daemon logs`` before a daemon has ever written a log file.
EXPECTED_NONZERO: dict[str, int] = {"daemon logs": 1}


def test_invocation_table_covers_every_enumerated_command():
    """A new subcommand must be added here, not silently skipped."""
    covered = set(INVOCATIONS) | set(LONG_RUNNING) | set(HELP_ONLY) | set(CONDITIONAL)
    missing = set(COMMANDS) - covered
    assert not missing, f"commands with no coverage entry: {sorted(missing)}"
    stale = covered - set(COMMANDS)
    assert not stale, f"coverage entries for commands argparse does not have: {sorted(stale)}"


@pytest.mark.parametrize("command", sorted(COMMAND_PARSERS))
def test_every_command_has_working_help(command, built_repo, cli_env):
    """``--help`` must work for every command and name the command."""
    result = run_cli(*command.split(), "--help", cwd=built_repo, env=cli_env)
    assert result.returncode == 0, result.describe()
    assert "usage:" in result.output, result.describe()
    assert command.split()[-1] in result.output, result.describe()


@pytest.mark.parametrize("command", sorted(INVOCATIONS))
def test_every_command_runs_and_prints_something(command, built_repo, cli_env):
    """Exit 0, no traceback, and output a user can read."""
    result = run_cli(*INVOCATIONS[command], cwd=built_repo, env=cli_env)
    assert not result.traceback, result.describe()
    expected = EXPECTED_NONZERO.get(command, 0)
    assert result.returncode == expected, result.describe()
    assert result.output.strip(), "exited 0 with no output at all:\n" + result.describe()


def test_embed_either_succeeds_or_says_what_is_missing(built_repo, cli_env):
    """``embed`` needs an optional stack; both outcomes must be clean.

    Parameterizing it with the rest would either skip the command on a lean
    checkout or pull a model down on a fat one, so it is asserted on the only
    property that holds either way.
    """
    result = run_cli("embed", cwd=built_repo, env=cli_env)
    assert not result.traceback, result.describe()
    assert result.returncode in (0, 1), result.describe()
    if result.returncode == 1:
        assert_clean_failure(result)
        assert "pip install" in result.output, result.describe()


@pytest.mark.parametrize("command", sorted(LONG_RUNNING))
def test_long_running_command_starts_without_tracebacking(command, built_repo, cli_env):
    result = run_cli(*LONG_RUNNING[command], cwd=built_repo, env=cli_env, timeout=20.0)
    assert not result.traceback, result.describe()
    assert result.returncode in (0, None), result.describe()


#: ``(baseline argv, flagged argv, predicate)`` — the predicate must be able
#: to tell the two runs apart, so a flag that quietly became a no-op fails.
FLAG_BEHAVIOUR = [
    pytest.param(
        ["status"],
        ["status", "--json"],
        lambda plain, flagged: plain.lstrip().startswith("Nodes:")
        and flagged.lstrip().startswith("{"),
        id="status--json",
    ),
    pytest.param(
        ["status"],
        ["status", "--quiet"],
        lambda plain, flagged: len(flagged.strip()) < len(plain.strip()),
        id="status--quiet",
    ),
    pytest.param(
        ["detect-changes"],
        ["detect-changes", "--brief"],
        lambda plain, flagged: plain.lstrip().startswith("{")
        and not flagged.lstrip().startswith("{"),
        id="detect-changes--brief",
    ),
    pytest.param(
        ["build"],
        ["build", "--skip-flows"],
        lambda plain, flagged: "postprocess=full" in plain
        and "postprocess=minimal" in flagged,
        id="build--skip-flows",
    ),
    pytest.param(
        ["build"],
        ["build", "--skip-postprocess"],
        lambda plain, flagged: "postprocess=full" in plain
        and "postprocess=none" in flagged,
        id="build--skip-postprocess",
    ),
    pytest.param(
        ["large-functions", "--min-lines", "1"],
        ["large-functions", "--min-lines", "1", "--limit", "1"],
        lambda plain, flagged: plain.count('"qualified_name"')
        > flagged.count('"qualified_name"'),
        id="large-functions--limit",
    ),
    pytest.param(
        ["large-functions", "--min-lines", "1"],
        ["large-functions", "--min-lines", "1000"],
        lambda plain, flagged: plain.count('"qualified_name"')
        > flagged.count('"qualified_name"'),
        id="large-functions--min-lines",
    ),
    pytest.param(
        ["search", "helper"],
        ["search", "helper", "--limit", "1"],
        lambda plain, flagged: plain.count('"qualified_name"')
        >= flagged.count('"qualified_name"')
        and flagged.count('"qualified_name"') <= 1,
        id="search--limit",
    ),
    pytest.param(
        ["community", "--id", "1"],
        ["community", "--id", "1", "--members"],
        lambda plain, flagged: len(flagged) > len(plain),
        id="community--members",
    ),
    pytest.param(
        ["flow", "--id", "1"],
        ["flow", "--id", "1", "--source"],
        lambda plain, flagged: len(flagged) > len(plain),
        id="flow--source",
    ),
    pytest.param(
        ["architecture"],
        ["architecture", "--detail-level", "standard"],
        lambda plain, flagged: len(flagged) > len(plain),
        id="architecture--detail-level",
    ),
    pytest.param(
        ["visualize"],
        ["visualize", "--format", "json"],
        lambda plain, flagged: "graph.html" in plain and "graph.json" in flagged,
        id="visualize--format",
    ),
    pytest.param(
        ["forget", "pkg/util.py", "--dry-run"],
        ["forget", "pkg/util.py"],
        lambda plain, flagged: "dry-run" in plain and "dry-run" not in flagged,
        id="forget--dry-run",
    ),
    pytest.param(
        ["dead-code"],
        ["dead-code", "--json"],
        lambda plain, flagged: '"qualified_name"' not in plain
        and '"qualified_name"' in flagged,
        id="dead-code--json",
    ),
]


@pytest.mark.parametrize("baseline, flagged, differs", FLAG_BEHAVIOUR)
def test_flag_changes_behaviour(baseline, flagged, differs, fresh_built_repo, cli_env):
    """A flag that stopped changing anything is a silent regression."""
    plain = run_cli(*baseline, cwd=fresh_built_repo, env=cli_env)
    assert plain.returncode == 0, plain.describe()
    other = run_cli(*flagged, cwd=fresh_built_repo, env=cli_env)
    assert other.returncode == 0, other.describe()
    assert differs(plain.output, other.output), (
        "the flag did not change the output:\n"
        + plain.describe()
        + "\n---\n"
        + other.describe()
    )


# ---------------------------------------------------------------------------
# 2. Failure modes
# ---------------------------------------------------------------------------

READ_ONLY_COMMANDS = ["status", "detect-changes", "visualize", "wiki", "dead-code"]
DB_CONSUMING_COMMANDS = [
    "status",
    "detect-changes",
    "visualize",
    "wiki",
    "dead-code",
    "update",
    "search",
    "query",
]


def _invoke(command: str) -> list[str]:
    """The minimal argv that makes *command* open the graph database."""
    return {
        "query": ["query", "callers_of", "helper"],
        "search": ["search", "helper"],
        "update": ["update", "-q"],
    }.get(command, [command])


@pytest.mark.parametrize("command", READ_ONLY_COMMANDS)
def test_missing_database_is_reported_cleanly(command, tmp_path, cli_env):
    """No graph yet: name the path, name the fix, exit 1."""
    repo = init_repo(tmp_path / "repo")
    write_sample_project(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "initial")
    result = run_cli(*_invoke(command), cwd=repo, env=cli_env)
    assert_clean_failure(result)
    assert "No graph found" in result.output, result.describe()
    assert "build" in result.output, result.describe()


def test_missing_database_does_not_create_one(tmp_path, cli_env):
    """A read-only command must not materialize graph state as a side effect."""
    repo = init_repo(tmp_path / "repo")
    write_sample_project(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "initial")
    run_cli("status", cwd=repo, env=cli_env)
    assert not (repo / ".code-review-graph" / "graph.db").exists()


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG: `postprocess` with no graph creates an empty graph.db and exits 0, "
        "unlike every other consumer, which reports `No graph found` and exits 1"
    ),
)
def test_postprocess_without_a_graph_is_reported_cleanly(tmp_path, cli_env):
    repo = init_repo(tmp_path / "repo")
    write_sample_project(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "initial")
    result = run_cli("postprocess", cwd=repo, env=cli_env)
    assert result.returncode == 1, result.describe()
    assert "No graph found" in result.output, result.describe()


def test_truncated_database_does_not_traceback(fresh_built_repo, cli_env):
    """Truncating graph.db to zero bytes must not crash the CLI.

    SQLite reads an empty file as a valid empty database, so this currently
    *succeeds* and prints a zero-node graph. Recorded here rather than
    asserted as a failure because the exit code is defensible; the thing that
    would not be defensible is a traceback.
    """
    db = fresh_built_repo / ".code-review-graph" / "graph.db"
    for suffix in ("-wal", "-shm"):
        db.with_name(db.name + suffix).unlink(missing_ok=True)
    db.write_bytes(b"")
    result = run_cli("status", cwd=fresh_built_repo, env=cli_env)
    assert not result.traceback, result.describe()


def _corrupt(db: Path, kind: str) -> None:
    """Damage *db* in one of the four ways a graph.db goes bad in the wild."""
    for suffix in ("-wal", "-shm"):
        db.with_name(db.name + suffix).unlink(missing_ok=True)
    if kind == "garbage":
        db.write_bytes(os.urandom(64 * 1024))
    elif kind == "truncated":
        # A partial write: a real SQLite header over a body that ends early.
        db.write_bytes(db.read_bytes()[:512])
    elif kind == "foreign-sqlite":
        # A perfectly valid SQLite file belonging to something else. CREATE
        # TABLE IF NOT EXISTS would otherwise graft our schema onto it.
        db.unlink()
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE invoices (id INTEGER PRIMARY KEY, total REAL)")
        conn.commit()
        conn.close()
    elif kind == "newer-schema":
        conn = sqlite3.connect(str(db))
        conn.execute(
            "INSERT OR REPLACE INTO metadata (key, value) "
            "VALUES ('schema_version', '99')"
        )
        conn.commit()
        conn.close()
    else:  # pragma: no cover - guards the parameter list against typos
        raise AssertionError(f"unknown corruption kind: {kind}")


@pytest.mark.parametrize("command", DB_CONSUMING_COMMANDS)
def test_corrupt_database_is_reported_cleanly(command, fresh_built_repo, cli_env):
    """Garbage bytes in graph.db: one line naming the file, exit 1."""
    db = fresh_built_repo / ".code-review-graph" / "graph.db"
    _corrupt(db, "garbage")
    result = run_cli(*_invoke(command), cwd=fresh_built_repo, env=cli_env)
    assert_clean_failure(result)
    assert str(db) in result.output, result.describe()


@pytest.mark.parametrize(
    "kind", ["garbage", "truncated", "foreign-sqlite", "newer-schema"]
)
def test_every_kind_of_corrupt_database_is_reported_cleanly(
    kind, fresh_built_repo, cli_env
):
    """Four ways a graph.db goes bad, one house-style line for each.

    ``garbage`` and ``truncated`` are read failures SQLite itself reports.
    ``foreign-sqlite`` and ``newer-schema`` are the quiet ones: both open
    without complaint and used to be answered from as though they were this
    repository's graph.
    """
    db = fresh_built_repo / ".code-review-graph" / "graph.db"
    _corrupt(db, kind)
    result = run_cli("status", cwd=fresh_built_repo, env=cli_env)
    assert_clean_failure(result)
    assert result.output.strip().startswith("Error: "), result.describe()
    assert str(db) in result.output, result.describe()
    if kind == "newer-schema":
        assert "newer" in result.output, result.describe()
    if kind == "foreign-sqlite":
        assert "not a code-review-graph" in result.output, result.describe()


def test_foreign_repository_database_is_rejected(tmp_path, cli_env):
    donor = init_repo(tmp_path / "donor")
    (donor / "lib").mkdir()
    (donor / "lib" / "donor_mod.py").write_text(
        "def donor_only():\n    return 42\n", encoding="utf-8"
    )
    _git(donor, "add", "-A")
    _git(donor, "commit", "-qm", "donor")
    assert run_cli("build", "-q", cwd=donor, env=cli_env).returncode == 0

    host = init_repo(tmp_path / "host")
    (host / "host_mod.py").write_text("def host_only():\n    return 7\n", encoding="utf-8")
    _git(host, "add", "-A")
    _git(host, "commit", "-qm", "host")
    assert run_cli("build", "-q", cwd=host, env=cli_env).returncode == 0

    host_db = host / ".code-review-graph" / "graph.db"
    for suffix in ("-wal", "-shm"):
        host_db.with_name(host_db.name + suffix).unlink(missing_ok=True)
    shutil.copyfile(donor / ".code-review-graph" / "graph.db", host_db)

    result = run_cli("dead-code", cwd=host, env=cli_env)
    assert "donor_only" not in result.output, (
        "the host repository was served the donor repository's graph:\n" + result.describe()
    )
    assert_clean_failure(result)
    assert "different repository root" in result.output, result.describe()


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory permissions")
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignores the read-only bit",
)
@pytest.mark.parametrize("command", ["status", "detect-changes", "wiki", "visualize", "build"])
def test_read_only_data_dir_is_reported_cleanly(command, fresh_built_repo, cli_env):
    data_dir = fresh_built_repo / ".code-review-graph"
    original = data_dir.stat().st_mode
    data_dir.chmod(0o500)
    try:
        result = run_cli(*_invoke(command), cwd=fresh_built_repo, env=cli_env)
        assert_clean_failure(result)
        assert result.output.strip().startswith("Error: "), result.describe()
        assert str(data_dir) in result.output, result.describe()
    finally:
        data_dir.chmod(original)


def test_directory_that_is_not_a_repository(tmp_path, cli_env):
    """``detect-changes`` needs a VCS; say so instead of tracebacking."""
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    assert run_cli("build", "-q", cwd=plain, env=cli_env).returncode == 0
    result = run_cli("detect-changes", cwd=plain, env=cli_env)
    assert not result.traceback, result.describe()
    assert result.returncode == 1, result.describe()
    assert "git" in result.output.lower(), result.describe()


def test_repository_with_no_commits(tmp_path, cli_env):
    """A fresh ``git init`` has no HEAD~1; build and update must still work."""
    repo = init_repo(tmp_path / "repo")
    write_sample_project(repo)
    for argv in (["build", "-q"], ["status"], ["update", "-q"], ["detect-changes", "--brief"]):
        result = run_cli(*argv, cwd=repo, env=cli_env)
        assert not result.traceback, result.describe()
        assert result.returncode == 0, result.describe()


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory permissions")
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root can traverse a 0o000 directory",
)
@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG: a tracked file the OS cannot stat (parent directory without +x) "
        "raises PermissionError out of collect_all_files (incremental.py:1226, "
        "`full_path.is_file()`) as a traceback; the file should be skipped with "
        "a warning"
    ),
)
def test_unstatable_file_does_not_crash_build(tmp_path, cli_env):
    repo = init_repo(tmp_path / "repo")
    write_sample_project(repo)
    (repo / "locked").mkdir()
    (repo / "locked" / "hidden.py").write_text("def hidden():\n    return 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "initial")
    locked = repo / "locked"
    locked.chmod(0o000)
    try:
        result = run_cli("build", "-q", cwd=repo, env=cli_env)
        assert not result.traceback, result.describe()
        assert result.returncode == 0, result.describe()
    finally:
        locked.chmod(0o755)


def test_dangling_symlink_does_not_crash_build(tmp_path, cli_env):
    repo = init_repo(tmp_path / "repo")
    write_sample_project(repo)
    (repo / "dangling.py").symlink_to(tmp_path / "does-not-exist.py")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "initial")
    result = run_cli("build", "-q", cwd=repo, env=cli_env)
    assert not result.traceback, result.describe()
    assert result.returncode == 0, result.describe()


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["forget", "{long}.py"], id="forget"),
        pytest.param(["query", "callers_of", "{long}"], id="query"),
        pytest.param(["impact", "--files", "{long}.py"], id="impact"),
        pytest.param(["search", "{long}"], id="search"),
    ],
)
def test_path_longer_than_the_filename_limit(argv, built_repo, cli_env):
    """NAME_MAX is 255 on every filesystem this ships on; 4096 chars must not crash."""
    long_name = "x" * 4096
    expanded = [part.format(long=long_name) for part in argv]
    result = run_cli(*expanded, cwd=built_repo, env=cli_env)
    assert not result.traceback, result.describe()
    assert result.returncode == 0, result.describe()


def test_missing_matplotlib_for_svg_export(built_repo, cli_env, blocked_imports):
    """The reference implementation of the house style. Keep it that way."""
    env = {**cli_env, "PYTHONPATH": str(blocked_imports)}
    result = run_cli("visualize", "--format", "svg", cwd=built_repo, env=env)
    assert_clean_failure(result)
    assert result.output.strip().startswith("Error: "), result.describe()
    assert "matplotlib" in result.output, result.describe()
    assert "pip install" in result.output, result.describe()


def test_missing_sentence_transformers_for_embed(built_repo, cli_env, blocked_imports):
    """``embed`` degrades, but not in the house style (see the report)."""
    env = {**cli_env, "PYTHONPATH": str(blocked_imports)}
    result = run_cli("embed", cwd=built_repo, env=env)
    assert_clean_failure(result)
    assert "sentence-transformers" in result.output, result.describe()
    assert "pip install" in result.output, result.describe()


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG: `embed` reports a missing optional dependency through the logging "
        "handler as `ERROR: ...`, not the house `Error: ...` on stderr that "
        "`visualize --format svg` and `update` use"
    ),
)
def test_missing_optional_dependency_uses_one_house_style(
    built_repo, cli_env, blocked_imports
):
    env = {**cli_env, "PYTHONPATH": str(blocked_imports)}
    result = run_cli("embed", cwd=built_repo, env=env)
    assert result.output.strip().startswith("Error: "), result.describe()


def _no_git_env(cli_env: dict[str, str], tmp_path: Path) -> dict[str, str]:
    """A PATH with no ``git`` (and no ``svn``) on it."""
    empty = tmp_path / "empty-bin"
    empty.mkdir(exist_ok=True)
    return {**cli_env, "PATH": str(empty)}


def test_absent_git_binary_is_not_a_false_all_clear(tmp_path, cli_env):
    repo = init_repo(tmp_path / "repo")
    write_sample_project(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "initial")
    assert run_cli("build", "-q", cwd=repo, env=cli_env).returncode == 0
    (repo / "pkg" / "core.py").write_text(
        (repo / "pkg" / "core.py").read_text(encoding="utf-8")
        + "\n\ndef added_later():\n    return helper(9)\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "second")

    with_git = run_cli("detect-changes", "--brief", cwd=repo, env=cli_env)
    assert "Analyzed 1 changed file" in with_git.output, with_git.describe()

    without_git = run_cli(
        "detect-changes", "--brief", cwd=repo, env=_no_git_env(cli_env, tmp_path)
    )
    assert not without_git.traceback, without_git.describe()
    assert "No changes detected" not in without_git.output, (
        "reported a clean tree while unable to read the diff:\n" + without_git.describe()
    )
    # A distinct, loud outcome: non-zero exit, and a message naming the cause.
    assert_clean_failure(without_git)
    assert without_git.output.strip().startswith("Error: "), without_git.describe()
    assert "git" in without_git.output, without_git.describe()
    assert without_git.returncode != with_git.returncode, (
        "'could not look' and 'nothing changed' exit the same way:\n"
        + without_git.describe()
    )


def test_git_timeout_is_not_a_false_all_clear(tmp_path, cli_env):
    repo = init_repo(tmp_path / "repo")
    write_sample_project(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "initial")
    assert run_cli("build", "-q", cwd=repo, env=cli_env).returncode == 0
    (repo / "pkg" / "core.py").write_text(
        (repo / "pkg" / "core.py").read_text(encoding="utf-8")
        + "\n\ndef added_later():\n    return helper(9)\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "second")

    timed_out = run_cli(
        "detect-changes", "--brief", cwd=repo, env={**cli_env, "CRG_GIT_TIMEOUT": "0"}
    )
    assert not timed_out.traceback, timed_out.describe()
    assert "No changes detected" not in timed_out.output, timed_out.describe()
    assert_clean_failure(timed_out)
    assert timed_out.output.strip().startswith("Error: "), timed_out.describe()
    assert "timed out" in timed_out.output, timed_out.describe()
    assert "CRG_GIT_TIMEOUT" in timed_out.output, timed_out.describe()


#: Exit code each command owes with no ``git`` on PATH. ``build`` and
#: ``update`` do not need the diff — they can re-parse the working tree and
#: reconcile by content hash — so they still succeed. ``detect-changes``
#: cannot: its whole answer is the diff, so it reports the inability and
#: exits 1 rather than printing the same all-clear as a clean tree
#: (see test_absent_git_binary_is_not_a_false_all_clear).
_NO_GIT_EXIT_CODES: dict[str, int] = {"detect-changes": 1}


def test_absent_git_binary_does_not_traceback(tmp_path, cli_env):
    """Whatever it reports, it must not crash."""
    repo = init_repo(tmp_path / "repo")
    write_sample_project(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "initial")
    assert run_cli("build", "-q", cwd=repo, env=cli_env).returncode == 0
    no_git = _no_git_env(cli_env, tmp_path)
    for argv in (["status"], ["detect-changes", "--brief"], ["update", "-q"], ["build", "-q"]):
        result = run_cli(*argv, cwd=repo, env=no_git)
        assert not result.traceback, result.describe()
        assert result.returncode == _NO_GIT_EXIT_CODES.get(argv[0], 0), result.describe()


def test_repo_flag_pointing_outside_any_project(built_repo, cli_env, tmp_path):
    """``--repo`` at a directory with no project marker is a user error."""
    stray = tmp_path / "stray"
    stray.mkdir()
    result = run_cli(
        "query", "callers_of", "helper", "--repo", str(stray), cwd=built_repo, env=cli_env
    )
    assert_clean_failure(result)
    assert "--repo" in result.output, result.describe()


# ---------------------------------------------------------------------------
# 3. Environment variables
# ---------------------------------------------------------------------------

#: Every ``CRG_*`` variable the package parses as an int or a float, with a
#: command that reaches the parse and a value that is valid for it.
#: ``reachable`` is False for sites only a running MCP server or an installed
#: igraph can hit; those are still covered by the source-level baseline below.
@dataclass(frozen=True)
class NumericVar:
    name: str
    command: tuple[str, ...]
    valid: str
    reachable: bool = True


NUMERIC_ENV_VARS: tuple[NumericVar, ...] = (
    NumericVar("CRG_GIT_TIMEOUT", ("status",), "45"),
    # Read through a local variable, so ``_numeric_env_sites`` cannot see
    # the parse; listed by hand so the reachable-value tests still run it.
    NumericVar("CRG_DISCOVERY_TIMEOUT", ("detect-changes", "--brief"), "3"),
    NumericVar("CRG_MAX_IMPACT_NODES", ("status",), "250"),
    NumericVar("CRG_MAX_IMPACT_DEPTH", ("status",), "3"),
    NumericVar("CRG_MAX_BFS_DEPTH", ("status",), "10"),
    NumericVar("CRG_MAX_SEARCH_RESULTS", ("status",), "15"),
    NumericVar("CRG_IMPACT_DEPTH_DECAY", ("status",), "0.5"),
    NumericVar("CRG_IMPACT_SCORE_FLOOR", ("status",), "0.1"),
    NumericVar("CRG_PARSE_WORKERS", ("status",), "2"),
    NumericVar("CRG_MODULE_SCAN_DEPTH", ("status",), "2"),
    NumericVar("CRG_MODULE_SCAN_MAX_DIRS", ("status",), "100"),
    NumericVar("CRG_NESTED_IGNORE_TTL", ("status",), "60"),
    NumericVar("CRG_DEPENDENT_HOPS", ("status",), "1"),
    NumericVar("CRG_WATCH_PLAN_DEPTH", ("status",), "2"),
    NumericVar("CRG_MAX_WATCH_SCHEDULES", ("status",), "8"),
    NumericVar("CRG_WATCH_SPLIT_MIN_DIRS", ("status",), "2"),
    NumericVar("CRG_WATCH_HEALTH_INTERVAL", ("status",), "5"),
    NumericVar("CRG_MAX_UNWATCHED_TRACKED", ("status",), "64"),
    NumericVar("CRG_MAX_CHANGED_FUNCS", ("detect-changes", "--brief"), "100"),
    NumericVar("CRG_MAX_TRANSITIVE_FRONTIER", ("detect-changes", "--brief"), "20"),
    NumericVar("CRG_CHURN_WINDOW_DAYS", ("detect-changes", "--brief", "--churn"), "30"),
    NumericVar("CRG_CHURN_TIMEOUT", ("detect-changes", "--brief", "--churn"), "10"),
    NumericVar("CRG_CHURN_MAX_COMMITS", ("detect-changes", "--brief", "--churn"), "500"),
    NumericVar("CRG_RESTART_BACKOFF", ("daemon", "status"), "15"),
    NumericVar("CRG_RESTART_BACKOFF_MAX", ("daemon", "status"), "600"),
    NumericVar("CRG_RESTART_HEALTHY_AFTER", ("daemon", "status"), "300"),
    NumericVar("CRG_WATCH_HEALTH_STALE", ("daemon", "status"), "60"),
    NumericVar("CRG_TOOL_TIMEOUT", ("status",), "30", False),
    NumericVar("CRG_LEIDEN_SEED", ("postprocess",), "42", False),
    NumericVar("CRG_OPENAI_DIMENSION", ("status",), "256", False),
    NumericVar("CRG_OPENAI_BATCH_SIZE", ("status",), "16", False),
    NumericVar("CRG_VOYAGE_OUTPUT_DIMENSION", ("status",), "512", False),
    NumericVar("CRG_VOYAGE_BATCH_SIZE", ("status",), "8", False),
    NumericVar("CRG_VOYAGE_MIN_INTERVAL_SEC", ("status",), "0.5", False),
)

#: Numeric variables whose invalid/empty value escapes as a ``ValueError``
#: traceback instead of a message naming the variable (#912). Empty since
#: every numeric override goes through ``constants.env_int`` /
#: ``constants.env_float``, which fall back to the documented default and
#: warn once, by name. Kept as the hook the parametrized test below reads:
#: a variable that regresses goes back in here with its own xfail.
UNGUARDED_AT_RUNTIME: frozenset[str] = frozenset()

REACHABLE_NUMERIC = tuple(var for var in NUMERIC_ENV_VARS if var.reachable)


def test_numeric_env_var_table_covers_every_numeric_site():
    """The table must not fall behind the source scan."""
    from_source = {name for name, _ in _numeric_env_sites()}
    tabled = {var.name for var in NUMERIC_ENV_VARS}
    missing = from_source - tabled
    assert not missing, f"numeric env vars parsed in the source but not covered: {sorted(missing)}"


@pytest.mark.parametrize("var", REACHABLE_NUMERIC, ids=lambda v: v.name)
def test_numeric_env_var_default_applies_when_unset(var, built_repo, cli_env):
    env = {key: value for key, value in cli_env.items() if key != var.name}
    result = run_cli(*var.command, cwd=built_repo, env=env)
    assert not result.traceback, result.describe()
    assert result.returncode == 0, result.describe()


@pytest.mark.parametrize("var", REACHABLE_NUMERIC, ids=lambda v: v.name)
def test_numeric_env_var_accepts_a_valid_value(var, built_repo, cli_env):
    env = {**cli_env, var.name: var.valid}
    result = run_cli(*var.command, cwd=built_repo, env=env)
    assert not result.traceback, result.describe()
    assert result.returncode == 0, result.describe()


@pytest.mark.parametrize("bad", ["", "abc", "1.5.2", "-"], ids=["empty", "word", "version", "dash"])
@pytest.mark.parametrize(
    "var",
    [
        pytest.param(
            var,
            id=var.name,
            marks=(
                pytest.mark.xfail(
                    strict=True,
                    reason=(
                        f"BUG (#912): an invalid {var.name} escapes as a "
                        "ValueError traceback instead of a message naming the "
                        "variable and falling back to the default"
                    ),
                )
                if var.name in UNGUARDED_AT_RUNTIME
                else ()
            ),
        )
        for var in REACHABLE_NUMERIC
    ],
)
def test_invalid_numeric_env_var_does_not_traceback(var, bad, built_repo, cli_env):
    """An invalid value must degrade with a message, not kill the process."""
    env = {**cli_env, var.name: bad}
    result = run_cli(*var.command, cwd=built_repo, env=env)
    assert not result.traceback, result.describe()
    assert result.returncode == 0, result.describe()


def _numeric_env_sites() -> list[tuple[str, str]]:
    """``[(variable, "incremental.py:28"), ...]`` for every numeric env parse.

    Both spellings: a bare ``int(os.environ.get(...))`` and a call to one of
    the guarded helpers. The helper sites are reported too, so the coverage
    table below still has to name every numeric setting in the package
    rather than going quiet the moment a site is fixed.
    """
    sites: list[tuple[str, str]] = []
    for path in _python_sources():
        text = path.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(text)
        except SyntaxError:  # pragma: no cover - the package must parse
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Name):
                continue
            if func.id in _GUARDED_NUMERIC_HELPERS:
                if node.args and isinstance(node.args[0], ast.Constant):
                    value = node.args[0].value
                    if isinstance(value, str) and value.isupper():
                        sites.append((value, f"{path.name}:{node.lineno}"))
                continue
            if func.id not in ("int", "float") or not node.args:
                continue
            for name in _env_names_in(node.args[0]):
                sites.append((name, f"{path.name}:{node.lineno}"))
    return sites


def _env_names_in(node: ast.AST) -> list[str]:
    """Environment variable names read anywhere inside *node*."""
    names: list[str] = []
    for inner in ast.walk(node):
        if not isinstance(inner, ast.Call):
            continue
        func = inner.func
        target = None
        if isinstance(func, ast.Attribute) and func.attr in ("get", "getenv"):
            value = func.value
            if isinstance(value, ast.Attribute) and value.attr == "environ":
                target = inner
            elif isinstance(value, ast.Name) and value.id == "os":
                target = inner
        if target is None or not target.args:
            continue
        first = target.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            names.append(first.value)
    return names


def _guarded_sites(path: Path) -> set[int]:
    """Line numbers that cannot raise on an unusable value.

    Either inside a ``try`` whose handlers catch ValueError, or a call to
    one of the helpers that already does exactly that — the point of the
    check is "does an invalid value fall back", not "is there a try here".
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(text)
    except SyntaxError:  # pragma: no cover
        return set()
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in _GUARDED_NUMERIC_HELPERS
        ):
            guarded.add(node.lineno)
        if not isinstance(node, ast.Try):
            continue
        catches_value_error = False
        for handler in node.handlers:
            if handler.type is None:
                catches_value_error = True
            for name in ast.walk(handler.type) if handler.type else ():
                if isinstance(name, ast.Name) and name.id in ("ValueError", "Exception"):
                    catches_value_error = True
        if not catches_value_error:
            continue
        for statement in node.body:
            for inner in ast.walk(statement):
                if hasattr(inner, "lineno"):
                    guarded.add(inner.lineno)
    return guarded


#: Frozen baseline of unguarded ``int()``/``float()`` env parses. It was the
#: 22 sites of #912; it is empty now that every one of them reads its value
#: through ``constants.env_int`` / ``constants.env_float``. Adding a *new*
#: bare ``int(os.environ.get(...))`` fails the test below today rather than
#: arriving as a bug report later.
UNGUARDED_BASELINE: frozenset[str] = frozenset(
    UNGUARDED_AT_RUNTIME
)


def test_no_new_unguarded_numeric_env_parse():
    """A new ``int(os.environ.get(...))`` without a fallback fails here.

    Existing offenders are frozen into ``UNGUARDED_BASELINE``; the test fails
    on anything outside it, in either direction, so fixing one forces the
    baseline to shrink rather than letting the coverage go stale.
    """
    guarded_by_file: dict[str, set[int]] = {
        path.name: _guarded_sites(path) for path in _python_sources()
    }
    unguarded: dict[str, list[str]] = {}
    for name, location in _numeric_env_sites():
        filename, _, lineno = location.partition(":")
        if int(lineno) in guarded_by_file.get(filename, set()):
            continue
        unguarded.setdefault(name, []).append(location)

    new = set(unguarded) - UNGUARDED_BASELINE
    assert not new, (
        "new unguarded numeric environment variable(s); wrap the parse in a "
        "try/except ValueError that warns and falls back to the default: "
        + repr({name: unguarded[name] for name in sorted(new)})
    )
    fixed = UNGUARDED_BASELINE - set(unguarded)
    assert not fixed, (
        "these are guarded now — remove them from UNGUARDED_BASELINE and from "
        f"UNGUARDED_AT_RUNTIME: {sorted(fixed)}"
    )


def test_crg_data_dir_override_is_honoured(tmp_path, cli_env):
    """The documented global override sends the graph somewhere else."""
    repo = init_repo(tmp_path / "repo")
    write_sample_project(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "initial")
    data_dir = tmp_path / "elsewhere"
    env = {**cli_env, "CRG_DATA_DIR": str(data_dir)}
    result = run_cli("build", "-q", cwd=repo, env=env)
    assert result.returncode == 0, result.describe()
    assert (data_dir / "graph.db").exists(), result.describe()
    assert not (repo / ".code-review-graph" / "graph.db").exists()


def test_crg_repo_root_override_is_honoured(built_repo, cli_env, tmp_path):
    """``CRG_REPO_ROOT`` resolves the repository from an unrelated cwd."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    env = {**cli_env, "CRG_REPO_ROOT": str(built_repo)}
    result = run_cli("status", cwd=elsewhere, env=env)
    assert result.returncode == 0, result.describe()
    assert result.output.lstrip().startswith("Nodes:"), result.describe()


def test_empty_crg_data_dir_falls_back_to_the_default(tmp_path, cli_env):
    """An empty string is 'unset', not 'the current directory'."""
    repo = init_repo(tmp_path / "repo")
    write_sample_project(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "initial")
    env = {**cli_env, "CRG_DATA_DIR": ""}
    result = run_cli("build", "-q", cwd=repo, env=env)
    assert not result.traceback, result.describe()
    assert result.returncode == 0, result.describe()
    assert (repo / ".code-review-graph" / "graph.db").exists(), result.describe()


@pytest.mark.parametrize(
    "name, value",
    [
        ("CRG_BFS_ENGINE", "nonsense"),
        ("CRG_PARSE_EXECUTOR", "nonsense"),
        ("CRG_SERIAL_PARSE", "maybe"),
        ("CRG_NESTED_OUTPUT_SCAN", "maybe"),
        ("CRG_RECURSE_SUBMODULES", "maybe"),
        ("CRG_ALLOW_REMOTE_CODE", "maybe"),
        ("CRG_REPO_ROOT", ""),
        ("CRG_TOOLS", ""),
    ],
)
def test_non_numeric_env_var_with_a_nonsense_value_degrades(
    name, value, built_repo, cli_env
):
    """A string variable with an unrecognised value falls back, not crashes."""
    env = {**cli_env, name: value}
    result = run_cli("status", cwd=built_repo, env=env)
    assert not result.traceback, result.describe()
    assert result.returncode == 0, result.describe()


#: Variables that tell the subprocess where its interpreter lives. Feeding
#: those a nonsense value tests the launcher, not this package, so the sweep
#: below leaves them alone.
_INTERPRETER_ENV = frozenset(
    {"VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "POETRY_ACTIVE", "LOCALAPPDATA", "HERMES_HOME"}
)

SCANNED_ENV_VARS = tuple(sorted(set(ENV_READS) - _INTERPRETER_ENV))

#: A variable is only exercised by a command that actually reads it, so the
#: sweep reuses the tabled command where there is one and falls back to
#: ``status`` for everything else.
_SWEEP_COMMANDS: dict[str, tuple[str, ...]] = {
    var.name: var.command for var in NUMERIC_ENV_VARS
}


@pytest.mark.parametrize(
    "name",
    [
        pytest.param(
            name,
            id=name,
            marks=(
                pytest.mark.xfail(
                    strict=True,
                    reason=(
                        f"BUG (#912): a nonsense {name} escapes as a ValueError "
                        "traceback at import time"
                    ),
                )
                if name in UNGUARDED_AT_RUNTIME
                else ()
            ),
        )
        for name in SCANNED_ENV_VARS
    ],
)
def test_every_scanned_env_var_survives_a_nonsense_value(name, built_repo, cli_env):
    """Sweep every variable the source scan found, not just the tabled ones.

    Deliberately weak on the exit code — ``CRG_DATA_DIR=nonsense`` correctly
    reports a missing graph and exits 1 — and deliberately strict on the one
    thing that is never acceptable: a traceback. Because the parameter list
    comes from the scan, a variable added tomorrow is covered tomorrow.
    """
    command = _SWEEP_COMMANDS.get(name, ("status",))
    result = run_cli(*command, cwd=built_repo, env={**cli_env, name: "nonsense-value"})
    assert not result.traceback, result.describe()
    assert result.returncode in (0, 1), result.describe()


def test_no_color_is_honoured(built_repo, cli_env):
    """The banner must drop ANSI colour when NO_COLOR is set."""
    coloured = run_cli(cwd=built_repo, env={**cli_env, "NO_COLOR": ""})
    plain = run_cli(cwd=built_repo, env={**cli_env, "NO_COLOR": "1"})
    assert plain.returncode == 0, plain.describe()
    assert "\x1b[" not in plain.output, plain.describe()
    # Not a tty in either run, so colour is off both times; the point is that
    # setting the variable can never *add* escapes.
    assert "\x1b[" not in coloured.output, coloured.describe()


# ---------------------------------------------------------------------------
# 4. Exit codes
# ---------------------------------------------------------------------------


def test_success_exit_code_is_zero_everywhere(built_repo, cli_env):
    """Success is 0. Recorded as one assertion so a drift shows up as a list."""
    failures = []
    for command, argv in sorted(INVOCATIONS.items()):
        result = run_cli(*argv, cwd=built_repo, env=cli_env)
        if result.returncode != EXPECTED_NONZERO.get(command, 0):
            failures.append((command, result.returncode))
    assert not failures, f"commands that did not exit 0 on success: {failures}"


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["no-such-command"], id="unknown-command"),
        pytest.param(["status", "--no-such-flag"], id="unknown-flag"),
        pytest.param(["visualize", "--format", "bogus"], id="bad-choice"),
        pytest.param(["refactor", "rename"], id="missing-required-pair"),
        pytest.param(["serve", "--port", "1234"], id="flag-requires-http"),
        pytest.param(["flow"], id="missing-mutually-exclusive"),
        pytest.param(["impact", "--depth", "-1"], id="bad-int-value"),
        pytest.param(["search", "x", "--limit", "0"], id="non-positive-limit"),
        pytest.param(["update", "--embedding-provider", "local"], id="half-a-pair"),
    ],
)
def test_argument_errors_exit_two(argv, built_repo, cli_env):
    """argparse's convention: usage errors are exit 2, never a traceback."""
    result = run_cli(*argv, cwd=built_repo, env=cli_env)
    assert not result.traceback, result.describe()
    assert result.returncode == 2, result.describe()
    assert "usage:" in result.output, result.describe()


@pytest.mark.parametrize(
    "argv, cwd_is_built",
    [
        pytest.param(["status"], False, id="no-graph"),
        pytest.param(["unregister", "no-such-alias"], True, id="unknown-alias"),
        pytest.param(["daemon", "remove", "no-such-alias"], True, id="unknown-daemon-alias"),
        pytest.param(["daemon", "logs"], True, id="no-log-file"),
    ],
)
def test_user_errors_exit_one(argv, cwd_is_built, built_repo, tmp_path, cli_env):
    """A user error is exit 1 with one line, distinct from argparse's 2."""
    if cwd_is_built:
        cwd = built_repo
    else:
        cwd = init_repo(tmp_path / "bare")
    result = run_cli(*argv, cwd=cwd, env=cli_env)
    assert not result.traceback, result.describe()
    assert result.returncode == 1, result.describe()
    assert result.output.strip(), result.describe()


def test_exit_codes_are_distinct_for_usage_and_runtime_errors(built_repo, tmp_path, cli_env):
    """The two failure classes must not collapse onto the same code."""
    usage = run_cli("status", "--no-such-flag", cwd=built_repo, env=cli_env)
    runtime = run_cli("status", cwd=init_repo(tmp_path / "bare2"), env=cli_env)
    assert usage.returncode == 2, usage.describe()
    assert runtime.returncode == 1, runtime.describe()
    assert usage.returncode != runtime.returncode
