"""Incremental graph update logic.

Detects changed files via git diff, re-parses only changed + impacted files,
and updates the graph accordingly. Also supports CLI invocation for hooks.
"""

from __future__ import annotations

import concurrent.futures
import fnmatch
import hashlib
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Callable, NamedTuple, Optional

from .build_state import advance_to_postprocess_pending
from .constants import GIT_TIMEOUT as _GIT_TIMEOUT
from .constants import discovery_timeout, env_float, env_int
from .errors import ChangeDiscoveryError, GraphRootMismatchError, GraphStoreError
from .graph import GraphStore
from .parser import CodeParser, normalize_file_path

_MAX_PARSE_WORKERS = env_int("CRG_PARSE_WORKERS", min(os.cpu_count() or 4, 8))

# Set only while the in-process FastMCP server is using stdio transport.
# This is deliberately separate from ``sys.stdin.isatty()``: CI, cron, and
# redirected CLI builds also have non-TTY stdin, but do not share the MCP
# transport's file-descriptor lifetime problem.
_MCP_STDIO_ACTIVE = False

# Each process-pool worker runs this module in its own process, while each
# thread-pool worker needs isolated parser state.  A thread-local cache covers
# both cases and avoids rebuilding CodeParser (including its grammar probes and
# parser caches) for every file in a parallel build.
_PARSE_WORKER_STATE = threading.local()


def _select_executor_kind() -> str:
    """Return 'process' or 'thread' for parallel parsing.

    Defaults to ``process`` (the original behavior, fastest on Linux/macOS).
    Auto-switches to ``thread`` for an active MCP stdio server on every
    platform, where ``ProcessPoolExecutor`` workers can inherit the transport
    pipe/socket and prevent EOF shutdown. The older Windows non-TTY fallback
    remains for direct integrations that predate the explicit transport flag
    (issues #46, #136, PR #615).

    Override explicitly with ``CRG_PARSE_EXECUTOR={process,thread}``.

    Tree-sitter parsing in the worker releases the GIL during native
    parsing, so the speedup loss for falling back to threads is small
    (typically <30% on the full-build path) and the trade is worth it
    to avoid the deadlock + zombie process accumulation.
    """
    explicit = os.environ.get("CRG_PARSE_EXECUTOR", "").strip().lower()
    if explicit in ("process", "thread"):
        return explicit
    if _MCP_STDIO_ACTIVE:
        return "thread"
    if sys.platform == "win32" and not sys.stdin.isatty():
        return "thread"
    return "process"


def _make_executor(max_workers: int):
    """Construct the parallel-parse executor selected by [_select_executor_kind]."""
    if _select_executor_kind() == "thread":
        return concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
    return concurrent.futures.ProcessPoolExecutor(max_workers=max_workers)

logger = logging.getLogger(__name__)

CPP_IDENTITY_VERSION = "1"
_CPP_IDENTITY_METADATA_KEY = "cpp_identity_version"
_CPP_IDENTITY_PENDING_KEY = "cpp_identity_pending"


def _load_cpp_identity_pending(store: GraphStore) -> set[str] | None:
    """Load failed paths from a complete attempt of this identity version.

    A missing/older checkpoint must still take the normal full migration path.
    Keep the global version stale until every recorded replacement succeeds.
    """
    try:
        state = json.loads(store.get_metadata(_CPP_IDENTITY_PENDING_KEY) or "null")
    except (TypeError, ValueError):
        return None
    if not isinstance(state, dict) or state.get("version") != CPP_IDENTITY_VERSION:
        return None
    files = state.get("files")
    if not isinstance(files, list) or any(
        not isinstance(path, str)
        or not path
        or Path(path).is_absolute()
        or ".." in Path(path).parts
        for path in files
    ):
        return None
    return set(files)


def _store_cpp_identity_pending(store: GraphStore, files: set[str]) -> None:
    # Publish completion before clearing retry state: interruption can cause
    # an extra retry, but cannot leave a stale version with no pending paths.
    if not files:
        store.set_metadata(_CPP_IDENTITY_METADATA_KEY, CPP_IDENTITY_VERSION)
    store.set_metadata(
        _CPP_IDENTITY_PENDING_KEY,
        json.dumps({"version": CPP_IDENTITY_VERSION, "files": sorted(files)}),
    )


def _run_python_resolver(store: GraphStore) -> Optional[dict]:
    """Run repository-wide Python import resolution without failing a build."""
    try:
        from .python_resolver import resolve_python_imports
        return resolve_python_imports(store)
    except Exception as exc:  # noqa: BLE001 - best-effort post-pass
        logger.warning("Python import resolver failed: %s", exc)
        return None


def _run_rescript_resolver(store: GraphStore) -> Optional[dict]:
    """Run the ReScript cross-module resolver, swallowing any failure so
    build never fails because of it. Returns stats or None on error.
    """
    try:
        from .rescript_resolver import resolve_rescript_cross_module
        return resolve_rescript_cross_module(store)
    except Exception as exc:  # noqa: BLE001 - best-effort post-pass
        logger.warning("ReScript cross-module resolver failed: %s", exc)
        return None


def _run_spring_resolver(store: GraphStore) -> Optional[dict]:
    """Run the Spring DI call resolver, swallowing any failure so
    build never fails because of it. Returns stats or None on error.
    """
    try:
        from .spring_resolver import resolve_spring_di_calls
        return resolve_spring_di_calls(store)
    except Exception as exc:  # noqa: BLE001 - best-effort post-pass
        logger.warning("Spring DI resolver failed: %s", exc)
        return None


def _run_spring_event_resolver(store: GraphStore) -> Optional[dict]:
    """Run the Spring application-event resolver without failing a build."""
    try:
        from .event_resolver import resolve_spring_events
        return resolve_spring_events(store)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Spring event resolver failed: %s", exc)
        return None


def _run_temporal_resolver(store: GraphStore) -> Optional[dict]:
    """Run the Temporal workflow/activity call resolver, swallowing any failure so
    build never fails because of it. Returns stats or None on error.
    """
    try:
        from .temporal_resolver import resolve_temporal_calls
        return resolve_temporal_calls(store)
    except Exception as exc:  # noqa: BLE001 - best-effort post-pass
        logger.warning("Temporal resolver failed: %s", exc)
        return None


def _run_hcl_resolver(store: GraphStore) -> Optional[dict]:
    """Run Terraform module-scope resolution without failing a build."""
    try:
        from .hcl_resolver import resolve_hcl_module_references
        return resolve_hcl_module_references(store)
    except Exception as exc:  # noqa: BLE001 - best-effort post-pass
        logger.warning("Terraform/HCL resolver failed: %s", exc)
        return None


def _run_scoped_resolver(store: GraphStore) -> Optional[dict]:
    """Resolve static/scoped ``Class::method`` calls without failing a build."""
    try:
        from .scoped_resolver import resolve_scoped_calls
        return resolve_scoped_calls(store)
    except Exception as exc:  # noqa: BLE001 - best-effort post-pass
        logger.warning("Scoped call resolver failed: %s", exc)
        return None


def _refresh_target_resolution(store: GraphStore) -> None:
    """Reclassify CALLS/REFERENCES targets after the cross-file resolvers.

    The resolvers above rewrite bare targets into qualified ones, so this has
    to run last. A failure here costs the query layer its stored certainty
    column, which every read falls back from safely, so it must never fail a
    build.
    """
    try:
        store.refresh_target_resolution()
    except Exception as exc:  # noqa: BLE001 - best-effort post-pass
        logger.warning("Target-resolution refresh failed: %s", exc)


# Default ignore patterns (in addition to .gitignore).
#
# ``**/<dir>/**`` patterns are safe-anywhere directory exclusions.  A leading
# slash anchors a pattern to the repository root, which prevents ambiguous
# output names such as ``build`` and ``dist`` from hiding nested source
# directories.  See: #91 and PR #92.
DEFAULT_IGNORE_PATTERNS = [
    "**/.code-review-graph/**",
    "**/node_modules/**",
    "**/.git/**",
    "**/.svn/**",
    "**/__pycache__/**",
    "*.pyc",
    "**/.venv/**",
    "**/venv/**",
    "/dist/**",
    "/build/**",
    "/.next/**",
    "/.nuxt/**",
    "/target/**",
    "/bin/**",
    "/obj/**",
    # PHP / Laravel / Composer
    "**/vendor/**",
    "/storage/**",
    "/bootstrap/cache/**",
    "/public/build/**",
    # Ruby / Bundler
    "**/.bundle/**",
    # Java / Kotlin / Gradle
    "**/.gradle/**",
    "*.jar",
    # Dart / Flutter
    "**/.dart_tool/**",
    "**/.pub-cache/**",
    # AWS CDK
    "**/cdk.out/**",
    # General
    "/coverage/**",
    "**/.cache/**",
    "/.tmp/**",
    "/tmp/**",  # nosec B108 -- repo-relative ignore glob, not a temp-file path
    "*.min.js",
    "*.min.css",
    "*.map",
    "*.lock",
    "package-lock.json",
    "yarn.lock",
    "*.db",
    "*.sqlite",
    "*.db-journal",
    "*.db-wal",
]

# Build-output directories that ``DEFAULT_IGNORE_PATTERNS`` only anchors at the
# repository root.  A nested copy is ignored as well, but only when a sibling
# manifest proves the directory is that module's build output — ``moduleA/pom.xml``
# next to ``moduleA/target/``.  Without that evidence the nested directory keeps
# being parsed and watched, so the root anchoring from #91/#92 still protects
# everyone whose nested ``build/`` or ``dist/`` holds real sources.  See: #811.
NESTED_OUTPUT_DIR_MARKERS: dict[str, frozenset[str]] = {
    "target": frozenset({"pom.xml", "Cargo.toml", "build.sbt"}),
    "build": frozenset({
        "build.gradle",
        "build.gradle.kts",
        "settings.gradle",
        "settings.gradle.kts",
    }),
    ".next": frozenset({
        "next.config.js",
        "next.config.mjs",
        "next.config.cjs",
        "next.config.ts",
    }),
    ".nuxt": frozenset({"nuxt.config.js", "nuxt.config.mjs", "nuxt.config.ts"}),
}

# Bounds for the nested build-output scan.  The scan only lists directories
# (no file stats), stops at ``CRG_MODULE_SCAN_DEPTH`` levels, never descends
# into an already-ignored tree, and its result is cached per repository so
# incremental updates never pay for it twice inside the TTL.
_MODULE_SCAN_DEPTH = env_int("CRG_MODULE_SCAN_DEPTH", 3)
_MODULE_SCAN_MAX_DIRS = env_int("CRG_MODULE_SCAN_MAX_DIRS", 2000)
_MAX_NESTED_OUTPUT_PATTERNS = 200
_NESTED_IGNORE_TTL_SECONDS = env_float("CRG_NESTED_IGNORE_TTL", 300.0)

_nested_ignore_cache: dict[tuple[str, tuple[str, ...]], tuple[float, list[str]]] = {}
_nested_ignore_lock = threading.Lock()


def find_svn_root(start: Path | None = None) -> Optional[Path]:
    """Walk up from start to find the SVN working copy root.

    For SVN 1.7+, there is a single ``.svn`` at the WC root.
    For older SVN, every directory has ``.svn`` — we return the topmost one
    found so that the WC root is correctly identified.
    """
    current = start or Path.cwd()
    candidate: Optional[Path] = None
    while current != current.parent:
        if (current / ".svn").exists():
            candidate = current
        current = current.parent
    if (current / ".svn").exists():
        candidate = current
    return candidate


def find_repo_root(
    start: Path | None = None,
    stop_at: Path | None = None,
) -> Optional[Path]:
    """Walk up from ``start`` to find the nearest ``.git`` directory or SVN working copy root.

    Args:
        start: Starting directory.  Defaults to ``Path.cwd()``.
        stop_at: Optional boundary — if provided, the walk examines
            ``stop_at`` for a ``.git`` directory and then stops without
            crossing above it.  Useful for tests that create a synthetic
            repo under ``tmp_path`` (so the walk does not accidentally
            climb into a developer's home-directory dotfiles repo) and
            for any production caller that wants to bound the ancestor
            walk — e.g. multi-repo orchestrators, CI containers with
            bind-mounted volumes, embedded sandboxes.  See #241.

    Returns:
        The first ancestor containing ``.git`` or an SVN working copy,
        or ``None`` if no ancestor up to and including ``stop_at`` (when
        set) or the filesystem root (when ``stop_at is None``) contains one.
    """
    current = start or Path.cwd()
    while current != current.parent:
        if (current / ".git").exists():
            return current
        if stop_at is not None and current == stop_at:
            return None
        current = current.parent
    if (current / ".git").exists():
        return current
    # No Git root found — try SVN
    return find_svn_root(start)


def detect_vcs(root: Path) -> str:
    """Return ``'git'``, ``'svn'``, or ``'none'`` based on VCS markers at *root*."""
    if (root / ".git").exists():
        return "git"
    if (root / ".svn").exists():
        return "svn"
    return "none"


def find_project_root(
    start: Path | None = None,
    stop_at: Path | None = None,
) -> Path:
    """Find the project root.

    Resolution order (highest precedence first):

    1. ``CRG_REPO_ROOT`` environment variable — explicit override for
       anyone scripting the CLI from outside the repo (CI jobs, daemons,
       multi-repo orchestrators). See: #155
    2. Git repository root via :func:`find_repo_root` from ``start``,
       honoring ``stop_at`` if provided.
    3. ``start`` itself (or cwd if no start given).

    ``stop_at`` is forwarded to :func:`find_repo_root` so callers that
    want to bound the ancestor walk (typically tests; see #241) can do so
    without having to call ``find_repo_root`` directly.
    """
    env_override = os.environ.get("CRG_REPO_ROOT", "").strip()
    if env_override:
        p = Path(env_override).expanduser().resolve()
        if p.exists():
            return p
    root = find_repo_root(start, stop_at=stop_at)
    if root:
        return root
    return start or Path.cwd()


def _write_data_dir_gitignore(data_dir: Path) -> None:
    """Write .gitignore file in data directory if it doesn't exist.

    The gitignore contains a single '*' to prevent accidental commits.
    """
    inner_gitignore = data_dir / ".gitignore"
    if not inner_gitignore.exists():
        try:
            # `encoding="utf-8"` is REQUIRED — the em-dash in the header is
            # U+2014 which falls outside cp1252.  On Windows, calling
            # write_text without an encoding silently uses the system default
            # codepage, producing a file that subsequently fails to decode as
            # UTF-8 (see issue #239).
            inner_gitignore.write_text(
                "# Auto-generated by code-review-graph — do not commit database files.\n"
                "# The graph.db contains absolute paths and code structure metadata.\n"
                "*\n",
                encoding="utf-8",
            )
        except OSError:
            # Data dir might be read-only (rare); that's OK, it's a best-effort guard.
            pass


