"""Receiver evidence has to bind the READ path, not only the resolver.

``_resolve_bare_endpoints`` refuses to attribute ``some_dict.get(...)`` to
``ConnectionPool.get``. ``callers_of`` then reached past it: its bare-name
fallback answers from every ``CALLS`` edge whose target is the plain method
name, so every attribution the resolver honestly declined came back through
the tool. On this repository's own graph that was 421 "callers" of
``ConnectionPool.get``, 5 of them real.

These tests assert the behaviour **through** ``query_graph`` — the entry
point ``main.py`` calls — not through the store, because the store was
already right and the tool was not.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build
from code_review_graph.parser import EdgeInfo, NodeInfo
from code_review_graph.postprocessing import run_post_processing
from code_review_graph.tools import query_graph


def _callers(root: Path, qualified_name: str) -> set[str]:
    """``callers_of`` as ``query_graph_tool`` calls it, as a name set."""
    result = query_graph(
        pattern="callers_of",
        target=qualified_name,
        repo_root=str(root),
        detail_level="minimal",
        max_results=500,
    )
    assert result["status"] == "ok", result
    # Minimal detail caps the visible prefix at five but still counts every
    # logical result, so re-read the full list for the identities.
    full = query_graph(
        pattern="callers_of",
        target=qualified_name,
        repo_root=str(root),
        detail_level="standard",
        max_results=500,
    )
    assert full["result_count"] == result["result_count"]
    return {r["name"] for r in full["results"]}


# ---------------------------------------------------------------------------
# Seeded edges: one per receiver-evidence kind, nothing else in the way
# ---------------------------------------------------------------------------


class TestCallersOfHonoursReceiverEvidence:
    """Each branch of the evidence, pinned at the tool boundary."""

    def setup_method(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.root = Path(self.tmp_dir)
        self.pool_file = (self.root / "pool.py").as_posix()
        self.mod_file = (self.root / "runner.py").as_posix()
        self.use_file = (self.root / "use.py").as_posix()
        self.db_path = str(self.root / ".code-review-graph" / "graph.db")
        self._seed()

    def teardown_method(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _seed(self):
        with GraphStore(self.db_path) as store:
            store.upsert_node(NodeInfo(
                kind="Class", name="ConnectionPool", file_path=self.pool_file,
                line_start=1, line_end=9, language="python",
            ))
            store.upsert_node(NodeInfo(
                kind="Function", name="get", file_path=self.pool_file,
                line_start=2, line_end=4, language="python",
                parent_name="ConnectionPool",
            ))
            # A module-level ``get`` in another file, so the module branch has
            # something correct to find and the class branch has a decoy.
            store.upsert_node(NodeInfo(
                kind="Function", name="get", file_path=self.mod_file,
                line_start=1, line_end=3, language="python",
            ))
            for index, caller in enumerate((
                "dict_user", "pool_user", "module_user",
                "other_class_user", "unknown_user", "plain_user",
            )):
                store.upsert_node(NodeInfo(
                    kind="Function", name=caller, file_path=self.use_file,
                    line_start=10 + index * 5, line_end=12 + index * 5,
                    language="python",
                ))
            extras = {
                # ``cache = {}`` then ``cache.get(...)``
                "dict_user": {"receiver": "cache", "receiver_binding": "builtin"},
                # ``pool = ConnectionPool()`` then ``pool.get(...)``
                "pool_user": {
                    "receiver": "pool", "receiver_binding": "class",
                    "receiver_class": "ConnectionPool",
                },
                # ``import runner`` then ``runner.get(...)``
                "module_user": {
                    "receiver": "runner", "receiver_binding": "module",
                    "receiver_module": self.mod_file,
                },
                # ``other: Other = ...`` then ``other.get(...)``
                "other_class_user": {
                    "receiver": "other", "receiver_binding": "class",
                    "receiver_class": "Other",
                },
                # ``thing = make()`` then ``thing.get(...)``
                "unknown_user": {"receiver": "thing", "receiver_binding": "unknown"},
                # a plain unqualified ``get(...)`` with no receiver at all
                "plain_user": {},
            }
            for index, (caller, extra) in enumerate(extras.items()):
                store.upsert_edge(EdgeInfo(
                    kind="CALLS",
                    source=f"{self.use_file}::{caller}",
                    target="get",
                    file_path=self.use_file,
                    line=11 + index * 5,
                    extra=extra,
                ))
            store.commit()

    def test_builtin_receiver_is_not_a_caller(self):
        """``some_dict.get(...)`` is not a call into ``ConnectionPool.get``."""
        assert "dict_user" not in _callers(
            self.root, f"{self.pool_file}::ConnectionPool.get",
        )

    def test_matching_class_receiver_is_a_caller(self):
        assert "pool_user" in _callers(
            self.root, f"{self.pool_file}::ConnectionPool.get",
        )

    def test_other_class_receiver_is_not_a_caller(self):
        assert "other_class_user" not in _callers(
            self.root, f"{self.pool_file}::ConnectionPool.get",
        )

    def test_module_receiver_answers_only_the_module_level_name(self):
        """``runner.get(...)`` is ``runner.py::get`` and nothing else."""
        assert "module_user" in _callers(self.root, f"{self.mod_file}::get")
        assert "module_user" not in _callers(
            self.root, f"{self.pool_file}::ConnectionPool.get",
        )

    def test_unknown_receiver_attributes_nothing(self):
        """No evidence is not evidence for: the resolver's rule, applied here."""
        assert "unknown_user" not in _callers(
            self.root, f"{self.pool_file}::ConnectionPool.get",
        )
        assert "unknown_user" not in _callers(self.root, f"{self.mod_file}::get")

    def test_receiverless_call_keeps_the_established_fallback(self):
        """A plain ``get(...)`` is the case the bare-name fallback exists for."""
        pool_callers = _callers(
            self.root, f"{self.pool_file}::ConnectionPool.get",
        )
        assert "plain_user" in pool_callers
        assert pool_callers == {"pool_user", "plain_user"}

    def test_the_whole_answer_is_exactly_the_admitted_callers(self):
        assert _callers(self.root, f"{self.pool_file}::ConnectionPool.get") == {
            "pool_user", "plain_user",
        }
        assert _callers(self.root, f"{self.mod_file}::get") == {
            "module_user", "plain_user",
        }

    def test_minimal_detail_reports_the_same_constrained_total(self):
        """The count agents read is the constrained one, not the raw match."""
        result = query_graph(
            pattern="callers_of",
            target=f"{self.pool_file}::ConnectionPool.get",
            repo_root=str(self.root),
            detail_level="minimal",
            max_results=500,
        )
        assert result["result_count"] == 2
        assert result["results_omitted"] == 0


