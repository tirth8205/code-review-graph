"""npm / yarn / pnpm workspace package resolver.

Resolves bare imports of sibling workspace packages (``@acme/shared``,
``@acme/sdk/browser``) to the package's source files, so that
``IMPORTS_FROM`` edges cross package boundaries (#1030). In a workspace
these names are package-manager links, not ``compilerOptions.paths``
aliases, so :class:`~code_review_graph.tsconfig_resolver.TsconfigResolver`
never sees them.

Entry points come from ``exports`` (subpaths, conditions, fallback arrays
and ``*`` patterns, with Node's key priority and ``null`` blocking), then
``source``/``module``/``main``/``types``. Packages usually point those at
compiled output under ``dist/``, which a checkout does not contain, so a
build-output target is first retried under ``src/`` with source
extensions, then probed as written.

A wrong edge is worse than a missing one, so the resolver stays inside the
repository, probes case-exactly, never claims Node builtin names, and only
maps an unscoped name when the importing package declares it.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Optional

from .tsconfig_resolver import _PROBE_EXTENSIONS

try:
    import yaml as _yaml
except ImportError:  # pragma: no cover - pyyaml is a declared dependency
    _yaml = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# Errors a hostile or broken manifest can raise while being read or matched.
# RecursionError (deeply nested JSON/YAML) and NotImplementedError (a
# non-relative glob) are both RuntimeError subclasses.
_LOOKUP_ERRORS = (OSError, ValueError, TypeError, RuntimeError)

# Top-level package directories that hold compiled output, not source.
_BUILD_OUTPUT_DIRS = frozenset({"dist", "build", "out", "lib"})

# Module-format subdirectories inside a build directory (``dist/types/x.d.ts``)
# that mirror ``src/`` rather than exist under it.
_FORMAT_DIRS = frozenset({"esm", "cjs", "mjs", "es", "umd", "types"})

# Suffixes of compiled files whose source twin has a different extension.
_COMPILED_SUFFIXES = (".d.ts", ".d.mts", ".d.cts", ".js", ".mjs", ".cjs", ".jsx")

# Preferred order when flattening an ``exports`` conditions object. Every
# reachable target is tried; the order decides which wins when several
# resolve. ``types`` is last: declaration output is often laid out
# differently from the runtime entry (``dist/types/``).
_CONDITION_ORDER = (
    "source", "import", "module", "default", "require", "node", "browser", "types",
)
_ENTRY_FIELDS = ("source", "module", "main", "types", "typings")

_DEPENDENCY_FIELDS = (
    "dependencies", "devDependencies", "peerDependencies", "optionalDependencies",
)

# Node resolves these before any package, so a workspace member with the
# same name is never what ``import 'events'`` loads.
_NODE_BUILTINS = frozenset({
    "assert", "async_hooks", "buffer", "child_process", "cluster", "console",
    "constants", "crypto", "dgram", "diagnostics_channel", "dns", "domain",
    "events", "fs", "http", "http2", "https", "inspector", "module", "net",
    "os", "path", "perf_hooks", "process", "punycode", "querystring",
    "readline", "repl", "stream", "string_decoder", "sys", "timers", "tls",
    "trace_events", "tty", "url", "util", "v8", "vm", "wasi", "worker_threads",
    "zlib",
})

_MAX_EXPORTS_DEPTH = 8

# Sentinel: the subpath is explicitly blocked by a ``null`` export.
_BLOCKED: list[str] = []


@dataclass(frozen=True)
class _Package:
    directory: Path
    manifest: dict[str, Any]


@dataclass(frozen=True)
class _Workspace:
    root: Path
    packages: dict[str, _Package] = field(default_factory=dict)


class WorkspaceResolver:
    """Resolves workspace package names to source files.

    One resolver belongs to one ``CodeParser``; parse workers each own a
    parser (see ``incremental._parse_single_file``), so the caches here are
    never shared between threads.

    *repo_root* bounds the upward search and every answer. *file_exists* is
    the case-exact file probe; ``CodeParser`` passes its own so that files
    hidden by ``forget`` stay hidden here too.
    """

    def __init__(
        self,
        repo_root: Optional[Path] = None,
        file_exists: Optional[Callable[[Path], bool]] = None,
    ) -> None:
        self._repo_root = Path(repo_root).resolve() if repo_root is not None else None
        self._listings: dict[str, frozenset[str]] = {}
        self._file_exists = file_exists or self._exists_exact
        self._raw_dir_cache: dict[str, Optional[_Workspace]] = {}
        self._dir_cache: dict[str, Optional[_Workspace]] = {}
        self._declared_cache: dict[str, frozenset[str]] = {}
        self._result_cache: dict[tuple[str, str], Optional[str]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resolve(self, import_str: str, file_path: str) -> Optional[str]:
        """Resolve a workspace package import to an absolute file path, or None."""
        split = _split_specifier(import_str)
        if split is None:
            return None
        name, subpath = split
        if not name.startswith("@") and name in _NODE_BUILTINS:
            return None

        importer_dir = os.path.dirname(file_path)
        workspace = self._workspace_for(importer_dir)
        if workspace is None:
            return None
        package = workspace.packages.get(name)
        if package is None:
            return None
        if not name.startswith("@") and not self._declares(importer_dir, workspace, name):
            return None

        key = (str(workspace.root), import_str)
        if key not in self._result_cache:
            try:
                result = self._resolve_in_package(package, subpath)
            except _LOOKUP_ERRORS:
                logger.warning(
                    "WorkspaceResolver: cannot resolve %s in %s",
                    import_str, package.directory, exc_info=True,
                )
                result = None
            self._result_cache[key] = result
        return self._result_cache[key]

    # ------------------------------------------------------------------
    # Workspace discovery
    # ------------------------------------------------------------------

    def _workspace_for(self, raw_dir: str) -> Optional[_Workspace]:
        """The workspace enclosing *raw_dir*, cached on the unresolved spelling."""
        if raw_dir not in self._raw_dir_cache:
            try:
                start = Path(raw_dir).resolve()
            except _LOOKUP_ERRORS:
                start = None
            self._raw_dir_cache[raw_dir] = (
                self._find_workspace(start) if start is not None else None
            )
        return self._raw_dir_cache[raw_dir]

    def _find_workspace(self, start_dir: Path) -> Optional[_Workspace]:
        """Return the nearest enclosing workspace, walking up from *start_dir*."""
        if self._repo_root is not None and not start_dir.is_relative_to(self._repo_root):
            return None
        current = start_dir
        visited: list[str] = []
        result: Optional[_Workspace] = None

        try:
            while True:
                dir_str = str(current)
                if dir_str in self._dir_cache:
                    result = self._dir_cache[dir_str]
                    break
                visited.append(dir_str)
                patterns = _workspace_patterns(current)
                if patterns is not None:
                    result = _load_workspace(current, patterns)
                    break
                parent = current.parent
                if parent == current or current == self._repo_root:
                    break
                current = parent
        except _LOOKUP_ERRORS:
            logger.warning(
                "WorkspaceResolver: cannot read the workspace around %s",
                start_dir, exc_info=True,
            )
            result = None

        for visited_dir in visited:
            self._dir_cache[visited_dir] = result
        return result

    def _declares(self, importer_dir: str, workspace: _Workspace, name: str) -> bool:
        """Whether the importer's own package depends on (or is) *name*."""
        if importer_dir not in self._declared_cache:
            try:
                declared = _declared_names(Path(importer_dir).resolve(), workspace.root)
            except _LOOKUP_ERRORS:
                logger.warning(
                    "WorkspaceResolver: cannot read the manifest above %s", importer_dir,
                )
                declared = frozenset()
            self._declared_cache[importer_dir] = declared
        return name in self._declared_cache[importer_dir]

    # ------------------------------------------------------------------
    # Entry-point resolution
    # ------------------------------------------------------------------

    def _resolve_in_package(self, package: _Package, subpath: str) -> Optional[str]:
        """Resolve an ``exports`` subpath key inside one package."""
        base = package.directory
        targets = _entry_targets(package.manifest, subpath)
        if targets is _BLOCKED:
            return None

        candidates: list[Path] = []
        for target in targets:
            candidates.extend(_target_candidates(base, target, subpath))
        rest = [] if subpath == "." else subpath[2:].split("/")
        candidates.append(base.joinpath("src", *rest) if rest else base / "src" / "index")
        candidates.append(base.joinpath(*rest) if rest else base / "index")

        package_root = base.resolve()
        for candidate in candidates:
            found = self._probe(candidate)
            if found is None or not self._exact_under(base, found):
                continue
            resolved = found.resolve()
            if not resolved.is_relative_to(package_root):
                continue
            if self._repo_root is not None and not resolved.is_relative_to(self._repo_root):
                continue
            return str(resolved)
        return None

    def _probe(self, base: Path) -> Optional[Path]:
        """Case-exact probe of *base*, *base* + source extensions, then index files."""
        exists = self._file_exists
        if exists(base):
            return base
        for ext in _PROBE_EXTENSIONS:
            candidate = base.with_suffix(ext) if not base.suffix else Path(str(base) + ext)
            if exists(candidate):
                return candidate
        if base.is_dir():
            for ext in _PROBE_EXTENSIONS:
                candidate = base / f"index{ext}"
                if exists(candidate):
                    return candidate
        return None

    def _exact_under(self, base: Path, found: Path) -> bool:
        """Whether every component of *found* below *base* has its on-disk case.

        NTFS and APFS open ``Utils/`` when only ``utils/`` exists, so a probe
        alone would give Windows and macOS edges that Linux never sees.
        """
        try:
            parts = found.relative_to(base).parts
        except ValueError:
            return False
        current = base
        for part in parts:
            if part not in self._listing(current):
                return False
            current = current / part
        return True

    def _exists_exact(self, path: Path) -> bool:
        """``is_file`` that also requires the exact name in the directory listing."""
        return path.name in self._listing(path.parent) and path.is_file()

    def _listing(self, directory: Path) -> frozenset[str]:
        key = str(directory)
        names = self._listings.get(key)
        if names is None:
            try:
                names = frozenset(os.listdir(directory))
            except OSError:
                names = frozenset()
            self._listings[key] = names
        return names


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _split_specifier(spec: str) -> Optional[tuple[str, str]]:
    """Split a bare specifier into (package name, ``exports`` subpath key).

    Follows npm naming: a scoped name is exactly ``@scope/name`` and an
    unscoped name is one segment; the rest is the subpath. Relative paths,
    absolute paths, ``node:`` builtins and ``@/`` style aliases return None.
    """
    if not spec or spec[0] in "./\\" or ":" in spec:
        return None
    parts = spec.split("/")
    if spec.startswith("@"):
        if len(parts) < 2 or len(parts[0]) < 2 or not parts[1]:
            return None
        name, rest = "/".join(parts[:2]), parts[2:]
    else:
        name, rest = parts[0], parts[1:]
    if any(not segment for segment in rest):
        return None
    return name, ("./" + "/".join(rest)) if rest else "."


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except _LOOKUP_ERRORS:
        logger.debug("WorkspaceResolver: cannot read %s", path)
        return None


