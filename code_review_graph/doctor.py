"""``code-review-graph doctor``: check the setup without changing anything.

Most "it does not work" reports come down to one of a few things, and each
is easy to see once it is named:

* two copies are installed, and the MCP entry or a hook runs the other one,
  which then refuses a graph a newer copy wrote;
* there is no graph yet, or it was built at another commit;
* the last build did not finish;
* hooks are missing, or cannot find the program they call.

``doctor`` reads files and runs ``--version`` on each copy it finds. It never
writes, and it never starts a download: an unpinned ``uvx`` entry with no
``uv tool install`` behind it is reported as unknown, not resolved. Only MCP
entries in the repository's own config files are checked; entries in a home
folder config are shared by every repository and left to the tool itself.

Exit 0 when nothing failed, 1 when something did.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import __version__

OK = "ok"
INFO = "info"
WARN = "warn"
FAIL = "fail"

#: A cold start of an older copy can take a while; a copy that hangs past
#: this is reported as "did not answer" rather than waited on.
_PROBE_TIMEOUT_SECONDS = 30.0

_NAMED_VERSION_RE = re.compile(r"code-review-graph\s+v?(\d+\.\d+\.\d+[0-9A-Za-z.+-]*)")
_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+[0-9A-Za-z.+-]*)")
_UV_TOOL_LINE_RE = re.compile(r"^code-review-graph v(\S+)", re.MULTILINE)
_PINNED_SPEC_RE = re.compile(r"^code-review-graph(?:\[[^\]]*\])?(?:==|@)(\d[^\s,;]*)$")
_PYTHON_RE = re.compile(r"\Apythonw?[0-9]*(?:\.[0-9]+)*\Z")
# A hook written by a later release may name the program by its full path, or
# a Python that runs it with -m, instead of looking it up on PATH.
_ABSOLUTE_PROGRAM_RE = re.compile(
    r"""(?:^|[\s"'])((?:/|[A-Za-z]:[\\/])[^\s"']*code-review-graph(?:\.exe)?)(?=["'\s]|$)""",
    re.MULTILINE,
)
_ABSOLUTE_PYTHON_RE = re.compile(
    r"""(?:^|[\s"'])((?:/|[A-Za-z]:[\\/])[^\s"']*pythonw?[0-9.]*(?:\.exe)?)"""
    r"""["']?\s+-m\s+code_review_graph\b""",
    re.MULTILINE,
)
# ``install`` puts that Python in single quotes, so a folder name may hold spaces.
_QUOTED_PYTHON_RE = re.compile(
    r"""'((?:/|[A-Za-z]:[\\/])[^']*pythonw?[0-9.]*(?:\.exe)?)'\s+-m\s+code_review_graph\b""",
)
# A hook ``install`` wrote from a ``uvx`` or ``uv tool`` copy runs ``uvx``.
_UVX_HOOK_RE = re.compile(r"(?:^|[\s;&|({])uvx\s+code-review-graph\b", re.MULTILINE)

_GIT_HOOK_NAMES = ("pre-commit", "post-checkout", "post-merge", "post-rewrite")
_HOOK_MARKERS = ("code-review-graph", "code_review_graph", "crg-")

_UPGRADE_ANY = (
    "pip install -U code-review-graph, pipx upgrade code-review-graph or "
    "uv tool upgrade code-review-graph, whichever installed it"
)


@dataclass
class Finding:
    """One line of the report."""

    status: str
    message: str
    fix: str = ""


@dataclass
class Copy:
    """A ``code-review-graph`` program found on PATH."""

    path: str
    version: str | None


@dataclass
class McpEntry:
    """The ``code-review-graph`` MCP entry in one config file."""

    file: str
    platforms: list[str]
    command: str
    version: str | None
    source: str


@dataclass
class Report:
    """Everything ``doctor`` found, for the text and the JSON output."""

    repo_root: str
    this_copy: dict[str, str]
    path_copies: list[Copy] = field(default_factory=list)
    mcp_entries: list[McpEntry] = field(default_factory=list)
    graph: dict[str, Any] = field(default_factory=dict)
    hooks: list[dict[str, Any]] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return not any(f.status == FAIL for f in self.findings)

    def to_json(self) -> dict[str, Any]:
        return {
            "healthy": self.healthy,
            "repo_root": self.repo_root,
            "this_copy": self.this_copy,
            "path_copies": [asdict(c) for c in self.path_copies],
            "mcp_entries": [asdict(e) for e in self.mcp_entries],
            "graph": self.graph,
            "hooks": self.hooks,
            "checks": [asdict(f) for f in self.findings],
        }


# ---------------------------------------------------------------------------
# Running other copies
# ---------------------------------------------------------------------------


def _run_quietly(argv: list[str], cwd: Path | None) -> str | None:
    """Run ``argv`` and return its output, or None when it could not run."""
    env = dict(os.environ)
    env["NO_COLOR"] = "1"
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            # A version probe passes no cwd and runs outside the repository: a
            # ``python -m`` started there would import a package the repository
            # ships under that name. Git calls pass the repository.
            cwd=str(cwd) if cwd else tempfile.gettempdir(),
            env=env,
            stdin=subprocess.DEVNULL,
            timeout=_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    if result.returncode != 0:
        return None
    return (result.stdout or "") + (result.stderr or "")


class _Prober:
    """Ask each copy for its version once per run."""

    def __init__(self, run: Callable[[list[str], Path | None], str | None]) -> None:
        self._run = run
        self._seen: dict[tuple[tuple[str, ...], str], str | None] = {}

    def version(self, argv: list[str], cwd: Path | None = None) -> str | None:
        key = (tuple(argv), str(cwd or ""))
        if key not in self._seen:
            output = self._run(argv, cwd) or ""
            match = _NAMED_VERSION_RE.search(output) or _VERSION_RE.search(output)
            self._seen[key] = match.group(1) if match else None
        return self._seen[key]

    def uv_tool_version(self) -> str | None:
        """The version ``uv tool install`` put in place, read without a download."""
        uv = shutil.which("uv")
        if uv is None:
            return None
        output = self._run([uv, "tool", "list"], None)
        match = _UV_TOOL_LINE_RE.search(output or "")
        return match.group(1) if match else None


# ---------------------------------------------------------------------------
# Copies on PATH
# ---------------------------------------------------------------------------


def _program_names() -> list[str]:
    if os.name != "nt":
        return ["code-review-graph"]
    exts = os.environ.get("PATHEXT", ".EXE;.CMD;.BAT").split(os.pathsep)
    return [f"code-review-graph{ext.lower()}" for ext in exts if ext]


def copies_on_path(path_env: str | None = None) -> list[Path]:
    """Every ``code-review-graph`` on PATH, first one first, each file once."""
    found: list[Path] = []
    seen: set[str] = set()
    raw = os.environ.get("PATH", "") if path_env is None else path_env
    for folder in raw.split(os.pathsep):
        if not folder:
            continue
        for name in _program_names():
            candidate = Path(folder) / name
            try:
                if not candidate.is_file() or not os.access(candidate, os.X_OK):
                    continue
                real = os.path.realpath(candidate)
            except OSError:
                continue
            if real in seen:
                continue
            seen.add(real)
            found.append(candidate)
    return found


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------


def _version_key(version: str) -> tuple[int, ...] | None:
    match = re.match(r"(\d+)\.(\d+)\.(\d+)", version)
    return tuple(int(part) for part in match.groups()) if match else None


def _older_than_this(version: str) -> bool | None:
    mine, theirs = _version_key(__version__), _version_key(version)
    if mine is None or theirs is None:
        return None
    return theirs < mine


def _mismatch_fix(version: str, where: str, upgrade: str) -> str:
    """Say which side to upgrade when two copies disagree."""
    if _older_than_this(version) is False:
        return (
            f"This copy ({__version__}) is the older one. Upgrade it "
            f"({_UPGRADE_ANY}), then restart your AI tool."
        )
    return f"Upgrade {where}: {upgrade}. Then restart your AI tool."


def _upgrade_for(path: str) -> str:
    """The upgrade command that most likely fits the copy at ``path``."""
    try:
        # ~/.local/bin/code-review-graph is often a link into uv's or pipx's
        # own folder, and the folder is what says which one installed it.
        path = os.path.realpath(path)
    except (OSError, ValueError):
        pass
    lowered = path.replace("\\", "/").lower()
    if "/uv/tools/" in lowered:
        return "uv tool upgrade code-review-graph"
    if "/pipx/" in lowered:
        return "pipx upgrade code-review-graph"
    return _UPGRADE_ANY


# ---------------------------------------------------------------------------
# MCP entries in this repository's config files
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> Any:
    from .skills import _strip_jsonc

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        return json.loads(_strip_jsonc(text))
    except (json.JSONDecodeError, ValueError):
        return None


def _repo_mcp_configs(root: Path) -> dict[Path, tuple[str, list[str]]]:
    """``{config file: (key, [platform names])}`` for JSON configs in ``root``."""
    from .skills import PLATFORMS

    configs: dict[Path, tuple[str, list[str]]] = {}
    for plat in PLATFORMS.values():
        if plat.get("format") != "object":
            continue
        try:
            paths = plat["config_paths"](root) if "config_paths" in plat else (
                plat["config_path"](root),
            )
        except (OSError, RuntimeError, KeyError):
            continue
        for path in paths:
            try:
                path.resolve().relative_to(root.resolve())
            except (OSError, ValueError):
                continue
            key, names = configs.setdefault(path, (plat["key"], []))
            names.append(plat["name"])
    return configs


def _entry_tokens(entry: dict[str, Any]) -> list[str] | None:
    command = entry.get("command")
    if isinstance(command, str):
        tokens = [command]
    elif isinstance(command, list) and all(isinstance(t, str) for t in command):
        tokens = list(command)
    else:
        return None
    args = entry.get("args", [])
    if not isinstance(args, list) or not all(isinstance(t, str) for t in args):
        return None
    return tokens + args if tokens else None


def _launcher(token: str) -> str:
    name = token.replace("\\", "/").rsplit("/", 1)[-1]
    return name[:-4] if name.lower().endswith(".exe") else name


def _uvx_spec(rest: list[str]) -> str | None:
    """The package spec a ``uvx`` command line asks for."""
    for index, word in enumerate(rest):
        if word == "--from" and index + 1 < len(rest):
            return rest[index + 1]
        if word.startswith("--from="):
            return word.split("=", 1)[1]
    for word in rest:
        if not word.startswith("-"):
            return word
    return None


@dataclass
class _Resolved:
    """What an MCP command line runs, as far as doctor can tell."""

    version: str | None
    source: str
    upgrade: str = _UPGRADE_ANY


def _program_doctor_may_run(exe: str, root: Path) -> str | None:
    """Return the program to ask for its version, or None when doctor must not run it.

    MCP configs and hook files are part of the repository, so whoever wrote the
    repository chose the commands in them. Doctor runs only a bare name it finds
    on PATH, or an absolute path, and never a program inside the repository.
    """
    if os.path.isabs(exe):
        found: str | None = exe
    elif os.sep in exe or "/" in exe or (os.altsep and os.altsep in exe):
        return None
    else:
        found = shutil.which(exe)
    if found is None or not os.path.isfile(found):
        return None
    real, repo = Path(os.path.realpath(found)), Path(os.path.realpath(root))
    if real == repo or repo in real.parents:
        # The copy running doctor, often a .venv inside the project, is
        # already trusted: the user started it.
        own = {Path(os.path.realpath(sys.executable))}
        own.add(Path(os.path.realpath(Path(sys.executable).parent / Path(found).name)))
        return found if real in own else None
    return found


_NOT_RUN = (
    "doctor does not run a program inside the repository or named by a relative "
    "path, because the repository chose it"
)


def resolve_mcp_entry(tokens: list[str], root: Path, prober: _Prober) -> _Resolved:
    """Find the version an MCP command line runs, without a download.

    Nothing the repository ships is run: see ``_program_doctor_may_run``.
    """
    launcher, rest = _launcher(tokens[0]), tokens[1:]

    if launcher == "uvx" or (launcher == "uv" and rest[:2] == ["tool", "run"]):
        if launcher == "uv":
            rest = rest[2:]
        pinned = _PINNED_SPEC_RE.match(_uvx_spec(rest) or "")
        if pinned:
            return _Resolved(
                pinned.group(1), "pinned in the entry",
                "run code-review-graph install again to rewrite the entry",
            )
        if "--isolated" not in rest:
            installed = prober.uv_tool_version()
            if installed:
                return _Resolved(
                    installed, "the copy uv tool install put in place",
                    "uv tool upgrade code-review-graph",
                )
        return _Resolved(None, (
            "uvx picks a copy when it starts, and finding out could mean a "
            "download. To see it, run: uvx code-review-graph --version"
        ))

    if launcher == "uv" and rest[:1] == ["run"]:
        # The project's .venv is part of the repository, so it is not run.
        return _Resolved(None, (
            "uv run uses the project's own copy. To see it, run: "
            "uv run code-review-graph --version"
        ))

    if launcher == "poetry":
        return _Resolved(None, (
            "poetry run uses the project's own copy. To see it, run: "
            "poetry run code-review-graph --version"
        ))

    if _PYTHON_RE.match(launcher) and rest[:2] == ["-m", "code_review_graph"]:
        python = tokens[0]
        if _program_doctor_may_run(python, root) is None:
            return _Resolved(None, _NOT_RUN)
        version = prober.version([python, "-I", "-m", "code_review_graph", "--version"])
        if version:
            return _Resolved(
                version, f"the copy installed for {python}",
                f'"{python}" -m pip install -U code-review-graph',
            )
        return _Resolved(None, f"{python} did not answer --version")

    if launcher == "code-review-graph":
        exe = tokens[0]
        if not os.path.isabs(exe) and os.sep not in exe and "/" not in exe \
                and shutil.which(exe) is None:
            return _Resolved(None, (
                "there is no code-review-graph on PATH here, and the AI "
                "tool's PATH may differ"
            ))
        allowed = _program_doctor_may_run(exe, root)
        if allowed is None:
            return _Resolved(None, _NOT_RUN)
        exe = allowed
        version = prober.version([exe, "--version"])
        if version:
            return _Resolved(version, f"the copy at {exe}", _upgrade_for(exe))
        return _Resolved(None, f"{exe} did not answer --version")

    return _Resolved(None, "it is a hand-written command doctor cannot read")


def check_mcp_entries(
    root: Path, prober: _Prober,
) -> tuple[list[McpEntry], list[Finding]]:
    entries: list[McpEntry] = []
    findings: list[Finding] = []
    for path, (key, names) in _repo_mcp_configs(root).items():
        if not path.is_file():
            continue
        data = _read_json(path)
        servers = data.get(key) if isinstance(data, dict) else None
        entry = servers.get("code-review-graph") if isinstance(servers, dict) else None
        if not isinstance(entry, dict):
            continue
        tokens = _entry_tokens(entry)
        relative = Path(os.path.relpath(path, root)).as_posix()
        label = f"MCP entry in {relative} ({', '.join(names)})"
        if tokens is None:
            findings.append(Finding(WARN, f"{label}: no command doctor can read."))
            continue
        found = resolve_mcp_entry(tokens, root, prober)
        command = " ".join(tokens)
        entries.append(McpEntry(relative, names, command, found.version, found.source))
        if found.version is None:
            findings.append(Finding(
                WARN, f"{label} runs: {command}. Version unknown: {found.source}.",
            ))
        elif found.version == __version__:
            findings.append(Finding(OK, f"{label} runs {found.version} ({found.source})."))
        else:
            findings.append(Finding(
                FAIL,
                f"{label} runs {found.version}, not {__version__} ({found.source}).",
                _mismatch_fix(found.version, "the copy the MCP entry runs", found.upgrade),
            ))
    return entries, findings


# ---------------------------------------------------------------------------
# The graph
# ---------------------------------------------------------------------------


def _read_only_uri(db_path: Path) -> str:
    """Open SQLite without creating or updating any side file."""
    sidecars = [db_path.with_name(db_path.name + s) for s in ("-wal", "-shm")]
    query = "mode=ro" if any(p.exists() for p in sidecars) else "mode=ro&immutable=1"
    return f"{db_path.resolve().as_uri()}?{query}"


def _live_head(root: Path) -> str | None:
    """The checked-out commit, or None outside git or before the first commit."""
    if not (root / ".git").exists():
        return None
    output = _run_quietly(["git", "rev-parse", "--verify", "HEAD"], root)
    return output.strip().splitlines()[0] if output and output.strip() else None


def _holds_commits_since(root: Path, db_path: Path, built: str) -> bool:
    """Whether the graph holds every file changed between ``built`` and HEAD."""
    from .incremental import files_hold_commits
    from .parser import normalize_file_path

    def file_hashes() -> dict[str, str]:
        conn = sqlite3.connect(_read_only_uri(db_path), uri=True, timeout=2)
        try:
            rows = conn.execute(
                "SELECT file_path, file_hash FROM nodes WHERE kind = ?", ("File",)
            ).fetchall()
        finally:
            conn.close()
        return {normalize_file_path(path): file_hash or "" for path, file_hash in rows}

    try:
        return files_hold_commits(root, file_hashes, built)
    except (sqlite3.Error, OSError):
        return False


def check_graph(root: Path) -> tuple[dict[str, Any], list[Finding]]:
    from .build_state import BUILD_STATE_KEY, INCOMPLETE_BUILD_STATES
    from .incremental import get_db_path
    from .migrations import LATEST_VERSION, get_schema_version

    db_path = get_db_path(root, read_only=True)
    info: dict[str, Any] = {
        "path": str(db_path),
        "exists": db_path.is_file(),
        "supported_schema_version": LATEST_VERSION,
    }
    if not db_path.is_file():
        return info, [Finding(
            FAIL, f"No graph at {db_path}.", "Run: code-review-graph build",
        )]
    try:
        conn = sqlite3.connect(_read_only_uri(db_path), uri=True, timeout=2)
        try:
            schema = get_schema_version(conn)
            metadata = dict(conn.execute(
                "SELECT key, value FROM metadata WHERE key IN (?, ?, ?)",
                (BUILD_STATE_KEY, "git_head_sha", "last_updated"),
            ).fetchall())
            nodes = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
            files = conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE kind = 'File'"
            ).fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return info, [Finding(
            FAIL, f"Could not read the graph at {db_path} ({exc}).",
            "Run: code-review-graph build",
        )]
    info.update(schema_version=schema, nodes=nodes, files=files)
    findings: list[Finding] = []

    if schema > LATEST_VERSION:
        findings.append(Finding(
            FAIL,
            f"The graph is schema v{schema}, written by a newer copy; this copy "
            f"({__version__}) understands v{LATEST_VERSION}.",
            f"Upgrade this copy ({_UPGRADE_ANY}).",
        ))
        return info, findings
    if schema < LATEST_VERSION:
        findings.append(Finding(
            WARN,
            f"The graph is schema v{schema}; the next build or update moves it "
            f"to v{LATEST_VERSION}, and older copies cannot read it after that.",
        ))

    state = str(metadata.get(BUILD_STATE_KEY) or "")
    info["build_state"] = state or None
    if state in INCOMPLETE_BUILD_STATES:
        findings.append(Finding(
            FAIL, "The last build did not finish, so files may be missing.",
            "Run: code-review-graph build",
        ))

    # Read here rather than through ``graph_provenance``: its read-only open
    # still leaves -wal and -shm files beside a checkpointed graph.
    built = metadata.get("git_head_sha") or None
    head = _live_head(root) if built else None
    info.update(built_at_sha=built, head_sha=head, updated_at=metadata.get("last_updated"))
    summary = f"Graph: schema v{schema}, {files} files, {nodes} nodes"
    matches = built == head if built and head else None
    if matches is False and _holds_commits_since(root, db_path, str(built)):
        # The pre-commit hook updates the graph before the commit exists, so
        # it records the parent. get_minimal_context_tool counts that graph
        # as current; doctor must not call it stale.
        info["stale"] = False
        findings.append(Finding(
            OK,
            f"{summary}, built at {str(built)[:7]}. HEAD is {str(head)[:7]}, but "
            "the graph already holds every file changed since, so it is current.",
        ))
    elif matches is False:
        info["stale"] = True
        findings.append(Finding(
            FAIL,
            f"{summary}, built at {str(built)[:7]} but HEAD is {str(head)[:7]}.",
            "Run: code-review-graph update",
        ))
    elif matches is True:
        info["stale"] = False
        findings.append(Finding(OK, f"{summary}, built at HEAD ({str(head)[:7]})."))
    else:
        info["stale"] = None
        findings.append(Finding(
            INFO, f"{summary}. Cannot tell which commit it was built at.",
        ))
    return info, findings


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------


