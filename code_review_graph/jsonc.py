"""Comment-preserving JSONC tokenising and surgical editing.

Several supported editors keep their configuration in JSONC: JSON that also
allows ``//`` and ``/* */`` comments and trailing commas. Reading such a file,
mutating the parsed dict and writing it back with :func:`json.dumps` deletes
every comment and every hand-made formatting choice in it, which is a silent
loss of something the user wrote. Every edit this project makes to a commented
file therefore has to be a splice into the original text.

The tokeniser keeps exact source offsets so a single member can be replaced,
inserted or removed while the rest of the file, comments included, is copied
through byte for byte.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

__all__ = [
    "Element",
    "Member",
    "Token",
    "array_elements",
    "find_value",
    "has_comments",
    "object_members",
    "remove_paths",
    "removal_ranges",
    "set_member",
    "skip_value",
    "tokenize",
]


@dataclass(frozen=True)
class Token:
    """One JSONC token with its exact offsets in the source text."""

    kind: str
    start: int
    end: int
    value: str | None = None


@dataclass(frozen=True)
class Member:
    """One ``"key": value`` pair, addressed by token index."""

    key: str
    key_index: int
    value_index: int
    value_end: int
    comma_index: int | None


@dataclass(frozen=True)
class Element:
    """One array element, addressed by token index."""

    value_index: int
    value_end: int
    comma_index: int | None


def has_comments(raw: str) -> bool:
    """Return whether ``raw`` carries a JSONC comment outside a string."""
    index = 0
    length = len(raw)
    while index < length:
        char = raw[index]
        if char == '"':
            index += 1
            while index < length:
                if raw[index] == "\\":
                    index += 2
                    continue
                if raw[index] == '"':
                    index += 1
                    break
                index += 1
            continue
        if char == "/" and index + 1 < length and raw[index + 1] in "/*":
            return True
        index += 1
    return False


def tokenize(raw: str) -> list[Token]:
    """Tokenize JSONC while retaining exact source offsets for safe splices."""
    tokens: list[Token] = []
    i = 0
    while i < len(raw):
        char = raw[i]
        if char.isspace():
            i += 1
            continue
        if char == "/" and i + 1 < len(raw):
            if raw[i + 1] == "/":
                newline = raw.find("\n", i + 2)
                i = len(raw) if newline < 0 else newline
                continue
            if raw[i + 1] == "*":
                end = raw.find("*/", i + 2)
                i = len(raw) if end < 0 else end + 2
                continue
        if char == '"':
            start = i
            i += 1
            while i < len(raw):
                if raw[i] == "\\":
                    i += 2
                    continue
                if raw[i] == '"':
                    i += 1
                    break
                i += 1
            else:
                raise ValueError("unterminated JSON string")
            try:
                value = json.loads(raw[start:i])
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON string: {exc}") from exc
            tokens.append(Token("string", start, i, value))
            continue
        if char in "{}[]:,":
            tokens.append(Token(char, i, i + 1))
            i += 1
            continue
        start = i
        while i < len(raw):
            if raw[i].isspace() or raw[i] in "{}[]:,":
                break
            if raw[i] == "/" and i + 1 < len(raw) and raw[i + 1] in "/*":
                break
            i += 1
        if i == start:
            raise ValueError(f"unexpected JSONC character at offset {i}")
        tokens.append(Token("literal", start, i, raw[start:i]))
    return tokens


def skip_value(tokens: Sequence[Token], index: int) -> int:
    """Return the token index immediately after one JSON value."""
    if index >= len(tokens):
        raise ValueError("missing JSON value")
    token = tokens[index]
    if token.kind not in ("{", "["):
        return index + 1
    closing = "}" if token.kind == "{" else "]"
    depth = 1
    index += 1
    while index < len(tokens):
        kind = tokens[index].kind
        if kind == token.kind:
            depth += 1
        elif kind == closing:
            depth -= 1
            if depth == 0:
                return index + 1
        elif kind in ("{", "["):
            index = skip_value(tokens, index)
            continue
        index += 1
    raise ValueError("unterminated JSON container")


def object_members(tokens: Sequence[Token], index: int) -> list[Member]:
    """Return the members of the object whose ``{`` is at ``index``."""
    if index >= len(tokens) or tokens[index].kind != "{":
        raise ValueError("expected JSON object")
    members: list[Member] = []
    cursor = index + 1
    while cursor < len(tokens) and tokens[cursor].kind != "}":
        if tokens[cursor].kind == ",":  # trailing comma
            cursor += 1
            continue
        key_token = tokens[cursor]
        if key_token.kind != "string" or not isinstance(key_token.value, str):
            raise ValueError("expected JSON object key")
        if cursor + 1 >= len(tokens) or tokens[cursor + 1].kind != ":":
            raise ValueError("expected colon after JSON object key")
        value_index = cursor + 2
        value_end = skip_value(tokens, value_index)
        comma_index = value_end if (
            value_end < len(tokens) and tokens[value_end].kind == ","
        ) else None
        members.append(
            Member(key_token.value, cursor, value_index, value_end, comma_index)
        )
        cursor = value_end + 1 if comma_index is not None else value_end
    return members


def array_elements(tokens: Sequence[Token], index: int) -> list[Element]:
    """Return the elements of the array whose ``[`` is at ``index``."""
    if index >= len(tokens) or tokens[index].kind != "[":
        raise ValueError("expected JSON array")
    elements: list[Element] = []
    cursor = index + 1
    while cursor < len(tokens) and tokens[cursor].kind != "]":
        if tokens[cursor].kind == ",":  # trailing comma
            cursor += 1
            continue
        value_end = skip_value(tokens, cursor)
        comma_index = value_end if (
            value_end < len(tokens) and tokens[value_end].kind == ","
        ) else None
        elements.append(Element(cursor, value_end, comma_index))
        cursor = value_end + 1 if comma_index is not None else value_end
    return elements


def find_value(tokens: Sequence[Token], path: Sequence[str | int]) -> int:
    """Return the token index of the value addressed by ``path``."""
    if not tokens:
        raise ValueError("empty JSON document")
    current = 0
    for component in path:
        if isinstance(component, str):
            member = next(
                (item for item in object_members(tokens, current) if item.key == component),
                None,
            )
            if member is None:
                raise KeyError(component)
            current = member.value_index
        else:
            elements = array_elements(tokens, current)
            if component < 0 or component >= len(elements):
                raise IndexError(component)
            current = elements[component].value_index
    return current


def removal_ranges(
    tokens: Sequence[Token], path: Sequence[str | int]
) -> list[tuple[int, int]]:
    """Return the source ranges to cut so that ``path`` disappears."""
    if not path:
        raise ValueError("refusing to remove the JSON document root")
    parent_index = find_value(tokens, path[:-1])
    component = path[-1]
    if isinstance(component, str):
        members = object_members(tokens, parent_index)
        sibling_index = next(
            (index for index, member in enumerate(members) if member.key == component),
            None,
        )
        if sibling_index is None:
            raise KeyError(component)
        item = members[sibling_index]
        start = tokens[item.key_index].start
        end = tokens[item.value_end - 1].end
        if item.comma_index is not None:
            return [(start, tokens[item.comma_index].end)]
        if sibling_index > 0:
            previous = members[sibling_index - 1]
            if previous.comma_index is not None:
                return [
                    (tokens[previous.comma_index].start, tokens[previous.comma_index].end),
                    (start, end),
                ]
        return [(start, end)]

    elements = array_elements(tokens, parent_index)
    if component < 0 or component >= len(elements):
        raise IndexError(component)
    element = elements[component]
    start = tokens[element.value_index].start
    end = tokens[element.value_end - 1].end
    if element.comma_index is not None:
        return [(start, tokens[element.comma_index].end)]
    if component > 0:
        previous_element = elements[component - 1]
        if previous_element.comma_index is not None:
            return [
                (
                    tokens[previous_element.comma_index].start,
                    tokens[previous_element.comma_index].end,
                ),
                (start, end),
            ]
    return [(start, end)]


def remove_paths(raw: str, paths: Iterable[Sequence[str | int]]) -> str:
    """Remove paths against one token snapshot, then merge overlapping spans."""
    tokens = tokenize(raw)
    ranges = [
        source_range
        for path in paths
        for source_range in removal_ranges(tokens, tuple(path))
    ]
    merged: list[tuple[int, int]] = []
    for start, end in sorted(set(ranges)):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    for start, end in reversed(merged):
        raw = raw[:start] + raw[end:]
    return raw


def _column_of(raw: str, offset: int) -> int:
    """Return the column ``offset`` sits at on its line."""
    line_start = raw.rfind("\n", 0, offset) + 1
    return max(offset - line_start, 0)


def _line_indent(raw: str, offset: int) -> int:
    """Return the leading-whitespace width of the line holding ``offset``."""
    line_start = raw.rfind("\n", 0, offset) + 1
    cursor = line_start
    while cursor < len(raw) and raw[cursor] in " \t":
        cursor += 1
    return cursor - line_start


def _render(value: Any, column: int) -> str:
    """Render ``value`` as JSON, continuation lines indented to ``column``."""
    text = json.dumps(value, indent=2, ensure_ascii=False)
    return text.replace("\n", "\n" + " " * column)


def set_member(raw: str, path: Sequence[str], value: Any) -> str:
    """Return ``raw`` with ``path`` set to ``value``, comments preserved.

    Only the addressed member is rewritten; everything else in the file,
    comments and formatting included, is copied through unchanged. Every
    component of ``path`` except the last must already exist and be an object,
    because inventing intermediate containers means guessing at layout the user
    did not write. Raises ``KeyError``/``ValueError`` when the splice point
    cannot be located, so a caller can refuse rather than fall back to a
    rewrite that would drop the comments.
    """
    if not path:
        raise ValueError("refusing to replace the JSON document root")
    tokens = tokenize(raw)
    parent_index = find_value(tokens, path[:-1])
    if tokens[parent_index].kind != "{":
        raise ValueError("parent of the target member is not a JSON object")
    key = path[-1]
    members = object_members(tokens, parent_index)
    existing = next((item for item in members if item.key == key), None)
    if existing is not None:
        start = tokens[existing.value_index].start
        end = tokens[existing.value_end - 1].end
        column = _column_of(raw, tokens[existing.key_index].start)
        return raw[:start] + _render(value, column) + raw[end:]

    if members:
        last = members[-1]
        column = _column_of(raw, tokens[last.key_index].start)
        member_text = f"{json.dumps(key)}: {_render(value, column)}"
        if last.comma_index is not None:
            anchor = tokens[last.comma_index].end
            return raw[:anchor] + "\n" + " " * column + member_text + raw[anchor:]
        anchor = tokens[last.value_end - 1].end
        return raw[:anchor] + ",\n" + " " * column + member_text + raw[anchor:]

    # An empty object: indent one step past the line that opens it.
    open_end = tokens[parent_index].end
    close_index = skip_value(tokens, parent_index) - 1
    close_start = tokens[close_index].start
    outer = _line_indent(raw, tokens[parent_index].start)
    column = outer + 2
    member_text = f"{json.dumps(key)}: {_render(value, column)}"
    return (
        raw[:open_end]
        + "\n"
        + " " * column
        + member_text
        + "\n"
        + " " * outer
        + raw[close_start:]
    )
