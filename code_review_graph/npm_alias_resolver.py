"""Resolve npm dependency aliases to local workspace packages.

A package reaches the alias's entry points the same way a workspace member
does: :meth:`WorkspaceResolver.resolve_in_package` reads ``exports``, then
``source``/``module``/``main``, retries build output under ``src/``, and
probes case-exactly (#343).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Callable, Optional

from .workspace_resolver import WorkspaceResolver

_DEPENDENCY_SECTIONS = (
    "dependencies",
    "devDependencies",
    "optionalDependencies",
    "peerDependencies",
)
# Generated dependency trees can contain thousands of manifests and duplicate
# package names. npm aliases are useful to the graph only when they point at
# source that is intentionally part of the repository.
_SKIPPED_DIRECTORIES = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "node_modules",
}


class NpmAliasResolver:
    """Resolve ``alias/subpath`` imports declared with npm aliases.

    npm allows a dependency key to differ from its real package name:
    ``"sharedLib": "npm:@scope/shared@^1.0.0"``. Source imports then use
    ``sharedLib/...``. When the real package is also present as workspace
    source, resolve the import to that source instead of storing an external,
    unqueryable package string.
    """

    def __init__(
        self,
        repo_root: Optional[Path] = None,
        *,
        package_resolver: Optional[WorkspaceResolver] = None,
        path_is_indexed: Optional[Callable[[Path], bool]] = None,
    ) -> None:
        """*package_resolver* finds entry points inside a package; ``CodeParser``
        passes its own so the probes share its case-exact, ``forget``-aware
        file check. *path_is_indexed* says whether a build indexes a path;
        a package the build ignores is never a candidate.
        """
        self._repo_root = Path(repo_root).resolve() if repo_root is not None else None
        self._package_resolver = package_resolver or WorkspaceResolver(self._repo_root)
        self._path_is_indexed = path_is_indexed
        self._manifest_cache: dict[Path, Optional[dict]] = {}
        self._alias_cache: dict[Path, dict[str, str]] = {}
        self._package_roots: Optional[dict[str, list[Path]]] = None

    def governs(self, import_str: str, file_path: str) -> bool:
        """Whether the nearest manifest declares this import's package as an alias.

        Then npm installs the aliased package under that name, so no other
        resolver (a workspace member with the same name, say) may answer.
        """
        if not import_str or import_str.startswith("."):
            return False
        return self._longest_matching_alias(
            import_str, self._aliases_for_file(file_path),
        ) is not None

    def resolve_alias(self, import_str: str, file_path: str) -> Optional[str]:
        """Return a local source file for an npm-aliased import, or ``None``."""
        if not import_str or import_str.startswith("."):
            return None

        aliases = self._aliases_for_file(file_path)
        matching_alias = self._longest_matching_alias(import_str, aliases)
        if matching_alias is None:
            return None

        parsed = _parse_alias_spec(aliases[matching_alias])
        if parsed is None:
            return None
        real_name, version_range = parsed

        package_roots = self._local_package_roots()
        candidates = package_roots.get(real_name, [])
        if len(candidates) != 1:
            # Absent and ambiguous packages must remain external rather than
            # producing a confidently wrong edge to another local package.
            return None

        package_root = candidates[0]
        # npm installs the version the alias asks for. A local copy at another
        # version is not what the importer loads, so it stays external.
        manifest = self._read_manifest(package_root / "package.json")
        if manifest is None:
            return None
        if not version_satisfies(manifest.get("version"), version_range):
            return None
        subpath = import_str[len(matching_alias) + 1:]
        if not _safe_subpath(subpath):
            return None

        resolved = self._package_resolver.resolve_in_package(
            package_root, manifest, "./" + subpath if subpath else ".",
        )
        if resolved is None:
            return None
        found = Path(resolved)

        try:
            package_root_resolved = package_root.resolve()
            found_resolved = found.resolve()
            if package_root_resolved not in found_resolved.parents:
                return None
        except (OSError, ValueError, RuntimeError):
            return None
        return found_resolved.as_posix()

    def _aliases_for_file(self, file_path: str) -> dict[str, str]:
        """Collect npm aliases from the nearest package manifests.

        Node finds a dependency in the nearest ``node_modules`` that has it,
        so the nearest manifest declaring a name decides what it is. An
        ordinary dependency there hides an ``npm:`` alias of the same name
        further up, and the import stays external.
        """
        try:
            start = Path(file_path).parent.resolve()
        except (OSError, ValueError, RuntimeError):
            return {}

        cache_key = start
        if cache_key in self._alias_cache:
            return self._alias_cache[cache_key]

        declared: dict[str, str] = {}
        current = start
        while True:
            manifest_path = current / "package.json"
            manifest = self._read_manifest(manifest_path)
            if manifest is None and manifest_path.is_file():
                # A manifest that cannot be read might declare the name, so
                # nothing further up may be trusted to decide it.
                break
            if manifest is not None:
                for section_name in _DEPENDENCY_SECTIONS:
                    section = manifest.get(section_name)
                    if not isinstance(section, dict):
                        continue
                    for alias, specification in section.items():
                        if not isinstance(alias, str) or not isinstance(specification, str):
                            continue
                        declared.setdefault(alias, specification.strip())

            if self._repo_root is not None and current == self._repo_root:
                break
            parent = current.parent
            if parent == current:
                break
            current = parent

        aliases = {
            alias: specification for alias, specification in declared.items()
            if specification.startswith("npm:")
        }
        self._alias_cache[cache_key] = aliases
        return aliases

    def _local_package_roots(self) -> dict[str, list[Path]]:
        """Index local package names without following generated/symlinked trees."""
        if self._package_roots is not None:
            return self._package_roots

        roots: dict[str, list[Path]] = {}
        scan_root = self._repo_root
        if scan_root is None:
            self._package_roots = roots
            return roots

        for directory, directory_names, file_names in os.walk(
            scan_root, topdown=True, followlinks=False,
        ):
            directory_names[:] = [
                name for name in directory_names
                if name not in _SKIPPED_DIRECTORIES and not name.startswith(".")
            ]
            if "package.json" not in file_names:
                continue
            current = Path(directory)
            # A copy under an ignored tree (``dist/``, ``vendor/``, a
            # ``.code-review-graphignore`` entry) is not a package the graph
            # holds: it would make the real one look ambiguous, or be picked
            # alone and name files no node answers to.
            if self._path_is_indexed is not None and not self._path_is_indexed(
                current / "package.json",
            ):
                continue
            manifest = self._read_manifest(current / "package.json")
            name = manifest.get("name") if manifest is not None else None
            if not isinstance(name, str) or not name:
                continue
            package_names = roots.setdefault(name, [])
            if current not in package_names:
                package_names.append(current)

        self._package_roots = roots
        return roots

    def _read_manifest(self, path: Path) -> Optional[dict]:
        try:
            canonical = path.resolve()
        except (OSError, ValueError, RuntimeError):
            return None
        if canonical in self._manifest_cache:
            return self._manifest_cache[canonical]

        manifest: Optional[dict] = None
        try:
            if path.is_file() and path.stat().st_size <= 1_048_576:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
                if isinstance(data, dict):
                    manifest = data
        except (OSError, UnicodeError, json.JSONDecodeError, RuntimeError):
            manifest = None

        self._manifest_cache[canonical] = manifest
        return manifest

    @staticmethod
    def _longest_matching_alias(
        import_str: str, aliases: dict[str, str],
    ) -> Optional[str]:
        matches = [
            alias for alias in aliases
            if import_str == alias or import_str.startswith(alias + "/")
        ]
        if not matches:
            return None
        return max(matches, key=len)


_PACKAGE_NAME = re.compile(r"^(?:@[^/@\s]+/)?[^/@\s]+$")


def _parse_alias_spec(specification: str) -> Optional[tuple[str, str]]:
    """Split ``npm:<name>@<range>`` into the real package name and the range."""
    value = specification[4:].strip()
    version_separator = value.find("@", 1) if value.startswith("@") else value.find("@")
    if version_separator < 0:
        name, version_range = value, ""
    else:
        name, version_range = value[:version_separator], value[version_separator + 1:]
    if not _PACKAGE_NAME.match(name):
        return None
    return name, version_range.strip()


# ---------------------------------------------------------------------------
# npm version ranges
# ---------------------------------------------------------------------------
#
# A small reading of node-semver ranges: ``||``, comparator sets, ``=`` ``<``
# ``<=`` ``>`` ``>=``, ``~`` ``^``, x-ranges and hyphen ranges, with npm's
# rule that a prerelease only matches a range naming the same version. A
# range it cannot read matches nothing, so the import stays external.

_Version = tuple[int, int, int, tuple[str, ...]]
_Comparator = tuple[str, _Version]

# No range, or the ``latest`` tag: the name alone says which package it is.
_TAG_RANGES = frozenset({"", "latest"})
_STAR_RANGES = frozenset({"*", "x", "X"})
_FULL_VERSION = re.compile(
    r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$"
)
_PARTIAL_VERSION = re.compile(
    r"^v?(\d+|[xX*])(?:\.(\d+|[xX*]))?(?:\.(\d+|[xX*]))?"
    r"(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$"
)
_OPERATOR = re.compile(r"^(<=|>=|<|>|=|~>|~|\^)?(.*)$")
_HYPHEN = re.compile(r"^(\S+)\s+-\s+(\S+)$")
_LOWEST_PRERELEASE = ("0",)


def version_satisfies(version: object, version_range: str) -> bool:
    """Whether a local package *version* satisfies an npm *version_range*."""
    if version_range in _TAG_RANGES:
        return True
    if not isinstance(version, str):
        # A package with no version still matches ``*``; any other range
        # cannot be checked, so it does not.
        return version is None and version_range in _STAR_RANGES
    parsed = _parse_version(version.strip())
    if parsed is None:
        return False
    for comparator_set in version_range.split("||"):
        comparators = _parse_comparator_set(comparator_set)
        if comparators is not None and _set_matches(parsed, comparators):
            return True
    return False


def _parse_version(text: str) -> Optional[_Version]:
    match = _FULL_VERSION.match(text)
    if match is None:
        return None
    prerelease = tuple(match.group(4).split(".")) if match.group(4) else ()
    return int(match.group(1)), int(match.group(2)), int(match.group(3)), prerelease


def _version_key(version: _Version) -> tuple:
    major, minor, patch, prerelease = version
    if not prerelease:
        return major, minor, patch, (1,)
    identifiers = tuple(
        (0, int(part), "") if part.isdigit() else (1, 0, part) for part in prerelease
    )
    return major, minor, patch, (0, identifiers)


def _parse_comparator_set(text: str) -> Optional[list[_Comparator]]:
    """Comparators that must all hold, or ``None`` when the set is unreadable."""
    text = re.sub(r"(<=|>=|<|>|=|~>|~|\^)\s+", r"\1", text.strip())
    if not text:
        return []
    hyphen = _HYPHEN.match(text)
    if hyphen is not None:
        return _hyphen_comparators(hyphen.group(1), hyphen.group(2))
    comparators: list[_Comparator] = []
    for token in text.split():
        parsed = _token_comparators(token)
        if parsed is None:
            return None
        comparators.extend(parsed)
    return comparators


def _partial(text: str) -> Optional[tuple[Optional[int], Optional[int], Optional[int], tuple]]:
    match = _PARTIAL_VERSION.match(text)
    if match is None:
        return None
    numbers: list[Optional[int]] = []
    for group in match.groups()[:3]:
        numbers.append(None if group is None or group in "xX*" else int(group))
    # 1.x.3 is not a range npm reads; everything after an x is an x.
    for index in range(1, 3):
        if numbers[index - 1] is None:
            numbers[index] = None
    prerelease = tuple(match.group(4).split(".")) if match.group(4) else ()
    return numbers[0], numbers[1], numbers[2], prerelease


def _token_comparators(token: str) -> Optional[list[_Comparator]]:
    match = _OPERATOR.match(token)
    if match is None:
        return None
    operator = match.group(1) or "="
    partial = _partial(match.group(2))
    if partial is None:
        return None
    major, minor, patch, prerelease = partial
    nothing: list[_Comparator] = [("<", (0, 0, 0, _LOWEST_PRERELEASE))]

    if major is None:
        return nothing if operator in ("<", ">") else []
    if operator == "^":
        low = (major, minor or 0, patch or 0, prerelease)
        if major > 0 or minor is None:
            high = (major + 1, 0, 0, _LOWEST_PRERELEASE)
        elif minor > 0 or patch is None:
            high = (0, minor + 1, 0, _LOWEST_PRERELEASE)
        else:
            high = (0, 0, patch + 1, _LOWEST_PRERELEASE)
        return [(">=", low), ("<", high)]
    if operator in ("~", "~>") or (operator == "=" and patch is None):
        low = (major, minor or 0, patch or 0, prerelease)
        if minor is None:
            high = (major + 1, 0, 0, _LOWEST_PRERELEASE)
        else:
            high = (major, minor + 1, 0, _LOWEST_PRERELEASE)
        return [(">=", low), ("<", high)]
    if operator == "=":
        return [("=", (major, minor or 0, patch or 0, prerelease))]
    if minor is None or patch is None:
        if operator == ">":
            bump = (major + 1, 0, 0, ()) if minor is None else (major, minor + 1, 0, ())
            return [(">=", bump)]
        if operator == ">=":
            return [(">=", (major, minor or 0, 0, ()))]
        if operator == "<":
            return [("<", (major, minor or 0, 0, _LOWEST_PRERELEASE))]
        bump = (major + 1, 0, 0, ()) if minor is None else (major, minor + 1, 0, ())
        return [("<", bump[:3] + (_LOWEST_PRERELEASE,))]
    return [(operator, (major, minor, patch, prerelease))]


def _hyphen_comparators(low_text: str, high_text: str) -> Optional[list[_Comparator]]:
    low = _partial(low_text)
    high = _partial(high_text)
    if low is None or high is None:
        return None
    comparators: list[_Comparator] = []
    if low[0] is not None:
        comparators.append((">=", (low[0], low[1] or 0, low[2] or 0, low[3])))
    major, minor, patch, prerelease = high
    if major is None:
        return comparators
    if minor is None:
        comparators.append(("<", (major + 1, 0, 0, _LOWEST_PRERELEASE)))
    elif patch is None:
        comparators.append(("<", (major, minor + 1, 0, _LOWEST_PRERELEASE)))
    else:
        comparators.append(("<=", (major, minor, patch, prerelease)))
    return comparators


def _set_matches(version: _Version, comparators: list[_Comparator]) -> bool:
    key = _version_key(version)
    for operator, bound in comparators:
        other = _version_key(bound)
        if operator == "=" and key != other:
            return False
        if operator == "<" and not key < other:
            return False
        if operator == "<=" and not key <= other:
            return False
        if operator == ">" and not key > other:
            return False
        if operator == ">=" and not key >= other:
            return False
    if version[3]:
        # npm: 1.3.0-beta matches >=1.3.0-alpha but never ^1.2.0.
        return any(
            bound[3] and bound[:3] == version[:3] for _operator, bound in comparators
        )
    return True


def _safe_subpath(subpath: str) -> bool:
    if not subpath:
        return True
    if "\\" in subpath or ":" in subpath or any(ord(char) < 32 for char in subpath):
        return False
    return all(part not in ("", ".", "..") for part in subpath.split("/"))

