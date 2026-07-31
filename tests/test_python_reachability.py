"""Regression tests for statically unreachable Python call edges."""

from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.parser import CodeParser
from code_review_graph.refactor import find_dead_code


def _call_targets(source: bytes) -> set[str]:
    """Return the bare names of Python CALLS targets in ``source``."""
    _, edges = CodeParser().parse_bytes(Path("guards.py"), source)
    return {
        edge.target.rsplit("::", 1)[-1]
        for edge in edges
        if edge.kind == "CALLS"
    }


def test_false_branch_calls_are_omitted_but_else_calls_remain() -> None:
    targets = _call_targets(
        b"""
def dead_target():
    pass

def live_target():
    pass

if False:
    dead_target()
else:
    live_target()

if 0:
    dead_target()
""",
    )

    assert "dead_target" not in targets
    assert "live_target" in targets


def test_typing_type_checking_aliases_make_guarded_calls_unreachable() -> None:
    targets = _call_targets(
        b"""
import typing
import typing as t
from typing import TYPE_CHECKING
from typing import TYPE_CHECKING as TC

def direct_target():
    pass

def module_target():
    pass

def module_alias_target():
    pass

def name_alias_target():
    pass

if TYPE_CHECKING:
    direct_target()
if typing.TYPE_CHECKING:
    module_target()
if t.TYPE_CHECKING:
    module_alias_target()
if TC:
    name_alias_target()
""",
    )

    assert targets.isdisjoint({
        "direct_target",
        "module_target",
        "module_alias_target",
        "name_alias_target",
    })


def test_reassigned_type_checking_name_is_not_treated_as_typing_sentinel() -> None:
    targets = _call_targets(
        b"""
from typing import TYPE_CHECKING

TYPE_CHECKING = True

def live_target():
    pass

if TYPE_CHECKING:
    live_target()
""",
    )

    assert "live_target" in targets


def test_function_parameters_can_shadow_type_checking_aliases() -> None:
    targets = _call_targets(
        b"""
import typing as t
from typing import TYPE_CHECKING as TC

def live_name_target():
    pass

def live_module_target():
    pass

def run(TC=True, t=None):
    if TC:
        live_name_target()
    if t.TYPE_CHECKING:
        live_module_target()
""",
    )

    assert "live_name_target" in targets
    assert "live_module_target" in targets


def test_class_attribute_can_shadow_typing_module_alias() -> None:
    targets = _call_targets(
        b"""
import typing as t

def live_target():
    pass

class Example:
    t = object()
    if t.TYPE_CHECKING:
        live_target()
""",
    )

    assert "live_target" in targets


def test_static_boolean_expressions_choose_only_reachable_branch() -> None:
    targets = _call_targets(
        b"""
from typing import TYPE_CHECKING

def dead_not_target():
    pass

def dead_and_target():
    pass

def dead_or_target():
    pass

def live_target():
    pass

if not True:
    dead_not_target()
if False and runtime_flag:
    dead_and_target()
if TYPE_CHECKING or False:
    dead_or_target()
if not TYPE_CHECKING:
    live_target()
""",
    )

    assert targets.isdisjoint({
        "dead_not_target",
        "dead_and_target",
        "dead_or_target",
    })
    assert "live_target" in targets


def test_nested_function_declared_in_dead_branch_has_no_call_edges() -> None:
    targets = _call_targets(
        b"""
def deep_target():
    pass

if False:
    def hidden():
        deep_target()
    hidden()
""",
    )

    assert "deep_target" not in targets
    assert "hidden" not in targets


def test_graph_consumers_do_not_observe_dead_branch_call(
    tmp_path: Path,
) -> None:
    targets_path = tmp_path / "targets.py"
    caller_path = tmp_path / "caller.py"
    targets_path.write_text(
        "def dead_target():\n"
        "    pass\n\n"
        "def live_target():\n"
        "    pass\n",
        encoding="utf-8",
    )
    caller_path.write_text(
        "from targets import dead_target, live_target\n\n"
        "def run():\n"
        "    if False:\n"
        "        dead_target()\n"
        "    live_target()\n",
        encoding="utf-8",
    )

    parser = CodeParser(repo_root=tmp_path)
    parsed = [parser.parse_file(path) for path in (targets_path, caller_path)]
    dead_qualified = f"{targets_path.as_posix()}::dead_target"
    live_qualified = f"{targets_path.as_posix()}::live_target"

    with GraphStore(tmp_path / "graph.db") as store:
        for nodes, edges in parsed:
            for node in nodes:
                store.upsert_node(node)
            for edge in edges:
                store.upsert_edge(edge)
        store.commit()

        dead_callers = [
            edge
            for edge in store.get_edges_by_target(dead_qualified)
            if edge.kind == "CALLS"
        ]
        live_callers = [
            edge
            for edge in store.get_edges_by_target(live_qualified)
            if edge.kind == "CALLS"
        ]
        assert dead_callers == []
        assert len(live_callers) == 1

        impact = store.get_impact_radius([str(targets_path)], max_depth=2)
        assert not any(
            edge.kind == "CALLS" and edge.target_qualified == dead_qualified
            for edge in impact["edges"]
        )

        dead_names = {entry["name"] for entry in find_dead_code(store)}
        assert "dead_target" in dead_names
        assert "live_target" not in dead_names
