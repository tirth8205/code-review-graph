import json
import re
from pathlib import Path

from code_review_graph.aop_resolver import (
    _aspectj_pattern_to_regex,
    _parse_execution_expression,
    resolve_aop_advice,
)
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, incremental_update
from code_review_graph.parser import CodeParser, EdgeInfo
from code_review_graph.tools.query import query_graph

ANNOTATION_SOURCE = """
import org.aspectj.lang.annotation.Aspect;
import org.aspectj.lang.annotation.Before;

@Aspect
class AccessLimitAspect {
    @Before("@annotation(com.foo.common.annotation.AccessLimit)")
    void beforeLimit() {}
}

class OrderController {
    @AccessLimit
    void placeOrder() {}

    void unrelatedMethod() {}
}
"""

EXECUTION_SOURCE = """
package com.foo.service;

import org.aspectj.lang.annotation.Aspect;
import org.aspectj.lang.annotation.Around;

@Aspect
class LoggingAspect {
    @Around("execution(* com.foo.service.PaymentService.*(..))")
    Object logAround() { return null; }
}

class PaymentService {
    void pay() {}
}

class OtherService {
    void unrelated() {}
}
"""

NAMED_POINTCUT_SOURCE = """
import org.aspectj.lang.annotation.Aspect;
import org.aspectj.lang.annotation.Pointcut;
import org.aspectj.lang.annotation.Before;
import org.aspectj.lang.annotation.AfterThrowing;

@Aspect
class AccessLimitAspect {
    @Pointcut("@annotation(com.foo.common.annotation.AccessLimit)")
    void accessLimitPointcut() {}

    @Before("accessLimitPointcut()")
    void before(JoinPoint jp) {}

    @AfterThrowing(pointcut = "accessLimitPointcut()", throwing = "ex")
    void afterThrowing(JoinPoint jp, Throwable ex) {}
}

class OrderController {
    @AccessLimit
    void placeOrder() {}
}
"""

COMPOUND_SOURCE = """
import org.aspectj.lang.annotation.Aspect;
import org.aspectj.lang.annotation.Before;

@Aspect
class SecurityAspect {
    @Before("within(com.foo.service..*) && !execution(* com.foo.service.internal.*.*(..))")
    void checkSecurity() {}
}

class SomeService {
    void doWork() {}
}
"""

PACKAGE_WILDCARD_SERVICE_SOURCE = """
package com.foo.service;

import org.aspectj.lang.annotation.Aspect;
import org.aspectj.lang.annotation.Around;

@Aspect
class LoggingAspect {
    @Around("execution(* com.foo.service.*.*(..))")
    Object logAround() { return null; }
}

class PaymentService {
    void pay() {}
}
"""

PACKAGE_WILDCARD_REPO_SOURCE = """
package com.foo.repo;

class UserRepo {
    void repoMethod() {}
}
"""

# Two classes with the identical simple name "Service" in different
# packages, each advised by a pointcut naming only one of the two packages
# — the exact reproduction from the PR #916 review comment.
DUPLICATE_CLASS_NAME_A_SOURCE = """
package a;

class Service {
    void run() {}
}
"""

DUPLICATE_CLASS_NAME_B_SOURCE = """
package b;

class Service {
    void run() {}
}
"""

DUPLICATE_CLASS_NAME_ASPECT_SOURCE = """
import org.aspectj.lang.annotation.Aspect;
import org.aspectj.lang.annotation.Before;

@Aspect
class ScopedAspect {
    @Before("execution(* a.Service.run(..))")
    void beforeRun() {}
}
"""

# Advice and target split across two files so seeding an impact-radius query
# by one file's path doesn't also seed the other node as "changed".
ANNOTATION_ASPECT_ONLY_SOURCE = """
import org.aspectj.lang.annotation.Aspect;
import org.aspectj.lang.annotation.Before;

@Aspect
class AccessLimitAspect {
    @Before("@annotation(com.foo.common.annotation.AccessLimit)")
    void beforeLimit() {}
}
"""

