"""Config-driven custom language support ("bring your own language").

Repos can teach the parser new tree-sitter languages without forking by
dropping a ``languages.toml`` file into ``.code-review-graph/``::

    [languages.erlang]
    extensions = [".erl", ".hrl"]
    grammar = "erlang"                        # tree_sitter_language_pack name
    function_node_types = ["function_clause"]
    class_node_types = ["record_decl"]
    import_node_types = ["import_attribute"]
    call_node_types = ["call"]
    comment = "Erlang via the bundled tree-sitter-erlang grammar"

The loader is deliberately defensive: a broken config must never crash a
build.  Invalid entries are skipped with a ``logger.warning``, and built-in
languages always win — custom entries can neither override built-in file
extensions nor reuse built-in language names.  At most
``MAX_CUSTOM_LANGUAGES`` entries are honoured per repo.

See docs/CUSTOM_LANGUAGES.md for the full schema reference (answers #320).
"""

from __future__ import annotations

import logging
import re
import sys
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import tree_sitter_language_pack as tslp

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        tomllib = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

#: Location of the config file, relative to the repo root.
CONFIG_RELATIVE_PATH = Path(".code-review-graph") / "languages.toml"

#: Hard cap on the number of custom languages loaded from a single config.
MAX_CUSTOM_LANGUAGES = 20

#: Custom language names: short lowercase identifiers.  The name becomes the
#: ``language`` field on every node parsed from matching files.
_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

#: Extensions: a leading dot followed by 1-15 safe characters (".erl",
#: ".cls", ".4gl").  Uppercase input is normalised to lowercase because the
#: parser lowercases file suffixes before lookup.
_EXTENSION_RE = re.compile(r"^\.[a-z0-9_+-]{1,15}$")

#: The four node-type lists recognised in each ``[languages.<name>]`` table.
_NODE_TYPE_KEYS = (
    "function_node_types",
    "class_node_types",
    "import_node_types",
    "call_node_types",
)


@dataclass(frozen=True)
class CustomLanguage:
    """One validated ``[languages.<name>]`` entry from languages.toml."""

    name: str
    grammar: str
    extensions: tuple[str, ...]
    function_node_types: tuple[str, ...] = ()
    class_node_types: tuple[str, ...] = ()
    import_node_types: tuple[str, ...] = ()
    call_node_types: tuple[str, ...] = ()
    comment: str = ""


@dataclass(frozen=True)
class _CacheEntry:
    mtime_ns: int
    size: int
    languages: dict[str, CustomLanguage] = field(default_factory=dict)


# Config files are re-read only when their mtime/size changes.  This matters
# because full builds construct one CodeParser per worker task, and probing
# tree-sitter grammars on every file parse would be wasteful.
_cache_lock = threading.Lock()
_cache: dict[str, _CacheEntry] = {}


def clear_cache() -> None:
    """Drop the loader cache (used by tests)."""
    with _cache_lock:
        _cache.clear()


def load_custom_languages(
    repo_root: Path,
    *,
    builtin_extensions: Mapping[str, str],
    builtin_languages: frozenset[str],
) -> dict[str, CustomLanguage]:
    """Load and validate ``<repo_root>/.code-review-graph/languages.toml``.

    Returns a mapping of custom language name -> :class:`CustomLanguage`.
    Always returns (possibly empty) — a broken config never raises.

    Args:
        repo_root: Repository root containing ``.code-review-graph/``.
        builtin_extensions: The parser's built-in extension map; custom
            entries colliding with these are skipped (built-ins win).
        builtin_languages: All built-in language identifiers; custom names
            shadowing these are skipped.
    """
    config_path = Path(repo_root) / CONFIG_RELATIVE_PATH
    try:
        stat = config_path.stat()
    except OSError:
        return {}  # No config file — the common case; not worth a log line.

    cache_key = str(config_path)
    with _cache_lock:
        cached = _cache.get(cache_key)
        if (
            cached is not None
            and cached.mtime_ns == stat.st_mtime_ns
            and cached.size == stat.st_size
        ):
            return dict(cached.languages)

    languages = _load_uncached(config_path, builtin_extensions, builtin_languages)
    with _cache_lock:
        _cache[cache_key] = _CacheEntry(stat.st_mtime_ns, stat.st_size, dict(languages))
    return languages