def _git_hooks_dir(root: Path) -> Path | None:
    output = _run_quietly(["git", "rev-parse", "--git-path", "hooks"], root)
    if output and output.strip():
        return root / output.strip().splitlines()[0]
    fallback = root / ".git" / "hooks"
    return fallback if fallback.is_dir() else None


def _ours(text: str) -> bool:
    return any(marker in text for marker in _HOOK_MARKERS)


def _runners(text: str) -> set[str]:
    """What a hook runs: full paths it names, ``uvx``, or ``PATH`` when it names neither."""
    named = (
        set(_ABSOLUTE_PROGRAM_RE.findall(text))
        | set(_ABSOLUTE_PYTHON_RE.findall(text))
        | set(_QUOTED_PYTHON_RE.findall(text))
    )
    if _UVX_HOOK_RE.search(text):
        named.add("uvx")
    return named or {"PATH"}


def _settings_hook_events(path: Path) -> list[str]:
    """Event names whose hooks in ``path`` call code-review-graph."""
    data = _read_json(path)
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, dict):
        return []
    events: list[str] = []
    for event, entries in hooks.items():
        if not isinstance(entries, list):
            continue
        commands: list[str] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            commands.append(str(entry.get("command", "")))
            for inner in entry.get("hooks", []) if isinstance(entry.get("hooks"), list) else []:
                if isinstance(inner, dict):
                    commands.append(str(inner.get("command", "")))
        if any(_ours(command) for command in commands):
            events.append(str(event))
    return events


