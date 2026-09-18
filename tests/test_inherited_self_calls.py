"""``this.method()`` belongs to the ancestor that declares the method.

For a ``this.`` call the parser builds ``<file>::<EnclosingClass>.<method>``.
That is right when the class declares the method and wrong when it inherits
it: the target then names no node at all, so the edge points at a phantom
while ``callers_of`` on the real declaration returns zero.

``GraphStore.resolve_inherited_self_calls`` walks ``INHERITS`` upwards and
retargets when **exactly one** ancestor declares the method. It is deliberately
conservative, so the tests here pin both directions: the calls that must land
on one specific node, and the calls that must be left alone rather than
retargeted to a plausible-looking wrong one.

See: #984
"""

import json
from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.parser import CodeParser

# --------------------------------------------------------------------------
# Fixture repository — the shape this pass exists for.
#
#   base.ts       class HttpService            _onHttpError(), _log()
#   api.ts        abstract class ApiService<U> extends HttpService
#   export.ts     class ExportService extends HttpService      1 this. call
#   import.ts     class ImportService extends ApiService<T>     1 this. call
#
# The bases here are deliberately plain classes, so that everything except the
# two-level case exercises this pass alone. The intermediate is `abstract`,
# which is the real-world shape and needs the abstract-class fix (#1031/#1033)
# to be indexed at all; the two tests that depend on it say so.
# --------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    (root / "base.ts").write_text(
        "export class HttpService {\n"
        "  protected _onHttpError(error: string): string {\n"
        "    return error;\n"
        "  }\n\n"
        "  protected _log(message: string): void {\n"
        "    console.log(message);\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    (root / "api.ts").write_text(
        "import { HttpService } from './base';\n\n"
        "export abstract class ApiService<U> extends HttpService {\n"
        "  protected unwrap(payload: U): U {\n"
        "    return payload;\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    (root / "export.ts").write_text(
        "import { HttpService } from './base';\n\n"
        "export class ExportService extends HttpService {\n"
        "  run(): string {\n"
        "    return this._onHttpError('boom');\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    (root / "import.ts").write_text(
        "import { ApiService } from './api';\n\n"
        "export class ImportService extends ApiService<string> {\n"
        "  load(): string {\n"
        "    return this._onHttpError('boom');\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    return root


def _write(repo_root: Path, relative: str, source: str) -> Path:
    path = repo_root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def _resolved_store(repo_root: Path, tmp_path: Path) -> GraphStore:
    """Parse every file under *repo_root* and run the inherited-self pass."""
    store = GraphStore(tmp_path / "graph.db")
    parser = CodeParser(repo_root=repo_root)
    for pattern in ("*.ts", "*.tsx", "*.java"):
        for path in sorted(repo_root.rglob(pattern)):
            nodes, edges = parser.parse_file(path)
            for node in nodes:
                store.upsert_node(node)
            for edge in edges:
                store.upsert_edge(edge)
    store.commit()
    store.resolve_inherited_self_calls()
    return store


def _posix(path: Path) -> str:
    return path.resolve().as_posix()


def _call_edges(store: GraphStore, caller_file: Path) -> list[dict]:
    rows = store._conn.execute(
        "SELECT target_qualified, confidence_tier, extra FROM edges "
        "WHERE kind = 'CALLS' AND file_path = ?",
        (_posix(caller_file),),
    ).fetchall()
    return [
        {
            "target": row["target_qualified"],
            "tier": row["confidence_tier"],
            "extra": json.loads(row["extra"] or "{}"),
        }
        for row in rows
    ]


# --------------------------------------------------------------------------
# What must resolve
# --------------------------------------------------------------------------


def test_one_level_inherited_call_lands_on_the_declaring_base(repo, tmp_path):
    """``this._onHttpError()`` in a direct subclass names the base's method."""
    store = _resolved_store(repo, tmp_path)
    try:
        edges = _call_edges(store, repo / "export.ts")
        targets = {e["target"] for e in edges}
        assert f"{_posix(repo / 'base.ts')}::HttpService._onHttpError" in targets
        # and NOT the phantom the parser first built
        assert (
            f"{_posix(repo / 'export.ts')}::ExportService._onHttpError"
            not in targets
        )
    finally:
        store.close()


def test_two_level_inherited_call_walks_through_the_intermediate(repo, tmp_path):
    """The declaration is two levels up, through a generic abstract class.

    Needs the abstract-class fix (#1031/#1033): without it ``ApiService`` has no
    node, the INHERITS chain has a hole, and the walk cannot reach the base.
    """
    store = _resolved_store(repo, tmp_path)
    try:
        targets = {e["target"] for e in _call_edges(store, repo / "import.ts")}
        assert f"{_posix(repo / 'base.ts')}::HttpService._onHttpError" in targets
    finally:
        store.close()


def test_a_retargeted_edge_is_labelled_inferred(repo, tmp_path):
    """The pass reasons from the hierarchy, so it must not claim EXTRACTED."""
    store = _resolved_store(repo, tmp_path)
    try:
        edge = next(
            e
            for e in _call_edges(store, repo / "export.ts")
            if e["target"].endswith("HttpService._onHttpError")
        )
        assert edge["tier"] == "INFERRED"
        assert edge["extra"]["inherited_self_resolution"] == "ancestor"
    finally:
        store.close()