ANNOTATION_TARGET_ONLY_SOURCE = """
class OrderController {
    @AccessLimit
    void placeOrder() {}
}
"""

CLASS_LEVEL_ANNOTATION_SOURCE = """
import org.aspectj.lang.annotation.Aspect;
import org.aspectj.lang.annotation.Before;

@Aspect
class TxAspect {
    @Before("@annotation(org.springframework.transaction.annotation.Transactional)")
    void beforeTx() {}
}

@Transactional
class BillingService {
    void charge() {}
    void refund() {}
}

class ReportService {
    @Transactional
    void writeReport() {}
}
"""

NAMED_COMPOUND_REFERENCE_SOURCE = """
import org.aspectj.lang.annotation.Aspect;
import org.aspectj.lang.annotation.Pointcut;
import org.aspectj.lang.annotation.Before;

@Aspect
class BroadAspect {
    @Pointcut("execution(* *(..)) || execution(* run(..))")
    void broadPointcut() {}

    @Before("broadPointcut()")
    void before() {}
}

class SomeService {
    void doWork() {}
    void run() {}
}
"""

VALUE_NAMED_ARG_SOURCE = """
package com.foo.service;

import org.aspectj.lang.annotation.Aspect;
import org.aspectj.lang.annotation.Around;

@Aspect
class LoggingAspect {
    @Around(value = "execution(* com.foo.service.PaymentService.*(..))")
    Object logAround() { return null; }
}

class PaymentService {
    void pay() {}
}
"""

CROSS_ASPECT_REFERENCE_SOURCE = """
import org.aspectj.lang.annotation.Aspect;
import org.aspectj.lang.annotation.Before;

@Aspect
class ConsumerAspect {
    @Before("OtherAspect.sharedPointcut()")
    void beforeShared() {}
}

class SomeService {
    void doWork() {}
}
"""

UNRESOLVABLE_NAMED_REFERENCE_SOURCE = """
import org.aspectj.lang.annotation.Aspect;
import org.aspectj.lang.annotation.Before;

@Aspect
class DanglingAspect {
    @Before("missingPointcut()")
    void beforeMissing() {}
}

class SomeService {
    void doWork() {}
}
"""

NO_STRING_ARGUMENT_SOURCE = """
import org.aspectj.lang.annotation.Aspect;
import org.aspectj.lang.annotation.Before;

@Aspect
class NoArgAspect {
    @Before(AccessCheck.class)
    void beforeCheck() {}
}

class SomeService {
    void doWork() {}
}
"""

UNIVERSAL_POINTCUT_SOURCE = """
import org.aspectj.lang.annotation.Aspect;
import org.aspectj.lang.annotation.Before;

@Aspect
class UniversalAspect {
    @Before("execution(* *(..))")
    void logAll() {}
}

class SomeService {
    void doWork() {}
}
"""


def _build_store(tmp_path: Path, source: str, filename: str = "Aspect.java"):
    path = tmp_path / filename
    nodes, edges = CodeParser().parse_bytes(path, source.encode())
    graph_dir = tmp_path / ".code-review-graph"
    graph_dir.mkdir(exist_ok=True)
    db_path = graph_dir / "graph.db"
    store = GraphStore(db_path)
    store.store_file_nodes_edges(str(path), nodes, edges, "hash")
    return path, store


def _build_store_multi(tmp_path: Path, sources: dict[str, str]):
    """Parse and store several Java files into one shared GraphStore.

    Needed for AOP fixtures where package selectivity depends on two
    classes with the same simple name living in different files/packages —
    a single-file fixture can't express two ``package`` declarations.
    """
    graph_dir = tmp_path / ".code-review-graph"
    graph_dir.mkdir(exist_ok=True)
    db_path = graph_dir / "graph.db"
    store = GraphStore(db_path)
    paths: dict[str, Path] = {}
    for filename, source in sources.items():
        path = tmp_path / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        nodes, edges = CodeParser().parse_bytes(path, source.encode())
        store.store_file_nodes_edges(str(path), nodes, edges, "hash")
        paths[filename] = path
    return paths, store


