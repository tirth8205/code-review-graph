"""Scoped/static ``Class::method`` calls are tracked as callers in PHP (#567).

A PHP call written ``Mailer::dispatch($x)`` used to store a ``CALLS`` edge whose
target was the intermediate string ``Mailer::dispatch``.  That key matched
neither the canonical node name (``<file>::Mailer.dispatch``) nor a bare method
name, so ``callers_of`` / ``get_impact_radius`` reported zero callers.  The
post-build scoped resolver rewrites the resolvable ones to the defining node.
"""

from __future__ import annotations

import json
from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, incremental_update
from code_review_graph.scoped_resolver import _path_tokens, resolve_scoped_calls
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


def _build_phpunit_calculator(tmp_path: Path) -> GraphStore:
    """Build the exact two-file PHPUnit reproduction from issue #745."""
    return _build(
        tmp_path,
        {
            "src/Calculator.php": (
                "<?php\n"
                "namespace App;\n"
                "\n"
                "class Calculator\n"
                "{\n"
                "    public function add(int $a, int $b): int\n"
                "    {\n"
                "        return $a + $b;\n"
                "    }\n"
                "}\n"
            ),
            "tests/CalculatorTest.php": (
                "<?php\n"
                "namespace App\\Tests;\n"
                "\n"
                "use App\\Calculator;\n"
                "use PHPUnit\\Framework\\TestCase;\n"
                "\n"
                "class CalculatorTest extends TestCase\n"
                "{\n"
                "    public function testItAddsTwoNumbers(): void\n"
                "    {\n"
                "        $calculator = new Calculator();\n"
                "        $this->assertSame(3, $calculator->add(1, 2));\n"
                "    }\n"
                "}\n"
            ),
        },
    )


def test_phpunit_instance_call_creates_canonical_tested_by_edge(
    tmp_path: Path,
) -> None:
    store = _build_phpunit_calculator(tmp_path)
    test_node = store._conn.execute(
        "SELECT qualified_name, kind, is_test FROM nodes "
        "WHERE name = 'testItAddsTwoNumbers'"
    ).fetchone()
    add_node = store._conn.execute(
        "SELECT qualified_name FROM nodes "
        "WHERE name = 'add' AND parent_name = 'Calculator'"
    ).fetchone()

    assert dict(test_node) == {
        "qualified_name": (
            f"{tmp_path}/tests/CalculatorTest.php"
            "::CalculatorTest.testItAddsTwoNumbers"
        ),
        "kind": "Test",
        "is_test": 1,
    }
    call = store._conn.execute(
        "SELECT target_qualified, extra, confidence_tier FROM edges "
        "WHERE kind = 'CALLS' AND source_qualified = ? AND target_qualified = ?",
        (test_node["qualified_name"], add_node["qualified_name"]),
    ).fetchone()
    tested_by = store._conn.execute(
        "SELECT source_qualified, target_qualified, confidence_tier FROM edges "
        "WHERE kind = 'TESTED_BY' AND source_qualified = ? AND target_qualified = ?",
        (add_node["qualified_name"], test_node["qualified_name"]),
    ).fetchone()

    assert call is not None
    assert call["confidence_tier"] == "INFERRED"
    assert json.loads(call["extra"]) == {
        "receiver": "$calculator",
        "receiver_resolution": "constructed_receiver",
        "receiver_scope": "App\\Calculator",
        "receiver_type": "Calculator",
        "scoped_resolved": True,
        "scoped_via": "single_match",
    }
    assert tested_by is not None
    assert tested_by["confidence_tier"] == "INFERRED"


def test_phpunit_instance_call_is_visible_through_public_tests_for(
    tmp_path: Path,
) -> None:
    store = _build_phpunit_calculator(tmp_path)
    add_qn = store._conn.execute(
        "SELECT qualified_name FROM nodes "
        "WHERE name = 'add' AND parent_name = 'Calculator'"
    ).fetchone()["qualified_name"]

    method_result = query_graph("tests_for", add_qn, repo_root=str(tmp_path))
    file_result = query_graph(
        "tests_for",
        "src/Calculator.php",
        repo_root=str(tmp_path),
    )

    assert method_result["status"] == "ok"
    assert [
        (result["name"], result["indirect"])
        for result in method_result["results"]
    ] == [("testItAddsTwoNumbers", False)]
    assert file_result["status"] == "ok"
    assert [
        (result["name"], result["indirect"])
        for result in file_result["results"]
    ] == [("testItAddsTwoNumbers", False)]


