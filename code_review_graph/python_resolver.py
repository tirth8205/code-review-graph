"""Post-build resolution for repository-local Python imports."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .graph import GraphStore

logger = logging.getLogger(__name__)


def resolve_python_imports(store: GraphStore) -> dict[str, int]:
    """Resolve raw Python modules by unique repository-wide path suffix."""
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
    for edge in conn.execute(
        "SELECT DISTINCT e.id, e.target_qualified, e.extra "
        "FROM edges e JOIN nodes f "
        "ON f.kind = 'File' AND f.file_path = e.file_path "
        "WHERE e.kind = 'IMPORTS_FROM' AND f.language = 'python'"
    ).fetchall():
        try:
            extra = json.loads(edge["extra"] or "{}")
        except (TypeError, json.JSONDecodeError):
            extra = {}
        if not isinstance(extra, dict):
            extra = {}

        raw_module = extra.get("python_module")
        if not isinstance(raw_module, str):
            raw_module = edge["target_qualified"]
            if (
                raw_module in python_file_set
                or raw_module.startswith(".")
                or "/" in raw_module
                or "\\" in raw_module
            ):
                continue

        candidates = sorted(modules.get(raw_module, ()))
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