def _hook_settings_files(root: Path) -> list[tuple[str, Path]]:
    from .skills import _codex_home

    return [
        ("Claude Code", root / ".claude" / "settings.json"),
        ("Qoder", root / ".qoder" / "settings.json"),
        ("CodeBuddy Code", root / ".codebuddy" / "settings.json"),
        ("Gemini CLI", root / ".gemini" / "settings.json"),
        ("Codex", _codex_home() / "hooks.json"),
        ("Cursor", Path.home() / ".cursor" / "hooks.json"),
    ]


def _check_uvx_hooks(prober: _Prober, path_copies: list[Copy]) -> list[Finding]:
    """Check hooks that run ``uvx code-review-graph``, without starting uvx."""
    if shutil.which("uvx") is None:
        if path_copies:
            return []  # the hooks fall back to the copy on PATH, checked above
        return [Finding(
            WARN,
            "The hooks call uvx, or code-review-graph from PATH, but neither is "
            "on PATH here, so they do nothing.",
            "Install it where your shell finds it, for example: "
            "uv tool install code-review-graph",
        )]
    version = prober.uv_tool_version()
    if version is None:
        return [Finding(
            WARN,
            "The hooks run uvx code-review-graph. Version unknown: uvx picks a copy "
            "when it starts, and finding out could mean a download. To see it, run: "
            "uvx code-review-graph --version",
        )]
    if version != __version__:
        return [Finding(
            FAIL,
            f"The hooks run uvx code-review-graph, which is {version}, not {__version__}.",
            _mismatch_fix(version, "the copy uvx runs", "uv tool upgrade code-review-graph"),
        )]
    return []


