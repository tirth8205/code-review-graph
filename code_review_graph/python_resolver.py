"""Post-build resolution for repository-local Python imports."""

from __future__ import annotations

import json
import logging
from collections import Counter
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from .graph import GraphStore

logger = logging.getLogger(__name__)

# A one-segment suffix ("exceptions") matches far too many unrelated files to
# be evidence of anything. Two is the shortest suffix that still carries the
# defining directory, and uniqueness is enforced on top of it.
_MIN_SUFFIX_SEGMENTS = 2

# Floor for the attestation count, so a two-file repository can still establish
# a prefix while a single coincidence cannot.
_MIN_PREFIX_EVIDENCE = 2


def learn_synthetic_prefixes(
    modules: dict[str, set[str]], raw_modules: Iterable[str],
) -> set[str]:
    """Infer which leading segments name a package the repository never spells.

    ``modules`` is keyed by every path suffix of every indexed file, so an
    import is found when the package name matches the repository layout. It is
    not when a framework assembles the package at runtime: Odoo serves
    ``odoo.addons.foo.bar`` from ``addons/<any>/foo/bar.py``, and no directory
    named ``odoo`` exists in the repository at all.

    Stripping whatever prefix happens to make a suffix match is not safe -- an
    import of a module that simply is not indexed will find some unrelated file
    that shares its last two segments. So the prefix has to earn it: count how
    many distinct imports each candidate prefix would resolve uniquely, and keep
    only those attested well enough to be a real namespace rather than a
    coincidence. A genuine one is shared by everything the framework loads,
    which puts it orders of magnitude above the noise.
    """
    votes: Counter[str] = Counter()
    for raw_module in raw_modules:
        segments = raw_module.split(".")
        for cut in range(1, len(segments) - _MIN_SUFFIX_SEGMENTS + 1):
            if len(modules.get(".".join(segments[cut:]), ())) == 1:
                votes[".".join(segments[:cut])] += 1
    if not votes:
        return set()
    strongest = max(votes.values())
    threshold = max(_MIN_PREFIX_EVIDENCE, strongest // 4)
    return {prefix for prefix, count in votes.items() if count >= threshold}


def _candidates_behind_prefix(
    modules: dict[str, set[str]], raw_module: str, prefixes: set[str],
) -> set[str]:
    """Look the import up with an attested synthetic prefix removed.

    Only the shortest attested prefix is tried, and its result is returned as
    is: if the remainder names nothing indexed, the import stays unresolved
    rather than falling through to a shorter, less specific suffix.
    """
    segments = raw_module.split(".")
    for cut in range(1, len(segments) - _MIN_SUFFIX_SEGMENTS + 1):
        if ".".join(segments[:cut]) in prefixes:
            return modules.get(".".join(segments[cut:]), set())
    return set()


def _module_of(edge, python_file_set: set[str]) -> tuple[dict, str | None]:
    """Return the edge's metadata and the module name still to be resolved.

    ``None`` for the module means there is nothing to resolve: the target is
    already a path, or a relative import the parser handles on its own.
    """
    try:
        extra = json.loads(edge["extra"] or "{}")
    except (TypeError, json.JSONDecodeError):
        extra = {}
    if not isinstance(extra, dict):
        extra = {}

    raw_module = extra.get("python_module")
    if isinstance(raw_module, str):
        return extra, raw_module

    raw_module = edge["target_qualified"]
    if (
        raw_module in python_file_set
        or raw_module.startswith(".")
        or "/" in raw_module
        or "\\" in raw_module
    ):
        return extra, None
    return extra, raw_module


def resolve_python_imports(store: GraphStore) -> dict[str, int]:
    """Resolve raw Python modules by unique repository-wide path suffix.

    Suffixes are taken on both sides: of the indexed file paths, and -- when the
    full module name is absent from the index -- of the import itself. See
    ``_candidates_by_module_suffix``.
    """
    conn = store._conn  # intentional: bounded post-build maintenance pass
    python_files = [
        row["file_path"]
        for row in conn.execute(
            "SELECT file_path FROM nodes "
            "WHERE kind = 'File' AND language = 'python'"
        ).fetchall()
    ]
    if not python_files:
        return {
            "files_indexed": 0,
            "imports_updated": 0,
            "imports_resolved": 0,
            "imports_ambiguous": 0,
        }

    modules: dict[str, set[str]] = {}
    for file_path in python_files:
        parts = [
            part for part in file_path.replace("\\", "/").split("/") if part
        ]
        if not parts:
            continue
        filename = parts[-1]
        if filename == "__init__.py":
            components = parts[:-1]
        elif filename.endswith(".py"):
            components = [*parts[:-1], filename[:-3]]
        else:
            continue
        for start in range(len(components)):
            modules.setdefault(".".join(components[start:]), set()).add(file_path)

    updates: list[tuple[str, str, int]] = []
    resolved = 0
    ambiguous = 0
    python_file_set = set(python_files)
    edge_rows = conn.execute(
        "SELECT DISTINCT e.id, e.target_qualified, e.extra "
        "FROM edges e JOIN nodes f "
        "ON f.kind = 'File' AND f.file_path = e.file_path "
        "WHERE e.kind = 'IMPORTS_FROM' AND f.language = 'python'"
    ).fetchall()

    # Which prefixes are synthetic is a property of the repository, not of any
    # one import, so it is settled once -- over the imports the layout cannot
    # explain -- before any of them is resolved.
    parsed_edges = []
    for edge in edge_rows:
        extra, raw_module = _module_of(edge, python_file_set)
        if raw_module is not None:
            parsed_edges.append((edge, extra, raw_module))
    synthetic_prefixes = learn_synthetic_prefixes(
        modules,
        {
            raw_module
            for _edge, _extra, raw_module in parsed_edges
            if raw_module not in modules
        },
    )

    for edge, extra, raw_module in parsed_edges:
        candidates = sorted(
            modules.get(raw_module)
            or _candidates_behind_prefix(modules, raw_module, synthetic_prefixes)
        )
        desired_extra = dict(extra)
        desired_extra["python_module"] = raw_module
        if len(candidates) == 1:
            desired_target = candidates[0]
            desired_extra["import_resolution"] = "repository_suffix"
            desired_extra.pop("import_candidates", None)
            desired_extra.pop("import_candidate_count", None)
            desired_extra.pop("import_candidates_truncated", None)
        else:
            desired_target = raw_module
            desired_extra["import_resolution"] = (
                "ambiguous" if candidates else "unresolved"
            )
            desired_extra["import_candidates"] = candidates[:20]
            desired_extra["import_candidate_count"] = len(candidates)
            desired_extra["import_candidates_truncated"] = len(candidates) > 20

        if edge["target_qualified"] == desired_target and extra == desired_extra:
            continue
        updates.append((
            desired_target,
            json.dumps(desired_extra, sort_keys=True),
            edge["id"],
        ))
        if len(candidates) == 1:
            resolved += 1
        elif candidates:
            ambiguous += 1

    conn.executemany(
        "UPDATE edges SET target_qualified = ?, extra = ? WHERE id = ?",
        updates,
    )
    if updates:
        conn.commit()
        store._invalidate_cache()

    result = {
        "files_indexed": len(python_files),
        "imports_updated": len(updates),
        "imports_resolved": resolved,
        "imports_ambiguous": ambiguous,
    }
    logger.info("Python import resolution: %s", result)
    return result