def _create_data_dir(data_dir: Path) -> None:
    """Create the data directory, or say why it could not be created.

    ``GraphStore`` already reports a directory it cannot *write* to as one
    ``Error: ...`` line naming ``CRG_DATA_DIR``. A directory that cannot be
    *created* is the same failure one step earlier, and reached the user as a
    ``PermissionError`` traceback out of ``pathlib.mkdir`` because it happens
    while the path is still being resolved, before any store exists.
    """
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise GraphStoreError(
            f"cannot create the graph data directory at {data_dir} ({exc}). "
            f"Check the permissions on {data_dir.parent}, or set CRG_DATA_DIR "
            "to a writable directory."
        ) from exc
    _write_data_dir_gitignore(data_dir)


def get_data_dir(repo_root: Path, *, create: bool = True) -> Path:
    """Return the directory where this project's graph data lives.

    Resolution priority:
    1. Registry entry for this repo (set via --data-dir)
    2. CRG_DATA_DIR environment variable (global override)
    3. Default: <repo>/.code-review-graph/

    By default, ``<repo_root>/.code-review-graph``. If the
    ``CRG_DATA_DIR`` environment variable is set, it is used verbatim
    instead — letting you keep graphs outside the working tree (useful
    for ephemeral workspaces, Docker volumes, or shared caches). See: #155

    By default the directory is created if it does not already exist; an
    inner ``.gitignore`` (with ``*``) is written so any accidentally-nested
    files never get committed. Both are idempotent. Pass ``create=False``
    when resolving the path for a read-only existence check.
    """
    # Check registry first
    try:
        from .registry import Registry, default_registry_path

        # Registry construction creates its parent directory. A read-only
        # lookup must skip it entirely when no registry file exists.
        if create or default_registry_path().is_file():
            registry_data_dir = Registry().get_data_dir_for_repo(str(repo_root))
            if registry_data_dir:
                data_dir = Path(registry_data_dir).resolve()
                if create:
                    _create_data_dir(data_dir)
                return data_dir
    except Exception as exc:
        # If registry lookup fails, log and fall through to other methods
        logger.debug("Registry lookup failed for %s: %s", repo_root, exc)

    # Check environment variable
    env_override = os.environ.get("CRG_DATA_DIR", "").strip()
    if env_override:
        data_dir = Path(env_override).expanduser().resolve()
    else:
        data_dir = repo_root / ".code-review-graph"

    if create:
        _create_data_dir(data_dir)

    return data_dir


def get_db_path(repo_root: Path, *, read_only: bool = False) -> Path:
    """Determine the database path for a repository.

    Respects ``CRG_DATA_DIR`` (see :func:`get_data_dir`). Migrates a
    legacy top-level ``.code-review-graph.db`` file into the new
    directory when it exists (WAL/SHM side-files are discarded). Pass
    ``read_only=True`` to resolve the current path without creating a data
    directory, migrating a legacy database, or deleting side-files.
    """
    crg_dir = get_data_dir(repo_root, create=not read_only)
    new_db = crg_dir / "graph.db"

    if read_only:
        return new_db

    # Migrate legacy database if present (only meaningful when the
    # legacy file sits at the repo root — if CRG_DATA_DIR is set we
    # skip the migration because there's no relationship between the
    # legacy location and the new one).
    legacy_db = repo_root / ".code-review-graph.db"
    if legacy_db.exists() and not new_db.exists():
        legacy_db.rename(new_db)
    # Discard stale WAL/SHM side-files from the old location
    for suffix in ("-wal", "-shm", "-journal"):
        side = repo_root / f".code-review-graph.db{suffix}"
        if side.exists():
            side.unlink()

    return new_db


def ensure_repo_gitignore_excludes_crg(repo_root: Path) -> str:
    """Ensure repo-level .gitignore excludes ``.code-review-graph/``.

    Returns one of:
    - ``created``: .gitignore was created with the entry
    - ``updated``: entry was appended to existing .gitignore
    - ``already-present``: no changes were needed
    """
    gitignore_path = repo_root / ".gitignore"
    existing = gitignore_path.read_text(encoding="utf-8") if gitignore_path.exists() else ""

    for raw_line in existing.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line == ".code-review-graph" or line.startswith(".code-review-graph/"):
            return "already-present"

    block = "# Added by code-review-graph\n.code-review-graph/\n"
    prefix = "\n" if existing and not existing.endswith("\n") else ""
    gitignore_path.write_text(existing + prefix + block, encoding="utf-8")

    if existing:
        return "updated"
    return "created"


def _load_ignore_patterns(repo_root: Path) -> list[str]:
    """Load ignore patterns from .code-review-graphignore file.

    A line starting with ``!`` keeps a path out of the automatic nested
    build-output detection (see :data:`NESTED_OUTPUT_DIR_MARKERS`); it does not
    negate the explicit patterns, which keep their existing meaning.
    """
    patterns = list(DEFAULT_IGNORE_PATTERNS)
    keep: list[str] = []
    ignore_file = repo_root / ".code-review-graphignore"
    if ignore_file.exists():
        for line in ignore_file.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                if line.startswith("!"):
                    keep.append(line[1:].strip().strip("/"))
                    continue
                # Directory names without a slash match at any depth, as in
                # .gitignore. A leading slash remains an explicit root anchor.
                if line.endswith("/"):
                    prefix = line[:-1]
                    if prefix.startswith("/") or "/" in prefix:
                        line = f"{prefix}/**"
                    else:
                        line = f"**/{prefix}/**"
                elif line.endswith("/**") and not line.startswith(("/", "**/")):
                    prefix = line[:-3]
                    if "/" in prefix:
                        line = f"/{line}"
                    else:
                        line = f"**/{line}"
                if line:
                    patterns.append(line)
    patterns.extend(_nested_output_ignore_patterns(repo_root, patterns, tuple(keep)))
    return patterns


def _should_ignore(path: str, patterns: list[str]) -> bool:
    """Check if a path matches any ignore pattern.

    ``**/<dir>/**`` and unanchored single-directory patterns match at any
    depth. A leading slash anchors a pattern to the repository root.
    """
    normalized = path.replace("\\", "/").lstrip("/")
    parts = PurePosixPath(normalized).parts
    for pattern in patterns:
        anchored = pattern.startswith("/")
        candidate = pattern[1:] if anchored else pattern

        if candidate.startswith("**/") and candidate.endswith("/**"):
            segment = candidate[3:-3]
            if segment and segment in parts:
                return True
            continue

        if candidate.endswith("/**"):
            prefix = tuple(part for part in candidate[:-3].split("/") if part)
            if not prefix:
                continue
            if anchored or len(prefix) > 1:
                if parts[: len(prefix)] == prefix:
                    return True
            elif prefix[0] in parts:
                return True
            continue

        if fnmatch.fnmatch(normalized, candidate):
            return True
    return False


def _child_directories(directory: Path) -> list[tuple[str, bool]]:
    """List ``directory`` once, returning ``(name, is_dir)`` for its entries.

    Symlinked directories are reported as non-directories so no walk follows
    them out of the repository.  Returns an empty list for anything that
    cannot be listed — an unreadable directory is not worth a failed watch.
    """
    try:
        with os.scandir(directory) as entries:
            listing: list[tuple[str, bool]] = []
            for entry in entries:
                try:
                    listing.append((entry.name, entry.is_dir(follow_symlinks=False)))
                except OSError:  # pragma: no cover - vanished mid-scan
                    continue
    except OSError as exc:
        logger.debug("Cannot list %s: %s", directory, exc)
        return []
    listing.sort()
    return listing


def _scan_nested_output_dirs(
    repo_root: Path,
    base_patterns: list[str],
    max_depth: int = _MODULE_SCAN_DEPTH,
    max_dirs: int = _MODULE_SCAN_MAX_DIRS,
) -> list[str]:
    """Find nested build-output directories that a sibling manifest confirms.

    Returns root-anchored ignore patterns such as ``/moduleA/target/**``.  The
    walk lists directories only, skips trees the base patterns already ignore,
    and is bounded by *max_depth* and *max_dirs*.
    """
    patterns: list[str] = []
    queue: list[tuple[Path, int]] = [(repo_root, 0)]
    visited = 0
    while queue:
        directory, depth = queue.pop()
        visited += 1
        if visited > max_dirs:
            logger.debug("Nested output scan hit the %d directory cap", max_dirs)
            break
        listing = _child_directories(directory)
        subdirectories = {name for name, is_dir in listing if is_dir}
        file_names = {name for name, is_dir in listing if not is_dir}
        flagged: set[str] = set()
        if depth > 0:
            for output_dir, markers in NESTED_OUTPUT_DIR_MARKERS.items():
                # A watch can start before the first build creates its output.
                # Reserve that path now, without rescanning on each file event.
                if file_names & markers and output_dir not in file_names:
                    relative = (directory / output_dir).relative_to(repo_root).as_posix()
                    patterns.append(f"/{relative}/**")
                    flagged.add(output_dir)
                    if len(patterns) >= _MAX_NESTED_OUTPUT_PATTERNS:
                        logger.debug("Nested output scan hit the pattern cap")
                        return patterns
        if depth >= max_depth:
            continue
        for name in subdirectories:
            if name in flagged:
                continue
            child = directory / name
            if _should_ignore(child.relative_to(repo_root).as_posix(), base_patterns):
                continue
            queue.append((child, depth + 1))
    return patterns


def _nested_output_ignore_patterns(
    repo_root: Path,
    base_patterns: list[str],
    keep: tuple[str, ...] = (),
) -> list[str]:
    """Cached wrapper around :func:`_scan_nested_output_dirs`.

    The result is reused for ``_NESTED_IGNORE_TTL_SECONDS`` so a long-running
    watch pays for the scan once, not on every incremental update, while a
    module added later is still picked up without a restart.  Set
    ``CRG_NESTED_OUTPUT_SCAN=0`` to turn the whole thing off, or list
    ``!some/path`` in ``.code-review-graphignore`` to spare one directory.

    Excluding a directory removes its files from the graph, so the result is
    logged at info level: an unexpected exclusion has to be discoverable from
    a normal build, not only by diffing file counts.
    """
    if os.environ.get("CRG_NESTED_OUTPUT_SCAN", "1").strip().lower() in ("0", "false", "no"):
        return []
    key = (str(repo_root), keep)
    now = time.monotonic()
    with _nested_ignore_lock:
        cached = _nested_ignore_cache.get(key)
        if cached is not None and now - cached[0] < _NESTED_IGNORE_TTL_SECONDS:
            return list(cached[1])
    patterns = _scan_nested_output_dirs(repo_root, base_patterns)
    if keep:
        spared = {entry.replace("\\", "/").strip("/") for entry in keep}
        patterns = [
            pattern for pattern in patterns if pattern[1:-3] not in spared
        ]
    if patterns:
        logger.info(
            "Excluding %d nested build-output director%s (a sibling manifest marks "
            "them as build output; keep one with '!<path>' in "
            ".code-review-graphignore): %s",
            len(patterns),
            "y" if len(patterns) == 1 else "ies",
            ", ".join(pattern[1:-3] for pattern in patterns[:10])
            + (" …" if len(patterns) > 10 else ""),
        )
    with _nested_ignore_lock:
        _nested_ignore_cache[key] = (time.monotonic(), patterns)
    return list(patterns)


def clear_nested_ignore_cache(repo_root: Path | None = None) -> None:
    """Drop cached output patterns for one repository, or all repositories."""
    with _nested_ignore_lock:
        if repo_root is None:
            _nested_ignore_cache.clear()
        else:
            for key in list(_nested_ignore_cache):
                if key[0] == str(repo_root):
                    del _nested_ignore_cache[key]


def _is_binary(path: Path) -> bool:
    """Quick heuristic: check if file appears to be binary."""
    try:
        with path.open("rb") as handle:
            chunk = handle.read(8192)
        return b"\x00" in chunk
    except (OSError, PermissionError):
        return True


# When True, `git ls-files --recurse-submodules` is used so that files
# inside git submodules are included in the graph.  Opt-in via env var;
# can also be overridden per-call through function parameters.
_RECURSE_SUBMODULES = os.environ.get("CRG_RECURSE_SUBMODULES", "").lower() in ("1", "true", "yes")


def _git_branch_info(repo_root: Path) -> tuple[str, str]:
    """Return (branch_name, head_sha) for the current repo state."""
    branch = ""
    sha = ""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True, encoding='utf-8', errors='replace',
            cwd=str(repo_root),
            timeout=_GIT_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            branch = result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, UnicodeDecodeError):
        pass
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True, encoding='utf-8', errors='replace',
            cwd=str(repo_root),
            timeout=_GIT_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            sha = result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, UnicodeDecodeError):
        pass
    return branch, sha