def check_hooks(
    root: Path, prober: _Prober, path_copies: list[Copy],
) -> tuple[list[dict[str, Any]], list[Finding]]:
    hooks: list[dict[str, Any]] = []
    runners: set[str] = set()

    hooks_dir = _git_hooks_dir(root)
    if hooks_dir is not None:
        names: list[str] = []
        for name in _GIT_HOOK_NAMES:
            path = hooks_dir / name
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if _ours(text):
                names.append(name)
                runners.update(_runners(text))
        if names:
            hooks.append({"where": "git", "file": str(hooks_dir), "events": names})

    for label, path in _hook_settings_files(root):
        if not path.is_file():
            continue
        events = _settings_hook_events(path)
        if events:
            hooks.append({"where": label, "file": str(path), "events": events})
            text = path.read_text(encoding="utf-8", errors="replace")
            runners.update(_runners(text))

    findings: list[Finding] = []
    if not hooks:
        findings.append(Finding(
            INFO,
            "Hooks: none found, so the graph changes only when you run update or "
            "build. code-review-graph install adds them.",
        ))
        return hooks, findings

    listed = "; ".join(f"{h['where']} {', '.join(h['events'])}" for h in hooks)
    findings.append(Finding(OK, f"Hooks: {listed}."))
    if "PATH" in runners and not path_copies:
        findings.append(Finding(
            WARN,
            "The hooks call code-review-graph from PATH, but there is none on "
            "PATH here, so they do nothing.",
            "Install it where your shell finds it, for example: "
            "uv tool install code-review-graph",
        ))
    if "uvx" in runners:
        findings.extend(_check_uvx_hooks(prober, path_copies))
    for runner in sorted(r for r in runners if r not in ("PATH", "uvx")):
        if _program_doctor_may_run(runner, root) is None:
            findings.append(Finding(
                WARN, f"The hooks run {runner}. Version unknown: {_NOT_RUN}.",
                "Run code-review-graph install again to rewrite the hooks.",
            ))
            continue
        argv = (
            [runner, "-I", "-m", "code_review_graph", "--version"]
            if _PYTHON_RE.match(_launcher(runner)) else [runner, "--version"]
        )
        version = prober.version(argv)
        if version is None:
            findings.append(Finding(
                FAIL, f"The hooks run {runner}, which did not answer --version.",
                "Run code-review-graph install again to rewrite the hooks.",
            ))
        elif version != __version__:
            findings.append(Finding(
                FAIL, f"The hooks run {runner}, which is {version}, not {__version__}.",
                _mismatch_fix(version, f"the copy at {runner}", _upgrade_for(runner)),
            ))
    return hooks, findings