def _workspace_patterns(directory: Path) -> Optional[list[str]]:
    """Workspace globs declared in *directory*, or None if it is not a root."""
    pnpm_file = directory / "pnpm-workspace.yaml"
    if pnpm_file.is_file():
        data: Any = None
        if _yaml is not None:
            try:
                data = _yaml.safe_load(pnpm_file.read_text(encoding="utf-8-sig"))
            except (*_LOOKUP_ERRORS, _yaml.YAMLError):
                logger.warning("WorkspaceResolver: cannot read %s", pnpm_file)
        packages = data.get("packages") if isinstance(data, dict) else None
        return [p for p in packages if isinstance(p, str)] if isinstance(packages, list) else []

    manifest_path = directory / "package.json"
    if not manifest_path.is_file():
        return None
    manifest = _read_json(manifest_path)
    if not isinstance(manifest, dict):
        return None
    workspaces = manifest.get("workspaces")
    if isinstance(workspaces, dict):  # yarn: {"packages": [...], "nohoist": [...]}
        workspaces = workspaces.get("packages")
    if not isinstance(workspaces, list):
        return None
    return [p for p in workspaces if isinstance(p, str)]


def _clean_pattern(raw: str) -> Optional[str]:
    """A workspace glob relative to the root, or None if it could escape it."""
    pattern = raw.strip().replace("\\", "/")
    if not pattern or pattern.startswith("/") or ":" in pattern:
        return None
    pattern = pattern.removeprefix("./").strip("/")
    if not pattern or ".." in pattern.split("/"):
        return None
    return pattern