def test_annotation_pointcut_before_advice_creates_calls_edge(tmp_path: Path) -> None:
    path, store = _build_store(tmp_path, ANNOTATION_SOURCE)
    try:
        stats = resolve_aop_advice(store)
        assert stats["aspects_indexed"] == 1
        assert stats["calls_created"] == 1

        source_qual = f"{path.as_posix()}::AccessLimitAspect.beforeLimit"
        edges = store.get_edges_by_source(source_qual)
        calls = [e for e in edges if e.kind == "CALLS"]
        assert len(calls) == 1
        edge = calls[0]
        assert edge.target_qualified == f"{path.as_posix()}::OrderController.placeOrder"
        assert edge.extra["aop_resolved"] is True
        assert edge.extra["pointcut_kind"] == "@annotation"
        assert edge.extra["pointcut_expr"] == (
            "@annotation(com.foo.common.annotation.AccessLimit)"
        )
    finally:
        store.close()


def test_execution_pointcut_around_advice_creates_calls_edge(tmp_path: Path) -> None:
    path, store = _build_store(tmp_path, EXECUTION_SOURCE)
    try:
        stats = resolve_aop_advice(store)
        assert stats["calls_created"] == 1

        source_qual = f"{path.as_posix()}::LoggingAspect.logAround"
        edges = [e for e in store.get_edges_by_source(source_qual) if e.kind == "CALLS"]
        assert len(edges) == 1
        edge = edges[0]
        assert edge.target_qualified == f"{path.as_posix()}::PaymentService.pay"
        assert edge.extra["pointcut_kind"] == "execution"
        assert edge.extra["aop_resolved"] is True
    finally:
        store.close()


def test_value_named_argument_form_is_extracted(tmp_path: Path) -> None:
    """``_extract_annotation_string_arg`` must also recognize the
    ``value = "..."`` named-argument form, not just the positional form
    (covered above) and the ``pointcut = "..."`` form (covered by
    test_named_pointcut_reference_resolves_both_advices).
    """
    path, store = _build_store(tmp_path, VALUE_NAMED_ARG_SOURCE)
    try:
        stats = resolve_aop_advice(store)
        assert stats["calls_created"] == 1

        source_qual = f"{path.as_posix()}::LoggingAspect.logAround"
        edges = [e for e in store.get_edges_by_source(source_qual) if e.kind == "CALLS"]
        assert len(edges) == 1
        assert edges[0].target_qualified == f"{path.as_posix()}::PaymentService.pay"
        assert edges[0].extra["pointcut_expr"] == (
            "execution(* com.foo.service.PaymentService.*(..))"
        )
    finally:
        store.close()


def test_resolve_aop_advice_is_idempotent(tmp_path: Path) -> None:
    """Running the resolver twice with no source changes must not duplicate
    or drift edges: the second run's ``stale_calls_removed`` must equal the
    first run's ``calls_created``, and the resulting edge set must be
    unchanged (see module docstring: "Safe to call multiple times").
    """
    path, store = _build_store(tmp_path, EXECUTION_SOURCE)
    try:
        first = resolve_aop_advice(store)
        assert first["calls_created"] == 1

        source_qual = f"{path.as_posix()}::LoggingAspect.logAround"
        first_edges = {
            e.target_qualified
            for e in store.get_edges_by_source(source_qual)
            if e.kind == "CALLS"
        }

        second = resolve_aop_advice(store)
        assert second["stale_calls_removed"] == first["calls_created"]
        assert second["calls_created"] == first["calls_created"]

        second_edges = {
            e.target_qualified
            for e in store.get_edges_by_source(source_qual)
            if e.kind == "CALLS"
        }
        assert second_edges == first_edges
    finally:
        store.close()