def test_php_instance_call_resolves_constructor_import_alias(
    tmp_path: Path,
) -> None:
    store = _build(
        tmp_path,
        {
            "src/Calculator.php": (
                "<?php\n"
                "namespace App;\n"
                "class Calculator {\n"
                "    public function add(int $a, int $b): int { return $a + $b; }\n"
                "}\n"
            ),
            "tests/CalculatorTest.php": (
                "<?php\n"
                "namespace App\\Tests;\n"
                "use App\\Calculator as MathCalculator;\n"
                "class CalculatorTest {\n"
                "    public function testAlias(): void {\n"
                "        $calculator = new MathCalculator();\n"
                "        $calculator->add(1, 2);\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    target = store._conn.execute(
        "SELECT qualified_name FROM nodes "
        "WHERE name = 'add' AND parent_name = 'Calculator'"
    ).fetchone()["qualified_name"]

    call = store._conn.execute(
        "SELECT extra FROM edges "
        "WHERE kind = 'CALLS' AND target_qualified = ?",
        (target,),
    ).fetchone()

    assert json.loads(call["extra"])["receiver_type"] == "MathCalculator"
    assert json.loads(call["extra"])["scoped_resolved"] is True


def test_php_reassignment_invalidates_constructed_receiver_type(
    tmp_path: Path,
) -> None:
    store = _build(
        tmp_path,
        {
            "src/Calculator.php": (
                "<?php\n"
                "class Calculator {\n"
                "    public function add(int $a, int $b): int { return $a + $b; }\n"
                "}\n"
            ),
            "tests/CalculatorTest.php": (
                "<?php\n"
                "class CalculatorTest {\n"
                "    public function testReassigned(): void {\n"
                "        $calculator = new Calculator();\n"
                "        $calculator = makeCalculator();\n"
                "        $calculator->add(1, 2);\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    canonical_target = store._conn.execute(
        "SELECT qualified_name FROM nodes "
        "WHERE name = 'add' AND parent_name = 'Calculator'"
    ).fetchone()["qualified_name"]

    assert store._conn.execute(
        "SELECT 1 FROM edges "
        "WHERE kind = 'CALLS' AND target_qualified = ?",
        (canonical_target,),
    ).fetchone() is None
    assert store._conn.execute(
        "SELECT 1 FROM edges "
        "WHERE kind = 'CALLS' AND target_qualified = 'add'",
    ).fetchone() is not None


def test_path_tokens_normalizes_windows_separators() -> None:
    assert _path_tokens(r"C:\repo\src\Order\Queue\Mailer.php") == [
        "C:",
        "repo",
        "src",
        "Order",
        "Queue",
        "Mailer",
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


def test_windows_resolved_import_path_disambiguates_same_named_methods(
    tmp_path: Path,
) -> None:
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
                "use App\\Unknown\\Mailer;\n"
                "class Ctrl {\n"
                "    public function reg($e) { return Mailer::dispatch($e); }\n"
                "}\n"
            ),
        },
    )
    queue_file = store._conn.execute(
        "SELECT file_path FROM nodes "
        "WHERE name = 'dispatch' AND file_path LIKE '%/Queue/Mailer.php'"
    ).fetchone()["file_path"]
    store._conn.execute(
        "UPDATE edges SET target_qualified = ? WHERE kind = 'IMPORTS_FROM'",
        (queue_file.replace("/", "\\"),),
    )
    store._conn.commit()

    stats = resolve_scoped_calls(store)
    assert stats["calls_resolved"] == 1
    resolved = [
        call
        for call in _calls(store)
        if call["confidence_tier"] == "INFERRED"
    ]
    assert len(resolved) == 1
    assert "Queue/Mailer.php::Mailer.dispatch" in resolved[0]["target_qualified"]


def test_php_method_matching_is_case_insensitive(tmp_path: Path) -> None:
    # PHP class/function names are case-insensitive, so a differently-cased call
    # still resolves to the same definition.
    _build(
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
                "    public function reg($e) { return MAILER::Dispatch($e); }\n"
                "}\n"
            ),
        },
    )
    result = query_graph("callers_of", "dispatch", repo_root=str(tmp_path))
    assert result["status"] == "ok"
    assert [r["name"] for r in result["results"]] == ["reg"]