def _load_workspace(root: Path, patterns: list[str]) -> _Workspace:
    """Map every member package name to its directory and manifest."""
    include = [p for p in patterns if not p.strip().startswith("!")]
    exclude = [
        cleaned for p in patterns if p.strip().startswith("!")
        if (cleaned := _clean_pattern(p.strip()[1:])) is not None
    ]
    packages: dict[str, _Package] = {}

    for package_dir in _expand_patterns(root, include):
        rel = package_dir.relative_to(root).as_posix()
        if any(fnmatch.fnmatch(rel, pattern) for pattern in exclude):
            continue
        manifest = _read_json(package_dir / "package.json")
        if not isinstance(manifest, dict):
            continue
        name = manifest.get("name")
        if isinstance(name, str) and name and name not in packages:
            packages[name] = _Package(package_dir, manifest)

    return _Workspace(root, packages)


def _expand_patterns(root: Path, patterns: list[str]) -> list[Path]:
    """Directories under *root* matching workspace globs, without node_modules."""
    resolved_root = root.resolve()
    found: dict[str, Path] = {}
    for raw in patterns:
        pattern = _clean_pattern(raw)
        if pattern is None:
            continue
        try:
            if "**" in pattern:
                matches = _walk_globstar(root, pattern)
            else:
                matches = [p.parent for p in root.glob(f"{pattern}/package.json")]
        except _LOOKUP_ERRORS:
            logger.warning("WorkspaceResolver: skipping workspace pattern %r", raw)
            continue
        for directory in matches:
            try:
                inside = directory.resolve().is_relative_to(resolved_root)
                rel_parts = directory.relative_to(root).parts
            except _LOOKUP_ERRORS:
                continue
            if inside and "node_modules" not in rel_parts:
                found.setdefault(str(directory), directory)
    return [found[key] for key in sorted(found)]


