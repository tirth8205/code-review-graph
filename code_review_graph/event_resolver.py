"""Resolve Spring application-event publishers to package-matched listeners."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from .parser import EdgeInfo, NodeInfo

if TYPE_CHECKING:
    from .graph import GraphStore

logger = logging.getLogger(__name__)

_EVENT_NODE_FILE = "event"
_DERIVED_FLAG = "spring_event_resolved"


def _clear_derived_event_data(store: GraphStore) -> tuple[int, int]:
    """Remove event nodes and derived calls before rebuilding the relation."""
    call_rows = store._conn.execute(
        "SELECT id, extra FROM edges WHERE kind = 'CALLS'"
    ).fetchall()
    derived_ids: list[tuple[int]] = []
    for row in call_rows:
        try:
            extra = json.loads(row["extra"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if extra.get(_DERIVED_FLAG):
            derived_ids.append((row["id"],))

    if derived_ids:
        store._conn.executemany("DELETE FROM edges WHERE id = ?", derived_ids)
    removed_nodes = store._conn.execute(
        "DELETE FROM nodes WHERE kind = 'Event' AND file_path = ?",
        (_EVENT_NODE_FILE,),
    ).rowcount
    store.commit()
    return len(derived_ids), removed_nodes


def resolve_spring_events(store: GraphStore) -> dict[str, int]:
    """Rebuild Event nodes and derived publisher-to-listener CALLS edges.

    The rebuild is intentionally global whenever Java changes. It prevents a
    listener deletion, rename, or event-type change from leaving a stale CALLS
    edge whose owning publisher file was not itself reparsed.
    """
    removed_calls, _ = _clear_derived_event_data(store)
    rows = store._conn.execute(
        "SELECT kind, source_qualified, target_qualified, file_path, line, extra "
        "FROM edges WHERE kind IN ('PUBLISHES', 'HANDLES')"
    ).fetchall()

    publishers: dict[str, list] = {}
    listeners: dict[str, list] = {}
    event_types: dict[str, str] = {}
    for row in rows:
        target = row["target_qualified"]
        if not isinstance(target, str) or not target.startswith("event::"):
            continue
        try:
            extra = json.loads(row["extra"] or "{}")
        except (json.JSONDecodeError, TypeError):
            extra = {}
        identity = extra.get("event_type")
        if not isinstance(identity, str) or not identity:
            identity = target.removeprefix("event::")
        event_types[target] = identity
        collection = publishers if row["kind"] == "PUBLISHES" else listeners
        collection.setdefault(target, []).append(row)

    for target, identity in sorted(event_types.items()):
        store.upsert_node(NodeInfo(
            kind="Event",
            name=identity,
            file_path=_EVENT_NODE_FILE,
            line_start=0,
            line_end=0,
            language="java",
            extra={"event_type": identity, "virtual": True},
        ))
        if target != f"event::{identity}":
            logger.warning("Unexpected Spring event identity target: %s", target)

    emitted = 0
    for event_target, event_publishers in publishers.items():
        event_listeners = listeners.get(event_target, [])
        if not event_listeners:
            continue
        identity = event_types[event_target]
        for publisher in event_publishers:
            for listener in event_listeners:
                store.upsert_edge(EdgeInfo(
                    kind="CALLS",
                    source=publisher["source_qualified"],
                    target=listener["source_qualified"],
                    file_path=publisher["file_path"],
                    line=publisher["line"],
                    extra={
                        _DERIVED_FLAG: True,
                        "event_type": identity,
                        "resolution": "spring_application_event",
                        "confidence": 0.95,
                        "confidence_tier": "INFERRED",
                    },
                ))
                emitted += 1

    store.commit()
    logger.info(
        "Spring event resolver: indexed %d events and emitted %d CALLS edges",
        len(event_types),
        emitted,
    )
    return {
        "events_indexed": len(event_types),
        "calls_emitted": emitted,
        "stale_calls_removed": removed_calls,
    }