def test_named_pointcut_reference_resolves_both_advices(tmp_path: Path) -> None:
    path, store = _build_store(tmp_path, NAMED_POINTCUT_SOURCE)
    try:
        stats = resolve_aop_advice(store)
        assert stats["calls_created"] == 2

        target_qual = f"{path.as_posix()}::OrderController.placeOrder"

        before_qual = f"{path.as_posix()}::AccessLimitAspect.before"
        before_edges = [
            e for e in store.get_edges_by_source(before_qual) if e.kind == "CALLS"
        ]
        assert len(before_edges) == 1
        assert before_edges[0].target_qualified == target_qual
        assert before_edges[0].extra["pointcut_expr"] == (
            "@annotation(com.foo.common.annotation.AccessLimit)"
        )

        after_throwing_qual = f"{path.as_posix()}::AccessLimitAspect.afterThrowing"
        after_edges = [
            e for e in store.get_edges_by_source(after_throwing_qual) if e.kind == "CALLS"
        ]
        assert len(after_edges) == 1
        assert after_edges[0].target_qualified == target_qual
    finally:
        store.close()


def test_compound_boolean_pointcut_is_safely_skipped(tmp_path: Path) -> None:
    path, store = _build_store(tmp_path, COMPOUND_SOURCE)
    try:
        stats = resolve_aop_advice(store)
        assert stats["calls_created"] == 0
        assert stats["advice_resolved"] == 0

        source_qual = f"{path.as_posix()}::SecurityAspect.checkSecurity"
        edges = [e for e in store.get_edges_by_source(source_qual) if e.kind == "CALLS"]
        assert edges == []
    finally:
        store.close()


def test_cross_aspect_dotted_reference_is_safely_skipped(tmp_path: Path) -> None:
    """A dotted ``Other.pointcut()`` reference crosses an ``@Aspect`` class
    boundary, which is out of scope for this pass (see module docstring) —
    it must be skipped rather than misparsed as an inline expression.
    """
    path, store = _build_store(tmp_path, CROSS_ASPECT_REFERENCE_SOURCE)
    try:
        stats = resolve_aop_advice(store)
        assert stats["calls_created"] == 0
        assert stats["advice_resolved"] == 0

        source_qual = f"{path.as_posix()}::ConsumerAspect.beforeShared"
        edges = [e for e in store.get_edges_by_source(source_qual) if e.kind == "CALLS"]
        assert edges == []
    finally:
        store.close()


def test_unresolvable_named_reference_is_safely_skipped(tmp_path: Path) -> None:
    """A same-class named reference to a ``@Pointcut`` that does not exist
    must be skipped rather than raising or matching everything.
    """
    path, store = _build_store(tmp_path, UNRESOLVABLE_NAMED_REFERENCE_SOURCE)
    try:
        stats = resolve_aop_advice(store)
        assert stats["calls_created"] == 0
        assert stats["advice_resolved"] == 0

        source_qual = f"{path.as_posix()}::DanglingAspect.beforeMissing"
        edges = [e for e in store.get_edges_by_source(source_qual) if e.kind == "CALLS"]
        assert edges == []
    finally:
        store.close()


def test_advice_with_no_string_argument_is_safely_skipped(tmp_path: Path) -> None:
    """An advice annotation with no string-literal argument (e.g.
    ``@Before(AccessCheck.class)``) has no pointcut expression to extract —
    it must be dropped before the advice list, not crash the resolver.
    """
    path, store = _build_store(tmp_path, NO_STRING_ARGUMENT_SOURCE)
    try:
        stats = resolve_aop_advice(store)
        assert stats["calls_created"] == 0
        assert stats["advice_resolved"] == 0

        source_qual = f"{path.as_posix()}::NoArgAspect.beforeCheck"
        edges = [e for e in store.get_edges_by_source(source_qual) if e.kind == "CALLS"]
        assert edges == []
    finally:
        store.close()