def _walk_globstar(root: Path, pattern: str) -> list[Path]:
    """Expand a ``**`` workspace glob with os.walk, pruning node_modules."""
    static: list[str] = []
    for part in PurePosixPath(pattern).parts:
        if any(ch in part for ch in "*?["):
            break
        static.append(part)
    start = root.joinpath(*static)
    if not start.is_dir():
        return []

    matches: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(start):
        dirnames[:] = [d for d in dirnames if d != "node_modules" and not d.startswith(".")]
        if "package.json" not in filenames:
            continue
        directory = Path(dirpath)
        if fnmatch.fnmatch(directory.relative_to(root).as_posix(), pattern):
            matches.append(directory)
    return matches


def _declared_names(importer_dir: Path, workspace_root: Path) -> frozenset[str]:
    """Names the importer's nearest named package.json depends on, plus its own name.

    Manifests without a ``name`` (``{"type": "module"}`` markers) are skipped.
    """
    current = importer_dir
    while True:
        manifest = _read_json(current / "package.json") if (
            current / "package.json"
        ).is_file() else None
        if isinstance(manifest, dict) and isinstance(manifest.get("name"), str):
            names: set[str] = set()
            own = manifest.get("name")
            if isinstance(own, str):
                names.add(own)
            for fld in _DEPENDENCY_FIELDS:
                deps = manifest.get(fld)
                if isinstance(deps, dict):
                    names.update(k for k in deps if isinstance(k, str))
            return frozenset(names)
        if current == workspace_root or current.parent == current:
            return frozenset()
        current = current.parent


