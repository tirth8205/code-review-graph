"""Structural parity between README.md and its four translations.

Prose is translated and must differ. Everything a reader relies on that is
*not* prose must not: the heading skeleton, the commands inside fenced code
blocks, the links and image sources, the size of each table, and the
language switcher. Those are the parts that silently rot when the English
README moves on and the translations do not.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]

SOURCE = "README.md"
TRANSLATIONS = (
    "README.zh-CN.md",
    "README.ja-JP.md",
    "README.ko-KR.md",
    "README.hi-IN.md",
)
ALL_READMES = (SOURCE,) + TRANSLATIONS

_FENCE_RE = re.compile(r"^(?P<marker>`{3,}|~{3,})")
_HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<text>.*?)\s*#*\s*$")
_TARGET_RE = re.compile(
    r"!?\[[^\]]*\]\(\s*(?P<md>[^)\s]+)[^)]*\)"
    r'|<a\b[^>]*?href="(?P<href>[^"]*)"'
    r'|<img\b[^>]*?src="(?P<src>[^"]*)"',
    re.IGNORECASE,
)
_SWITCHER_ANCHOR = '<a href="README.zh-CN.md">'


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _split_fences(text: str) -> tuple[list[str], list[str]]:
    """Split a Markdown document into code blocks and everything else.

    Returns ``(code_blocks, prose_lines)``. Each code block is the raw text
    of the block including its opening and closing fence lines, so a changed
    info string (``bash`` vs ``sh``) counts as drift too. ``prose_lines``
    keeps one entry per source line, with code lines blanked out, so line
    numbers reported to a human still line up with the file.
    """
    code_blocks: list[str] = []
    prose_lines: list[str] = []
    current: list[str] | None = None
    closing: str | None = None

    for line in text.split("\n"):
        fence = _FENCE_RE.match(line.lstrip())
        if current is None:
            if fence:
                current = [line]
                closing = fence.group("marker")[0] * 3
                prose_lines.append("")
            else:
                prose_lines.append(line)
            continue

        current.append(line)
        prose_lines.append("")
        bare = line.strip()
        if closing and bare.startswith(closing) and set(bare) == {closing[0]}:
            code_blocks.append("\n".join(current))
            current = None
            closing = None

    if current is not None:  # unterminated fence — keep what we saw
        code_blocks.append("\n".join(current))

    return code_blocks, prose_lines


def _headings(prose_lines: list[str]) -> list[tuple[int, str]]:
    """Return ``(level, text)`` for every ATX heading outside code blocks."""
    headings: list[tuple[int, str]] = []
    for line in prose_lines:
        match = _HEADING_RE.match(line)
        if match:
            headings.append((len(match.group("hashes")), match.group("text")))
    return headings


def _targets(prose_lines: list[str]) -> list[str]:
    """Return every outbound link target and image source, in document order.

    Same-document anchors (``#language-coverage``) are excluded: they point
    at a heading whose text is translated, so they are expected to differ.
    Everything else — URLs, repository-relative paths, image sources — must
    match, because a stale path is a broken link for the reader.
    """
    text = "\n".join(prose_lines)
    found: list[str] = []
    for match in _TARGET_RE.finditer(text):
        target = match.group("md") or match.group("href") or match.group("src")
        if target and not target.startswith("#"):
            found.append(target)
    return found


def _table_row_counts(prose_lines: list[str]) -> list[int]:
    """Return the number of rows in each pipe table, in document order."""
    counts: list[int] = []
    run = 0
    for line in prose_lines:
        if line.lstrip().startswith("|"):
            run += 1
            continue
        if run:
            counts.append(run)
            run = 0
    if run:
        counts.append(run)
    return counts


def _switcher(text: str) -> str:
    """Return the language switcher paragraph verbatim."""
    lines = text.split("\n")
    anchor = next(
        (i for i, line in enumerate(lines) if _SWITCHER_ANCHOR in line),
        None,
    )
    assert anchor is not None, f"no language switcher row containing {_SWITCHER_ANCHOR}"
    start = anchor
    while start > 0 and "<p" not in lines[start]:
        start -= 1
    end = anchor
    while end < len(lines) - 1 and "</p>" not in lines[end]:
        end += 1
    return "\n".join(lines[start:end + 1])


def _first_line_difference(expected: str, actual: str) -> str:
    expected_lines = expected.split("\n")
    actual_lines = actual.split("\n")
    for index in range(max(len(expected_lines), len(actual_lines))):
        want = expected_lines[index] if index < len(expected_lines) else "<missing>"
        got = actual_lines[index] if index < len(actual_lines) else "<missing>"
        if want != got:
            return f"line {index + 1}: {SOURCE} has {want!r}, this file has {got!r}"
    return "no line differs"


@pytest.fixture(scope="module")
def parsed() -> dict[str, dict[str, object]]:
    documents: dict[str, dict[str, object]] = {}
    for name in ALL_READMES:
        text = _read(name)
        code_blocks, prose_lines = _split_fences(text)
        documents[name] = {
            "code_blocks": code_blocks,
            "headings": _headings(prose_lines),
            "targets": _targets(prose_lines),
            "tables": _table_row_counts(prose_lines),
            "switcher": _switcher(text),
        }
    return documents


def test_the_parity_checks_have_something_to_compare(parsed):
    """Guard against the extractors quietly degrading into no-ops."""
    source = parsed[SOURCE]
    assert len(source["code_blocks"]) >= 15, "no fenced code blocks found"
    assert len(source["headings"]) >= 15, "no headings found"
    assert len(source["targets"]) >= 30, "no outbound links found"
    assert len(source["tables"]) >= 5, "no tables found"
    assert "README.hi-IN.md" in source["switcher"], "switcher row not located"
    # The README titles itself with <h1>, so every ATX heading is level 2+.
    # A level-1 heading here means a '# comment' inside a fenced block leaked
    # out of the code-block filter.
    assert all(level >= 2 for level, _ in source["headings"]), (
        "a '#' line inside a fenced code block was parsed as a heading"
    )


@pytest.mark.parametrize("name", TRANSLATIONS)
def test_heading_levels_match_english(name, parsed):
    """The heading skeleton is structure, not prose: it must not drift."""
    expected = [level for level, _ in parsed[SOURCE]["headings"]]
    actual = [level for level, _ in parsed[name]["headings"]]
    english_text = [text for _, text in parsed[SOURCE]["headings"]]
    translated_text = [text for _, text in parsed[name]["headings"]]

    for index in range(max(len(expected), len(actual))):
        want = expected[index] if index < len(expected) else None
        got = actual[index] if index < len(actual) else None
        if want != got:
            want_label = (
                f"level {want} ({english_text[index]!r})"
                if want is not None
                else "no heading (section missing from English)"
            )
            got_label = (
                f"level {got} ({translated_text[index]!r})"
                if got is not None
                else "no heading (section missing from the translation)"
            )
            pytest.fail(
                f"{name}: heading structure drifted from {SOURCE}. "
                f"{len(expected)} headings in {SOURCE}, {len(actual)} here. "
                f"First difference at heading {index + 1}: "
                f"{SOURCE} has {want_label}, {name} has {got_label}."
            )


@pytest.mark.parametrize("name", TRANSLATIONS)
def test_code_blocks_are_byte_identical(name, parsed):
    """Commands, flags, paths and output are never translated."""
    expected = parsed[SOURCE]["code_blocks"]
    actual = parsed[name]["code_blocks"]

    for index in range(max(len(expected), len(actual))):
        if index >= len(expected) or index >= len(actual):
            pytest.fail(
                f"{name}: has {len(actual)} fenced code blocks, "
                f"{SOURCE} has {len(expected)}. First unmatched block is "
                f"number {index + 1}."
            )
        if expected[index] != actual[index]:
            pytest.fail(
                f"{name}: fenced code block {index + 1} differs from {SOURCE}. "
                f"Code blocks must be byte-identical in all READMEs. "
                f"{_first_line_difference(expected[index], actual[index])}"
            )


@pytest.mark.parametrize("name", TRANSLATIONS)
def test_links_and_images_match_english(name, parsed):
    """Same outbound links and image sources, in the same order."""
    expected = parsed[SOURCE]["targets"]
    actual = parsed[name]["targets"]

    for index in range(max(len(expected), len(actual))):
        want = expected[index] if index < len(expected) else "<missing>"
        got = actual[index] if index < len(actual) else "<missing>"
        if want != got:
            pytest.fail(
                f"{name}: link/image targets drifted from {SOURCE}. "
                f"{len(expected)} in {SOURCE}, {len(actual)} here. "
                f"First difference at target {index + 1}: "
                f"{SOURCE} has {want!r}, {name} has {got!r}."
            )


@pytest.mark.parametrize("name", TRANSLATIONS)
def test_tables_have_the_same_number_of_rows(name, parsed):
    """A translated table that lost a row has lost a documented feature."""
    expected = parsed[SOURCE]["tables"]
    actual = parsed[name]["tables"]

    for index in range(max(len(expected), len(actual))):
        want = expected[index] if index < len(expected) else None
        got = actual[index] if index < len(actual) else None
        if want != got:
            pytest.fail(
                f"{name}: table row counts drifted from {SOURCE}. "
                f"{len(expected)} tables in {SOURCE}, {len(actual)} here. "
                f"First difference at table {index + 1}: "
                f"{SOURCE} has {want} rows, {name} has {got} rows."
            )


@pytest.mark.parametrize("name", TRANSLATIONS)
def test_language_switcher_is_identical(name, parsed):
    """Every README must offer the same five languages, spelled the same."""
    expected = parsed[SOURCE]["switcher"]
    actual = parsed[name]["switcher"]
    assert actual == expected, (
        f"{name}: the language switcher row differs from {SOURCE}. "
        f"It must be identical in all five READMEs. "
        f"{_first_line_difference(expected, actual)}"
    )