# ---------------------------------------------------------------------------
# The same rule over a real parse of a real repository
# ---------------------------------------------------------------------------


@pytest.fixture
def built_repo(tmp_path: Path) -> Path:
    """A repository whose ``.get``/``.start`` calls span every receiver kind."""
    repo = tmp_path / "repo"
    package = repo / "app"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "pool.py").write_text(
        "class ConnectionPool:\n"
        "    def get(self, key):\n"
        "        return key\n",
        encoding="utf-8",
    )
    (package / "handler.py").write_text(
        "class Handler:\n"
        "    def start(self):\n"
        "        return 1\n"
        "\n"
        "\n"
        "def make_handler():\n"
        "    return Handler()\n",
        encoding="utf-8",
    )
    (package / "unique.py").write_text(
        "def sanitize_label(value):\n"
        "    return value.replace('x', 'y')\n",
        encoding="utf-8",
    )
    # A top-level function whose bare name a builtin method also answers to.
    # ``tests_for`` looks a bare TESTED_BY source up by the qualified tail, so
    # only a module-level name reaches that fallback at all.
    (package / "textutil.py").write_text(
        "def strip(value):\n"
        "    return value\n",
        encoding="utf-8",
    )
    (package / "caller.py").write_text(
        "from app.handler import Handler, make_handler\n"
        "from app.pool import ConnectionPool\n"
        "from app.unique import sanitize_label\n"
        "\n"
        "\n"
        "def reads_a_dict(config):\n"
        "    cache = {}\n"
        "    return cache.get('key')\n"
        "\n"
        "\n"
        "def reads_the_pool():\n"
        "    pool = ConnectionPool()\n"
        "    return pool.get('key')\n"
        "\n"
        "\n"
        "def starts_a_handler():\n"
        "    handler = Handler()\n"
        "    return handler.start()\n"
        "\n"
        "\n"
        "def starts_an_unknown():\n"
        "    handler = make_handler()\n"
        "    return handler.start()\n"
        "\n"
        "\n"
        "def labels():\n"
        "    return sanitize_label(' x ')\n",
        encoding="utf-8",
    )
    (repo / "tests").mkdir()
    (repo / "tests" / "test_app.py").write_text(
        "from app.pool import ConnectionPool\n"
        "\n"
        "\n"
        "def test_pool_get():\n"
        "    pool = ConnectionPool()\n"
        "    assert pool.get('k') == 'k'\n"
        "\n"
        "\n"
        "def test_a_dict_instead():\n"
        "    payload = {}\n"
        "    assert payload.get('k') is None\n",
        encoding="utf-8",
    )
    (repo / "tests" / "test_text.py").write_text(
        "from app.textutil import strip\n"
        "\n"
        "\n"
        "def test_strip_directly():\n"
        "    assert strip('a') == 'a'\n"
        "\n"
        "\n"
        "def test_a_str_instead():\n"
        "    value = '  a  '\n"
        "    assert value.strip() == 'a'\n",
        encoding="utf-8",
    )
    db_path = repo / ".code-review-graph" / "graph.db"
    store = GraphStore(db_path)
    try:
        full_build(repo, store)
        run_post_processing(store)
    finally:
        store.close()
    return repo