def test_package_wildcard_execution_pointcut_stays_scoped_to_its_package(
    tmp_path: Path,
) -> None:
    """A package-scoped ``execution(* com.foo.service.*.*(..))`` pointcut
    must match every class in ``com.foo.service`` (here, ``PaymentService``)
    and nothing in an unrelated package such as ``com.foo.repo`` — package
    selectivity is preserved via each class's persisted
    ``extra["java_package"]`` rather than discarded (see module docstring /
    PR #916 review).
    """
    paths, store = _build_store_multi(tmp_path, {
        "service/Aspect.java": PACKAGE_WILDCARD_SERVICE_SOURCE,
        "repo/Repo.java": PACKAGE_WILDCARD_REPO_SOURCE,
    })
    try:
        stats = resolve_aop_advice(store)
        assert stats["calls_created"] == 1

        service_path = paths["service/Aspect.java"].as_posix()
        source_qual = f"{service_path}::LoggingAspect.logAround"
        edges = [e for e in store.get_edges_by_source(source_qual) if e.kind == "CALLS"]
        targets = {e.target_qualified for e in edges}
        assert targets == {f"{service_path}::PaymentService.pay"}

        repo_path = paths["repo/Repo.java"].as_posix()
        assert f"{repo_path}::UserRepo.repoMethod" not in targets
    finally:
        store.close()


def test_execution_pointcut_does_not_conflate_same_named_classes_across_packages(
    tmp_path: Path,
) -> None:
    """The exact PR #916 review reproduction: two classes both named
    ``Service`` in different packages (``a`` and ``b``), each declaring
    ``run()``. A pointcut naming only ``a.Service.run`` must resolve to
    ``a.Service.run`` alone — not to both, which is what happened when the
    package portion of the pattern was discarded during matching.
    """
    paths, store = _build_store_multi(tmp_path, {
        "aspect/Aspect.java": DUPLICATE_CLASS_NAME_ASPECT_SOURCE,
        "a/Service.java": DUPLICATE_CLASS_NAME_A_SOURCE,
        "b/Service.java": DUPLICATE_CLASS_NAME_B_SOURCE,
    })
    try:
        stats = resolve_aop_advice(store)
        assert stats["calls_created"] == 1

        aspect_path = paths["aspect/Aspect.java"].as_posix()
        source_qual = f"{aspect_path}::ScopedAspect.beforeRun"
        edges = [e for e in store.get_edges_by_source(source_qual) if e.kind == "CALLS"]
        targets = {e.target_qualified for e in edges}

        a_path = paths["a/Service.java"].as_posix()
        b_path = paths["b/Service.java"].as_posix()
        assert targets == {f"{a_path}::Service.run"}
        assert f"{b_path}::Service.run" not in targets
    finally:
        store.close()


def test_class_level_annotation_is_not_expanded_to_every_method(tmp_path: Path) -> None:
    """``@annotation(X)`` must only match methods carrying X directly — a
    class-level X (e.g. ``@Transactional`` on ``BillingService``) is
    ``@within()`` semantics, a different designator, and must not be
    expanded to every method of that class (see module docstring / PR #916
    review).
    """
    path, store = _build_store(tmp_path, CLASS_LEVEL_ANNOTATION_SOURCE)
    try:
        stats = resolve_aop_advice(store)
        assert stats["calls_created"] == 1

        source_qual = f"{path.as_posix()}::TxAspect.beforeTx"
        edges = [e for e in store.get_edges_by_source(source_qual) if e.kind == "CALLS"]
        targets = {e.target_qualified for e in edges}
        assert targets == {f"{path.as_posix()}::ReportService.writeReport"}
        assert f"{path.as_posix()}::BillingService.charge" not in targets
        assert f"{path.as_posix()}::BillingService.refund" not in targets
    finally:
        store.close()


def test_named_reference_to_a_compound_pointcut_is_safely_skipped(tmp_path: Path) -> None:
    """The compound-operator guard must also apply to the ``@Pointcut`` body
    a named reference resolves to — a bare reference like
    ``"broadPointcut()"`` carries no ``&&``/``||`` in its own text even when
    the pointcut it names does (see module docstring / PR #916 review).
    """
    path, store = _build_store(tmp_path, NAMED_COMPOUND_REFERENCE_SOURCE)
    try:
        stats = resolve_aop_advice(store)
        assert stats["calls_created"] == 0
        assert stats["advice_resolved"] == 0

        source_qual = f"{path.as_posix()}::BroadAspect.before"
        edges = [e for e in store.get_edges_by_source(source_qual) if e.kind == "CALLS"]
        assert edges == []
    finally:
        store.close()


