"""Scoped/static ``Class::method`` calls are tracked as callers in PHP (#567).

A PHP call written ``Mailer::dispatch($x)`` used to store a ``CALLS`` edge whose
target was the intermediate string ``Mailer::dispatch``.  That key matched
neither the canonical node name (``<file>::Mailer.dispatch``) nor a bare method
name, so ``callers_of`` / ``get_impact_radius`` reported zero callers.  The
post-build scoped resolver rewrites the resolvable ones to the defining node.
"""

from __future__ import annotations

from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, incremental_update
from code_review_graph.tools.query import get_impact_radius, query_graph


def _build(tmp_path: Path, files: dict[str, str]) -> GraphStore:
    for rel, source in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    graph_dir = tmp_path / ".code-review-graph"
    graph_dir.mkdir(exist_ok=True)
    store = GraphStore(graph_dir / "graph.db")
    full_build(tmp_path, store)
    return store


def _calls(store: GraphStore) -> list[dict]:
    return [
        dict(row)
        for row in store._conn.execute(
            "SELECT source_qualified, target_qualified, confidence_tier "
            "FROM edges WHERE kind = 'CALLS'"
        ).fetchall()
    ]


def test_cross_file_scoped_call_makes_caller_visible(tmp_path: Path) -> None:
    _build(
        tmp_path,
        {
            "src/Mailer.php": (
                "<?php\n"
                "class Mailer {\n"
                "    public static function dispatch($to) { return true; }\n"
                "}\n"
            ),
            "src/SignupController.php": (
                "<?php\n"
                "class SignupController {\n"
                "    public function register($email) {\n"
                "        return Mailer::dispatch($email);\n"
                "    }\n"
                "}\n"
            ),
        },
    )

    result = query_graph("callers_of", "dispatch", repo_root=str(tmp_path))
    assert result["status"] == "ok"
    assert [r["name"] for r in result["results"]] == ["register"]
    assert result["results"][0]["parent_name"] == "SignupController"


def test_scoped_call_edge_is_tagged_inferred(tmp_path: Path) -> None:
    store = _build(
        tmp_path,
        {
            "src/Mailer.php": (
                "<?php\n"
                "class Mailer {\n"
                "    public static function dispatch($to) { return true; }\n"
                "}\n"
            ),
            "src/Ctrl.php": (
                "<?php\n"
                "class Ctrl {\n"
                "    public function reg($e) { return Mailer::dispatch($e); }\n"
                "}\n"
            ),
        },
    )
    calls = [c for c in _calls(store) if c["target_qualified"].endswith("Mailer.dispatch")]
    assert len(calls) == 1
    assert calls[0]["confidence_tier"] == "INFERRED"
    assert "::" in calls[0]["target_qualified"]
    assert calls[0]["target_qualified"].endswith("::Mailer.dispatch")


def test_impact_radius_of_definition_file_includes_caller(tmp_path: Path) -> None:
    _build(
        tmp_path,
        {
            "src/Mailer.php": (
                "<?php\n"
                "class Mailer {\n"
                "    public static function dispatch($to) { return true; }\n"
                "}\n"
            ),
            "src/SignupController.php": (
                "<?php\n"
                "class SignupController {\n"
                "    public function register($email) {\n"
                "        return Mailer::dispatch($email);\n"
                "    }\n"
                "}\n"
            ),
        },
    )

    impact = get_impact_radius(
        changed_files=["src/Mailer.php"], repo_root=str(tmp_path)
    )
    assert impact["status"] == "ok"
    impacted = {n["name"] for n in impact["impacted_nodes"]}
    assert "register" in impacted


def test_namespaced_scoped_call_with_use_import_resolves(tmp_path: Path) -> None:
    _build(
        tmp_path,
        {
            "src/Mail/Mailer.php": (
                "<?php\n"
                "namespace App\\Mail;\n"
                "class Mailer {\n"
                "    public static function dispatch($to) { return true; }\n"
                "}\n"
            ),
            "src/Http/Ctrl.php": (
                "<?php\n"
                "namespace App\\Http;\n"
                "use App\\Mail\\Mailer;\n"
                "class Ctrl {\n"
                "    public function reg($e) { return Mailer::dispatch($e); }\n"
                "}\n"
            ),
        },
    )

    result = query_graph("callers_of", "dispatch", repo_root=str(tmp_path))
    assert result["status"] == "ok"
    assert [r["name"] for r in result["results"]] == ["reg"]


