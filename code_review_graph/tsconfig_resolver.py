"""TypeScript tsconfig.json path alias resolver.

Resolves TypeScript path aliases (e.g., ``@/ -> src/``) declared in
``compilerOptions.paths`` so that ``IMPORTS_FROM`` edges can point to
real file paths instead of raw alias strings.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Extensions probed when resolving an alias target
_PROBE_EXTENSIONS = [".ts", ".tsx", ".js", ".jsx", ".vue"]

# Tsconfig filenames to look for when walking up the directory tree
_TSCONFIG_NAMES = ["tsconfig.json", "tsconfig.app.json"]


class TsconfigResolver:
    """Resolves TypeScript path aliases (e.g., @/ -> src/) using tsconfig.json."""

    def __init__(self) -> None:
        self._cache: dict[str, Optional[dict]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resolve_alias(self, import_str: str, file_path: str) -> Optional[str]:
        """Resolve a TS path alias to an absolute file path, or None."""
        try:
            config = self._load_tsconfig_for_file(file_path)
            if config is None:
                return None

            base_url: Optional[str] = config.get("baseUrl")
            paths: dict[str, list[str]] = config.get("paths", {})
            tsconfig_dir: str = config.get("_tsconfig_dir", "")

            if not paths:
                return None

            if base_url:
                base_dir = (Path(tsconfig_dir) / base_url).resolve()
            else:
                base_dir = Path(tsconfig_dir).resolve()

            return self._match_and_probe(import_str, paths, base_dir)
        except (OSError, ValueError, TypeError):
            logger.debug(
                "TsconfigResolver: unexpected error for %s", file_path, exc_info=True,
            )
            return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_tsconfig_for_file(self, file_path: str) -> Optional[dict]:
        """Find and load tsconfig.json for the given file."""
        start_dir = Path(file_path).parent.resolve()
        current = start_dir
        visited: list[str] = []

        while True:
            dir_str = str(current)
            if dir_str in self._cache:
                result = self._cache[dir_str]
                for visited_dir in visited:
                    self._cache[visited_dir] = result
                return result

            visited.append(dir_str)

            for name in _TSCONFIG_NAMES:
                candidate = current / name
                if candidate.is_file():
                    config = self._parse_tsconfig(candidate)
                    config["_tsconfig_dir"] = dir_str
                    for visited_dir in visited:
                        self._cache[visited_dir] = config
                    return config

            parent = current.parent
            if parent == current:
                for visited_dir in visited:
                    self._cache[visited_dir] = None
                return None
            current = parent

    def _parse_tsconfig(self, tsconfig_path: Path) -> dict:
        """Parse a tsconfig.json file (supports JSONC comments)."""
        seen: set[str] = set()
        return self._resolve_extends(tsconfig_path, seen)

    def _resolve_extends(self, tsconfig_path: Path, seen: set[str]) -> dict:
        """Recursively resolve the tsconfig extends chain."""
        canonical = str(tsconfig_path.resolve())
        if canonical in seen:
            logger.debug("TsconfigResolver: cycle detected at %s", canonical)
            return {}
        seen = seen | {canonical}

        try:
            raw = tsconfig_path.read_text(encoding="utf-8")
        except OSError:
            logger.debug("TsconfigResolver: cannot read %s", tsconfig_path)
            return {}

        stripped = self._strip_jsonc_comments(raw)
        try:
            data: dict = json.loads(stripped)
        except json.JSONDecodeError:
            logger.debug("TsconfigResolver: invalid JSON in %s", tsconfig_path)
            return {}

        result: dict = {}

        extends: Optional[str] = data.get("extends")
        if extends and isinstance(extends, str) and extends.startswith("."):
            parent_path = (tsconfig_path.parent / extends).resolve()
            if not parent_path.suffix:
                parent_path = parent_path.with_suffix(".json")
            if parent_path.is_file():
                parent_config = self._resolve_extends(parent_path, seen)
                parent_opts = parent_config.get("compilerOptions", {})
                result.setdefault("compilerOptions", {}).update(parent_opts)

        child_opts: dict = data.get("compilerOptions", {})
        result.setdefault("compilerOptions", {}).update(child_opts)

        compiler_options = result.get("compilerOptions", {})
        if "baseUrl" in compiler_options:
            result["baseUrl"] = compiler_options["baseUrl"]
        if "paths" in compiler_options:
            result["paths"] = compiler_options["paths"]

        return result

    def _strip_jsonc_comments(self, text: str) -> str:
        """Remove // and /* */ comments and trailing commas from JSONC."""
        result: list[str] = []
        i = 0
        n = len(text)

        while i < n:
            ch = text[i]

            if ch == '"':
                result.append(ch)
                i += 1
                while i < n:
                    c = text[i]
                    result.append(c)
                    if c == "\\" and i + 1 < n:
                        i += 1
                        result.append(text[i])
                    elif c == '"':
                        break
                    i += 1
                i += 1
                continue

            if ch == "/" and i + 1 < n and text[i + 1] == "*":
                i += 2
                while i < n - 1:
                    if text[i] == "*" and text[i + 1] == "/":
                        i += 2
                        break
                    i += 1
                else:
                    i = n
                continue

            if ch == "/" and i + 1 < n and text[i + 1] == "/":
                i += 2
                while i < n and text[i] != "\n":
                    i += 1
                continue

            result.append(ch)
            i += 1

        stripped = "".join(result)
        stripped = re.sub(r",\s*([\]}])", r"\1", stripped)
        return stripped

    def _match_and_probe(
        self,
        import_str: str,
        paths: dict[str, list[str]],
        base_dir: Path,
    ) -> Optional[str]:
        """Match import_str against alias patterns and probe the filesystem."""
        def _pattern_specificity(item: tuple[str, list[str]]) -> int:
            pat = item[0]
            return len(pat.partition("*")[0])

        sorted_paths = sorted(paths.items(), key=_pattern_specificity, reverse=True)

        for pattern, replacements in sorted_paths:
            suffix = _match_pattern(pattern, import_str)
            if suffix is None:
                continue

            for replacement in replacements:
                if "*" in replacement:
                    mapped = replacement.replace("*", suffix, 1)
                else:
                    mapped = replacement

                candidate_base = (base_dir / mapped).resolve()
                found = _probe_path(candidate_base)
                if found:
                    return str(found)

        return None


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _match_pattern(pattern: str, import_str: str) -> Optional[str]:
    """Return the wildcard-matched suffix if pattern matches import_str."""
    if "*" not in pattern:
        return "" if import_str == pattern else None

    prefix, _, suffix_pat = pattern.partition("*")
    if not (import_str.startswith(prefix) and import_str.endswith(suffix_pat)):
        return None

    end = len(import_str) - len(suffix_pat) if suffix_pat else len(import_str)
    return import_str[len(prefix):end]


def _probe_path(base: Path) -> Optional[Path]:
    """Probe base and base + extensions for an existing file."""
    if base.is_file():
        return base
    for ext in _PROBE_EXTENSIONS:
        candidate = base.with_suffix(ext) if not base.suffix else Path(str(base) + ext)
        if candidate.is_file():
            return candidate
    if base.is_dir():
        for ext in _PROBE_EXTENSIONS:
            candidate = base / f"index{ext}"
            if candidate.is_file():
                return candidate
    return None