# ---------------------------------------------------------------------------
# Putting it together
# ---------------------------------------------------------------------------


def check_path_copies(prober: _Prober) -> tuple[list[Copy], list[Finding]]:
    copies = [Copy(str(p), prober.version([str(p), "--version"])) for p in copies_on_path()]
    findings: list[Finding] = []
    if not copies:
        findings.append(Finding(
            INFO,
            "On PATH: no code-review-graph. Your shell cannot find it, and nor can a "
            "hook that names no copy.",
        ))
    for index, copy in enumerate(copies):
        # Hooks install writes now name their own copy (checked under
        # Hooks); only a shell, and a hook that names none, use this one.
        role = (
            "Your shell runs this one, and so does a hook that names no copy"
            if index == 0 else "Hidden behind the one above"
        )
        shown = copy.version or "no answer to --version"
        message = f"On PATH: {copy.path} {shown}. {role}."
        if copy.version == __version__:
            findings.append(Finding(OK, message))
        elif index == 0:
            findings.append(Finding(
                FAIL, message,
                _mismatch_fix(copy.version or "", f"the copy at {copy.path}",
                              _upgrade_for(copy.path))
                if copy.version else f"Reinstall the copy at {copy.path}.",
            ))
        else:
            findings.append(Finding(WARN, message, f"Remove it, or upgrade it too: "
                                                   f"{_upgrade_for(copy.path)}."))
    return copies, findings


