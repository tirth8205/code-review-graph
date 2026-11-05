"""The opt-in marker gate in ``tests/conftest.py`` is one hook, not several.

pytest resolves ``pytest_collection_modifyitems`` by name on the module, so a
second definition silently replaces the first and every gate defined above it
stops running. That happened once already: a ``packaging`` gate and a generic
``_OPT_IN_MARKERS`` gate were added by two different branches, and after they
met the packaging gate was dead -- ``pytest tests/`` would have built wheels
and created virtual environments over the network without asking.

These checks are deliberately cheap and ungated: they run in the ordinary
suite, which is the run the shadowing would have broken.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

CONFTEST = Path(__file__).with_name("conftest.py")

HOOK = "pytest_collection_modifyitems"


def _conftest_tree() -> ast.Module:
    return ast.parse(CONFTEST.read_text(encoding="utf-8"))


def _top_level_defs(name: str) -> list[int]:
    return [
        node.lineno
        for node in _conftest_tree().body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    ]


def _opt_in_markers() -> set[str]:
    """Read ``_OPT_IN_MARKERS`` out of the source.

    Read rather than imported, for the same reason the hook count is: this
    module is checking what the file *says*, and importing it would go
    through whichever copy of the conftest pytest already loaded.
    """
    for node in _conftest_tree().body:
        if not isinstance(node, ast.Assign):
            continue
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "_OPT_IN_MARKERS" not in names:
            continue
        return set(ast.literal_eval(node.value.args[0]))
    raise AssertionError(f"_OPT_IN_MARKERS not found in {CONFTEST}")


def test_the_collection_hook_is_defined_exactly_once():
    lines = _top_level_defs(HOOK)
    assert len(lines) == 1, (
        f"{HOOK} is defined {len(lines)} times in {CONFTEST} (lines {lines}); "
        "only the last definition runs, so every gate above it is dead. Add "
        "the marker to _OPT_IN_MARKERS instead of defining a second hook."
    )


def test_every_gated_marker_is_registered_in_pyproject():
    """A gate on an unregistered marker skips tests nothing can select."""
    registered = set()
    pyproject = CONFTEST.parents[1] / "pyproject.toml"
    for line in pyproject.read_text(encoding="utf-8").splitlines():
        stripped = line.strip().strip(",").strip('"')
        if ":" in stripped:
            registered.add(stripped.split(":", 1)[0])
    missing = sorted(_opt_in_markers() - registered)
    assert not missing, (
        "gated markers missing from the pyproject.toml markers list: "
        f"{missing}"
    )


@pytest.mark.parametrize("marker", sorted(_opt_in_markers()))
def test_each_gated_marker_is_used_by_a_suite(marker):
    """A gate for a marker nobody uses is a gate that proves nothing."""
    used = any(
        marker in path.read_text(encoding="utf-8", errors="replace")
        for path in CONFTEST.parent.glob("test_*.py")
    )
    assert used, f"no test module uses the gated marker {marker!r}"