def test_self_edge_guard_skips_advice_matching_itself(tmp_path: Path) -> None:
    """A pointcut broad enough to match the advice method's own signature
    (``execution(* *(..))``) must not create a self-referencing CALLS edge,
    while still resolving to genuine other targets in the same build.
    """
    path, store = _build_store(tmp_path, UNIVERSAL_POINTCUT_SOURCE)
    try:
        stats = resolve_aop_advice(store)

        source_qual = f"{path.as_posix()}::UniversalAspect.logAll"
        edges = [e for e in store.get_edges_by_source(source_qual) if e.kind == "CALLS"]
        targets = {e.target_qualified for e in edges}

        assert source_qual not in targets
        assert f"{path.as_posix()}::SomeService.doWork" in targets
        assert stats["calls_created"] == len(targets)
    finally:
        store.close()


def test_callers_of_query_finds_advice_method(tmp_path: Path) -> None:
    path, store = _build_store(tmp_path, ANNOTATION_SOURCE)
    try:
        resolve_aop_advice(store)
    finally:
        store.close()

    target_qual = f"{path.as_posix()}::OrderController.placeOrder"
    result = query_graph("callers_of", target_qual, repo_root=str(tmp_path))
    assert result["status"] == "ok"
    assert result["result_count"] == 1
    assert result["results"][0]["name"] == "beforeLimit"
    assert {edge["kind"] for edge in result["edges"]} == {"CALLS"}


def test_advises_and_advised_by_filter_out_plain_calls_edges(tmp_path: Path) -> None:
    """``advises``/``advised_by`` must return only aop_resolved edges, while
    ``callers_of`` keeps returning every CALLS edge regardless of provenance.
    """
    path, store = _build_store(tmp_path, ANNOTATION_SOURCE)
    try:
        resolve_aop_advice(store)

        target_qual = f"{path.as_posix()}::OrderController.placeOrder"
        advice_qual = f"{path.as_posix()}::AccessLimitAspect.beforeLimit"
        # A regular, non-AOP caller of the same target method.
        plain_caller_qual = f"{path.as_posix()}::OrderController.unrelatedMethod"
        store.upsert_edge(EdgeInfo(
            kind="CALLS",
            source=plain_caller_qual,
            target=target_qual,
            file_path=str(path),
            line=1,
        ))
        store.commit()
    finally:
        store.close()

    callers = query_graph("callers_of", target_qual, repo_root=str(tmp_path))
    assert callers["result_count"] == 2
    assert {r["name"] for r in callers["results"]} == {"beforeLimit", "unrelatedMethod"}

    advised_by = query_graph("advised_by", target_qual, repo_root=str(tmp_path))
    assert advised_by["status"] == "ok"
    assert advised_by["result_count"] == 1
    assert advised_by["results"][0]["name"] == "beforeLimit"
    assert advised_by["edges"][0]["kind"] == "CALLS"

    advises = query_graph("advises", advice_qual, repo_root=str(tmp_path))
    assert advises["status"] == "ok"
    assert advises["result_count"] == 1
    assert advises["results"][0]["name"] == "placeOrder"

    # The plain caller has no AOP relationships at all in either direction.
    assert query_graph("advises", plain_caller_qual, repo_root=str(tmp_path))["result_count"] == 0


