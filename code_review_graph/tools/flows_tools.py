"""Tools 10, 11: list_flows, get_flow."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..flows import get_flow_by_id, get_flows
from ..hints import generate_hints, get_session
from ._common import _bounded, _get_store, _shown_of, _validate_positive_int

# ---------------------------------------------------------------------------
# Tool 10: list_flows  [EXPLORE]
# ---------------------------------------------------------------------------

# ``get_flows`` slices in SQL, so asking for "all" is how the tool counts the
# untruncated total before keeping a bounded prefix.
_FETCH_ALL = 10**9

# Hard ceilings. Flow counts scale with entry points, and ``get_flow``
# embeds one record per step (plus optional source), so both need bounds.
_MAX_FLOWS = 200
_MAX_FLOW_STEPS = 200
_MAX_FLOW_SOURCE_LINES = 2000


def list_flows(
    repo_root: str | None = None,
    sort_by: str = "criticality",
    limit: int = 50,
    kind: str | None = None,
    detail_level: str = "standard",
) -> dict[str, Any]:
    """List execution flows in the codebase, sorted by criticality.

    [EXPLORE] Retrieves stored execution flows from the knowledge graph.
    Each flow represents a call chain starting from an entry point
    (e.g. HTTP handler, CLI command, test function).

    Args:
        repo_root: Repository root path. Auto-detected if omitted.
        sort_by: Sort column: criticality, depth, node_count, file_count,
                 or name.
        limit: Maximum flows to return (default: 50, capped at 200).
        kind: Optional filter by entry point kind (e.g. "Test", "Function").
        detail_level: "standard" (default) returns full flow data;
                      "minimal" returns only name, criticality, and
                      node_count per flow.

    Returns:
        Flows with criticality scores, plus ``total`` and ``truncated``.
    """
    _validate_positive_int(limit, "limit")

    store, root = _get_store(repo_root)
    try:
        # Count every matching flow, then keep the bounded prefix — the same
        # "count all, return a prefix" contract query.py uses for max_results.
        # The flows table has one row per entry point, so a full read is cheap
        # relative to serializing them all back to the client.
        flows = get_flows(store, sort_by=sort_by, limit=_FETCH_ALL)

        if kind:
            filtered = []
            for f in flows:
                ep_id = f.get("entry_point_id")
                if ep_id is not None:
                    node_kind = store.get_node_kind_by_id(ep_id)
                    if node_kind == kind:
                        filtered.append(f)
            flows = filtered

        flows, total, truncated = _bounded(flows, limit, _MAX_FLOWS)

        if detail_level == "minimal":
            flows = [
                {
                    "name": f["name"],
                    "criticality": f["criticality"],
                    "node_count": f["node_count"],
                }
                for f in flows
            ]

        result: dict[str, object] = {
            "status": "ok",
            "summary": (
                f"Found {total} execution flow(s)"
                + _shown_of(len(flows), total)
            ),
            "flows": flows,
            "total": total,
            "truncated": truncated,
        }
        result["_hints"] = generate_hints(
            "list_flows", result, get_session()
        )
        return result
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Tool 11: get_flow  [EXPLORE]
# ---------------------------------------------------------------------------


def get_flow(
    flow_id: int | None = None,
    flow_name: str | None = None,
    include_source: bool = False,
    repo_root: str | None = None,
    max_steps: int = 50,
    max_source_lines: int = 400,
) -> dict[str, Any]:
    """Get details of a single execution flow.

    [EXPLORE] Retrieves full path details for a flow, including each step's
    function name, file, and line numbers.  Optionally includes source
    snippets for every step in the path.

    Args:
        flow_id: Database ID of the flow (from list_flows).
        flow_name: Name to search for (partial match). Ignored if flow_id
                   given.
        include_source: If True, include source code snippets for each step.
        repo_root: Repository root path. Auto-detected if omitted.
        max_steps: Maximum steps to return (default 50, capped at 200).
            ``flow["total_steps"]`` reports the untruncated count.
        max_source_lines: Total source lines emitted across all steps when
            ``include_source`` is set (default 400, capped at 2000). Without
            this, one deep flow inlines every function body it touches.

    Returns:
        Flow details with steps, or not_found status. ``flow["truncated"]``
        marks that ``max_steps`` or the source budget cut the response.
    """
    _validate_positive_int(max_steps, "max_steps")
    _validate_positive_int(max_source_lines, "max_source_lines")

    store, root = _get_store(repo_root)
    try:
        flow: dict | None = None

        if flow_id is not None:
            flow = get_flow_by_id(store, flow_id)
        elif flow_name is not None:
            # Search flows by name match
            all_flows = get_flows(
                store, sort_by="criticality", limit=500
            )
            for f in all_flows:
                if flow_name.lower() in f["name"].lower():
                    flow = get_flow_by_id(store, f["id"])
                    break

        if flow is None:
            return {
                "status": "not_found",
                "summary": "No flow found matching the given criteria.",
            }

        steps, total_steps, truncated = _bounded(
            flow.get("steps") or [], max_steps, _MAX_FLOW_STEPS,
        )
        flow["steps"] = steps
        flow["total_steps"] = total_steps
        flow["truncated"] = truncated

        # Optionally include source snippets for each step, spending a shared
        # line budget so a deep flow cannot inline the whole call chain.
        if include_source:
            budget = min(max_source_lines, _MAX_FLOW_SOURCE_LINES)
            for step in steps:
                if budget <= 0:
                    flow["truncated"] = True
                    flow["source_truncated"] = True
                    break
                fp = Path(step["file"]) if step.get("file") else None
                if fp is not None and not fp.is_absolute():
                    fp = root / fp
                file_path = fp
                if file_path and file_path.is_file():
                    try:
                        lines = file_path.read_text(
                            errors="replace"
                        ).splitlines()
                        start = max(
                            0, (step.get("line_start") or 1) - 1
                        )
                        end = min(
                            len(lines),
                            step.get("line_end") or len(lines),
                            start + budget,
                        )
                        step["source"] = "\n".join(
                            f"{i + 1}: {lines[i]}"
                            for i in range(start, end)
                        )
                        budget -= max(0, end - start)
                    except (OSError, UnicodeDecodeError):
                        step["source"] = "(could not read file)"

        result = {
            "status": "ok",
            "summary": (
                f"Flow '{flow['name']}': {flow['node_count']} nodes, "
                f"depth {flow['depth']}, "
                f"criticality {flow['criticality']:.4f}"
                + _shown_of(len(steps), total_steps)
            ),
            "flow": flow,
        }
        result["_hints"] = generate_hints(
            "get_flow", result, get_session()
        )
        return result
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    finally:
        store.close()