def test_the_pass_reports_how_many_edges_it_moved(repo, tmp_path):
    """One call site in the plain hierarchy, and the pass is idempotent.

    Only ``base.ts`` and ``export.ts`` are parsed here, so the count does not
    depend on the abstract intermediate being indexed.
    """
    store = GraphStore(tmp_path / "graph.db")
    parser = CodeParser(repo_root=repo)
    for path in (repo / "base.ts", repo / "export.ts"):
        nodes, edges = parser.parse_file(path)
        for node in nodes:
            store.upsert_node(node)
        for edge in edges:
            store.upsert_edge(edge)
    store.commit()
    try:
        assert store.resolve_inherited_self_calls() == 1
        # Idempotent: the target now names a real node, so nothing is left.
        assert store.resolve_inherited_self_calls() == 0
    finally:
        store.close()


# --------------------------------------------------------------------------
# What must be left alone
# --------------------------------------------------------------------------


def test_a_method_the_class_declares_itself_is_untouched(repo, tmp_path):
    """An override calling ``this.method()`` already names a real node."""
    caller = _write(
        repo,
        "own.ts",
        "import { HttpService } from './base';\n\n"
        "export class OwnService extends HttpService {\n"
        "  protected _onHttpError(error: string): string {\n"
        "    return error;\n"
        "  }\n\n"
        "  run(): string {\n"
        "    return this._onHttpError('boom');\n"
        "  }\n"
        "}\n",
    )
    store = _resolved_store(repo, tmp_path)
    try:
        edge = next(
            e
            for e in _call_edges(store, caller)
            if e["target"].endswith("._onHttpError")
        )
        assert edge["target"] == f"{_posix(caller)}::OwnService._onHttpError"
        assert "inherited_self_resolution" not in edge["extra"]
    finally:
        store.close()


def test_an_ambiguous_ancestor_name_resolves_to_nothing(repo, tmp_path):
    """Two same-named bases both declaring the method: no honest answer exists.

    The walk matches ancestors by simple class name, so a homonym is the case
    where it must decline rather than pick one.
    """
    _write(
        repo,
        "other/base.ts",
        "export class HttpService {\n"
        "  protected _onHttpError(error: string): string {\n"
        "    return 'other';\n"
        "  }\n"
        "}\n",
    )
    caller = _write(
        repo,
        "ambiguous.ts",
        "import { HttpService } from './other/base';\n\n"
        "export class AmbiguousService extends HttpService {\n"
        "  run(): string {\n"
        "    return this._onHttpError('boom');\n"
        "  }\n"
        "}\n",
    )
    store = _resolved_store(repo, tmp_path)
    try:
        edge = next(
            e
            for e in _call_edges(store, caller)
            if e["target"].endswith("._onHttpError")
        )
        assert (
            edge["target"]
            == f"{_posix(caller)}::AmbiguousService._onHttpError"
        )
        assert "inherited_self_resolution" not in edge["extra"]
    finally:
        store.close()


def test_a_call_on_another_object_is_not_a_self_call(repo, tmp_path):
    """The pass keys on the receiver, not on the method name."""
    caller = _write(
        repo,
        "holder.ts",
        "import { ExportService } from './export';\n\n"
        "export class Holder {\n"
        "  constructor(private inner: ExportService) {}\n\n"
        "  go(): string {\n"
        "    return this.inner.run();\n"
        "  }\n"
        "}\n",
    )
    store = _resolved_store(repo, tmp_path)
    try:
        for edge in _call_edges(store, caller):
            assert "inherited_self_resolution" not in edge["extra"]
    finally:
        store.close()


def test_an_unrelated_class_does_not_inherit_the_method(repo, tmp_path):
    """No INHERITS edge means no ancestor to walk to."""
    caller = _write(
        repo,
        "standalone.ts",
        "export class Standalone {\n"
        "  run(): string {\n"
        "    return this._onHttpError('boom');\n"
        "  }\n"
        "}\n",
    )
    store = _resolved_store(repo, tmp_path)
    try:
        for edge in _call_edges(store, caller):
            assert not edge["target"].startswith(_posix(repo / "base.ts"))
            assert "inherited_self_resolution" not in edge["extra"]
    finally:
        store.close()


# --------------------------------------------------------------------------
# Wiring
#
# The pass lives in graph.py and is called from postprocessing.py and from
# both paths in tools/build.py. Landing the method without a caller is a
# silent no-op; landing a caller without the method raises AttributeError on
# every build. Either way the graph and the code disagree, so the wiring is
# worth a test of its own.
# --------------------------------------------------------------------------


def test_post_processing_runs_the_pass_and_reports_it(repo, tmp_path):
    """Both call sites resolve through the public post-processing entry point.

    The count of 2 includes the two-level case, so this one also needs
    #1031/#1033.
    """
    from code_review_graph.postprocessing import run_post_processing

    store = GraphStore(tmp_path / "graph.db")
    parser = CodeParser(repo_root=repo)
    for path in sorted(repo.rglob("*.ts")):
        nodes, edges = parser.parse_file(path)
        for node in nodes:
            store.upsert_node(node)
        for edge in edges:
            store.upsert_edge(edge)
    store.commit()

    try:
        result = run_post_processing(store)

        assert result.get("warnings", []) == []
        assert result["inherited_self_edges_resolved"] == 2
        # The count is its own key: these targets were qualified but phantom,
        # not bare, so the bare-target counter must not absorb them. Nothing
        # in this fixture has a bare target at all.
        assert result["bare_edges_resolved"] == 0
        targets = {e["target"] for e in _call_edges(store, repo / "export.ts")}
        assert f"{_posix(repo / 'base.ts')}::HttpService._onHttpError" in targets
    finally:
        store.close()
