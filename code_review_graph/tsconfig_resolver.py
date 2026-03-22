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
        # Maps tsconfig directory (str) -> parsed compilerOptions dict (or None
        # when no tsconfig was found or parsing failed)
        self._cache: dict[str, Optional[dict]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resolve_alias(self, import_str: str, file_path: str) -> Optional[str]:
        """Resolve a TS path alias to an absolute file path, or None.

        Returns None when:
        - No tsconfig.json is found for the file.
        - The import does not match any configured alias.
        - The resolved candidate file does not exist on disk.
        """
        try:
            config = self._load_tsconfig_for_file(file_path)
            if config is None:
                return None

            base_url: Optional[str] = config.get("baseUrl")
            paths: dict[str, list[str]] = config.get("paths", {})
            tsconfig_dir: str = config.get("_tsconfig_dir", "")

            if not paths:
                return None

            # Resolve baseUrl relative to the tsconfig directory
            if base_url:
                base_dir = (Path(tsconfig_dir) / base_url).resolve()
            else:
                base_dir = Path(tsconfig_dir).resolve()

            return self._match_and_probe(import_str, paths, base_dir)
        except Exception:
            logger.debug("TsconfigResolver: unexpected error for %s", file_path, exc_info=True)
            return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_tsconfig_for_file(self, file_path: str) -> Optional[dict]:
        """Find and load tsconfig.json for the given file.

        Walks up from the file's directory looking for tsconfig.json or
        tsconfig.app.json.  Results are cached by the tsconfig's parent
        directory so that all files in the same project share one entry.
        """
        start_dir = Path(file_path).parent.resolve()
        current = start_dir

        while True:
            dir_str = str(current)
            if dir_str in self._cache:
                return self._cache[dir_str]

            for name in _TSCONFIG_NAMES:
                candidate = current / name
                if candidate.is_file():
                    config = self._parse_tsconfig(candidate)
                    # Store tsconfig directory so callers can resolve baseUrl
                    config["_tsconfig_dir"] = dir_str
                    self._cache[dir_str] = config
                    return config

            parent = current.parent
            if parent == current:
                # Reached filesystem root without finding a tsconfig
                self._cache[dir_str] = None
                return None
            current = parent

    def _parse_tsconfig(self, tsconfig_path: Path) -> dict:
        """Parse a tsconfig.json file (supports JSONC comments).

        Handles ``extends`` recursively so that inherited ``compilerOptions``
        (especially ``paths`` and ``baseUrl``) are merged in.
        """
        seen: set[str] = set()
        return self._resolve_extends(tsconfig_path, seen)

    def _resolve_extends(self, tsconfig_path: Path, seen: set[str]) -> dict:
        """Recursively resolve the tsconfig extends chain.

        Deep-merges ``compilerOptions`` from parent configs, with child
        values taking priority.  Cycle detection is performed via *seen*.
        """
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

        # Resolve parent first so child can override
        extends: Optional[str] = data.get("extends")
        if extends and isinstance(extends, str) and extends.startswith("."):
            parent_path = (tsconfig_path.parent / extends).resolve()
            if not parent_path.suffix:
                parent_path = parent_path.with_suffix(".json")
            if parent_path.is_file():
                parent_config = self._resolve_extends(parent_path, seen)
                parent_opts = parent_config.get("compilerOptions", {})
                result.setdefault("compilerOptions", {}).update(parent_opts)

        # Merge child compilerOptions (child wins)
        child_opts: dict = data.get("compilerOptions", {})
        result.setdefault("compilerOptions", {}).update(child_opts)

        # Flatten compilerOptions to top-level for convenient access
        compiler_options = result.get("compilerOptions", {})
        if "baseUrl" in compiler_options:
            result["baseUrl"] = compiler_options["baseUrl"]
        if "paths" in compiler_options:
            result["paths"] = compiler_options["paths"]

        return result

    def _strip_jsonc_comments(self, text: str) -> str:
        """Remove ``//`` and ``/* */`` comments and trailing commas from JSONC."""
        # Block comments first (may span lines)
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
        # Line comments
        text = re.sub(r"//[^\n]*", "", text)
        # Trailing commas before } or ]
        text = re.sub(r",\s*([\]}])", r"\1", text)
        return text

    def _match_and_probe(
        self,
        import_str: str,
        paths: dict[str, list[str]],
        base_dir: Path,
    ) -> Optional[str]:
        """Match *import_str* against alias patterns and probe the filesystem.

        Patterns like ``@/*`` use a ``*`` wildcard.  The matched suffix is
        substituted into each mapped replacement path and probed with the
        known file extensions.
        """
        for pattern, replacements in paths.items():
            suffix = _match_pattern(pattern, import_str)
            if suffix is None:
                continue  # pattern did not match

            for replacement in replacements:
                # Substitute wildcard — if no *, treat as exact replacement
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
    """Return the wildcard-matched suffix if *pattern* matches *import_str*.

    Examples:
        ``_match_pattern("@/*", "@/hooks/foo")`` -> ``"hooks/foo"``
        ``_match_pattern("@utils", "@utils")``   -> ``""``
        ``_match_pattern("@/*", "react")``       -> ``None``
    """
    if "*" not in pattern:
        return "" if import_str == pattern else None

    prefix, _, suffix_pat = pattern.partition("*")
    if not (import_str.startswith(prefix) and import_str.endswith(suffix_pat)):
        return None

    end = len(import_str) - len(suffix_pat) if suffix_pat else len(import_str)
    return import_str[len(prefix):end]


def _probe_path(base: Path) -> Optional[Path]:
    """Probe *base* and *base* + extensions for an existing file.

    Returns the first existing path, or None.
    """
    # Exact path (may already have extension)
    if base.is_file():
        return base
    # Try appending known extensions
    for ext in _PROBE_EXTENSIONS:
        candidate = base.with_suffix(ext)
        if candidate.is_file():
            return candidate
    # Try index file inside a directory
    if base.is_dir():
        for ext in _PROBE_EXTENSIONS:
            candidate = base / f"index{ext}"
            if candidate.is_file():
                return candidate
    return None