def test_unrelated_import_namespace_does_not_resolve(tmp_path: Path) -> None:
    # Two same-named classes in different namespaces; the caller imports a third
    # namespace matching neither, so the ambiguous call stays unresolved rather
    # than picking an unrelated same-named definition on a single shared segment.
    store = _build(
        tmp_path,
        {
            "src/Billing/Mailer.php": (
                "<?php\n"
                "namespace App\\Billing;\n"
                "class Mailer {\n"
                "    public static function dispatch($to) { return 1; }\n"
                "}\n"
            ),
            "src/Shipping/Mailer.php": (
                "<?php\n"
                "namespace App\\Shipping;\n"
                "class Mailer {\n"
                "    public static function dispatch($to) { return 2; }\n"
                "}\n"
            ),
            "src/Http/Ctrl.php": (
                "<?php\n"
                "namespace App\\Http;\n"
                "use App\\Warehouse\\Mailer;\n"
                "class Ctrl {\n"
                "    public function reg($e) { return Mailer::dispatch($e); }\n"
                "}\n"
            ),
        },
    )
    assert not any(c["confidence_tier"] == "INFERRED" for c in _calls(store))
    dangling = [c for c in _calls(store) if c["target_qualified"] == "Mailer::dispatch"]
    assert len(dangling) == 1


def test_partial_suffix_from_unrelated_namespace_does_not_resolve(
    tmp_path: Path,
) -> None:
    """A coincidental Queue/Mailer suffix is not proof of the imported namespace."""
    store = _build(
        tmp_path,
        {
            "src/Order/Queue/Mailer.php": (
                "<?php\n"
                "namespace App\\Order\\Queue;\n"
                "class Mailer {\n"
                "    public static function dispatch($to) { return 1; }\n"
                "}\n"
            ),
            "src/Billing/Legacy/Mailer.php": (
                "<?php\n"
                "namespace App\\Billing\\Legacy;\n"
                "class Mailer {\n"
                "    public static function dispatch($to) { return 2; }\n"
                "}\n"
            ),
            "src/Http/Ctrl.php": (
                "<?php\n"
                "namespace App\\Http;\n"
                "use App\\Warehouse\\Queue\\Mailer;\n"
                "class Ctrl {\n"
                "    public function reg($e) { return Mailer::dispatch($e); }\n"
                "}\n"
            ),
        },
    )

    assert not any(c["confidence_tier"] == "INFERRED" for c in _calls(store))
    dangling = [
        c for c in _calls(store) if c["target_qualified"] == "Mailer::dispatch"
    ]
    assert len(dangling) == 1


def test_import_suffix_match_selects_correct_namespace(tmp_path: Path) -> None:
    # A deep import path must select by the full path suffix, not a single
    # shared middle segment: ``App\Order\Queue\Mailer`` picks Order/Queue/Mailer
    # over an unrelated Queue/Mailer that only shares the ``Queue`` segment.
    store = _build(
        tmp_path,
        {
            "src/Queue/Mailer.php": (
                "<?php\n"
                "namespace App\\Queue;\n"
                "class Mailer {\n"
                "    public static function dispatch($to) { return 1; }\n"
                "}\n"
            ),
            "src/Order/Queue/Mailer.php": (
                "<?php\n"
                "namespace App\\Order\\Queue;\n"
                "class Mailer {\n"
                "    public static function dispatch($to) { return 2; }\n"
                "}\n"
            ),
            "src/Http/Ctrl.php": (
                "<?php\n"
                "namespace App\\Http;\n"
                "use App\\Order\\Queue\\Mailer;\n"
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
    assert "Order/Queue/Mailer" in resolved[0]["target_qualified"]


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
