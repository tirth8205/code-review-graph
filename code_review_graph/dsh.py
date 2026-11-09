"""DeepSeek Harness (DSH): the MCP server row in its home patch layer.

DSH builds its plugin tree from YAML patch layers. Every profile, the Desktop
one included, applies the home-level layer ``$DSH_HOME/cordis.patch.yml`` (by
default ``~/.dsh/cordis.patch.yml``) after its own. That file is a top-level
YAML list of patch operations; an ``insert`` operation adds plugin rows, and a
row for DSH's own ``@deepseek-ai/dsh-mcp-client`` plugin starts a stdio MCP
server. See the DSH repository: ``apps/cli/README.md`` ("Profiles"),
``packages/mcp/mcp-client/README.md`` and ``docs/user/guide/mcp-memory.md``.

``skills._dsh_config_path`` finds the file; this module only reads and edits
its text. The file is written by hand and uses DSH's ``!!js`` tag, so it is
never re-serialised: the one ``insert`` operation this project owns is added,
replaced or cut out as a block of lines, and the result is parsed again before
anything is written.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import yaml  # type: ignore[import-untyped]

#: The row id this project writes. DSH row ids name the row; ``mcp-`` matches
#: the examples DSH ships.
ENTRY_ID = "mcp-code-review-graph"
#: The MCP client plugin that ships with DSH.
PLUGIN = "@deepseek-ai/dsh-mcp-client"
#: DSH names the tools ``mcp__<serverName>__<tool>``.
SERVER_NAME = "code-review-graph"

_ROW_FIELDS = frozenset({"id", "name", "config"})
_CONFIG_FIELDS = frozenset({"serverName", "transport", "command", "args"})


def server_row(entry: dict[str, Any]) -> dict[str, Any]:
    """Wrap a ``{"command", "args"}`` server entry as a DSH MCP client row."""
    return {
        "id": ENTRY_ID,
        "name": PLUGIN,
        "config": {
            "serverName": SERVER_NAME,
            "transport": "stdio",
            "command": entry["command"],
            "args": list(entry["args"]),
        },
    }


@dataclass(frozen=True)
class Tagged:
    """A value under a tag this module does not interpret, such as ``!!js``."""

    tag: str
    value: Any


class _PatchLoader(yaml.SafeLoader):
    """Safe loader that keeps unknown tags as opaque :class:`Tagged` values.

    DSH evaluates ``!!js`` values itself; here they only have to be told apart
    from plain strings. Nothing is executed.
    """


def _construct_tagged(loader: _PatchLoader, _suffix: str, node: yaml.Node) -> Tagged:
    value: Any
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    return Tagged(node.tag, value)


# The empty prefix matches only tags no exact constructor claims.
_PatchLoader.add_multi_constructor("", _construct_tagged)


class _Dumper(yaml.SafeDumper):
    """Indent nested lists, as the examples in DSH's documentation do."""

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> Any:
        return super().increase_indent(flow, False)


def load(text: str) -> Any:
    """Parse a patch layer. Raises :class:`yaml.YAMLError` when it is not YAML."""
    loader = _PatchLoader(text)
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


def _names_us(row: Any) -> bool:
    if not isinstance(row, dict):
        return False
    if row.get("id") == ENTRY_ID:
        return True
    config = row.get("config")
    return (
        row.get("name") == PLUGIN
        and isinstance(config, dict)
        and config.get("serverName") == SERVER_NAME
    )


def registrations(document: list[Any]) -> list[tuple[int, int]]:
    """Return ``(operation, row)`` indexes of every row registering this server."""
    found: list[tuple[int, int]] = []
    for op_index, operation in enumerate(document):
        rows = operation.get("insert") if isinstance(operation, dict) else None
        if not isinstance(rows, list):
            continue
        found.extend((op_index, row_index) for row_index, row in enumerate(rows) if _names_us(row))
    return found


def is_generated_row(row: Any, entry_is_generated: Callable[[Any], bool]) -> bool:
    """Whether *row* has exactly the shape this project writes.

    *entry_is_generated* judges the command line, so every serve command a
    release has written is recognised the same way as on other platforms.
    """
    if not isinstance(row, dict) or set(row) != _ROW_FIELDS:
        return False
    config = row["config"]
    if row["id"] != ENTRY_ID or row["name"] != PLUGIN or not isinstance(config, dict):
        return False
    if not set(config) <= _CONFIG_FIELDS:
        return False
    if config.get("serverName") != SERVER_NAME or config.get("transport") != "stdio":
        return False
    return entry_is_generated({key: config[key] for key in ("command", "args") if key in config})


def _owned_operation(document: list[Any], op_index: int) -> bool:
    """Whether the operation holds nothing but one row (so it can be cut out whole)."""
    operation = document[op_index]
    return (
        isinstance(operation, dict)
        and set(operation) == {"insert"}
        and isinstance(operation["insert"], list)
        and len(operation["insert"]) == 1
    )


def render_operation(row: dict[str, Any], indent: int = 0) -> str:
    """Render the ``insert`` operation for *row* as YAML lines."""
    text = yaml.dump(
        [{"insert": [row]}],
        Dumper=_Dumper,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
    )
    pad = " " * indent
    return "".join(f"{pad}{line}" for line in text.splitlines(keepends=True))