def _svn_revision_info(repo_root: Path) -> tuple[str, str]:
    """Return (branch_path, revision_str) for the current SVN working copy."""
    branch = ""
    rev = ""
    try:
        result = subprocess.run(
            ["svn", "info", "--non-interactive"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(repo_root), timeout=_GIT_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if line.startswith("URL: "):
                    url = line[5:].strip()
                    # Extract trunk/branches/tags segment from SVN URL
                    for marker in ("/branches/", "/tags/", "/trunk"):
                        if marker in url:
                            idx = url.index(marker)
                            branch = url[idx:].lstrip("/")
                            break
                    if not branch and url:
                        branch = url.rstrip("/").split("/")[-1]
                elif line.startswith("Revision: "):
                    rev = line[10:].strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return branch, rev


_SAFE_GIT_REF = re.compile(r"^[A-Za-z0-9_.~^/@{}\-]+$")
_SAFE_SVN_REV = re.compile(r"^r?\d+(:r?\d+|:HEAD|:BASE|:COMMITTED)?$", re.IGNORECASE)


def _decode_name_status_paths(output: bytes) -> list[str]:
    """Decode ``git diff --name-status -z`` output into a list of paths.

    Renames and copies (``R<score>``/``C<score>`` records) carry two paths —
    the old and the new one.  Both are emitted so the old path flows through
    the purge loop in :func:`incremental_update`; otherwise a rename leaves
    the old path's nodes and edges in the graph and the incremental result
    diverges from a full rebuild.
    """
    fields = [os.fsdecode(f) for f in output.split(b"\0") if f]
    paths: list[str] = []
    seen: set[str] = set()
    i = 0
    while i < len(fields):
        status = fields[i]
        takes_two = status[:1] in ("R", "C")
        entry = fields[i + 1 : i + (3 if takes_two else 2)]
        i += 3 if takes_two else 2
        for path in entry:
            if path not in seen:
                seen.add(path)
                paths.append(path)
    return paths


def _store_vcs_metadata(repo_root: Path, store: "GraphStore") -> bool:
    """Persist VCS branch/revision info and report whether its anchor was stored."""
    # The root the stored absolute ``file_path`` values were built from.
    # Consumers that read a path convention out of a file path (``tests/``,
    # ``src/test/``) need it to know where the repository starts; without it
    # they would read the directories above the checkout. See #1023.
    store.set_metadata("repo_root", str(repo_root))
    vcs = detect_vcs(repo_root)
    if vcs == "git":
        branch, sha = _git_branch_info(repo_root)
        if branch:
            store.set_metadata("git_branch", branch)
        if sha:
            store.set_metadata("git_head_sha", sha)
            return True
    elif vcs == "svn":
        branch, rev = _svn_revision_info(repo_root)
        if branch:
            store.set_metadata("svn_branch", branch)
        if rev:
            store.set_metadata("svn_revision", rev)
            return True
    return False


def _commit_object_exists(repo_root: Path, ref: str) -> bool:
    """Return True if *ref* resolves to a commit object present in the repo.

    This is an object-existence check, not an ancestry check: a commit that is
    only reachable from a branch we have since switched away from is still a
    valid ``git diff`` base, so we must accept it. Any git failure (missing
    binary, timeout, unknown ref) is treated as "not usable".
    """
    if not ref or ref.startswith("-") or not _SAFE_GIT_REF.fullmatch(ref):
        return False
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
            capture_output=True,
            cwd=str(repo_root),
            timeout=_GIT_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def resolve_incremental_base(repo_root: Path, store: "GraphStore") -> str | None:
    """Resolve the automatic diff base for a default incremental update.

    The graph records the commit it was last built at (``git_head_sha``). Using
    that as the diff base lets a single ``update`` reconcile every change since
    the graph was last in sync, instead of only the most recent commit, which
    is what a fixed ``HEAD~1`` base does. That fixed base silently misses work
    that arrived through a multi-commit pull, rebase, or branch switch.

    Returns:
        - the stored commit SHA when it is still a usable diff base;
        - ``"HEAD~1"`` for SVN or non-git working copies, whose change
          discovery ignores or reinterprets the base anyway;
        - ``None`` for a git repo with no usable anchor (a fresh or legacy
          database, or a stored commit lost to a history rewrite or shallow
          clone), signalling the caller to do a full rebuild rather than
          diff against a wrong base.
    """
    if detect_vcs(repo_root) != "git":
        return "HEAD~1"
    stored = store.get_metadata("git_head_sha")
    if stored and _commit_object_exists(repo_root, stored):
        return stored
    return None


def resolve_review_base(
    repo_root: Path,
    base: str,
    *,
    timeout: float | None = None,
    require_vcs: bool = False,
) -> str:
    """Resolve a branch-like Git review base to its common ancestor with HEAD.

    ``git diff <branch>`` compares the two tips and therefore includes commits
    made only on the base branch after the reviewed branch diverged.  GitHub's
    "Files changed" view instead uses the merge base.  Preserve explicit
    commit/revision inputs, but translate local and remote-tracking branch
    refs to that common ancestor.

    If ref detection or merge-base resolution fails (for example in a shallow
    clone), return *base* unchanged so callers retain the existing diff
    behaviour rather than silently reporting no changes.

    Args:
        repo_root: Repository root directory.
        base: Git ref to resolve.
        timeout: Seconds allowed for each Git subprocess. ``None`` (default)
            uses the general ``CRG_GIT_TIMEOUT`` budget; the change-discovery
            chain passes the shorter :func:`discovery_timeout` instead.
        require_vcs: Raise
            :class:`~code_review_graph.errors.ChangeDiscoveryError` when git
            cannot be run or overruns *timeout*, instead of returning *base*
            unresolved.

            Falling back to the unresolved ref is right for a shallow clone,
            where there genuinely is no merge base, and wrong for a timeout:
            the caller then diffs two tips instead of the common ancestor and
            silently scopes the review to the base branch's commits too. The
            shorter the budget, the likelier that is, so the discovery chain
            asks to be told.
    """
    if timeout is None:
        timeout = _GIT_TIMEOUT
    if (
        detect_vcs(repo_root) != "git"
        or not base
        or base.startswith("-")
        or not _SAFE_GIT_REF.fullmatch(base)
    ):
        return base

    try:
        symbolic = subprocess.run(
            ["git", "rev-parse", "--symbolic-full-name", "--verify", base],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(repo_root),
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
        symbolic_ref = symbolic.stdout.strip()
        if symbolic.returncode != 0 or not symbolic_ref.startswith(
            ("refs/heads/", "refs/remotes/")
        ):
            return base

        result = subprocess.run(
            ["git", "merge-base", base, "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(repo_root),
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
        resolved = result.stdout.strip()
        if result.returncode == 0 and re.fullmatch(r"[0-9a-fA-F]{40,64}", resolved):
            return resolved
    except (OSError, subprocess.TimeoutExpired) as exc:
        if require_vcs:
            raise _vcs_unavailable("git", exc, timeout=timeout) from exc

    logger.debug("Could not resolve review merge base for %s; using it directly", base)
    return base


def _vcs_unavailable(
    tool: str, exc: BaseException, *, timeout: float | None = None,
) -> ChangeDiscoveryError:
    """Describe a VCS command that could not be run at all.

    Missing binary and timeout are the two failures that say nothing about
    the working tree, so a caller must never read them as "nothing changed".

    *timeout* is the budget that actually expired. It matters which one is
    named: change discovery runs on :func:`~.constants.discovery_timeout`,
    and telling that caller to raise ``CRG_GIT_TIMEOUT`` would send them to a
    knob that does not govern the call they just made.
    """
    if isinstance(exc, subprocess.TimeoutExpired):
        budget = _GIT_TIMEOUT if timeout is None else timeout
        knob = (
            "CRG_GIT_TIMEOUT"
            if timeout is None or budget >= _GIT_TIMEOUT
            else "CRG_DISCOVERY_TIMEOUT"
        )
        return ChangeDiscoveryError(
            f"could not determine the changes: {tool} timed out after "
            f"{budget:g}s. Raise {knob}, or re-run when the "
            "repository is not busy."
        )
    return ChangeDiscoveryError(
        f"could not determine the changes: {tool} could not be run ({exc}). "
        f"Install {tool} and make sure it is on PATH."
    )


def get_changed_files(
    repo_root: Path,
    base: str = "HEAD~1",
    *,
    strict: bool = False,
    timeout: float | None = None,
    require_vcs: bool = False,
) -> list[str]:
    """Get list of changed files via git diff or svn status.

    For SVN working copies the *base* parameter is ignored; modified/added/
    deleted files are detected from ``svn status``.  Pass an SVN revision
    range (e.g. ``"r100:HEAD"``) as *base* to compare against a specific
    revision instead.  When *strict* is true, Git discovery failures raise
    instead of being reported as an empty change list.

    Args:
        repo_root: Repository root directory.
        base: Git ref (or SVN revision range) to diff against.
        strict: Raise instead of returning ``[]`` when Git discovery fails.
        timeout: Seconds allowed for each subprocess. ``None`` (default) uses
            the general ``CRG_GIT_TIMEOUT`` budget, which is what build,
            incremental update and watch want; the read-only change-discovery
            chain passes the shorter :func:`discovery_timeout` instead.
        require_vcs: The narrower half of *strict*, for callers whose whole
            answer is "these files changed": it raises
            :class:`~code_review_graph.errors.ChangeDiscoveryError` when the
            VCS binary is missing or times out, but keeps the documented
            fallback for a base ref that simply does not resolve (a repository
            with no commits still has to work). Returning ``[]`` for a VCS that
            could not be run is what let ``detect-changes`` report a clean tree
            it never looked at.

            It is also what makes the short *timeout* above safe to use: a
            budget that is exhausted has to be reported, not rounded down to
            "no changes".
    """
    if timeout is None:
        timeout = _GIT_TIMEOUT
    if detect_vcs(repo_root) == "svn":
        return _get_svn_changed_files(
            repo_root,
            base if _SAFE_SVN_REV.match(base) else None,
            timeout=timeout,
            require_vcs=require_vcs or strict,
        )
    # Git path
    if base.startswith("-") or not _SAFE_GIT_REF.fullmatch(base):
        logger.warning("Invalid git ref rejected: %s", base)
        if strict:
            raise ChangeDiscoveryError(f"invalid git diff base: {base}")
        return []
    try:
        # --name-status (not --name-only): renames/copies must report BOTH
        # paths, or the old path never reaches the purge loop (issue #684).
        result = subprocess.run(
            ["git", "diff", "--name-status", "-z", base, "--"],
            capture_output=True,
            cwd=str(repo_root),
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            if strict:
                raise ChangeDiscoveryError(
                    f"git diff failed while discovering changed files (rc={result.returncode})"
                )
            # Fallback: try diff against empty tree (initial commit)
            result = subprocess.run(
                ["git", "diff", "--name-status", "-z", "--cached"],
                capture_output=True,
                cwd=str(repo_root),
                timeout=timeout,
                stdin=subprocess.DEVNULL,
            )
        if result.returncode != 0:
            logger.warning("git diff failed while discovering changed files")
            return []
        return _decode_name_status_paths(result.stdout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        if strict:
            raise ChangeDiscoveryError("git change discovery failed") from exc
        if require_vcs:
            raise _vcs_unavailable("git", exc, timeout=timeout) from exc
        return []


def _find_content_mismatches(
    repo_root: Path,
    store: "GraphStore",
) -> tuple[list[str], dict[str, str], set[str]]:
    """Find indexed files whose bytes differ from the graph's last parsed hash.

    Also returns the stored spellings of every file read that is not binary,
    so stale-file reconciliation can reuse this pass instead of reading the
    repository a second time.

    Git diffs compare the working tree with a base commit.  If a file is
    changed, incrementally indexed, and then reverted before the next update,
    that diff is empty even though the graph still represents the intermediate
    content.  Comparing indexed hashes with the files on disk catches that
    round trip and also catches a reverted file when another path keeps the
    git diff non-empty.
    """
    mismatched_files: list[str] = []
    current_hashes: dict[str, str] = {}
    text_files: set[str] = set()

    for stored_path, stored_hash in store.get_file_hashes().items():
        path = Path(stored_path)
        if not path.is_absolute():
            path = repo_root / path
        try:
            relative_path = path.relative_to(repo_root).as_posix()
            raw = path.read_bytes()
            current_hash = hashlib.sha256(raw).hexdigest()
            current_hashes[relative_path] = current_hash
        except (OSError, ValueError):
            # Missing files are handled by stale-file reconciliation.  Paths
            # outside the repository cannot be represented as relative update
            # inputs and are left to that reconciliation path as well.
            continue
        if b"\x00" not in raw[:8192]:
            text_files.add(stored_path)
        if current_hash != stored_hash:
            mismatched_files.append(relative_path)

    return mismatched_files, current_hashes, text_files


def _get_svn_changed_files(
    repo_root: Path,
    rev_range: str | None = None,
    *,
    timeout: float | None = None,
    require_vcs: bool = False,
) -> list[str]:
    """Return changed files in an SVN working copy.

    When *rev_range* is given (e.g. ``"r100:HEAD"``), ``svn diff --summarize``
    is used to list files changed between those revisions.  Otherwise
    ``svn status`` reports working-copy modifications.

    *timeout* is the per-subprocess budget; ``None`` uses ``CRG_GIT_TIMEOUT``.

    *require_vcs* has the same meaning as in :func:`get_changed_files`: an
    ``svn`` that cannot be run is raised rather than reported as "nothing
    changed".
    """
    if timeout is None:
        timeout = _GIT_TIMEOUT
    try:
        if rev_range:
            result = subprocess.run(
                ["svn", "diff", "--summarize", "--non-interactive", "-r", rev_range],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                cwd=str(repo_root), timeout=timeout,
                stdin=subprocess.DEVNULL,
            )
            if result.returncode != 0:
                logger.warning("svn diff --summarize failed (rc=%d): %s",
                               result.returncode, result.stderr[:200])
                return []
            files = []
            for line in result.stdout.splitlines():
                # Format: "M       path/to/file"  (first char is status)
                if len(line) >= 2 and line[0] in ("M", "A", "D"):
                    files.append(line[1:].strip())
            return files
        else:
            result = subprocess.run(
                ["svn", "status", "--non-interactive"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                cwd=str(repo_root), timeout=timeout,
                stdin=subprocess.DEVNULL,
            )
            files = []
            for line in result.stdout.splitlines():
                if len(line) < 2:
                    continue
                status_char = line[0]
                # M=modified, A=added, D=deleted, R=replaced, C=conflicted
                if status_char in ("M", "A", "D", "R", "C"):
                    # SVN status: 8 fixed-width columns then the path
                    path = line[8:].strip() if len(line) > 8 else line[1:].strip()
                    files.append(path)
            return files
    except (OSError, subprocess.TimeoutExpired) as exc:
        if require_vcs:
            raise _vcs_unavailable("svn", exc, timeout=timeout) from exc
        return []
    except UnicodeDecodeError:
        return []


def get_staged_and_unstaged(
    repo_root: Path,
    *,
    timeout: float | None = None,
    require_vcs: bool = False,
) -> list[str]:
    """Get all modified files (staged + unstaged + untracked).

    ``--untracked-files=all`` is deliberate and load-bearing, not a default
    nobody chose. It is what makes a brand-new, never-committed directory
    report the files inside it. Git's cheaper ``normal`` mode collapses such a
    directory to a single ``dir/`` record, which is not a path any caller can
    open, so scoping the walk down would delete the first commit of every new
    feature package from every review. See the note in
    :func:`discover_review_changes`.

    Args:
        repo_root: Repository root directory.
        timeout: Seconds allowed for the subprocess. ``None`` (default) uses
            the general ``CRG_GIT_TIMEOUT`` budget; the change-discovery chain
            passes the shorter :func:`discovery_timeout` instead.
        require_vcs: Raise
            :class:`~code_review_graph.errors.ChangeDiscoveryError` when git
            cannot be run or overruns *timeout*, instead of returning ``[]``.
            A caller whose answer is an all-clear must pass this.
    """
    if timeout is None:
        timeout = _GIT_TIMEOUT
    if detect_vcs(repo_root) == "svn":
        return _get_svn_changed_files(
            repo_root, timeout=timeout, require_vcs=require_vcs,
        )
    try:
        result = subprocess.run(
            [
                "git",
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
            ],
            capture_output=True,
            cwd=str(repo_root),
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            logger.warning("git status failed while discovering working-tree files")
            return []
        files: list[str] = []
        records = result.stdout.split(b"\0")
        index = 0
        while index < len(records):
            record = records[index]
            if len(record) > 3:
                status = record[:2]
                files.append(os.fsdecode(record[3:]))
                # With porcelain -z, a rename/copy record stores the
                # destination first and its source in the following record.
                if b"R" in status or b"C" in status:
                    index += 1
            index += 1
        return files
    except (OSError, subprocess.TimeoutExpired) as exc:
        if require_vcs:
            raise _vcs_unavailable("git", exc, timeout=timeout) from exc
        return []


def discover_review_changes(
    repo_root: Path,
    base: str = "HEAD~1",
) -> tuple[list[str], str]:
    """Discover the files under review, on the short discovery budget.

    This is the chain every review-shaped tool and command runs when the
    caller did not name ``changed_files`` itself: resolve the base, diff
    against it, and fall back to the working tree when that diff is empty.

    Two things make it different from calling the three functions directly,
    and both exist because this chain runs inside an MCP tool call that a
    client will abandon at its own request ceiling (#262):

    * every subprocess gets :func:`discovery_timeout` rather than the
      30-second ``CRG_GIT_TIMEOUT`` that build, update and watch need, so the
      worst case for the whole chain is seconds rather than two minutes;
    * every subprocess runs with ``require_vcs=True``, so exhausting that
      budget raises :class:`~code_review_graph.errors.ChangeDiscoveryError`.

    The second is what licenses the first. Shortening a budget whose timeout
    path returns ``[]`` would not have made this chain safer -- it would have
    made #913's false all-clear several times easier to hit, and extended it
    to the base resolution, where a timed-out merge base silently degrades a
    three-dot diff into a two-dot one and scopes the review to the wrong
    commits. Timing out is a failure, and it is now reported as one: callers
    turn the error into ``status: error`` naming
    ``CRG_DISCOVERY_TIMEOUT``, which a user can act on, instead of an
    all-clear they cannot tell from a clean tree.

    Note what this chain deliberately does *not* do: scope down the untracked
    walk. ``git status --untracked-files=normal`` is much cheaper on a large
    tree, and it was tried, but it collapses a wholly-untracked directory to
    one ``dir/`` record -- so the first commit of a new package disappears
    from every review tool with ``status: ok`` and no warning. Being slow is a
    bug; confidently reviewing nothing is a worse one. The budget above bounds
    the walk instead, and reports it when it does.

    Returns:
        ``(changed_files, resolved_base)``. The resolved base is returned
        because callers need the same ref afterwards, for diff hunks and risk
        scoring, and resolving it twice would spend the budget twice.

    Raises:
        ChangeDiscoveryError: git could not be run, or overran the discovery
            budget. Never raised for a repository that simply has no changes.
    """
    budget = discovery_timeout()
    resolved_base = resolve_review_base(
        repo_root, base, timeout=budget, require_vcs=True,
    )
    changed = get_changed_files(
        repo_root, resolved_base, timeout=budget, require_vcs=True,
    )
    if not changed:
        changed = get_staged_and_unstaged(
            repo_root, timeout=budget, require_vcs=True,
        )
    return changed, resolved_base


def get_all_tracked_files(
    repo_root: Path,
    recurse_submodules: bool | None = None,
) -> list[str]:
    """Get all files tracked by git or svn.

    Args:
        repo_root: Repository root directory.
        recurse_submodules: If True, pass ``--recurse-submodules`` to
            ``git ls-files`` so that files inside git submodules are
            included.  When *None* (default), falls back to the
            ``CRG_RECURSE_SUBMODULES`` environment variable.
            (Ignored for SVN working copies.)
    """
    if detect_vcs(repo_root) == "svn":
        return _get_svn_all_tracked_files(repo_root)

    if recurse_submodules is None:
        recurse_submodules = _RECURSE_SUBMODULES

    # -z is required, not a nicety: without it core.quotePath (on by
    # default) makes git C-quote any path holding a non-ASCII or control
    # byte, so `git ls-files` answers `"src/caf\303\251.py"` — quotes,
    # backslashes and all — and every such file is silently dropped from the
    # inventory because that spelling does not exist on disk. With -z the
    # paths arrive raw and NUL-separated, and a newline in a path is no
    # longer a record separator either.
    cmd = ["git", "ls-files", "-z"]
    if recurse_submodules:
        cmd.append("--recurse-submodules")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True, encoding='utf-8', errors='replace',
            cwd=str(repo_root),
            timeout=_GIT_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
        return [f for f in result.stdout.split("\0") if f]
    except (FileNotFoundError, subprocess.TimeoutExpired, UnicodeDecodeError):
        return []

def _get_svn_all_tracked_files(repo_root: Path) -> list[str]:
    """Return SVN-versioned files by walking the working copy.

    Uses ``svn list -R`` to get the server-side file list, falling back to
    a filesystem walk (which is also the fallback in :func:`collect_all_files`).
    """
    try:
        result = subprocess.run(
            ["svn", "list", "--recursive", "--non-interactive"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(repo_root), timeout=60,  # svn list queries the server
            stdin=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            # svn list returns paths relative to the WC URL; directories end with "/"
            files = [
                f.strip()
                for f in result.stdout.splitlines()
                if f.strip() and not f.strip().endswith("/")
            ]
            if files:
                return files
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    # Fallback: let collect_all_files do a filesystem walk
    return []


def collect_all_files(
    repo_root: Path,
    recurse_submodules: bool | None = None,
) -> list[str]:
    """Collect all parseable files in the repo, respecting ignore patterns.

    Args:
        repo_root: Repository root directory.
        recurse_submodules: If True, include files from git submodules.
            When *None*, falls back to ``CRG_RECURSE_SUBMODULES`` env var.
    """
    ignore_patterns = _load_ignore_patterns(repo_root)
    parser = CodeParser(repo_root)
    files = []

    # Prefer git ls-files for tracked files
    tracked = get_all_tracked_files(repo_root, recurse_submodules)
    if tracked:
        candidates = tracked
    else:
        # Fallback: walk directory
        candidates = [str(p.relative_to(repo_root)) for p in repo_root.rglob("*") if p.is_file()]

    for rel_path in candidates:
        if _should_ignore(rel_path, ignore_patterns):
            continue
        # Skip paths that would exceed OS filename limits (macOS: 255 bytes
        # per component, ~1024 total; Windows: 260 total).
        try:
            full_path = repo_root / rel_path
        except (OSError, ValueError):
            logger.debug("Skipping path that cannot be constructed: %s", rel_path)
            continue
        if len(str(full_path)) > 1000 or any(len(p.encode()) > 255 for p in full_path.parts):
            logger.debug("Skipping overlong path: %s", rel_path[:120])
            continue
        if not full_path.is_file():
            continue
        if full_path.is_symlink():
            continue
        if parser.detect_language(full_path) is None:
            continue
        if _is_binary(full_path):
            continue
        files.append(rel_path)

    return files


def _reconcile_stale_files(
    repo_root: Path,
    store: GraphStore,
    current_files: list[str] | None = None,
    *,
    known_text: set[str] | None = None,
) -> list[str]:
    """Remove graph files absent from the current parseable repository inventory.

    ``known_text`` holds stored spellings already read and found not binary in
    this update, so they are not read again just to repeat that check.
    """
    stored_files = set(store.get_all_files())
    current_paths: set[str]
    if current_files is not None:
        current_paths = {
            normalize_file_path(repo_root / file_path) for file_path in current_files
        }
    else:
        ignore_patterns = _load_ignore_patterns(repo_root)
        parser = CodeParser(repo_root)
        current_paths = set()
        for stored_file in stored_files:
            path = Path(stored_file)
            try:
                relative = str(path.relative_to(repo_root))
            except ValueError:
                continue
            if (
                path.is_file()
                and not path.is_symlink()
                and not _should_ignore(relative, ignore_patterns)
                and parser.detect_language(path) is not None
                and (
                    (known_text is not None and stored_file in known_text)
                    or not _is_binary(path)
                )
            ):
                current_paths.add(stored_file)
    stale_files = sorted(stored_files - current_paths)
    if stale_files:
        store.remove_files_permanently(stale_files, stored_paths=True)
    return stale_files


def _assert_graph_matches_root(repo_root: Path, store: GraphStore) -> None:
    """Refuse an incremental reconciliation anchored to a different root.

    Authoritative File nodes identify the root a graph was built with. Every
    marker must be under the requested root: partial overlap can otherwise
    delete valid files in a parent or sibling repository (#909). Orphan-only
    databases have no File markers and retain the purge behavior from #861.
    """
    file_paths = store.get_file_marker_paths()
    if not file_paths:
        return
    prefix = normalize_file_path(repo_root)
    prefix = prefix if prefix.endswith("/") else prefix + "/"
    foreign_paths = [
        normalize_file_path(path) for path in file_paths
        if not normalize_file_path(path).startswith(prefix)
    ]
    if not foreign_paths:
        return
    sample = sorted(foreign_paths)[0]
    raise RuntimeError(
        f"the graph holds {len(foreign_paths)} file(s) such as {sample!r} outside "
        f"{str(repo_root)!r}; it was built with a different "
        "repository root. Rebuild it, or retry with the root it was built "
        "with, instead of reconciling every file away."
    )


#: How many markers the two filesystem probes below look at. They only have
#: to establish which root the graph is anchored to, and every marker in one
#: graph shares that root, so a handful is as conclusive as all of them.
_ROOT_PROBE_LIMIT = 25


def assert_graph_serves_root(repo_root: Path, store: GraphStore) -> None:
    """Refuse to *answer* from a graph built for a different repository.

    ``_assert_graph_matches_root`` guards the write side only: it runs during
    incremental reconciliation, where a wrong root would purge files. Every
    read-only consumer opened the same database without asking, so dropping
    one repository's ``graph.db`` into another repository (a copied file, a
    restored CI cache) served that repository's symbols and absolute paths
    under this repository's name, and exited 0 while doing it.

    Refusing to answer is not destructive the way purging is, so this check
    is deliberately narrower than the write-side one: it fires only on an
    unambiguously foreign graph. Four things are not that, and pass:

    * a graph with no authoritative File markers at all;
    * a graph of repo-relative paths, which carry no root identity;
    * a different spelling of the same root (macOS ``/var`` against
      ``/private/var``, a symlinked checkout) — resolved before comparing;
    * a graph whose files are not on this machine at all, which is stale or
      synthetic rather than another live checkout being served.
    """
    # Materialised, not consumed lazily: this runs on every command, so a
    # store that answers with something other than a list of paths must skip
    # the check rather than take the process down with it.
    markers = [str(path) for path in store.get_file_marker_paths()]
    absolute = [path for path in markers if Path(path).is_absolute()]
    if not absolute:
        return

    prefix = normalize_file_path(_canonical_repo_root(repo_root))
    prefix = prefix if prefix.endswith("/") else prefix + "/"
    if any(normalize_file_path(path).startswith(prefix) for path in absolute):
        return

    probe = absolute[:_ROOT_PROBE_LIMIT]
    if any(
        normalize_file_path(os.path.realpath(path)).startswith(prefix)
        for path in probe
    ):
        return

    elsewhere = next((path for path in probe if Path(path).exists()), None)
    if elsewhere is None:
        logger.debug(
            "Graph at %s holds no file under %s and none on this machine; "
            "treating it as stale rather than as another repository's graph.",
            store.db_path,
            repo_root,
        )
        return

    raise GraphRootMismatchError(
        f"the graph at {store.db_path} was built for a different repository "
        f"root: none of its {len(absolute)} file(s), such as "
        f"{normalize_file_path(elsewhere)}, are under {repo_root}. Run "
        "`code-review-graph build` here, or point --repo at the root it was "
        "built for."
    )


_MAX_DEPENDENT_HOPS = env_int("CRG_DEPENDENT_HOPS", 2)
_MAX_DEPENDENT_FILES = 500


def _single_hop_dependents(store: GraphStore, file_path: str) -> set[str]:
    """Find files that directly depend on *file_path* (single hop)."""
    dependents: set[str] = set()
    edges = store.get_edges_by_target(file_path)
    for e in edges:
        if e.kind == "IMPORTS_FROM":
            dependents.add(e.file_path)

    nodes = store.get_nodes_by_file(file_path)
    for node in nodes:
        for e in store.get_edges_by_target(node.qualified_name):
            if e.kind in ("CALLS", "IMPORTS_FROM", "INHERITS", "IMPLEMENTS"):
                dependents.add(e.file_path)

    dependents.discard(file_path)
    return dependents


class DependentList(list):
    """A ``list[str]`` with a ``.truncated`` flag.

    When :func:`find_dependents` hits ``_MAX_DEPENDENT_FILES`` it truncates
    the result and sets ``truncated = True`` so callers can distinguish a
    complete expansion from a capped one.  See issue #261.

    This is a transparent ``list`` subclass — existing callers that iterate,
    ``len()``, or slice continue to work unchanged; only callers that
    specifically check ``.truncated`` benefit from the signal.
    """

    truncated: bool

    def __init__(self, items: list, *, truncated: bool = False) -> None:
        super().__init__(items)
        self.truncated = truncated


def find_dependents(
    store: GraphStore,
    file_path: str,
    max_hops: int = _MAX_DEPENDENT_HOPS,
) -> DependentList:
    """Find files that import from or depend on the given file.

    Performs up to *max_hops* iterations of expansion (default 2).
    Stops early if the total exceeds 500 files.

    Returns a :class:`DependentList` — a regular ``list[str]`` that also
    carries a ``.truncated`` flag.  When ``truncated is True`` the
    returned list is capped at ``_MAX_DEPENDENT_FILES`` and the full
    set of dependents was not explored.  See issue #261.
    """
    all_dependents: set[str] = set()
    visited: set[str] = {file_path}
    frontier: set[str] = {file_path}
    for _hop in range(max_hops):
        next_frontier: set[str] = set()
        for fp in frontier:
            deps = _single_hop_dependents(store, fp)
            new_deps = deps - visited
            all_dependents.update(new_deps)
            next_frontier.update(new_deps)
        visited.update(next_frontier)
        frontier = next_frontier
        if not frontier:
            break
        if len(all_dependents) > _MAX_DEPENDENT_FILES:
            logger.warning(
                "Dependent expansion capped at %d files for %s",
                len(all_dependents),
                file_path,
            )
            return DependentList(
                list(all_dependents)[:_MAX_DEPENDENT_FILES],
                truncated=True,
            )
    return DependentList(list(all_dependents))


def _parse_single_file(
    args: tuple[str, str],
) -> tuple[str, list, list, str | None, str]:
    """Parse one file in a process- or thread-pool worker.

    Returns ``(rel_path, nodes, edges, error_or_none, file_hash)``.
    Must be a module-level function so ``ProcessPoolExecutor`` can
    serialise it across processes.
    """
    rel_path, repo_root_str = args
    abs_path = Path(repo_root_str) / rel_path
    try:
        raw = abs_path.read_bytes()
        fhash = hashlib.sha256(raw).hexdigest()
        parser = getattr(_PARSE_WORKER_STATE, "parser", None)
        parser_repo_root = getattr(_PARSE_WORKER_STATE, "repo_root", None)
        if parser is None or parser_repo_root != repo_root_str:
            parser = CodeParser(Path(repo_root_str))
            _PARSE_WORKER_STATE.parser = parser
            _PARSE_WORKER_STATE.repo_root = repo_root_str
        nodes, edges = parser.parse_bytes(abs_path, raw)
        return (rel_path, nodes, edges, None, fhash)
    except Exception as e:
        return (rel_path, [], [], str(e), "")


def _canonical_repo_root(repo_root: Path) -> Path:
    """Return one stable identity for a repository root.

    Graph paths are anchored to the root supplied by the caller. Resolving the
    root once prevents equivalent spellings (notably ``.`` and an absolute
    path) from looking like two different repositories during reconciliation.
    """
    return Path(repo_root).expanduser().resolve()


def full_build(
    repo_root: Path,
    store: GraphStore,
    recurse_submodules: bool | None = None,
) -> dict:
    """Full rebuild of the entire graph.

    Args:
        repo_root: Repository root directory.
        store: Graph database store.
        recurse_submodules: If True, include files from git submodules.
            When *None*, falls back to ``CRG_RECURSE_SUBMODULES`` env var.
    """
    repo_root = _canonical_repo_root(repo_root)
    parser = CodeParser(repo_root)
    files = collect_all_files(repo_root, recurse_submodules)
    stale_files = _reconcile_stale_files(repo_root, store, files)

    total_nodes = 0
    total_edges = 0
    errors = []
    cpp_errors: set[str] = set()
    file_count = len(files)

    use_serial = os.environ.get("CRG_SERIAL_PARSE", "") == "1"

    if use_serial or file_count < 8:
        # Serial fallback (for debugging or tiny repos)
        for i, rel_path in enumerate(files, 1):
            full_path = repo_root / rel_path
            parsed: tuple[list, list, str] | None = None
            try:
                source = full_path.read_bytes()
                fhash = hashlib.sha256(source).hexdigest()
                nodes, edges = parser.parse_bytes(full_path, source)
                parsed = (nodes, edges, fhash)
            except (OSError, PermissionError) as e:
                errors.append({"file": rel_path, "error": str(e)})
                if parser.detect_language(full_path) == "cpp":
                    cpp_errors.add(str(rel_path))
            except Exception as e:
                logger.warning("Error parsing %s: %s", rel_path, e)
                errors.append({"file": rel_path, "error": str(e)})
                if parser.detect_language(full_path) == "cpp":
                    cpp_errors.add(str(rel_path))
            if parsed is not None:
                # Deliberately outside the handlers above. A file this loop
                # could not *parse* is reported and keeps no rows; a file it
                # could not *write* is a different thing entirely — the usual
                # cause is another process holding the SQLite write lock — and
                # filing that as a parse error let the build carry on, write
                # the VCS anchor and exit 0 while those files were missing
                # from the graph for good. The build stops instead, so no
                # anchor is written and the next run rebuilds.
                store.store_file_nodes_edges(str(full_path), *parsed)
                total_nodes += len(parsed[0])
                total_edges += len(parsed[1])
            if i % 50 == 0 or i == file_count:
                logger.info("Progress: %d/%d files parsed", i, file_count)
    else:
        # Parallel parsing — store calls remain serial (SQLite single-writer).
        # Executor kind auto-selected: process for normal CLI/automation;
        # thread for MCP stdio to avoid pipe-handle inheritance deadlocks and
        # orphan workers (issues #46, #136, PR #615). Override via
        # CRG_PARSE_EXECUTOR env.
        args_list = [(rel_path, str(repo_root)) for rel_path in files]
        with _make_executor(_MAX_PARSE_WORKERS) as executor:
            for i, (rel_path, nodes, edges, error, fhash) in enumerate(
                executor.map(_parse_single_file, args_list, chunksize=20),
                1,
            ):
                if error:
                    logger.warning("Error parsing %s: %s", rel_path, error)
                    errors.append({"file": rel_path, "error": error})
                    if parser.detect_language(repo_root / rel_path) == "cpp":
                        cpp_errors.add(str(rel_path))
                    continue
                full_path = repo_root / rel_path
                store.store_file_nodes_edges(
                    str(full_path),
                    nodes,
                    edges,
                    fhash,
                )
                total_nodes += len(nodes)
                total_edges += len(edges)
                if i % 200 == 0 or i == file_count:
                    logger.info("Progress: %d/%d files parsed", i, file_count)

    store.set_metadata("last_updated", time.strftime("%Y-%m-%dT%H:%M:%S"))
    store.set_metadata("last_build_type", "full")
    _store_cpp_identity_pending(store, cpp_errors)
    # Storing is over: every file that could be parsed has its rows. Recorded
    # before the anchor and before post-processing, so a process killed from
    # here on leaves a graph that ``postprocess`` can genuinely finish, told
    # apart from one killed mid-parse that only a build can repair.
    advance_to_postprocess_pending(store)
    # Failed files are reported in ``errors`` and simply hold no rows; the
    # anchor still describes the commit the stored files were parsed at.
    _store_vcs_metadata(repo_root, store)
    store.commit()

    python_stats = _run_python_resolver(store)
    rescript_stats = _run_rescript_resolver(store)
    spring_stats = _run_spring_resolver(store)
    spring_event_stats = _run_spring_event_resolver(store)
    temporal_stats = _run_temporal_resolver(store)
    hcl_stats = _run_hcl_resolver(store)
    scoped_stats = _run_scoped_resolver(store)
    _refresh_target_resolution(store)

    return {
        "files_parsed": len(files),
        "stale_files_removed": len(stale_files),
        "total_nodes": total_nodes,
        "total_edges": total_edges,
        "errors": errors,
        "python_resolution": python_stats,
        "rescript_resolution": rescript_stats,
        "spring_resolution": spring_stats,
        "event_resolution": spring_event_stats,
        "temporal_resolution": temporal_stats,
        "hcl_resolution": hcl_stats,
        "scoped_resolution": scoped_stats,
    }


def _relative_update_input(repo_root: Path, path: str) -> str:
    """Return *path* relative to *repo_root* when it is absolute and inside it.

    Update inputs are repo-relative, and the ignore rules are anchored to the
    repository root, so an absolute path handed in by a caller would be matched
    against patterns such as ``/tmp/**`` by its filesystem spelling instead of
    its place in the repository. Paths outside the root are returned unchanged
    and are left to stale-file reconciliation.
    """
    candidate = Path(path)
    if not candidate.is_absolute():
        return path
    try:
        return candidate.relative_to(repo_root).as_posix()
    except ValueError:
        return path


def incremental_update(
    repo_root: Path,
    store: GraphStore,
    base: str = "HEAD~1",
    changed_files: list[str] | None = None,
    reconcile_stale: bool = True,
) -> dict:
    """Incremental update: re-parse changed + dependent files only."""
    repo_root = _canonical_repo_root(repo_root)
    if reconcile_stale:
        _assert_graph_matches_root(repo_root, store)
    parser = CodeParser(repo_root)
    ignore_patterns = _load_ignore_patterns(repo_root)
    vcs = detect_vcs(repo_root)
    stored_git_sha = store.get_metadata("git_head_sha") if vcs == "git" else None
    authoritative_git_sync = (
        changed_files is None
        and bool(stored_git_sha)
        and base == stored_git_sha
    )

    identity_pending = _load_cpp_identity_pending(store)
    if (
        identity_pending is None
        and store.get_metadata(_CPP_IDENTITY_METADATA_KEY) != CPP_IDENTITY_VERSION
        and store.has_nodes_for_language("cpp")
    ):
        logger.info(
            "C++ identity format changed; rebuilding the graph before incremental update",
        )
        rebuilt = full_build(repo_root, store)
        return {
            "files_updated": rebuilt["files_parsed"],
            "total_nodes": rebuilt["total_nodes"],
            "total_edges": rebuilt["total_edges"],
            "changed_files": list(changed_files or []),
            "dependent_files": [],
            "errors": rebuilt["errors"],
            "identity_rebuild": True,
            "python_resolution": rebuilt["python_resolution"],
            "rescript_resolution": rebuilt["rescript_resolution"],
            "spring_resolution": rebuilt["spring_resolution"],
            "event_resolution": rebuilt["event_resolution"],
            "temporal_resolution": rebuilt["temporal_resolution"],
            "hcl_resolution": rebuilt["hcl_resolution"],
        }

    # Determine changed files
    if changed_files is None:
        changed_files = get_changed_files(
            repo_root,
            base,
            strict=authoritative_git_sync,
        )
    content_mismatches: list[str] = []
    current_hashes: dict[str, str] = {}
    known_text: set[str] | None = None
    if reconcile_stale:
        # The content scan reads every indexed file once; the hashes and the
        # binary check it produces are reused below so nothing is read twice.
        # Watch batches disable stale reconciliation to remain proportional to
        # filesystem events; reverted content arrives in those events and is
        # covered by the normal hash check.
        content_mismatches, current_hashes, known_text = _find_content_mismatches(
            repo_root,
            store,
        )
    changed_files = list(
        dict.fromkeys(
            [_relative_update_input(repo_root, p) for p in [*changed_files, *content_mismatches]]
        )
    )
    stale_files = (
        _reconcile_stale_files(repo_root, store, known_text=known_text)
        if reconcile_stale
        else []
    )

    # Find dependent files (files that import from changed files)
    dependent_files: set[str] = set()
    for rel_path in changed_files:
        full_path = normalize_file_path(repo_root / rel_path)
        deps = find_dependents(store, full_path)
        for d in deps:
            # Convert back to relative path if needed
            try:
                dependent_files.add(str(Path(d).relative_to(repo_root)))
            except ValueError:
                dependent_files.add(d)

    # Combine changed + dependent
    all_files = set(changed_files) | dependent_files | (identity_pending or set())
    remaining_identity = set(identity_pending or [])

    total_nodes = 0
    total_edges = 0
    errors = []
    missing_paths: set[str] = set()

    # Separate deleted/unparseable files from files that need re-parsing
    to_parse: list[str] = []
    for rel_path in all_files:
        if _should_ignore(rel_path, ignore_patterns):
            if rel_path in remaining_identity:
                ignored_path = repo_root / rel_path
                if (
                    normalize_file_path(ignored_path) in stale_files
                    or not store.get_nodes_by_file(str(ignored_path))
                ):
                    # Nothing of this file is in the graph (it was removed as
                    # stale, or never parsed), so there is nothing to migrate.
                    remaining_identity.discard(rel_path)
                else:
                    errors.append({
                        "file": rel_path,
                        "error": "Identity migration pending for ignored file",
                    })
            continue
        abs_path = repo_root / rel_path
        if not abs_path.is_file():
            remaining_identity.discard(rel_path)
            if normalize_file_path(abs_path) not in stale_files:
                missing_paths.add(normalize_file_path(abs_path))
            continue
        if parser.detect_language(abs_path) is None:
            continue
        # Quick hash check to skip unchanged files
        normalized_path = normalize_file_path(rel_path)
        fhash = current_hashes.get(normalized_path)
        if fhash is None:
            try:
                raw = abs_path.read_bytes()
                fhash = hashlib.sha256(raw).hexdigest()
            except (OSError, PermissionError):
                fhash = None
        try:
            existing_nodes = store.get_nodes_by_file(str(abs_path))
            if (
                rel_path not in remaining_identity
                and fhash is not None
                and existing_nodes
                and existing_nodes[0].file_hash == fhash
            ):
                continue
        except (OSError, PermissionError):
            pass
        to_parse.append(rel_path)

    # Persist deletions before store_file_nodes_edges() opens its own
    # explicit transaction — avoids nested transaction errors.
    use_serial = os.environ.get("CRG_SERIAL_PARSE", "") == "1"
    parsed_files = 0

    if use_serial or len(to_parse) < 8:
        for rel_path in to_parse:
            abs_path = repo_root / rel_path
            try:
                source = abs_path.read_bytes()
                fhash = hashlib.sha256(source).hexdigest()
                nodes, edges = parser.parse_bytes(abs_path, source)
            except (OSError, PermissionError) as e:
                errors.append({"file": rel_path, "error": str(e)})
                continue
            except Exception as e:
                logger.warning("Error parsing %s: %s", rel_path, e)
                errors.append({"file": rel_path, "error": str(e)})
                continue
            # Same reasoning as the serial loop in full_build: a failed write
            # is not a failed parse. Letting it propagate stops the update
            # before the freshness anchor is advanced, so the next run still
            # sees this file as changed.
            store.store_file_nodes_edges(str(abs_path), nodes, edges, fhash)
            remaining_identity.discard(rel_path)
            parsed_files += 1
            total_nodes += len(nodes)
            total_edges += len(edges)
    else:
        # See full-build comment above for executor kind rationale.
        args_list = [(rel_path, str(repo_root)) for rel_path in to_parse]
        with _make_executor(_MAX_PARSE_WORKERS) as executor:
            for rel_path, nodes, edges, error, fhash in executor.map(
                _parse_single_file,
                args_list,
                chunksize=20,
            ):
                if error:
                    logger.warning("Error parsing %s: %s", rel_path, error)
                    errors.append({"file": rel_path, "error": error})
                    continue
                store.store_file_nodes_edges(
                    str(repo_root / rel_path),
                    nodes,
                    edges,
                    fhash,
                )
                remaining_identity.discard(rel_path)
                parsed_files += 1
                total_nodes += len(nodes)
                total_edges += len(edges)

    removed_files = store.remove_files_permanently(sorted(missing_paths)) if missing_paths else 0
    files_updated = parsed_files + len(stale_files) + removed_files
    if identity_pending is not None and remaining_identity != identity_pending:
        _store_cpp_identity_pending(store, remaining_identity)
        store.commit()

    # Only re-run language-specific resolvers when the relevant files changed.
    python_changed = any(
        path.endswith(".py")
        for path in set(all_files) | set(stale_files) | missing_paths
    )
    python_stats = _run_python_resolver(store) if python_changed else None

    rescript_changed = any(
        rp.endswith((".res", ".resi")) for rp in all_files
    )
    rescript_stats = (
        _run_rescript_resolver(store) if rescript_changed else None
    )

    # Like python_changed above, include stale/missing paths so a deletion
    # that only surfaces through reconciliation still clears derived state
    # (e.g. virtual Spring Event nodes — issue #474).
    spring_changed = any(
        path.endswith(".java")
        for path in set(all_files) | set(stale_files) | missing_paths
    )
    spring_stats = _run_spring_resolver(store) if spring_changed else None
    spring_event_stats = (
        _run_spring_event_resolver(store) if spring_changed else None
    )
    temporal_stats = _run_temporal_resolver(store) if spring_changed else None
    hcl_changed = any(rp.endswith((".tf", ".hcl")) for rp in all_files)
    hcl_stats = _run_hcl_resolver(store) if hcl_changed else None
    scoped_changed = any(rp.endswith((".php", ".rs", ".cs")) for rp in all_files)
    scoped_stats = _run_scoped_resolver(store) if scoped_changed else None
    if files_updated or stale_files:
        _refresh_target_resolution(store)

    # Freshness follows what was stored. A file that failed to parse is
    # reported in ``errors`` and keeps its previous rows; it must not stop the
    # successfully stored files from being recorded as current, otherwise one
    # persistently failing file pins the anchor forever (every later update
    # re-diffs the same range, and a failed full build forces full rebuilds).
    # Explicit batches (watch mode, ``--base``) record HEAD when they stored
    # something; an explicit batch that stored nothing is not evidence that
    # the graph matches HEAD, so it keeps the old anchor.
    freshness_advanced = False
    if files_updated or authoritative_git_sync:
        store.set_metadata("last_updated", time.strftime("%Y-%m-%dT%H:%M:%S"))
        store.set_metadata("last_build_type", "incremental")
        if not remaining_identity:
            store.set_metadata(_CPP_IDENTITY_METADATA_KEY, CPP_IDENTITY_VERSION)
        # Same point as in ``full_build``: the changed files are stored, so a
        # kill from here leaves only derived data to rebuild.
        advance_to_postprocess_pending(store)
        freshness_advanced = _store_vcs_metadata(repo_root, store)
        store.commit()

    return {
        "files_updated": files_updated,
        "total_nodes": total_nodes,
        "total_edges": total_edges,
        "changed_files": list(changed_files),
        "dependent_files": list(dependent_files),
        "stale_files_removed": len(stale_files),
        "errors": errors,
        "freshness_advanced": freshness_advanced,
        "python_resolution": python_stats,
        "rescript_resolution": rescript_stats,
        "spring_resolution": spring_stats,
        "event_resolution": spring_event_stats,
        "temporal_resolution": temporal_stats,
        "hcl_resolution": hcl_stats,
        "scoped_resolution": scoped_stats,
    }


# ---------------------------------------------------------------------------
# Watch mode
# ---------------------------------------------------------------------------


_DEBOUNCE_SECONDS = 1


def _raise_watch_update_errors(result: dict, context: str) -> None:
    """Fail the watch boundary when an incremental update reports errors."""
    errors = result.get("errors") or []
    if not errors:
        return
    details = "; ".join(
        f"{error.get('file', 'unknown')}: {error.get('error', 'unknown error')}"
        for error in errors
    )
    raise RuntimeError(f"{context} reported errors: {details}")


def _raise_watch_postprocess_warnings(result: object) -> None:
    """Treat structured post-processing warnings as a failed watch update."""
    if not isinstance(result, dict):
        return
    warnings = result.get("warnings") or []
    if warnings:
        details = "; ".join(str(warning) for warning in warnings)
        raise RuntimeError(f"post-processing reported warnings: {details}")


# ---------------------------------------------------------------------------
# Watch scheduling and supervision
# ---------------------------------------------------------------------------

# A single recursive watch on the repository root makes the OS register one
# watch per directory in the tree — including every temp directory a build tool
# churns through inside ``target/`` or ``node_modules/``.  Planning the watches
# ourselves keeps ignored trees off the OS watch list entirely.  See: #811.
_WATCH_PLAN_DEPTH = env_int("CRG_WATCH_PLAN_DEPTH", 3)
_MAX_WATCH_SCHEDULES = env_int("CRG_MAX_WATCH_SCHEDULES", 24)
# Splitting a watch costs one watchdog emitter, so it has to buy more than it
# costs: an ignored tree is only worth excluding once it holds this many
# directories.  A lone ``__pycache__`` is not worth a thread; ``target/`` is.
_WATCH_SPLIT_MIN_DIRS = env_int("CRG_WATCH_SPLIT_MIN_DIRS", 4)
_WATCH_HEALTH_INTERVAL = env_float("CRG_WATCH_HEALTH_INTERVAL", 10.0)
_WATCH_STOP_TIMEOUT = 10.0
_WATCH_TICK_SECONDS = 1.0
# A failed recursive promotion is retried no more often than this. Each attempt
# walks the parent subtree and, on Linux, can leave a partly built inotify
# instance behind, so retrying every tick would consume the quota it waits for.
_PROMOTION_RETRY_SECONDS = 30.0
# Upper bound on the refused-watch record.  It is pruned of directories that no
# longer exist on every reconciliation tick, so reaching this cap means a tree
# that genuinely cannot be watched is larger than anyone will read; keeping the
# most recent refusals is more useful than keeping the first ones.
_MAX_UNWATCHED_TRACKED = env_int("CRG_MAX_UNWATCHED_TRACKED", 256)


def _watch_child_dirs(
    directory: Path,
    cache: dict[Path, list[Path]] | None = None,
) -> list[Path]:
    """Return the real (non-symlink) subdirectories of *directory*."""
    if cache is not None and directory in cache:
        return cache[directory]
    children = [directory / name for name, is_dir in _child_directories(directory) if is_dir]
    if cache is not None:
        cache[directory] = children
    return children


def _ignored_tree_weight(directory: Path, cap: int) -> int:
    """Count the directories inside an ignored tree, stopping at *cap*.

    Used to decide whether excluding the tree is worth a separate watch.  The
    count stops as soon as the cap is reached, so probing ``node_modules`` is
    barely more expensive than probing an empty ``__pycache__``.
    """
    if cap <= 0:
        return 0
    total = 1
    queue = [directory]
    while queue and total < cap:
        current = queue.pop()
        for name, is_dir in _child_directories(current):
            if not is_dir:
                continue
            total += 1
            if total >= cap:
                return total
            queue.append(current / name)
    return total


def _plan_watch_subtree(
    directory: Path,
    repo_root: Path,
    ignore_patterns: list[str],
    depth: int,
    max_depth: int,
    cache: dict[Path, list[Path]],
    split_threshold: int = _WATCH_SPLIT_MIN_DIRS,
) -> tuple[bool, list[tuple[Path, bool]]]:
    """Plan the watches covering *directory*.

    Returns ``(clean, plan)``.  ``clean`` means nothing below *directory*
    within *max_depth* is worth excluding, in which case a single recursive
    watch covers it.  Otherwise the directory is watched non-recursively and
    each surviving child is planned in turn, so the ignored subtree is never
    handed to the OS at all.
    """
    ignored_weight = 0
    kept: list[Path] = []
    for child in _watch_child_dirs(directory, cache):
        if _should_ignore(child.relative_to(repo_root).as_posix(), ignore_patterns):
            if ignored_weight < split_threshold:
                ignored_weight += _ignored_tree_weight(child, split_threshold - ignored_weight)
        else:
            kept.append(child)
    clean = ignored_weight < split_threshold
    if depth >= max_depth:
        if clean:
            return True, [(directory, True)]
        return False, [(directory, False)] + [(child, True) for child in kept]
    child_plans: list[tuple[Path, bool]] = []
    for child in kept:
        child_clean, child_plan = _plan_watch_subtree(
            child, repo_root, ignore_patterns, depth + 1, max_depth, cache, split_threshold
        )
        clean = clean and child_clean
        child_plans.extend(child_plan)
    if clean:
        return True, [(directory, True)]
    return False, [(directory, False)] + child_plans


def _plan_watch_paths(
    repo_root: Path,
    ignore_patterns: list[str],
    max_depth: int = _WATCH_PLAN_DEPTH,
    max_schedules: int = _MAX_WATCH_SCHEDULES,
    split_threshold: int = _WATCH_SPLIT_MIN_DIRS,
) -> list[tuple[Path, bool]]:
    """Return the ``(path, recursive)`` watches that cover the repo.

    Deeper plans exclude more ignored trees but cost one watchdog emitter each,
    so an over-budget plan is retried at a shallower depth before falling back
    to the single recursive root watch.
    """
    cache: dict[Path, list[Path]] = {}
    for depth in range(max(1, max_depth), 0, -1):
        _, plan = _plan_watch_subtree(
            repo_root, repo_root, ignore_patterns, 0, depth, cache, split_threshold
        )
        if len(plan) <= max(1, max_schedules):
            return plan
    logger.warning(
        "%s needs more than %d watches to skip its ignored trees; "
        "falling back to one recursive watch (raise CRG_MAX_WATCH_SCHEDULES to split it)",
        repo_root,
        max_schedules,
    )
    return [(repo_root, True)]


def _run_time_boxed(operation: Callable[[], Any], description: str, timeout: float = 10.0) -> None:
    """Run *operation* on a throwaway thread so a wedged watcher cannot hang exit.

    Watchdog's teardown joins its emitter threads, and the whole point of this
    module's health check is that one of those threads may be stuck.  Every
    watchdog thread is a daemon thread, so abandoning the join is safe.
    """

    def _call() -> None:
        try:
            operation()
        except Exception as exc:  # noqa: BLE001 - teardown must not mask the real error
            logger.debug("%s failed: %s", description, exc)

    thread = threading.Thread(target=_call, name="crg-watch-teardown", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        logger.warning(
            "%s did not finish in %.0fs; leaving it to process exit", description, timeout
        )


def _watch_health_path(repo_root: Path) -> Path | None:
    """Where this watcher publishes its health, or None if that is unavailable."""
    try:
        from .daemon import watch_health_path

        return watch_health_path(repo_root)
    except Exception as exc:  # noqa: BLE001 - health reporting is best-effort
        logger.debug("Watcher health reporting disabled: %s", exc)
        return None


def _watch_identity(path: str | Path) -> tuple[int, int, float] | None:
    """Identify a directory by inode, not by name.

    ``rm -rf src && mkdir src`` leaves the path spelled exactly as before while
    the watch on it is dead, so a name is not an identity.  ``st_birthtime``
    joins the tuple where the platform has it (macOS, Windows), which catches
    the recreated directory that happens to reuse an inode.
    """
    try:
        status = os.stat(path)
    except OSError:
        return None
    return (status.st_dev, status.st_ino, float(getattr(status, "st_birthtime", 0.0)))


class _WatchEntry(NamedTuple):
    """One scheduled watch: the watchdog handle and the directory it covers."""

    handle: Any
    identity: tuple[int, int, float] | None


class _WatchSupervisor:
    """Owns the observer's watches and reports whether they still work.

    Three jobs, all deliberately cheap:

    * schedule only the directories that survive the ignore patterns, so the OS
      never registers a watch inside an ignored tree;
    * adopt directories created later underneath a non-recursive watch, and
      notice when a watched directory has been replaced by a new one wearing
      the same name;
    * notice a dead watchdog thread and publish watcher health, so a stalled
      watcher stops looking healthy to ``crg-daemon status``.
    """

    def __init__(
        self,
        observer: Any,
        repo_root: Path,
        ignore_patterns: list[str],
        health_path: Path | None = None,
        max_schedules: int = _MAX_WATCH_SCHEDULES,
    ) -> None:
        self._observer = observer
        # One boundary resolves the path.  ``--repo .`` stays relative all the
        # way down from the CLI, and a relative root would make every
        # ``relative_to`` on an absolute event path raise.
        self._repo_root = Path(os.path.abspath(repo_root))
        self._ignore_patterns = ignore_patterns
        self._health_path = health_path
        self._max_schedules = max(1, max_schedules)
        self._handler: Any = None
        self._watches: dict[str, _WatchEntry] = {}
        self._shallow: set[str] = set()
        self._live_threads: dict[int, threading.Thread] = {}
        self._repaired_roots: set[str] = set()
        self._degraded = False
        self._promotion_failed = False
        # Directories the OS refused to watch (inotify ENOSPC, the watch
        # budget in #811).  Losing coverage quietly is the worst thing a
        # watcher can do, so this record feeds `degraded` and, when it swallows
        # everything, ends the process.  A dict, not a set, because it is
        # bounded by age as well as by existence: refusals are only ever
        # dropped on a successful reschedule or an explicit release, and a
        # directory that was refused and then deleted — a build tree recreated
        # on every run — would otherwise be remembered, and published to the
        # health file, for as long as the daemon lives.  Path -> wall-clock
        # time of the most recent refusal, newest last.
        self._unwatched: dict[str, float] = {}
        self._promotion_retry_at: dict[str, float] = {}
        self._last_health_write = 0.0
        self._last_health_state: tuple[bool, bool, tuple[str, ...]] | None = None
        self._started_at = time.time()
        self._token = f"{os.getpid()}.{uuid.uuid4().hex[:8]}"

    # -- scheduling -----------------------------------------------------

    @property
    def watched_paths(self) -> list[str]:
        return sorted(self._watches)

    @property
    def degraded(self) -> bool:
        """True for coarser coverage, a failed promotion, or a refused watch."""
        return self._degraded or self._promotion_failed or bool(self._unwatched)

    @property
    def unwatched_paths(self) -> list[str]:
        """Directories the OS refused to watch, so they are not covered."""
        return sorted(self._unwatched)

    def attach(self, observer: Any) -> None:
        """Bind the observer, once the initial build has earned one."""
        self._observer = observer

    def schedule_initial(self, handler: Any) -> None:
        """Schedule the planned watches for *handler*."""
        self._handler = handler
        plan = _plan_watch_paths(
            self._repo_root,
            self._ignore_patterns,
            max_schedules=self._max_schedules,
        )
        for path, recursive in plan:
            self._schedule(path, recursive=recursive)
        logger.info(
            "Watching %d path(s) under %s; ignored trees are never registered",
            len(self._watches),
            self._repo_root,
        )

    def _schedule(self, path: Path, *, recursive: bool) -> None:
        key = str(path)
        if key in self._watches:
            return
        try:
            handle = self._observer.schedule(self._handler, key, recursive=recursive)
        except OSError as exc:
            # Recording the loss is the whole point.  Swallowing the OSError
            # here (inotify's ENOSPC when the OS watch budget is exhausted)
            # left the supervisor watching nothing while `report_health`
            # published observer_alive=true, degraded=false and `crg-daemon
            # status` printed "ok" — blind, and claiming otherwise.
            # `_promote_to_recursive` already marked itself degraded on the
            # same failure; this path did not.
            self._note_unwatched(key)
            logger.warning(
                "Could not watch %s: %s — coverage of that directory is lost", key, exc
            )
            return
        self._unwatched.pop(key, None)
        self._watches[key] = _WatchEntry(handle, _watch_identity(key))
        if recursive:
            self._shallow.discard(key)
        else:
            self._shallow.add(key)

    def rewatch_all(self) -> bool:
        """Re-attempt the whole watch plan after a total loss of coverage.

        An exhausted OS watch budget is often transient — the build tool that
        consumed it finishes, or the user raises the limit — so a watcher that
        ended up watching nothing tries once more before giving up.  Returns
        True when at least one watch is live afterwards.
        """
        if self._handler is None:  # pragma: no cover - never scheduled
            return False
        self._promotion_retry_at.clear()
        plan = _plan_watch_paths(
            self._repo_root,
            self._ignore_patterns,
            max_schedules=self._max_schedules,
        )
        for path, recursive in plan:
            self._schedule(path, recursive=recursive)
        self._prune_unwatched()
        if self._watches:
            logger.warning(
                "Re-established %d watch(es) on %s after a total loss of coverage",
                len(self._watches),
                self._repo_root,
            )
        return bool(self._watches)

    def sync_watches(
        self, *, ignore_patterns: list[str] | None = None,
    ) -> tuple[list[str], list[str]]:
        """Reconcile the watches under every non-recursive watch.

        Returns ``(adopted, vanished)`` as absolute paths, so the caller can
        index a directory that appeared and reconcile one that disappeared.

        Directory events cannot be used for this.  macOS drops every directory
        event for a child of a non-recursive watch (``FSEventsEmitter.
        _is_recursive_event``), so a brand-new top-level directory — or one
        recreated by ``rm -rf src && mkdir src`` — would never be noticed and
        would stay unindexed forever.  One ``scandir`` per non-recursive watch
        per tick (typically one or two) is nothing next to the thousands of
        kernel watches this planning saves, and it cannot go blind.
        """
        # Batch processing publishes a fresh list; scheduling remains on this
        # thread so adoption cannot race against liveness or watch promotion.
        previous_patterns = self._ignore_patterns
        if ignore_patterns is not None:
            self._ignore_patterns = ignore_patterns
        adopted: list[str] = []
        vanished: list[str] = []
        self._promotion_failed = False
        for parent in sorted(self._shallow):
            present = {
                name for name, is_dir in _child_directories(Path(parent)) if is_dir
            }
            for child in sorted(self._children_of(parent)):
                if os.path.basename(child) not in present:
                    self._release_directory(child)
                    vanished.append(child)
                elif self._watches[child].identity != _watch_identity(child):
                    # Same name, different directory: the watch on it died with
                    # the old inode.  Release it now so the loop below adopts
                    # the replacement, instead of mistaking it for a corpse.
                    logger.info("Directory %s was replaced; re-watching it", child)
                    self._release_directory(child)
                    vanished.append(child)
            for name in sorted(present):
                if parent not in self._shallow:
                    # A promotion replaced this parent with one recursive
                    # watch, which already covers everything below it.
                    break
                candidate = os.path.join(parent, name)
                if candidate in self._watches:
                    continue
                newly_included = (
                    previous_patterns is not self._ignore_patterns
                    and _should_ignore(
                        Path(candidate).relative_to(self._repo_root).as_posix(),
                        previous_patterns,
                    )
                )
                if self._adopt_directory(candidate, required=newly_included):
                    adopted.append(candidate)
        self._prune_unwatched()
        return adopted, vanished

    def _note_unwatched(self, key: str) -> None:
        """Record a directory the OS refused, newest last, and bound the record."""
        self._unwatched.pop(key, None)
        self._unwatched[key] = time.time()
        while len(self._unwatched) > _MAX_UNWATCHED_TRACKED:
            self._unwatched.pop(next(iter(self._unwatched)))

    def _prune_unwatched(self) -> None:
        """Forget refusals for directories that are no longer there.

        A refused directory never enters ``_watches``, so neither a successful
        reschedule nor ``_release_directory`` ever reaches it once it is
        deleted.  Without this the record only grows: ``degraded`` stays true
        for the daemon's whole lifetime over directories that do not exist,
        and ``crg-daemon status`` keeps reporting a gap nobody can close.
        """
        for path in [p for p in self._unwatched if not os.path.isdir(p)]:
            self._unwatched.pop(path, None)

    def _children_of(self, parent: str) -> list[str]:
        """Watched paths directly underneath *parent*."""
        return [path for path in self._watches if os.path.dirname(path) == parent]

    def _descendants_of(self, parent: str) -> list[str]:
        """Watched paths anywhere underneath *parent*, at any depth."""
        prefix = parent.rstrip(os.sep) + os.sep
        return [path for path in self._watches if path.startswith(prefix)]

    def _adopt_directory(self, candidate: str, *, required: bool = False) -> bool:
        """Watch a directory that appeared under a non-recursive watch.

        Planned the same way startup plans the repository: a module arriving
        from a branch switch must not hand its ``node_modules`` and ``target``
        straight back to the OS, which is the exposure #811 is about.
        """
        try:
            relative = Path(candidate).relative_to(self._repo_root).as_posix()
        except ValueError:
            return False
        if _should_ignore(relative, self._ignore_patterns):
            logger.debug("Not watching ignored directory %s", relative)
            return False
        plan = self._affordable_plan(Path(candidate))
        if plan is None:
            # Promoting the parent — often the repository root — hands every
            # ignored tree under it back to the OS, which is the condition
            # #811 is about.  It is the last resort, never the first.
            promoted = self._promote_to_recursive(os.path.dirname(candidate))
            if required and not promoted:
                raise RuntimeError(f"Cannot watch newly included directory: {candidate}")
            return promoted
        for path, recursive in plan:
            self._schedule(path, recursive=recursive)
            if required and str(path) not in self._watches:
                raise RuntimeError(f"Cannot watch newly included directory: {path}")
        logger.info("Watching new directory %s (%d watch(es))", relative, len(plan))
        return True

    def _affordable_plan(self, directory: Path) -> list[tuple[Path, bool]] | None:
        """The most selective plan for *directory* that fits the budget.

        Mirrors :func:`_plan_watch_paths`: try the deepest split first, then
        shallower ones, then a single recursive watch on the directory itself.
        Only when even one slot is unavailable does the caller fall back to
        promoting the parent.
        """
        cache: dict[Path, list[Path]] = {}
        available = self._max_schedules - len(self._watches)
        if available <= 0:
            return None
        for depth in range(max(1, _WATCH_PLAN_DEPTH), 0, -1):
            _, plan = _plan_watch_subtree(
                directory, self._repo_root, self._ignore_patterns, 0, depth, cache
            )
            if len(plan) <= available:
                return plan
        # One recursive watch still filters every other directory in the repo.
        return [(directory, True)]

    def _promote_to_recursive(self, parent: str) -> bool:
        """Replace the filtered plan only after its recursive watch is live."""
        retry_at = self._promotion_retry_at.get(parent)
        if retry_at is not None and time.monotonic() < retry_at:
            # Still inside the cool-down from the last failure: coverage stays
            # as it is and health keeps reporting the gap.
            self._promotion_failed = True
            return False
        # Watchdog distinguishes handles by both path and recursive flag. Bypass
        # _schedule's path-only dedup so both parent watches can coexist briefly.
        try:
            handle = self._observer.schedule(self._handler, parent, recursive=True)
        except OSError as exc:
            self._promotion_failed = True
            self._promotion_retry_at[parent] = time.monotonic() + _PROMOTION_RETRY_SECONDS
            logger.warning(
                "Could not promote watch on %s; keeping existing watches and retrying "
                "in %.0fs: %s",
                parent,
                _PROMOTION_RETRY_SECONDS,
                exc,
            )
            return False
        self._promotion_retry_at.pop(parent, None)
        for path in [parent, *self._descendants_of(parent)]:
            self._release_directory(path)
        self._watches[parent] = _WatchEntry(handle, _watch_identity(parent))
        self._degraded = True
        logger.warning(
            "Watch budget of %d reached; watching %s recursively instead — ignored "
            "trees under it are watched again. Raise CRG_MAX_WATCH_SCHEDULES to "
            "keep filtering.",
            self._max_schedules,
            parent,
        )
        return True

    def _release_directory(self, path: str) -> None:
        entry = self._watches.pop(path, None)
        self._shallow.discard(path)
        self._repaired_roots.discard(path)
        # A directory we deliberately let go is not a coverage gap.
        self._unwatched.pop(path, None)
        if entry is None:
            return
        # unschedule() joins the emitter thread with no timeout, and a wedged
        # emitter is the very thing this class exists to survive.
        _run_time_boxed(
            lambda: self._observer.unschedule(entry.handle),
            f"unschedule {path}",
            timeout=_WATCH_STOP_TIMEOUT,
        )

    # -- liveness -------------------------------------------------------

    def _watchdog_threads(self) -> list[tuple[threading.Thread, str | None]]:
        """Every thread the observer depends on, with the root it watches.

        The dispatcher, each emitter, and any reader thread an emitter owns
        (inotify keeps its buffer thread there) can die on their own; the
        process survives all three, which is what makes the failure silent.
        """
        threads: list[tuple[threading.Thread, str | None]] = []
        observer = self._observer
        if isinstance(observer, threading.Thread):
            threads.append((observer, None))
        try:
            emitters = list(getattr(observer, "emitters", ()) or ())
        except TypeError:  # a stub or mock observer — nothing to inspect
            return threads
        for emitter in emitters:
            root = getattr(getattr(emitter, "watch", None), "path", None)
            root = root if isinstance(root, str) else None
            if isinstance(emitter, threading.Thread):
                threads.append((emitter, root))
            try:
                members = list(vars(emitter).values())
            except TypeError:
                continue
            threads.extend(
                (member, root)
                for member in members
                if isinstance(member, threading.Thread) and member is not emitter
            )
        return threads

    def check_liveness(self) -> tuple[list[str], list[str]]:
        """Return ``(dead_thread_names, repaired_roots)``.

        A thread that stopped is only a death if the watch it belonged to is
        still the live watch for a directory that is still the same directory.
        Three things are deliberately not deaths:

        * a thread not yet started — an emitter caught between construction
          and start is not a corpse;
        * a thread whose watch we have already released, or whose root is gone.
          Both backends stop an emitter when its own root disappears, so a
          plain ``rm -rf lib/`` would otherwise exit the watcher, and the
          daemon would restart it every 30s forever;
        * a thread whose root has been replaced since we scheduled it.
          ``rm -rf src && mkdir src`` inside one tick leaves the name in place
          but kills the watch, and calling that a death both exits the process
          and loses the recreated directory's contents.

        The remaining case — the watch is current, the directory is the same
        one, and its thread died anyway — is repaired once per root by
        rescheduling it, so an inode the filesystem handed straight back does
        not cost a restart.  A second death of the same root is reported, and
        the caller exits: that is #811's crash, and it must stay loud.
        """
        dead: list[str] = []
        repaired: list[str] = []
        still_present: dict[int, threading.Thread] = {}
        for thread, root in self._watchdog_threads():
            key = id(thread)
            if thread.is_alive():
                still_present[key] = thread
                continue
            # Thread.ident survives termination, so a thread that started
            # and died before our first tick still counts as a death (#891).
            if key not in self._live_threads and thread.ident is None:
                continue
            if root is None:
                dead.append(thread.name)
                continue
            entry = self._watches.get(root)
            if entry is None:
                logger.debug("Watch on %s was already released; not a death", root)
                continue
            if entry.identity != _watch_identity(root):
                logger.info(
                    "Watch root %s is gone or replaced; not a death — sync_watches "
                    "releases the stale watch and adopts the replacement",
                    root,
                )
                continue
            if root in self._repaired_roots:
                dead.append(thread.name)
                continue
            logger.warning(
                "Watch on %s stopped while the directory is still there; "
                "rescheduling it once before giving up",
                root,
            )
            recursive = root not in self._shallow
            self._release_directory(root)  # also clears the repair mark
            self._schedule(Path(root), recursive=recursive)
            if root not in self._watches:
                # Rescheduling failed — ENOSPC from inotify is the very trigger
                # behind #811 — so the directory is now unwatched.  That is a
                # loss of coverage, and the one thing it must not do is pass
                # quietly as a repair.
                logger.error("Could not reschedule the watch on %s", root)
                dead.append(thread.name)
                continue
            self._repaired_roots.add(root)
            repaired.append(root)
        self._live_threads = still_present
        return dead, repaired

    # -- health reporting ------------------------------------------------

    def report_health(
        self,
        *,
        observer_alive: bool,
        last_event_at: float | None = None,
        events_seen: int = 0,
        dead_threads: tuple[str, ...] = (),
        phase: str = "watching",
        force: bool = False,
    ) -> None:
        """Publish watcher health, rate-limited to one write per interval."""
        if self._health_path is None:
            return
        now = time.time()
        state = (observer_alive, self.degraded, tuple(dead_threads))
        if (
            not force
            and state == self._last_health_state
            and now - self._last_health_write < _WATCH_HEALTH_INTERVAL
        ):
            return
        payload = {
            "repo": str(self._repo_root),
            "pid": os.getpid(),
            "started_at": self._started_at,
            "updated_at": now,
            "observer_alive": observer_alive,
            "last_event_at": last_event_at,
            "events_seen": events_seen,
            "watched_paths": len(self._watches),
            "dead_threads": list(dead_threads),
            "degraded": self.degraded,
            "phase": phase,
        }
        try:
            self._health_path.parent.mkdir(parents=True, exist_ok=True)
            # Unique per writer, not per process: two supervisors in one
            # process would otherwise race on the same temp file and hand a
            # reader a torn document.
            temporary = self._health_path.with_name(f"{self._health_path.name}.{self._token}.tmp")
            temporary.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(temporary, self._health_path)
        except OSError as exc:
            logger.debug("Could not write watcher health to %s: %s", self._health_path, exc)
            return
        self._last_health_write = now
        self._last_health_state = state

    def clear_health(self) -> None:
        """Remove the health file on a clean shutdown."""
        if self._health_path is None:
            return
        try:
            self._health_path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - best-effort cleanup
            pass


def _create_watch_handler(
    repo_root: Path,
    store: GraphStore,
    on_files_updated: Optional[Callable],
):
    """Create the debounced watchdog handler for one repository."""
    from watchdog.events import FileSystemEvent, FileSystemEventHandler
    from watchdog.utils.event_debouncer import EventDebouncer

    ignore_patterns = _load_ignore_patterns(repo_root)
    parser = CodeParser(repo_root)
    lexical_root = Path(os.path.abspath(repo_root))
    resolved_root = lexical_root.resolve()
    manifest_names = set().union(*NESTED_OUTPUT_DIR_MARKERS.values())

    class WatchBatchProcessor:
        def __init__(self) -> None:
            self.failure: BaseException | None = None
            self.last_event_at: float | None = None
            self.events_seen: int = 0

        def _relative_path(self, path: str, *, apply_ignores: bool = True) -> str | None:
            candidate = Path(os.path.abspath(path))
            try:
                relative = candidate.relative_to(lexical_root)
            except ValueError:
                return None
            try:
                existing = candidate
                while not existing.exists() and existing != lexical_root:
                    existing = existing.parent
                existing.resolve().relative_to(resolved_root)
                if any(
                    component.is_symlink()
                    for component in [
                        lexical_root / Path(*relative.parts[:index])
                        for index in range(1, len(relative.parts) + 1)
                    ]
                ):
                    return None
            except ValueError:
                return None
            except OSError as exc:
                # A path the OS cannot stat — a component past NAME_MAX, a
                # broken mount — is nothing the graph can hold. Drop the event
                # the way an ignored one is dropped instead of letting the error
                # reach process() and end the watch loop (#897).
                logger.debug("Skipping unstattable watch path %s: %s", path[:120], exc)
                return None
            if apply_ignores and _should_ignore(str(relative), ignore_patterns):
                return None
            return str(relative)

        def _refresh_ignore_patterns(
            self, events: list[FileSystemEvent],
        ) -> tuple[int, set[str]]:
            nonlocal ignore_patterns
            # Only metadata/topology changes invalidate the bounded module scan.
            # The normal source-edit path reads the explicit rules and uses cache.
            invalidate = False
            for event in events:
                for path in (event.src_path, getattr(event, "dest_path", "")):
                    if not path:
                        continue
                    relative = self._relative_path(os.fsdecode(path), apply_ignores=False)
                    if relative is None:
                        continue
                    # An inferred future output directory can instead become a
                    # regular source file. Recheck only that exact reserved path;
                    # explicit exclusions still apply when the rules reload.
                    output_became_file = (
                        not event.is_directory
                        and event.event_type in {"created", "moved"}
                        and Path(relative).name in NESTED_OUTPUT_DIR_MARKERS
                        and f"/{Path(relative).as_posix()}/**" in ignore_patterns
                        and (repo_root / relative).is_file()
                    )
                    if output_became_file or relative == ".code-review-graphignore" or (
                        not _should_ignore(relative, ignore_patterns)
                        and (
                            (
                                event.is_directory
                                and event.event_type in {"created", "deleted", "moved"}
                            )
                            or Path(relative).name in manifest_names
                        )
                    ):
                        invalidate = True
            if invalidate:
                clear_nested_ignore_cache(repo_root)
            refreshed = _load_ignore_patterns(repo_root)
            if set(refreshed) == set(ignore_patterns):
                return 0, set()
            previous = ignore_patterns
            ignore_patterns = refreshed
            _assert_graph_matches_root(repo_root, store)
            newly_included = set()
            if set(previous) - set(refreshed):
                # Relaxed exclusions need an inventory, even without source events.
                # Only previously excluded files join this batch's update inputs.
                newly_included = {
                    path for path in collect_all_files(repo_root)
                    if _should_ignore(path, previous)
                    and self._relative_path(str(repo_root / path)) is not None
                }
            # Purge by stored path only: no repository inventory, stats or reads.
            ignored_files = []
            for stored_path in store.get_all_files():
                try:
                    stored_relative = PurePosixPath(normalize_file_path(stored_path)).relative_to(
                        PurePosixPath(normalize_file_path(repo_root))
                    )
                except ValueError:
                    continue
                if _should_ignore(stored_relative.as_posix(), ignore_patterns):
                    ignored_files.append(stored_path)
            removed = (
                store.remove_files_permanently(ignored_files, stored_paths=True)
                if ignored_files else 0
            )
            return removed, newly_included

        def _stored_descendants(self, relative_directory: str) -> set[str]:
            # Stored file paths use POSIX separators (#774).
            directory = normalize_file_path(repo_root / relative_directory) + "/"
            return {
                str(Path(file_path).relative_to(repo_root))
                for file_path in store.get_all_files()
                if file_path.startswith(directory)
            }

        def _parseable_file(self, relative_path: str) -> bool:
            absolute_path = repo_root / relative_path
            resolved_path = absolute_path.resolve()
            try:
                resolved_path.relative_to(resolved_root)
            except ValueError:
                return False
            return (
                absolute_path.is_file()
                and not absolute_path.is_symlink()
                and parser.detect_language(absolute_path) is not None
                and not _is_binary(absolute_path)
            )

        def _parseable_descendants(self, relative_directory: str) -> set[str]:
            directory = repo_root / relative_directory
            if not directory.is_dir() or directory.is_symlink():
                return set()
            return {
                str(path.relative_to(repo_root))
                for path in directory.rglob("*")
                if self._parseable_file(str(path.relative_to(repo_root)))
                and not _should_ignore(str(path.relative_to(repo_root)), ignore_patterns)
            }

        def _event_paths(self, event: FileSystemEvent) -> set[str]:
            paths: set[str] = set()
            source = self._relative_path(os.fsdecode(event.src_path))
            destination_path = getattr(event, "dest_path", "")
            destination = (
                self._relative_path(os.fsdecode(destination_path))
                if destination_path
                else None
            )
            if event.is_directory:
                if source is not None and event.event_type in {"deleted", "moved"}:
                    paths.update(self._stored_descendants(source))
                if destination is not None:
                    paths.update(self._parseable_descendants(destination))
                elif source is not None and event.event_type == "created":
                    paths.update(self._parseable_descendants(source))
            else:
                if source is not None and event.event_type in {"deleted", "moved"}:
                    paths.add(source)
                elif source is not None and self._parseable_file(source):
                    paths.add(source)
                if destination is not None and self._parseable_file(destination):
                    paths.add(destination)
            return paths

        def process(self, events: list[FileSystemEvent]) -> None:
            # Recorded before the work so a stalled watcher is distinguishable
            # from a watcher whose repository is simply quiet.
            self.last_event_at = time.time()
            self.events_seen += len(events)
            try:
                files_updated, newly_included = self._refresh_ignore_patterns(events)
                changed_files = sorted(
                    newly_included | {path for event in events for path in self._event_paths(event)}
                )
                if changed_files:
                    result = incremental_update(
                        repo_root,
                        store,
                        changed_files=changed_files,
                        reconcile_stale=False,
                    )
                    _raise_watch_update_errors(result, "incremental update")
                    files_updated += result["files_updated"]
                if files_updated > 0 and on_files_updated is not None:
                    postprocess_result = on_files_updated(store)
                    _raise_watch_postprocess_warnings(postprocess_result)
            except BaseException as exc:
                self.failure = exc

        def raise_if_failed(self) -> None:
            if self.failure is not None:
                raise RuntimeError("watch update failed") from self.failure

    processor = WatchBatchProcessor()
    debouncer = EventDebouncer(_DEBOUNCE_SECONDS, processor.process)

    class GraphUpdateHandler(FileSystemEventHandler):
        def dispatch(self, event: FileSystemEvent) -> None:
            if event.event_type not in {"created", "modified", "deleted", "moved"}:
                return
            if event.is_directory and event.event_type == "modified":
                return
            debouncer.handle_event(event)

        def start(self) -> None:
            debouncer.start()

        def stop(self) -> None:
            debouncer.stop()
            debouncer.join()

        def process(self, events: list[FileSystemEvent]) -> None:
            processor.process(events)

        def raise_if_failed(self) -> None:
            processor.raise_if_failed()

        @property
        def ignore_patterns(self) -> list[str]:
            # Lists are replaced, never mutated, by the batch processor. The
            # supervisor can therefore adopt one consistent snapshot per tick.
            return ignore_patterns

        @property
        def last_event_at(self) -> float | None:
            return processor.last_event_at

        @property
        def events_seen(self) -> int:
            return processor.events_seen

    return GraphUpdateHandler()


def _sync_watch_tree(supervisor: _WatchSupervisor, handler: Any) -> None:
    """Reconcile watches, then bring the graph in line with what changed.

    A directory adopted this tick may already hold files, and one that
    vanished may still have nodes in the graph — on macOS neither produces a
    single event, so the sync has to do the bookkeeping itself.  The work goes
    through the debouncer, exactly as a real event would, so indexing a large
    new directory never blocks the loop that publishes the heartbeat.
    """
    from watchdog.events import DirCreatedEvent, DirDeletedEvent

    adopted, vanished = supervisor.sync_watches(ignore_patterns=handler.ignore_patterns)
    for path in adopted:
        handler.dispatch(DirCreatedEvent(path))
    for path in vanished:
        handler.dispatch(DirDeletedEvent(path))


def _install_sigterm_interrupt() -> Callable[[], None]:
    """Make SIGTERM unwind like Ctrl+C, and return an undo callable.

    ``crg-daemon stop`` terminates its children, so without this the watcher
    dies at 143 and leaves its health file behind, which then reads as a
    stalled watcher forever.  Only the main thread may install handlers.
    """
    def _raise_interrupt(_signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt

    try:
        previous = signal.signal(signal.SIGTERM, _raise_interrupt)
    except (ValueError, OSError, AttributeError):  # not the main thread, or no SIGTERM
        return lambda: None

    def _restore() -> None:
        try:
            signal.signal(signal.SIGTERM, previous)
        except (ValueError, OSError):  # pragma: no cover - shutdown race
            pass

    return _restore


def watch(
    repo_root: Path,
    store: GraphStore,
    on_files_updated: Optional[Callable] = None,
    stop_event: threading.Event | None = None,
) -> None:
    """Watch for file changes and auto-update the graph.

    Uses a one-second debounce to batch rapid-fire saves into a single update.

    Ignored trees are never handed to the OS watcher, and every tick checks
    that watchdog's own threads are still running: a dead one raises, so the
    process exits non-zero and the daemon restarts it instead of the graph
    going quietly stale.  See: #811.

    Args:
        repo_root: Repository root to watch.
        store: Graph database to update.
        on_files_updated: Optional callback invoked after each debounced
            batch of file updates completes.  Receives the store as its
            only argument.  Used by the CLI to run post-processing
            (FTS, flows, communities) after watch updates.
        stop_event: Optional event that ends the loop cleanly, for callers
            that run ``watch`` on a thread they need to shut down.

    Raises:
        RuntimeError: if a watch update fails, or if the filesystem observer
            stops running.
    """
    from watchdog.events import DirCreatedEvent, DirDeletedEvent
    from watchdog.observers import Observer

    # One boundary, once: ``--repo .`` reaches here relative, and every path
    # comparison below — stored file paths, watch keys, event paths — assumes
    # they are all spelled the same way.
    repo_root = _canonical_repo_root(repo_root)
    supervisor = _WatchSupervisor(
        None,
        repo_root,
        _load_ignore_patterns(repo_root),
        health_path=_watch_health_path(repo_root),
    )
    # The first build of a large repository takes minutes.  Without a
    # heartbeat up front, ``crg-daemon status`` calls that healthy watcher
    # stalled for the whole build.
    supervisor.report_health(observer_alive=True, phase="initial-build", force=True)

    initial = incremental_update(repo_root, store, changed_files=[])
    _raise_watch_update_errors(initial, "initial watch reconciliation")
    if initial["files_updated"] > 0 and on_files_updated is not None:
        postprocess_result = on_files_updated(store)
        _raise_watch_postprocess_warnings(postprocess_result)
    observer = Observer()
    supervisor.attach(observer)
    handler = _create_watch_handler(repo_root, store, on_files_updated)
    supervisor.schedule_initial(handler)
    handler.start()
    observer.start()
    supervisor.report_health(observer_alive=True, force=True)

    logger.info("Watching %s for changes... (Ctrl+C to stop)", repo_root)
    restore_sigterm = _install_sigterm_interrupt()
    try:
        import time as _time

        while True:
            if stop_event is not None:
                if stop_event.wait(_WATCH_TICK_SECONDS):
                    break
            else:
                _time.sleep(_WATCH_TICK_SECONDS)
            handler.raise_if_failed()
            _sync_watch_tree(supervisor, handler)
            if not supervisor.watched_paths:
                # Nothing is being watched at all: every schedule was refused,
                # or every watched directory went away. Either way the process
                # would otherwise sit here forever reporting perfect health
                # while the graph silently froze. Try to recover once, then
                # exit loudly so the daemon restarts it.
                if not supervisor.rewatch_all():
                    supervisor.report_health(
                        observer_alive=False,
                        last_event_at=handler.last_event_at,
                        events_seen=handler.events_seen,
                        dead_threads=("no filesystem watches",),
                        force=True,
                    )
                    logger.error(
                        "No filesystem watches could be established on %s (the OS "
                        "watch limit is the usual cause); this watcher is exiting "
                        "rather than reporting health while watching nothing. "
                        "Raise the limit (Linux: fs.inotify.max_user_watches) or "
                        "CRG_MAX_WATCH_SCHEDULES, then restart it.",
                        repo_root,
                    )
                    raise RuntimeError(
                        f"watch observer has no watches on {repo_root}: the OS "
                        "refused every watch (watch limit exhausted)"
                    )
            dead, repaired = supervisor.check_liveness()
            for path in repaired:
                # A rescheduled watch missed whatever happened while it was
                # down, so re-read the directory rather than trust the gap.
                # Both halves are needed: the deletion contributes the stored
                # descendants, without which a file removed during the outage
                # keeps its rows, and the creation contributes what is on disk
                # now.  Watch batches run with reconcile_stale=False, so
                # nothing else would ever catch the stale side.
                handler.dispatch(DirDeletedEvent(path))
                handler.dispatch(DirCreatedEvent(path))
            if dead:
                names = ", ".join(dead)
                supervisor.report_health(
                    observer_alive=False,
                    last_event_at=handler.last_event_at,
                    events_seen=handler.events_seen,
                    dead_threads=tuple(dead),
                    force=True,
                )
                logger.error(
                    "Filesystem watcher thread(s) died (%s); %s would stop updating "
                    "silently, so this watcher is exiting for the daemon to restart it",
                    names,
                    repo_root,
                )
                raise RuntimeError(f"watch observer stopped: dead thread(s) {names}")
            supervisor.report_health(
                observer_alive=True,
                last_event_at=handler.last_event_at,
                events_seen=handler.events_seen,
            )
        supervisor.clear_health()
    except KeyboardInterrupt:
        supervisor.clear_health()
        _run_time_boxed(observer.stop, "observer stop")
    finally:
        restore_sigterm()
        _run_time_boxed(observer.stop, "observer stop")
        observer.join(timeout=_WATCH_STOP_TIMEOUT)
        handler.stop()
    logger.info("Watch stopped.")


def start_watch_thread(
    repo_root: Path,
    store: GraphStore,
    daemon: bool = True,
) -> threading.Thread | None:
    """Start watch mode in a background thread.

    Returns the started thread, or None if watchdog is unavailable.
    """
    try:
        import watchdog  # noqa: F401
    except ImportError:
        logger.warning("watchdog not installed; auto-watch disabled")
        return None

    def _run() -> None:
        # A thread cannot take the process down, so the one thing it must not
        # do is die quietly: the server would keep serving a frozen graph.
        try:
            watch(repo_root, store)
        except RuntimeError as exc:
            logger.error("Auto-watch for %s stopped: %s", repo_root, exc)

    thread = threading.Thread(
        target=_run,
        daemon=daemon,
        name="crg-watch",
    )
    thread.start()
    logger.info("Auto-watch started for %s", repo_root)
    return thread