def test_impact_radius_propagates_from_advice_to_target_not_the_reverse(
    tmp_path: Path,
) -> None:
    """An AOP CALLS edge (advice -> target) must impact-propagate
    source->target: changing the advice impacts the method(s) it wraps.
    Plain CALLS propagates target->source (a callee change impacts its
    callers), but an advice isn't "called by" its target, so that policy
    would answer the wrong question here — see module docstring / PR #916
    review.
    """
    paths, store = _build_store_multi(tmp_path, {
        "AccessLimitAspect.java": ANNOTATION_ASPECT_ONLY_SOURCE,
        "OrderController.java": ANNOTATION_TARGET_ONLY_SOURCE,
    })
    try:
        resolve_aop_advice(store)

        aspect_path = paths["AccessLimitAspect.java"].as_posix()
        target_path = paths["OrderController.java"].as_posix()
        advice_qual = f"{aspect_path}::AccessLimitAspect.beforeLimit"
        target_qual = f"{target_path}::OrderController.placeOrder"

        # Changing the aspect's file impacts the advised method.
        advice_impact = store.get_impact_radius([aspect_path])
        impacted_from_advice = {n.qualified_name for n in advice_impact["impacted_nodes"]}
        assert target_qual in impacted_from_advice

        # Changing the target does NOT, via this AOP edge alone, impact the
        # aspect — the useless direction from the review is gone.
        target_impact = store.get_impact_radius([target_path])
        impacted_from_target = {n.qualified_name for n in target_impact["impacted_nodes"]}
        assert advice_qual not in impacted_from_target
    finally:
        store.close()


def test_networkx_impact_radius_propagates_from_advice_to_target_not_the_reverse(
    tmp_path: Path,
) -> None:
    """Same direction guarantee as
    test_impact_radius_propagates_from_advice_to_target_not_the_reverse,
    verified directly against the NetworkX engine (``CRG_BFS_ENGINE=networkx``
    routes through this method) rather than the default SQL engine — both
    engines special-case ``extra.aop_resolved`` edges the same way (see
    module docstring).
    """
    paths, store = _build_store_multi(tmp_path, {
        "AccessLimitAspect.java": ANNOTATION_ASPECT_ONLY_SOURCE,
        "OrderController.java": ANNOTATION_TARGET_ONLY_SOURCE,
    })
    try:
        resolve_aop_advice(store)

        aspect_path = paths["AccessLimitAspect.java"].as_posix()
        target_path = paths["OrderController.java"].as_posix()
        advice_qual = f"{aspect_path}::AccessLimitAspect.beforeLimit"
        target_qual = f"{target_path}::OrderController.placeOrder"

        advice_impact = store._get_impact_radius_networkx([aspect_path])
        impacted_from_advice = {n.qualified_name for n in advice_impact["impacted_nodes"]}
        assert target_qual in impacted_from_advice

        target_impact = store._get_impact_radius_networkx([target_path])
        impacted_from_target = {n.qualified_name for n in target_impact["impacted_nodes"]}
        assert advice_qual not in impacted_from_target
    finally:
        store.close()


def _aop_edges(store: GraphStore) -> list:
    rows = store._conn.execute(
        "SELECT source_qualified, target_qualified, extra FROM edges WHERE kind = 'CALLS'"
    ).fetchall()
    return [row for row in rows if json.loads(row["extra"] or "{}").get("aop_resolved")]


def test_incremental_annotation_removal_removes_stale_aop_call(tmp_path: Path) -> None:
    """Mirrors
    test_spring_events.py::test_incremental_listener_change_removes_stale_event_call:
    a full_build resolves the advice->target edge, then removing the
    target's annotation and running incremental_update on just that file
    must clear the now-stale ``aop_resolved`` edge (see module docstring:
    "previously derived edges ... are cleared and rebuilt on every call").
    """
    aspect_path = tmp_path / "AccessLimitAspect.java"
    controller_path = tmp_path / "OrderController.java"
    aspect_path.write_text(ANNOTATION_ASPECT_ONLY_SOURCE, encoding="utf-8")
    controller_path.write_text(ANNOTATION_TARGET_ONLY_SOURCE, encoding="utf-8")

    graph_dir = tmp_path / ".code-review-graph"
    graph_dir.mkdir()

    with GraphStore(graph_dir / "graph.db") as store:
        first = full_build(tmp_path, store)
        assert first["aop_resolution"]["calls_created"] == 1
        assert len(_aop_edges(store)) == 1

        controller_path.write_text(
            "class OrderController {\n    void placeOrder() {}\n}\n",
            encoding="utf-8",
        )
        updated = incremental_update(
            tmp_path,
            store,
            changed_files=["OrderController.java"],
        )

        assert updated["aop_resolution"] is not None
        assert updated["aop_resolution"]["calls_created"] == 0
        assert _aop_edges(store) == []