def _entry_targets(manifest: dict[str, Any], subpath: str) -> list[str]:
    """Candidate target strings for *subpath*, or ``_BLOCKED``."""
    targets: list[str] = []
    exports = manifest.get("exports")
    if exports is not None:
        exported = _exports_targets(exports, subpath)
        if exported is _BLOCKED:
            return _BLOCKED
        targets.extend(exported)
    if subpath == ".":
        for key in _ENTRY_FIELDS:
            value = manifest.get(key)
            if isinstance(value, str) and value:
                targets.append(value)
    return list(dict.fromkeys(targets))


def _exports_targets(exports: Any, subpath: str) -> list[str]:
    """Targets ``exports`` maps *subpath* to, or ``_BLOCKED`` when Node would refuse it.

    A matched entry with no reachable target (``null``, ``[null]``, ``[]``,
    all conditions ``null``) blocks the subpath; an unmatched subpath falls
    back to conventional probing instead.
    """
    if isinstance(exports, (str, list)):
        return (_flatten_targets(exports) or _BLOCKED) if subpath == "." else []
    if not isinstance(exports, dict):
        return []
    keys = [k for k in exports if isinstance(k, str)]
    if not any(k.startswith(".") for k in keys):  # a bare conditions object for "."
        return (_flatten_targets(exports) or _BLOCKED) if subpath == "." else []
    if subpath in exports:
        return _flatten_targets(exports[subpath]) or _BLOCKED

    # Node's PATTERN_KEY_COMPARE: the longer prefix wins, then the longer key.
    best: Optional[tuple[int, int, str, str, str]] = None
    for key in keys:
        prefix, star, suffix = key.partition("*")
        if not star or "*" in suffix:
            continue
        if (
            subpath.startswith(prefix)
            and subpath.endswith(suffix)
            and len(subpath) > len(prefix) + len(suffix)
        ):
            rank = (len(prefix), len(key), prefix, suffix, key)
            if best is None or rank[:2] > best[:2]:
                best = rank
    if best is None:
        return []
    _, _, prefix, suffix, key = best
    stem = subpath[len(prefix): len(subpath) - len(suffix)]
    return [t.replace("*", stem) for t in _flatten_targets(exports[key])] or _BLOCKED


def _flatten_targets(value: Any, depth: int = 0) -> list[str]:
    """Every target string reachable through conditions and fallback arrays."""
    if depth > _MAX_EXPORTS_DEPTH:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [t for item in value for t in _flatten_targets(item, depth + 1)]
    if isinstance(value, dict):
        rank = {name: i for i, name in enumerate(_CONDITION_ORDER)}
        ordered = sorted(value, key=lambda k: rank.get(k, len(rank)))
        return [t for k in ordered for t in _flatten_targets(value[k], depth + 1)]
    return []  # None blocks a condition; anything else is malformed


def _target_candidates(base: Path, target: str, subpath: str) -> list[Path]:
    """Source-first probe bases for one target string."""
    normalised = target.replace("\\", "/")
    if normalised.startswith("/") or ":" in normalised:
        return []
    parts = [p for p in PurePosixPath(normalised).parts if p not in ("", ".")]
    if not parts or ".." in parts:
        return []

    candidates: list[Path] = []
    if len(parts) > 1 and parts[0] in _BUILD_OUTPUT_DIRS:
        stem = _strip_compiled_suffix(parts[-1])
        middle = parts[1:-1]
        kept = base.joinpath("src", *middle, stem)
        if middle and middle[0] in _FORMAT_DIRS:
            stripped = base.joinpath("src", *middle[1:], stem)
            # dist/types/ mirrors src/ -- unless the subpath itself asks for it.
            if middle[0] in subpath.split("/"):
                candidates.extend([kept, stripped])
            else:
                candidates.extend([stripped, kept])
        else:
            candidates.append(kept)
    candidates.append(base.joinpath(*parts))
    return candidates


def _strip_compiled_suffix(name: str) -> str:
    for suffix in _COMPILED_SUFFIXES:
        if name.endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)]
    return name