def _load_uncached(
    config_path: Path,
    builtin_extensions: Mapping[str, str],
    builtin_languages: frozenset[str],
) -> dict[str, CustomLanguage]:
    if tomllib is None:
        logger.warning(
            "%s found but TOML parsing requires the 'tomli' package on "
            "Python < 3.11 — no custom languages loaded",
            config_path,
        )
        return {}
    try:
        raw = config_path.read_bytes()
    except (OSError, PermissionError) as exc:
        logger.warning("Cannot read %s: %s — no custom languages loaded", config_path, exc)
        return {}
    try:
        data = tomllib.loads(raw.decode("utf-8", errors="replace"))
    except tomllib.TOMLDecodeError as exc:
        logger.warning("Malformed TOML in %s: %s — no custom languages loaded", config_path, exc)
        return {}

    tables = data.get("languages")
    if tables is None:
        return {}
    if not isinstance(tables, dict):
        logger.warning(
            "%s: [languages] must be a table of tables — no custom languages loaded",
            config_path,
        )
        return {}

    result: dict[str, CustomLanguage] = {}
    claimed_extensions: set[str] = set()
    for name, table in tables.items():
        if len(result) >= MAX_CUSTOM_LANGUAGES:
            logger.warning(
                "%s defines more than %d custom languages — ignoring the rest",
                config_path, MAX_CUSTOM_LANGUAGES,
            )
            break
        lang = _validate_entry(
            name, table, builtin_extensions, builtin_languages,
            claimed_extensions, config_path,
        )
        if lang is None:
            continue
        result[lang.name] = lang
        claimed_extensions.update(lang.extensions)
    return result


def _validate_entry(
    name: object,
    table: object,
    builtin_extensions: Mapping[str, str],
    builtin_languages: frozenset[str],
    claimed_extensions: set[str],
    config_path: Path,
) -> Optional[CustomLanguage]:
    """Validate one ``[languages.<name>]`` table; None (after a warning) on
    any problem so a bad entry can never break a build."""
    label = name if isinstance(name, str) else repr(name)
    if not isinstance(table, dict):
        logger.warning("%s: [languages.%s] is not a table — skipping", config_path, label)
        return None
    if not isinstance(name, str) or not _NAME_RE.match(name):
        logger.warning(
            "%s: invalid custom language name %r (expected lowercase "
            "letters/digits/_/-, max 32 chars) — skipping",
            config_path, label,
        )
        return None
    if name in builtin_languages:
        logger.warning(
            "%s: custom language %r shadows a built-in language — skipping "
            "(built-ins cannot be overridden)",
            config_path, name,
        )
        return None

    grammar = table.get("grammar")
    if not isinstance(grammar, str) or not grammar.strip():
        logger.warning(
            "%s: custom language %r needs a non-empty 'grammar' string — skipping",
            config_path, name,
        )
        return None
    grammar = grammar.strip()

    raw_extensions = table.get("extensions")
    if not isinstance(raw_extensions, list) or not raw_extensions:
        logger.warning(
            "%s: custom language %r needs a non-empty 'extensions' list — skipping",
            config_path, name,
        )
        return None
    extensions: list[str] = []
    for ext in raw_extensions:
        normalized = ext.strip().lower() if isinstance(ext, str) else ""
        if not normalized.startswith("."):
            logger.warning(
                "%s: custom language %r: extension %r must start with a dot — skipping",
                config_path, name, ext,
            )
            return None
        if not _EXTENSION_RE.match(normalized):
            logger.warning(
                "%s: custom language %r: extension %r is not a valid file "
                "extension — skipping",
                config_path, name, ext,
            )
            return None
        if normalized in builtin_extensions:
            logger.warning(
                "%s: custom language %r: extension %r is already handled by "
                "the built-in %r parser — skipping (built-ins cannot be overridden)",
                config_path, name, normalized, builtin_extensions[normalized],
            )
            return None
        if normalized in claimed_extensions:
            logger.warning(
                "%s: custom language %r: extension %r is already claimed by "
                "an earlier custom language — skipping",
                config_path, name, normalized,
            )
            return None
        if normalized not in extensions:
            extensions.append(normalized)

    node_types: dict[str, tuple[str, ...]] = {}
    for key in _NODE_TYPE_KEYS:
        value = table.get(key, [])
        if not isinstance(value, list) or any(
            not isinstance(item, str) or not item.strip() for item in value
        ):
            logger.warning(
                "%s: custom language %r: %s must be a list of non-empty "
                "strings — skipping",
                config_path, name, key,
            )
            return None
        node_types[key] = tuple(item.strip() for item in value)
    if not any(node_types.values()):
        logger.warning(
            "%s: custom language %r defines no node types — nothing to "
            "extract, skipping",
            config_path, name,
        )
        return None

    comment = table.get("comment", "")
    if not isinstance(comment, str):
        comment = ""

    # Probe the grammar last (it is the expensive check).  Parser objects
    # themselves are created lazily by CodeParser._get_parser.
    try:
        tslp.get_language(grammar)  # type: ignore[arg-type]
    except (LookupError, ValueError, ImportError, OSError) as exc:
        logger.warning(
            "%s: custom language %r: grammar %r is not available in "
            "tree_sitter_language_pack (%s) — skipping",
            config_path, name, grammar, exc,
        )
        return None

    return CustomLanguage(
        name=name,
        grammar=grammar,
        extensions=tuple(extensions),
        function_node_types=node_types["function_node_types"],
        class_node_types=node_types["class_node_types"],
        import_node_types=node_types["import_node_types"],
        call_node_types=node_types["call_node_types"],
        comment=comment,
    )