def diagnose(
    repo_root: Path,
    run: Callable[[list[str], Path | None], str | None] = _run_quietly,
) -> Report:
    """Run every check and return the report. Reads only."""
    prober = _Prober(run)
    here = Path(__file__).resolve().parent
    report = Report(
        repo_root=str(repo_root),
        this_copy={"version": __version__, "package": str(here), "python": sys.executable},
    )
    report.findings.append(Finding(OK, f"This copy: {__version__} at {here}."))

    report.path_copies, found = check_path_copies(prober)
    report.findings.extend(found)
    report.mcp_entries, found = check_mcp_entries(repo_root, prober)
    report.findings.extend(found)
    report.graph, found = check_graph(repo_root)
    report.findings.extend(found)
    report.hooks, found = check_hooks(repo_root, prober, report.path_copies)
    report.findings.extend(found)
    return report


def render(report: Report) -> str:
    lines = [f"code-review-graph doctor: {report.repo_root}", ""]
    for finding in report.findings:
        lines.append(f"  {finding.status:<5} {finding.message}")
        if finding.fix:
            lines.append(f"        Fix: {finding.fix}")
    problems = sum(1 for f in report.findings if f.status == FAIL)
    lines.append("")
    lines.append(
        "All good." if not problems
        else f"{problems} problem{'s' if problems != 1 else ''} found."
    )
    return "\n".join(lines)


def run_doctor(repo_root: Path, *, as_json: bool = False) -> int:
    """Print the report and return the exit code: 0 healthy, 1 not."""
    report = diagnose(repo_root)
    if as_json:
        print(json.dumps(report.to_json(), indent=2))
    else:
        print(render(report))
    return 0 if report.healthy else 1