class TestAspectjPatternToRegex:
    """Direct unit tests for the three documented conversion rules."""

    def test_literal_pattern_matches_only_itself(self) -> None:
        pattern = re.compile("^" + _aspectj_pattern_to_regex("PaymentService") + "$")
        assert pattern.match("PaymentService")
        assert not pattern.match("PaymentServiceImpl")
        assert not pattern.match("paymentservice")

    def test_single_star_does_not_cross_a_dot_boundary(self) -> None:
        pattern = re.compile("^" + _aspectj_pattern_to_regex("*Service") + "$")
        assert pattern.match("PaymentService")
        assert pattern.match("Service")
        assert not pattern.match("internal.Service")

    def test_double_dot_becomes_a_permissive_any_match(self) -> None:
        # ".." is documented as "any number of intervening path segments", but
        # it compiles to a bare ".*" — it matches any characters at all, not
        # only well-formed dot-separated segments. This test pins the actual
        # (looser) behavior so a future change to the translation is a
        # deliberate, visible diff rather than a silent regression either way.
        pattern = re.compile("^" + _aspectj_pattern_to_regex("com..Service") + "$")
        assert pattern.match("com.foo.bar.Service")
        assert pattern.match("comXService")  # crosses non-dot characters too

    def test_special_regex_characters_are_escaped(self) -> None:
        pattern = re.compile("^" + _aspectj_pattern_to_regex("Foo$Bar") + "$")
        assert pattern.match("Foo$Bar")
        assert not pattern.match("FooXBar")


class TestParseExecutionExpression:
    def test_package_and_class_wildcard_needs_class_true(self) -> None:
        result = _parse_execution_expression(
            "execution(* com.foo.service.PaymentService.*(..))"
        )
        assert result is not None
        regex_str, needs_class = result
        assert needs_class is True
        pattern = re.compile(regex_str)
        # The full package.Class.method pattern is preserved — no longer
        # reduced to the last two segments (see module docstring / PR #916
        # review) — so it must be matched against the fully qualified
        # subject, and a same-named class in a different package must not
        # match.
        assert pattern.match("com.foo.service.PaymentService.pay")
        assert not pattern.match("PaymentService.pay")
        assert not pattern.match("com.foo.other.PaymentService.pay")

    def test_method_name_only_pattern_needs_class_false(self) -> None:
        result = _parse_execution_expression("execution(* *get*Index(..))")
        assert result is not None
        regex_str, needs_class = result
        assert needs_class is False
        pattern = re.compile(regex_str)
        assert pattern.match("getUserIndex")
        assert not pattern.match("createUser")

    def test_non_execution_expression_returns_none(self) -> None:
        assert _parse_execution_expression("@annotation(com.foo.Bar)") is None

    def test_missing_parameter_parens_returns_none(self) -> None:
        assert _parse_execution_expression("execution(* com.foo.Bar.baz)") is None

    def test_package_wildcard_matches_only_its_own_package(self) -> None:
        # "com.foo.service.*.*" must match any class.method inside
        # com.foo.service, but not the same shape in another package —
        # package selectivity is preserved rather than discarded.
        result = _parse_execution_expression(
            "execution(* com.foo.service.*.*(..))"
        )
        assert result is not None
        regex_str, needs_class = result
        assert needs_class is True
        pattern = re.compile(regex_str)
        assert pattern.match("com.foo.service.PaymentService.pay")
        assert not pattern.match("com.foo.repo.UserRepo.repoMethod")

    def test_bare_method_wildcard_matches_every_method_name(self) -> None:
        # A method-name-only "*" pattern (no declaring-type segment) is
        # genuinely universal by AspectJ semantics — real, not an
        # approximation artifact — so it resolves rather than being rejected.
        result = _parse_execution_expression("execution(* *(..))")
        assert result is not None
        regex_str, needs_class = result
        assert needs_class is False
        pattern = re.compile(regex_str)
        assert pattern.match("anyMethodAtAll")