def test_ambiguous_same_named_methods_disambiguated_by_import(tmp_path: Path) -> None:
    # Two different classes both define ``dispatch``; only the imported one
    # should become the caller target.
    store = _build(
        tmp_path,
        {
            "src/Mail/Mailer.php": (
                "<?php\n"
                "namespace App\\Mail;\n"
                "class Mailer {\n"
                "    public static function dispatch($to) { return 1; }\n"
                "}\n"
            ),
            "src/Queue/Mailer.php": (
                "<?php\n"
                "namespace App\\Queue;\n"
                "class Mailer {\n"
                "    public static function dispatch($to) { return 2; }\n"
                "}\n"
            ),
            "src/Http/Ctrl.php": (
                "<?php\n"
                "namespace App\\Http;\n"
                "use App\\Queue\\Mailer;\n"
                "class Ctrl {\n"
                "    public function reg($e) { return Mailer::dispatch($e); }\n"
                "}\n"
            ),
        },
    )

    resolved = [
        c for c in _calls(store)
        if c["confidence_tier"] == "INFERRED" and "dispatch" in c["target_qualified"]
    ]
    assert len(resolved) == 1
    # Disambiguated to the imported Queue\Mailer, not Mail\Mailer.
    assert "Queue" in resolved[0]["target_qualified"]
    assert "Mail/Mailer" not in resolved[0]["target_qualified"]


def test_unresolved_external_scoped_call_is_left_untouched(tmp_path: Path) -> None:
    # ``Redis`` is not defined anywhere in the graph — the edge must stay a
    # raw, directly-extracted target and must not fabricate a resolved caller.
    store = _build(
        tmp_path,
        {
            "src/Cache.php": (
                "<?php\n"
                "class Cache {\n"
                "    public function warm($k) { return Redis::get($k); }\n"
                "}\n"
            ),
        },
    )
    external = [c for c in _calls(store) if c["target_qualified"] == "Redis::get"]
    assert len(external) == 1
    assert external[0]["confidence_tier"] == "EXTRACTED"

    result = query_graph("callers_of", "get", repo_root=str(tmp_path))
    # No node named ``get`` exists, so there is nothing to (falsely) resolve.
    assert result["status"] in ("not_found", "ok")
    if result["status"] == "ok":
        assert result["results"] == []


def test_incremental_update_reresolves_scoped_call(tmp_path: Path) -> None:
    store = _build(
        tmp_path,
        {
            "src/Mailer.php": (
                "<?php\n"
                "class Mailer {\n"
                "    public static function dispatch($to) { return true; }\n"
                "}\n"
            ),
            "src/Ctrl.php": (
                "<?php\n"
                "class Ctrl {\n"
                "    public function reg($e) { return 0; }\n"
                "}\n"
            ),
        },
    )
    # Initially Ctrl does not call Mailer.
    assert query_graph("callers_of", "dispatch", repo_root=str(tmp_path))["results"] == []

    (tmp_path / "src/Ctrl.php").write_text(
        "<?php\n"
        "class Ctrl {\n"
        "    public function reg($e) { return Mailer::dispatch($e); }\n"
        "}\n",
        encoding="utf-8",
    )
    incremental_update(tmp_path, store, changed_files=["src/Ctrl.php"])

    result = query_graph("callers_of", "dispatch", repo_root=str(tmp_path))
    assert [r["name"] for r in result["results"]] == ["reg"]


def test_resolver_is_idempotent(tmp_path: Path) -> None:
    from code_review_graph.scoped_resolver import resolve_scoped_calls

    store = _build(
        tmp_path,
        {
            "src/Mailer.php": (
                "<?php\n"
                "class Mailer {\n"
                "    public static function dispatch($to) { return true; }\n"
                "}\n"
            ),
            "src/Ctrl.php": (
                "<?php\n"
                "class Ctrl {\n"
                "    public function reg($e) { return Mailer::dispatch($e); }\n"
                "}\n"
            ),
        },
    )
    before = _calls(store)
    # A second pass must not resolve anything further or change targets.
    stats = resolve_scoped_calls(store)
    assert stats["calls_resolved"] == 0
    assert _calls(store) == before
