"""tests_for gives the same answer on every run of the same graph.

``get_transitive_tests`` follows at most ``max_frontier`` callees per hop. It
used to cut that list from a Python ``set``, whose order changes with the
string hash seed, so each new process kept different callees. On koel's
``PodcastService`` three runs on one graph gave 58, 51 and 19 tests. The
risk score in ``detect_changes`` counts the same tests, so it moved too.
"""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404 - fixed argv, no shell
import sys
from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.parser import EdgeInfo, NodeInfo

HUB_FILE = "/repo/app/Hub.php"
HUB = f"{HUB_FILE}::Hub"
CALLEES = 40


def _callee(i: int) -> str:
    # Zero-padded so name order is number order and easy to read in a failure.
    return f"/repo/app/Lib{i:02d}.php::run{i:02d}"


def _test_for(i: int) -> str:
    return f"/repo/tests/Lib{i:02d}Test.php::test_run{i:02d}"


def _build(db: Path) -> None:
    with GraphStore(db) as store:
        store.upsert_node(NodeInfo("Class", "Hub", HUB_FILE, 1, 200, "php"))
        store.upsert_node(NodeInfo("Function", "work", HUB_FILE, 2, 199, "php"))
        method = f"{HUB_FILE}::work"
        store.upsert_edge(EdgeInfo("CONTAINS", HUB, method, HUB_FILE, 2))
        for i in range(CALLEES):
            callee_file, callee_name = _callee(i).split("::")
            test_file, test_name = _test_for(i).split("::")
            store.upsert_node(NodeInfo("Function", callee_name, callee_file, 1, 5, "php"))
            store.upsert_node(
                NodeInfo("Test", test_name, test_file, 1, 5, "php", is_test=True),
            )
            store.upsert_edge(EdgeInfo("CALLS", method, _callee(i), HUB_FILE, 3 + i))
            store.upsert_edge(EdgeInfo("TESTED_BY", _callee(i), _test_for(i), test_file, 2))
        store.commit()


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "graph.db"
    _build(path)
    return path


def test_capped_callees_are_kept_in_name_order(db: Path) -> None:
    with GraphStore(db) as store:
        got = [row["qualified_name"] for row in store.get_transitive_tests(HUB, max_frontier=5)]
    assert got == [_test_for(i) for i in range(5)]


def test_uncapped_answer_is_in_name_order(db: Path) -> None:
    with GraphStore(db) as store:
        got = [row["qualified_name"] for row in store.get_transitive_tests(HUB)]
    assert got == [_test_for(i) for i in range(CALLEES)]


_CHILD = """
import json, sys
from code_review_graph.graph import GraphStore
with GraphStore(sys.argv[1]) as store:
    rows = store.get_transitive_tests(sys.argv[2], max_frontier=int(sys.argv[3]))
print(json.dumps([row["qualified_name"] for row in rows]))
"""


@pytest.mark.parametrize("cap", [10, 1000])
def test_answer_does_not_change_with_the_hash_seed(db: Path, cap: int) -> None:
    """A new process is a new hash seed: an MCP restart, a CLI call."""
    answers = set()
    for seed in ("1", "2", "3", "4"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        proc = subprocess.run(  # nosec B603 - fixed argv, no shell
            [sys.executable, "-c", _CHILD, str(db), HUB, str(cap)],
            env=env, capture_output=True, text=True, check=True, timeout=120,
        )
        answers.add(proc.stdout.strip().splitlines()[-1])
    assert len(answers) == 1, answers
    assert json.loads(answers.pop()) == [_test_for(i) for i in range(min(cap, CALLEES))]
