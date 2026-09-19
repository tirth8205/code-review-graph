"""Resolve npm dependency aliases to local workspace packages."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

_PROBE_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".vue")
_TYPESCRIPT_SOURCE_EXTENSIONS = {
    ".js": (".ts", ".tsx"),
    ".jsx": (".tsx",),
    ".mjs": (".ts",),
    ".cjs": (".ts",),
}
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

    def __init__(self, repo_root: Optional[Path] = None) -> None:
        self._repo_root = Path(repo_root).resolve() if repo_root is not None else None
        self._manifest_cache: dict[Path, Optional[dict]] = {}
        self._alias_cache: dict[Path, dict[str, str]] = {}
        self._package_roots: Optional[dict[str, list[Path]]] = None

    def resolve_alias(self, import_str: str, file_path: str) -> Optional[str]:
        """Return a local source file for an npm-aliased import, or ``None``."""
        if not import_str or import_str.startswith("."):
            return None

        aliases = self._aliases_for_file(file_path)
        matching_alias = self._longest_matching_alias(import_str, aliases)
        if matching_alias is None:
            return None

        real_name = _real_package_name(aliases[matching_alias])
        if real_name is None:
            return None

        package_roots = self._local_package_roots()
        candidates = package_roots.get(real_name, [])
        if len(candidates) != 1:
            # Absent and ambiguous packages must remain external rather than
            # producing a confidently wrong edge to another local package.
            return None

        package_root = candidates[0]
        subpath = import_str[len(matching_alias):].lstrip("/")
        if not _safe_subpath(subpath):
            return None

        found = self._probe_package_path(package_root, subpath)
        if found is None:
            return None

        try:
            package_root_resolved = package_root.resolve()
            found_resolved = found.resolve()
            if package_root_resolved not in found_resolved.parents:
                return None
        except (OSError, ValueError, RuntimeError):
            return None
        return found_resolved.as_posix()

    def _aliases_for_file(self, file_path: str) -> dict[str, str]:
        """Collect npm aliases from the nearest package manifests."""
        try:
            start = Path(file_path).parent.resolve()
        except (OSError, ValueError, RuntimeError):
            return {}

        cache_key = start
        if cache_key in self._alias_cache:
            return self._alias_cache[cache_key]

        aliases: dict[str, str] = {}
        current = start
        while True:
            manifest = self._read_manifest(current / "package.json")
            if manifest is not None:
                for section_name in _DEPENDENCY_SECTIONS:
                    section = manifest.get(section_name)
                    if not isinstance(section, dict):
                        continue
                    for alias, specification in section.items():
                        if not isinstance(alias, str) or not isinstance(specification, str):
                            continue
                        specification = specification.strip()
                        if specification.startswith("npm:"):
                            aliases.setdefault(alias, specification)

            if self._repo_root is not None and current == self._repo_root:
                break
            parent = current.parent
            if parent == current:
                break
            current = parent

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
                data = json.loads(path.read_text(encoding="utf-8"))
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

    @staticmethod
    def _probe_package_path(package_root: Path, subpath: str) -> Optional[Path]:
        if not subpath:
            return _probe_package_entry(package_root)
        return _probe_import_path(package_root / subpath)


def _real_package_name(specification: str) -> Optional[str]:
    """Extract the package name from an npm alias specification."""
    value = specification[4:]
    version_separator = value.find("@", 1) if value.startswith("@") else value.find("@")
    name = value if version_separator < 0 else value[:version_separator]
    if not name or name in ("@", "@/"):
        return None
    return name


def _safe_subpath(subpath: str) -> bool:
    if not subpath:
        return True
    if "\\" in subpath or any(ord(char) < 32 for char in subpath):
        return False
    parts = Path(subpath).parts
    return bool(parts) and all(part not in ("", ".", "..") for part in parts)


def _probe_package_entry(package_root: Path) -> Optional[Path]:
    try:
        manifest_path = package_root / "package.json"
        if manifest_path.stat().st_size > 1_048_576:
            return None
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, RuntimeError):
        return None
    if not isinstance(manifest, dict):
        return None

    for field in ("module", "main"):
        entry = manifest.get(field)
        if not isinstance(entry, str) or not _safe_subpath(entry):
            continue
        found = _probe_import_path(package_root / entry)
        if found is not None:
            return found
    return _probe_import_path(package_root)


def _probe_import_path(base: Path) -> Optional[Path]:
    try:
        if base.is_file():
            return base
        for extension in _PROBE_EXTENSIONS:
            candidate = Path(str(base) + extension)
            if candidate.is_file():
                return candidate
        if base.suffix in _TYPESCRIPT_SOURCE_EXTENSIONS:
            for extension in _TYPESCRIPT_SOURCE_EXTENSIONS[base.suffix]:
                candidate = base.with_suffix(extension)
                if candidate.is_file():
                    return candidate
        if base.is_dir():
            for extension in _PROBE_EXTENSIONS:
                candidate = base / f"index{extension}"
                if candidate.is_file():
                    return candidate
    except (OSError, ValueError, RuntimeError):
        return None
    return None