class PatchError(ValueError):
    """The layer cannot be edited without risking what is already in it."""


def _root(raw: str) -> yaml.Node | None:
    return yaml.compose(raw, Loader=_PatchLoader)


def _operation_lines(raw: str, op_index: int) -> tuple[int, int, int]:
    """Return ``(first, end, indent)``: the lines of one top-level operation.

    Comments and blank lines after the operation belong to what follows and
    are left outside the span.
    """
    root = _root(raw)
    if not isinstance(root, yaml.SequenceNode) or root.flow_style:
        raise PatchError("the patch list is not a block list")
    lines = raw.splitlines(keepends=True)
    items = root.value
    first = items[op_index].start_mark.line
    if not lines[first].lstrip().startswith("-"):
        raise PatchError("the operation does not start its own line")
    indent = len(lines[first]) - len(lines[first].lstrip())
    end = items[op_index + 1].start_mark.line if op_index + 1 < len(items) else len(lines)
    while end > first + 1:
        line = lines[end - 1]
        stripped = line.strip()
        if stripped and not (
            stripped.startswith("#") and len(line) - len(line.lstrip()) <= indent
        ):
            break
        end -= 1
    return first, end, indent


def _list_indent(raw: str) -> int:
    root = _root(raw)
    if isinstance(root, yaml.SequenceNode) and root.value and not root.flow_style:
        line = raw.splitlines()[root.value[0].start_mark.line]
        return len(line) - len(line.lstrip())
    return 0


def _without_operation(document: list[Any], op_index: int) -> list[Any]:
    return [operation for index, operation in enumerate(document) if index != op_index]


def _check(rewritten: str, expected: list[Any]) -> None:
    """Refuse an edit whose result does not parse to exactly *expected*."""
    try:
        parsed = load(rewritten)
    except yaml.YAMLError as exc:
        raise PatchError(f"the edit would not parse ({exc.__class__.__name__})") from exc
    if parsed != expected:
        raise PatchError("the edit would change more than this project's row")


def parse(raw: str) -> list[Any]:
    """Parse a layer into its operations, treating an empty file as none."""
    try:
        document = load(raw)
    except yaml.YAMLError as exc:
        raise PatchError(f"it is not valid YAML ({exc.__class__.__name__})") from exc
    if document is None:
        return []
    if not isinstance(document, list):
        raise PatchError(
            f"it is a {type(document).__name__}, not the list of patch operations DSH reads"
        )
    return document


def with_row(raw: str, row: dict[str, Any], replace: int | None) -> str:
    """Return *raw* with the operation for *row* added, or put at *replace*.

    *replace* is the index of an operation that holds only this project's row.
    """
    document = parse(raw)
    if replace is not None:
        first, end, indent = _operation_lines(raw, replace)
        lines = raw.splitlines(keepends=True)
        rewritten = "".join(
            lines[:first] + [render_operation(row, indent)] + lines[end:]
        )
        expected = list(document)
        expected[replace] = {"insert": [row]}
        _check(rewritten, expected)
        return rewritten

    root = _root(raw)
    if isinstance(root, yaml.SequenceNode) and root.flow_style:
        if root.value:
            raise PatchError("the patch list is written in flow style ([...])")
        # ``[]``, as DSH writes an empty layer: it must be alone on its line.
        start, end = root.start_mark.index, root.end_mark.index
        line_start = raw.rfind("\n", 0, start) + 1
        line_end = raw.find("\n", end)
        line_end = len(raw) if line_end == -1 else line_end
        if raw[line_start:start].strip() or raw[end:line_end].strip():
            raise PatchError("the empty list [] shares its line with other text")
        rewritten = raw[:line_start] + render_operation(row) + raw[line_end + 1:]
    else:
        prefix = raw if not raw or raw.endswith("\n") else raw + "\n"
        rewritten = prefix + render_operation(row, _list_indent(raw))
    _check(rewritten, [*document, {"insert": [row]}])
    return rewritten


def without_row(raw: str, op_index: int) -> str:
    """Return *raw* without the operation at *op_index*.

    An emptied layer becomes ``[]``: DSH refuses a layer that is not a list.
    """
    document = parse(raw)
    first, end, _indent = _operation_lines(raw, op_index)
    lines = raw.splitlines(keepends=True)
    rewritten = "".join(lines[:first] + lines[end:])
    remaining = _without_operation(document, op_index)
    if not remaining:
        try:
            emptied = load(rewritten)
        except yaml.YAMLError as exc:
            raise PatchError(f"the edit would not parse ({exc.__class__.__name__})") from exc
        if emptied is None:
            if rewritten and not rewritten.endswith("\n"):
                rewritten += "\n"
            rewritten += "[]\n"
    _check(rewritten, remaining)
    return rewritten


def find_owned(raw: str) -> tuple[list[Any], list[tuple[int, int]]]:
    """Parse *raw* and return the document with every row registering this server."""
    document = parse(raw)
    return document, registrations(document)


def removable(document: list[Any], found: list[tuple[int, int]]) -> int | None:
    """Return the operation uninstall can cut out whole, or None."""
    if len(found) != 1:
        return None
    op_index, _row_index = found[0]
    return op_index if _owned_operation(document, op_index) else None
