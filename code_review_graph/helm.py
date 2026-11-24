"""File-level dependencies on a Helm chart's default values."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .graph import GraphStore


_ACTION_TOKEN = re.compile(rb"\{\{|\}\}|/\*|[\"'`]|(?<![\w.$])\$?\.Values(?!\w)")
_QUOTED = re.compile(rb''' "(?:\\[\s\S]|[^"\\])*" | '(?:\\[\s\S]|[^'\\])*' | `[^`]*` ''', re.X)
VALUES_LINE_KEY = "helm_values_line"


def template_values_line(source: bytes) -> int | None:
    """Return the first .Values access in a complete Go template action."""
    offset = source.find(b"{{")
    while offset != -1:
        offset += 2
        value_offset = None
        while token := _ACTION_TOKEN.search(source, offset):
            offset = token.end()
            value = token[0]
            if value == b"}}":
                if value_offset is not None:
                    return source.count(b"\n", 0, value_offset) + 1
                break
            if value == b"{{":
                # A new opener invalidates an incomplete action.
                value_offset = None
            elif value == b"/*":
                closing = source.find(b"*/", offset)
                if closing == -1:
                    return None
                offset = closing + 2
            elif value in (b'"', b"'", b"`"):
                quoted = _QUOTED.match(source, token.start())
                if not quoted:
                    return None
                offset = quoted.end()
            elif value_offset is None:
                value_offset = token.start()
        else:
            return None
        offset = source.find(b"{{", offset)
    return None


def resolve_helm_values(store: GraphStore) -> None:
    """Connect candidate templates to the nearest indexed chart's values file."""
    from .parser import EdgeInfo

    conn = store._conn
    templates = conn.execute(
        "SELECT file_path, json_extract(extra, ?) AS line FROM nodes "
        "WHERE kind = 'File' AND json_type(extra, ?) = 'integer'",
        (f"$.{VALUES_LINE_KEY}", f"$.{VALUES_LINE_KEY}"),
    ).fetchall()
    if not templates:
        return

    files = {
        row[0] for row in conn.execute(
            "SELECT file_path FROM nodes WHERE kind = 'File' AND "
            "(file_path LIKE '%/Chart.yaml' OR file_path LIKE '%/values.yaml')"
        )
    }
    desired: set[tuple[str, str, int]] = set()
    for row in templates:
        template = PurePosixPath(row["file_path"])
        for directory in template.parents:
            if directory.name != "templates":
                continue
            chart = directory.parent
            if (chart / "Chart.yaml").as_posix() not in files:
                continue
            values = (chart / "values.yaml").as_posix()
            if values in files:
                desired.add((row["file_path"], values, row["line"]))
            break

    existing = {
        (row["file_path"], row["target_qualified"], row["line"]): row["id"]
        for row in conn.execute(
            "SELECT id, file_path, target_qualified, line FROM edges "
            "WHERE kind = 'IMPORTS_FROM' AND json_extract(extra, '$.helm_values') = 1"
        )
    }
    removed = existing.keys() - desired
    added = desired - existing.keys()
    if not (removed or added):
        return

    store._begin_immediate()
    try:
        conn.executemany("DELETE FROM edges WHERE id = ?", [(existing[key],) for key in removed])
        for template_path, values, line in sorted(added):
            store.upsert_edge(EdgeInfo(
                kind="IMPORTS_FROM",
                source=template_path,
                target=values,
                file_path=template_path,
                line=line,
                extra={"helm_values": True},
            ))
        store.commit()
    except BaseException:
        store.rollback()
        raise
