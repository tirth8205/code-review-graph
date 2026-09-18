"""Guard against CodeQL's ``py/incomplete-url-substring-sanitization`` pattern.

That query fires on a containment test whose left operand is a string
literal that is, on its own, a whole URL or a bare hostname::

    if "example.com" in candidate: ...

It is a high-severity code-scanning rule, so a single occurrence anywhere in
the repository (test code included) turns every pull request check red. The
query inspects only the shape of the literal, never what the comparison
means, so an assertion about rendered markdown trips it just as readily as a
real access-control check.

This module reimplements the query's own ``looksLikeUrl`` predicate and its
three sinks against the repository's Python sources, so the pattern is caught
in the ordinary test run rather than by a red check on a pull request. The
fix is always the same: compare against enough surrounding context that the
literal is no longer a URL by itself, or parse the URL and inspect its host.

Query source: ``python/ql/src/Security/CWE-020/IncompleteUrlSubstringSanitization.ql``
in github/codeql.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Iterator

REPO_ROOT = Path(__file__).resolve().parents[1]

SCAN_ROOTS = ("code_review_graph", "scripts", "tests")

# Sample sources for the parser tests. They are inputs, not code we run, and
# several are deliberately malformed.
SKIP_DIRS = {".git", ".venv", "__pycache__", "build", "dist", "node_modules", "fixtures"}

_COMMON_TLDS = "com|org|edu|gov|uk|net|io"

# Both alternatives of the query's looksLikeUrl predicate. CodeQL's
# regexpMatch is a full match, which is why a URL embedded in a longer
# literal is not flagged.
_LOOKS_LIKE_URL = (
    re.compile(
        r"(?i)([a-z]*:?//)?\.?([a-z0-9-]+\.)+(" + _COMMON_TLDS + r")(:[0-9]+)?/?"
    ),
    re.compile(r"(?i)https?://([a-z0-9-]+\.)+([a-z]+)(:[0-9]+)?/?"),
)

# unsafe_call_to_startswith: safe only when the literal reaches past the host.
_SAFE_STARTSWITH = re.compile(r"(?i)https?://[\.a-z0-9-]+/.*")
# unsafe_call_to_endswith: safe only when the literal is a dotted suffix.
_SAFE_ENDSWITH = re.compile(r"(?i)\.([a-z0-9-]+)(\.[a-z0-9-]+)+")


def looks_like_url(text: str) -> bool:
    return any(pattern.fullmatch(text) for pattern in _LOOKS_LIKE_URL)


def python_sources() -> Iterator[Path]:
    for root in SCAN_ROOTS:
        base = REPO_ROOT / root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if SKIP_DIRS.intersection(path.relative_to(REPO_ROOT).parts):
                continue
            yield path


def _string_literal(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _report(path: Path, node: ast.AST, text: str, sink: str) -> str:
    rel = path.relative_to(REPO_ROOT).as_posix()
    return f"{rel}:{getattr(node, 'lineno', '?')}: {sink} {text!r}"


def incomplete_url_sanitizations(path: Path) -> list[str]:
    """Return one description per ``py/incomplete-url-substring-sanitization`` hit."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        # CodeQL cannot extract it either; nothing to guard.
        return []

    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            # Chained comparisons compare each operand with the next.
            operands = [node.left, *node.comparators[:-1]]
            for operand, op in zip(operands, node.ops):
                if not isinstance(op, ast.In):
                    continue
                text = _string_literal(operand)
                if text is not None and looks_like_url(text):
                    hits.append(_report(path, node, text, "url substring check on"))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            method = node.func.attr
            if method not in ("startswith", "endswith") or not node.args:
                continue
            text = _string_literal(node.args[0])
            if text is None or not looks_like_url(text):
                continue
            safe = _SAFE_STARTSWITH if method == "startswith" else _SAFE_ENDSWITH
            if safe.fullmatch(text):
                continue
            hits.append(_report(path, node, text, f"unsafe {method} on"))
    return hits


def test_scan_covers_the_repository():
    """A silent zero-file scan would make the guard below vacuous."""
    sources = list(python_sources())
    assert len(sources) > 50, sources
    names = {path.name for path in sources}
    assert "render_pr_comment.py" in names
    assert "test_action_e2e.py" in names


def test_looks_like_url_matches_the_query():
    assert looks_like_url("https://attacker.invalid")
    assert looks_like_url("example.com")
    assert looks_like_url("https://github.com/")
    # A URL inside a longer literal is not a URL by itself.
    assert not looks_like_url("[click](https://attacker.invalid)")
    assert not looks_like_url("\\[click\\](https://attacker.invalid)")
    assert not looks_like_url("https://github.com/tirth8205/code-review-graph")


def test_no_incomplete_url_substring_sanitization():
    hits = [hit for path in python_sources() for hit in incomplete_url_sanitizations(path)]
    assert hits == [], (
        "CodeQL py/incomplete-url-substring-sanitization would fail the build on:\n"
        + "\n".join(hits)
    )