def _qn(repo: Path, relative: str, symbol: str) -> str:
    return f"{(repo / relative).resolve().as_posix()}::{symbol}"


def test_a_dict_get_is_not_a_caller_of_connection_pool_get(built_repo):
    callers = _callers(
        built_repo, _qn(built_repo, "app/pool.py", "ConnectionPool.get"),
    )
    assert "reads_the_pool" in callers
    assert "reads_a_dict" not in callers


def test_a_factory_bound_receiver_is_not_guessed_into_a_caller(built_repo):
    """An unannotated factory leaves no evidence, so it produces no caller.

    This is a deliberate recall cost, and the same trade the resolver makes:
    ``handler = make_handler()`` says nothing about what ``handler`` holds,
    and a guess here is how ``thread.start()`` became a call into
    ``GraphUpdateHandler.start``.
    """
    callers = _callers(
        built_repo, _qn(built_repo, "app/handler.py", "Handler.start"),
    )
    assert "starts_a_handler" in callers
    assert "starts_an_unknown" not in callers


def test_a_uniquely_named_plain_call_keeps_its_caller(built_repo):
    """The control: the constraint must not cost an unambiguous name."""
    assert _callers(
        built_repo, _qn(built_repo, "app/unique.py", "sanitize_label"),
    ) == {"labels"}


def test_tests_for_does_not_count_a_builtin_receiver_as_coverage(built_repo):
    """``'  a  '.strip()`` in a test does not test ``textutil.strip``.

    ``TESTED_BY`` is minted from the test's own ``CALLS`` edge and carries its
    metadata, so the bare-source fallback in ``get_transitive_tests`` reads
    the same receiver evidence the call target does.
    """
    result = query_graph(
        pattern="tests_for",
        target=_qn(built_repo, "app/textutil.py", "strip"),
        repo_root=str(built_repo),
        detail_level="standard",
        max_results=500,
    )
    assert result["status"] == "ok", result
    names = {r["name"] for r in result["results"]}
    assert "test_strip_directly" in names
    assert "test_a_str_instead" not in names
