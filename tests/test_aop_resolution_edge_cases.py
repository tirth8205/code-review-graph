"""Edge cases for Spring AOP advice-to-target resolution (PR #916, issue #592).

``tests/test_aop_resolver.py`` pins the main pointcut forms. This module pins
the cases around them: empty and corrupt inputs, name collisions, unicode and
spaces in paths, graphs written before ``java_package`` existed, full build
versus incremental update parity, deletions, failure isolation, the other Java
post-build resolvers that share the ``CALLS`` table, flow entry points, and a
pathological pointcut that must not stall the build.
"""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import code_review_graph
from code_review_graph.aop_resolver import resolve_aop_advice
from code_review_graph.event_resolver import resolve_spring_events
from code_review_graph.flows import detect_entry_points
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, incremental_update
from code_review_graph.parser import CodeParser
from code_review_graph.tools.query import query_graph

ASPECT_SOURCE = """\
package com.acme.aspect;

import org.aspectj.lang.annotation.Aspect;
import org.aspectj.lang.annotation.Around;
import org.aspectj.lang.annotation.Before;

@Aspect
public class AuditAspect {
    @Before("@annotation(com.acme.annotation.Audited)")
    public void beforeAudited() {}

    @Around("execution(* com.acme.service.*.*(..))")
    public Object aroundService() { return null; }
}
"""

SERVICE_SOURCE = """\
package com.acme.service;

public class OrderService {
    @Audited
    public void place() {}

    public void cancel() {}
}
"""

REPO_SOURCE = """\
package com.acme.repo;

public class OrderRepo {
    public void save() {}
}
"""


def _store_sources(tmp_path: Path, sources: dict[str, str]):
    graph_dir = tmp_path / ".code-review-graph"
    graph_dir.mkdir(exist_ok=True)
    store = GraphStore(graph_dir / "graph.db")
    paths: dict[str, Path] = {}
    for name, source in sources.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        nodes, edges = CodeParser().parse_bytes(path, source.encode())
        store.store_file_nodes_edges(str(path), nodes, edges, "hash")
        paths[name] = path
    return paths, store


def _short(qualified_name: str) -> str:
    """``/abs/dir/File.java::Cls.m`` -> ``File.java::Cls.m``."""
    file_part, _, symbol = qualified_name.partition("::")
    return f"{file_part.rsplit('/', 1)[-1]}::{symbol}"


def _derived_calls(store: GraphStore, flag: str = "aop_resolved") -> set[tuple[str, str]]:
    rows = store._conn.execute(
        "SELECT source_qualified, target_qualified, extra FROM edges WHERE kind = 'CALLS'"
    ).fetchall()
    return {
        (_short(row["source_qualified"]), _short(row["target_qualified"]))
        for row in rows
        if json.loads(row["extra"] or "{}").get(flag)
    }


def _write_tree(root: Path, sources: dict[str, str]) -> None:
    for name, source in sources.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")


ACME_TREE = {
    "aspect/AuditAspect.java": ASPECT_SOURCE,
    "service/OrderService.java": SERVICE_SOURCE,
    "repo/OrderRepo.java": REPO_SOURCE,
}

ACME_EXPECTED = {
    ("AuditAspect.java::AuditAspect.beforeAudited", "OrderService.java::OrderService.place"),
    ("AuditAspect.java::AuditAspect.aroundService", "OrderService.java::OrderService.place"),
    ("AuditAspect.java::AuditAspect.aroundService", "OrderService.java::OrderService.cancel"),
}


# ---------------------------------------------------------------------------
# Empty, missing and corrupt input
# ---------------------------------------------------------------------------


def test_graph_without_java_returns_zero_counts(tmp_path: Path) -> None:
    _, store = _store_sources(tmp_path, {"app.py": "def f():\n    return 1\n"})
    try:
        assert resolve_aop_advice(store) == {
            "aspects_indexed": 0,
            "advice_resolved": 0,
            "calls_created": 0,
            "stale_calls_removed": 0,
        }
    finally:
        store.close()


def test_dropping_aspect_annotation_removes_edges_and_reports_them(tmp_path: Path) -> None:
    paths, store = _store_sources(tmp_path, {
        "AuditAspect.java": ASPECT_SOURCE,
        "OrderService.java": SERVICE_SOURCE,
    })
    try:
        first = resolve_aop_advice(store)
        assert first["calls_created"] == 3

        plain = ASPECT_SOURCE.replace("@Aspect\n", "")
        path = paths["AuditAspect.java"]
        nodes, edges = CodeParser().parse_bytes(path, plain.encode())
        store.store_file_nodes_edges(str(path), nodes, edges, "hash2")
        # Re-storing the aspect file already dropped its own derived edges;
        # put one back to stand in for an edge whose owning file was not
        # re-parsed, and check the no-aspect branch still clears and counts it.
        resolve_aop_advice(store)  # no-op: no @Aspect left
        store._conn.execute(
            "INSERT INTO edges (kind, source_qualified, target_qualified, file_path, line,"
            " extra, confidence, confidence_tier, updated_at)"
            " VALUES ('CALLS', 'x::A.a', 'y::B.b', 'x', 0, ?, 0.7, 'INFERRED', 0)",
            (json.dumps({"aop_resolved": True}),),
        )
        second = resolve_aop_advice(store)
        assert second["aspects_indexed"] == 0
        assert second["calls_created"] == 0
        assert second["stale_calls_removed"] == 1
        assert _derived_calls(store) == set()
    finally:
        store.close()


def test_corrupt_and_null_edge_extra_is_tolerated_and_preserved(tmp_path: Path) -> None:
    _, store = _store_sources(tmp_path, {
        "AuditAspect.java": ASPECT_SOURCE,
        "OrderService.java": SERVICE_SOURCE,
    })
    try:
        for extra in ("{not json", None):
            store._conn.execute(
                "INSERT INTO edges (kind, source_qualified, target_qualified, file_path,"
                " line, extra, confidence, confidence_tier, updated_at)"
                " VALUES ('CALLS', 'legacy::A.a', 'legacy::B.b', 'legacy', 7, ?, 1.0,"
                " 'EXTRACTED', 0)",
                (extra,),
            )
        stats = resolve_aop_advice(store)
        assert stats["calls_created"] == 3
        survivors = store._conn.execute(
            "SELECT COUNT(*) FROM edges WHERE source_qualified = 'legacy::A.a'"
        ).fetchone()[0]
        assert survivors == 2
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Matching details
# ---------------------------------------------------------------------------


def test_target_annotation_with_arguments_is_matched(tmp_path: Path) -> None:
    _, store = _store_sources(tmp_path, {
        "AuditAspect.java": ASPECT_SOURCE,
        "Limited.java": (
            "package com.acme.web;\n\n"
            "public class Limited {\n"
            "    @Audited(level = \"high\", seconds = 5)\n"
            "    public void call() {}\n"
            "}\n"
        ),
    })
    try:
        resolve_aop_advice(store)
        assert _derived_calls(store) == {
            ("AuditAspect.java::AuditAspect.beforeAudited", "Limited.java::Limited.call"),
        }
    finally:
        store.close()


def test_same_named_aspects_resolve_only_their_own_named_pointcuts(tmp_path: Path) -> None:
    aspect = """\
package com.acme.{pkg};

import org.aspectj.lang.annotation.Aspect;
import org.aspectj.lang.annotation.Before;
import org.aspectj.lang.annotation.Pointcut;

@Aspect
public class LoggingAspect {{
    @Pointcut("@annotation(com.acme.{ann})")
    public void pc() {{}}

    @Before("pc()")
    public void advice() {{}}
}}
"""
    _, store = _store_sources(tmp_path, {
        "one/LoggingAspect.java": aspect.format(pkg="one", ann="First"),
        "two/LoggingAspect.java": aspect.format(pkg="two", ann="Second"),
        "Targets.java": (
            "package com.acme.t;\n\n"
            "public class Targets {\n"
            "    @First\n    public void first() {}\n\n"
            "    @Second\n    public void second() {}\n"
            "}\n"
        ),
    })
    try:
        stats = resolve_aop_advice(store)
        assert stats["aspects_indexed"] == 2
        rows = store._conn.execute(
            "SELECT source_qualified, target_qualified, extra FROM edges WHERE kind = 'CALLS'"
        ).fetchall()
        pairs = {
            (row["source_qualified"].split("::")[0].rsplit("/", 2)[-2],
             row["target_qualified"].rsplit(".", 1)[-1])
            for row in rows
            if json.loads(row["extra"] or "{}").get("aop_resolved")
        }
        assert pairs == {("one", "first"), ("two", "second")}
    finally:
        store.close()


def test_package_qualified_pointcut_skips_default_package_class(tmp_path: Path) -> None:
    _, store = _store_sources(tmp_path, {
        "ScopedAspect.java": (
            "package com.acme.aspect;\n\n"
            "@Aspect\n"
            "public class ScopedAspect {\n"
            "    @Before(\"execution(* com.acme.Service.run(..))\")\n"
            "    public void advice() {}\n"
            "}\n"
        ),
        "Service.java": "public class Service {\n    public void run() {}\n}\n",
    })
    try:
        stats = resolve_aop_advice(store)
        assert stats["calls_created"] == 0
        assert _derived_calls(store) == set()
    finally:
        store.close()


def test_method_name_only_pointcut_matches_across_packages(tmp_path: Path) -> None:
    _, store = _store_sources(tmp_path, {
        "FindAspect.java": (
            "package com.acme.aspect;\n\n"
            "@Aspect\n"
            "public class FindAspect {\n"
            "    @Before(\"execution(* find*(..))\")\n"
            "    public void advice() {}\n"
            "}\n"
        ),
        "a/Users.java": (
            "package com.a;\n\npublic class Users {\n"
            "    public void findAll() {}\n    public void save() {}\n}\n"
        ),
        "b/Orders.java": (
            "package com.b;\n\npublic class Orders {\n    public void findOne() {}\n}\n"
        ),
    })
    try:
        resolve_aop_advice(store)
        assert _derived_calls(store) == {
            ("FindAspect.java::FindAspect.advice", "Users.java::Users.findAll"),
            ("FindAspect.java::FindAspect.advice", "Orders.java::Orders.findOne"),
        }
    finally:
        store.close()


@pytest.mark.parametrize("expr", [
    "!execution(* com.acme.service.*.*(..))",
    "!@annotation(com.acme.annotation.Audited)",
    "within(com.acme.service..*)",
    "args(java.lang.String)",
    "target(com.acme.service.OrderService)",
    "this(com.acme.service.OrderService)",
    "execution(* com.acme.service.*.*(..)) || execution(* com.acme.repo.*.*(..))",
])
def test_unsupported_designators_emit_no_edges(tmp_path: Path, expr: str) -> None:
    aspect = (
        "package com.acme.aspect;\n\n"
        "@Aspect\n"
        "public class GuardAspect {\n"
        f"    @Before(\"{expr}\")\n"
        "    public void advice() {}\n"
        "}\n"
    )
    _, store = _store_sources(tmp_path, {
        "GuardAspect.java": aspect,
        "OrderService.java": SERVICE_SOURCE,
    })
    try:
        stats = resolve_aop_advice(store)
        assert stats["calls_created"] == 0
        assert _derived_calls(store) == set()
    finally:
        store.close()


def test_chained_named_pointcut_is_skipped_not_partially_resolved(tmp_path: Path) -> None:
    _, store = _store_sources(tmp_path, {
        "ChainAspect.java": (
            "package com.acme.aspect;\n\n"
            "@Aspect\n"
            "public class ChainAspect {\n"
            "    @Pointcut(\"execution(* com.acme.service.*.*(..))\")\n"
            "    public void inner() {}\n\n"
            "    @Pointcut(\"inner()\")\n"
            "    public void outer() {}\n\n"
            "    @Before(\"outer()\")\n"
            "    public void advice() {}\n"
            "}\n"
        ),
        "OrderService.java": SERVICE_SOURCE,
    })
    try:
        assert resolve_aop_advice(store)["calls_created"] == 0
    finally:
        store.close()


def test_advice_annotations_outside_an_aspect_class_are_ignored(tmp_path: Path) -> None:
    _, store = _store_sources(tmp_path, {
        "NotAnAspect.java": ASPECT_SOURCE.replace("@Aspect\n", "@Component\n"),
        "OrderService.java": SERVICE_SOURCE,
    })
    try:
        stats = resolve_aop_advice(store)
        assert stats["aspects_indexed"] == 0
        assert _derived_calls(store) == set()
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def test_unicode_and_spaces_in_repo_path(tmp_path: Path) -> None:
    root = tmp_path / "my repo" / "ünïcödé dir"
    root.mkdir(parents=True)
    _write_tree(root, ACME_TREE)
    (root / ".code-review-graph").mkdir()

    with GraphStore(root / ".code-review-graph" / "graph.db") as store:
        result = full_build(root, store)
        assert result["aop_resolution"]["calls_created"] == 3
        assert _derived_calls(store) == ACME_EXPECTED
        place = next(
            n for n in store.get_nodes_by_file(str(root / "service" / "OrderService.java"))
            if n.name == "place"
        )

    advised = query_graph("advised_by", place.qualified_name, repo_root=str(root))
    assert advised["status"] == "ok"
    assert {r["name"] for r in advised["results"]} == {"beforeAudited", "aroundService"}


# ---------------------------------------------------------------------------
# Full build, incremental update, rebuild, deletions
# ---------------------------------------------------------------------------


def test_full_build_twice_is_idempotent(tmp_path: Path) -> None:
    _write_tree(tmp_path, ACME_TREE)
    (tmp_path / ".code-review-graph").mkdir()
    with GraphStore(tmp_path / ".code-review-graph" / "graph.db") as store:
        first = full_build(tmp_path, store)
        edges_first = _derived_calls(store)
        second = full_build(tmp_path, store)
        assert _derived_calls(store) == edges_first == ACME_EXPECTED
        assert second["aop_resolution"]["calls_created"] == first["aop_resolution"][
            "calls_created"
        ]
        count = store._conn.execute(
            "SELECT COUNT(*) FROM edges WHERE kind = 'CALLS' AND extra LIKE '%aop_resolved%'"
        ).fetchone()[0]
        assert count == len(ACME_EXPECTED)


def test_incremental_pointcut_change_matches_a_fresh_full_build(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_tree(repo, ACME_TREE)
    (repo / ".code-review-graph").mkdir()
    changed = ASPECT_SOURCE.replace(
        "execution(* com.acme.service.*.*(..))", "execution(* com.acme.repo.*.*(..))",
    )

    with GraphStore(repo / ".code-review-graph" / "graph.db") as store:
        full_build(repo, store)
        (repo / "aspect" / "AuditAspect.java").write_text(changed, encoding="utf-8")
        result = incremental_update(repo, store, changed_files=["aspect/AuditAspect.java"])
        assert result["aop_resolution"] is not None
        incremental_edges = _derived_calls(store)

    fresh = tmp_path / "fresh.db"
    with GraphStore(fresh) as store:
        full_build(repo, store)
        assert _derived_calls(store) == incremental_edges
    assert incremental_edges == {
        ("AuditAspect.java::AuditAspect.beforeAudited", "OrderService.java::OrderService.place"),
        ("AuditAspect.java::AuditAspect.aroundService", "OrderRepo.java::OrderRepo.save"),
    }


def test_deleting_target_then_aspect_leaves_no_dangling_aop_edges(tmp_path: Path) -> None:
    _write_tree(tmp_path, ACME_TREE)
    (tmp_path / ".code-review-graph").mkdir()
    with GraphStore(tmp_path / ".code-review-graph" / "graph.db") as store:
        full_build(tmp_path, store)
        assert _derived_calls(store) == ACME_EXPECTED

        (tmp_path / "service" / "OrderService.java").unlink()
        result = incremental_update(
            tmp_path, store, changed_files=["service/OrderService.java"],
        )
        assert result["aop_resolution"] is not None
        assert _derived_calls(store) == set()

        (tmp_path / "aspect" / "AuditAspect.java").unlink()
        incremental_update(tmp_path, store, changed_files=["aspect/AuditAspect.java"])
        dangling = store._conn.execute(
            "SELECT COUNT(*) FROM edges WHERE kind = 'CALLS' AND extra LIKE '%aop_resolved%'"
        ).fetchone()[0]
        assert dangling == 0


def test_update_without_java_changes_keeps_aop_edges(tmp_path: Path) -> None:
    _write_tree(tmp_path, {**ACME_TREE, "tool.py": "def f():\n    return 1\n"})
    (tmp_path / ".code-review-graph").mkdir()
    with GraphStore(tmp_path / ".code-review-graph" / "graph.db") as store:
        full_build(tmp_path, store)
        (tmp_path / "tool.py").write_text("def f():\n    return 2\n", encoding="utf-8")
        result = incremental_update(tmp_path, store, changed_files=["tool.py"])
        assert result["aop_resolution"] is None
        assert _derived_calls(store) == ACME_EXPECTED


def test_resolver_failure_does_not_fail_the_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import code_review_graph.aop_resolver as aop_resolver

    def boom(store):
        raise RuntimeError("synthetic resolver failure")

    monkeypatch.setattr(aop_resolver, "resolve_aop_advice", boom)
    _write_tree(tmp_path, ACME_TREE)
    (tmp_path / ".code-review-graph").mkdir()
    with GraphStore(tmp_path / ".code-review-graph" / "graph.db") as store:
        result = full_build(tmp_path, store)
        assert result["aop_resolution"] is None
        assert result["files_parsed"] == 3
        assert _derived_calls(store) == set()


# ---------------------------------------------------------------------------
# Graphs written by earlier releases
# ---------------------------------------------------------------------------


def test_class_nodes_without_java_package_never_gain_false_package_matches(
    tmp_path: Path,
) -> None:
    """Class nodes stored by a release before #916 carry no ``java_package``.

    A package-qualified ``execution`` pointcut must not match them (no false
    edge), ``@annotation`` pointcuts must still resolve, and re-parsing the
    target file (which ``update`` or ``build`` does) restores the package match.
    """
    paths, store = _store_sources(tmp_path, {
        "AuditAspect.java": ASPECT_SOURCE,
        "OrderService.java": SERVICE_SOURCE,
    })
    try:
        for row in store._conn.execute(
            "SELECT id, extra FROM nodes WHERE kind = 'Class'"
        ).fetchall():
            extra = json.loads(row["extra"] or "{}")
            extra.pop("java_package", None)
            store._conn.execute(
                "UPDATE nodes SET extra = ? WHERE id = ?", (json.dumps(extra), row["id"]),
            )
        store.commit()

        resolve_aop_advice(store)
        assert _derived_calls(store) == {
            (
                "AuditAspect.java::AuditAspect.beforeAudited",
                "OrderService.java::OrderService.place",
            ),
        }

        path = paths["OrderService.java"]
        nodes, edges = CodeParser().parse_bytes(path, SERVICE_SOURCE.encode())
        store.store_file_nodes_edges(str(path), nodes, edges, "hash2")
        resolve_aop_advice(store)
        assert _derived_calls(store) == ACME_EXPECTED
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Neighbouring resolvers and features
# ---------------------------------------------------------------------------


def test_aop_and_spring_event_resolvers_leave_each_others_edges_alone(tmp_path: Path) -> None:
    _write_tree(tmp_path, {
        "alpha/SharedEvent.java": "package alpha;\nclass SharedEvent {}\n",
        "alpha/Publisher.java": (
            "package alpha;\nclass Publisher {\n"
            "    void publish() { events.publishEvent(new SharedEvent()); }\n}\n"
        ),
        "alpha/Listener.java": (
            "package alpha;\n"
            "import org.springframework.context.event.EventListener;\n"
            "class Listener {\n    @EventListener void on(SharedEvent event) {}\n}\n"
        ),
        "alpha/ListenerAspect.java": (
            "package alpha;\n\n@Aspect\nclass ListenerAspect {\n"
            "    @Before(\"execution(* alpha.Listener.on(..))\")\n"
            "    void before() {}\n}\n"
        ),
    })
    (tmp_path / ".code-review-graph").mkdir()
    event_edge = {("Publisher.java::Publisher.publish", "Listener.java::Listener.on")}
    aop_edge = {("ListenerAspect.java::ListenerAspect.before", "Listener.java::Listener.on")}

    with GraphStore(tmp_path / ".code-review-graph" / "graph.db") as store:
        full_build(tmp_path, store)
        assert _derived_calls(store, "spring_event_resolved") == event_edge
        assert _derived_calls(store) == aop_edge

        resolve_aop_advice(store)
        assert _derived_calls(store, "spring_event_resolved") == event_edge
        resolve_spring_events(store)
        assert _derived_calls(store) == aop_edge


def test_static_call_from_advice_to_target_survives_resolution(tmp_path: Path) -> None:
    source = """\
package com.acme.service;

@Aspect
class SelfCheck {
    @Before("execution(* com.acme.service.Helper.check(..))")
    void advice() {
        new Helper().check();
    }
}

class Helper {
    void check() {}
}
"""
    paths, store = _store_sources(tmp_path, {"Both.java": source})
    path = paths["Both.java"]
    try:
        advice = f"{path.as_posix()}::SelfCheck.advice"
        target = f"{path.as_posix()}::Helper.check"

        def calls():
            return [
                e for e in store.get_edges_by_source(advice)
                if e.kind == "CALLS" and e.target_qualified == target
            ]

        static_before = [e for e in calls() if not e.extra.get("aop_resolved")]
        assert len(static_before) == 1
        assert static_before[0].line > 0

        resolve_aop_advice(store)
        resolve_aop_advice(store)
        edges = calls()
        assert len([e for e in edges if e.extra.get("aop_resolved")]) == 1
        static_after = [e for e in edges if not e.extra.get("aop_resolved")]
        assert [(e.line, e.confidence_tier) for e in static_after] == [
            (static_before[0].line, static_before[0].confidence_tier),
        ]
    finally:
        store.close()


def test_advised_mapping_method_stays_a_flow_entry_point(tmp_path: Path) -> None:
    _, store = _store_sources(tmp_path, {
        "AuditAspect.java": ASPECT_SOURCE,
        "Controller.java": (
            "package com.acme.web;\n\n"
            "public class OrderController {\n"
            "    @Audited\n"
            "    @GetMapping(\"/orders\")\n"
            "    public void list() {}\n"
            "}\n"
        ),
    })
    try:
        resolve_aop_advice(store)
        assert (
            "AuditAspect.java::AuditAspect.beforeAudited",
            "Controller.java::OrderController.list",
        ) in _derived_calls(store)
        entry_names = {node.name for node in detect_entry_points(store)}
        assert "list" in entry_names
        assert "beforeAudited" in entry_names
    finally:
        store.close()


def test_impact_engines_agree_on_multi_hop_through_an_aop_edge(tmp_path: Path) -> None:
    """Changing the aspect impacts the advised method and, one hop further,
    that method's static callers; SQL and NetworkX engines must agree."""
    paths, store = _store_sources(tmp_path, {
        "AuditAspect.java": ASPECT_SOURCE,
        "OrderService.java": SERVICE_SOURCE,
        "Api.java": (
            "package com.acme.web;\n\n"
            "public class Api {\n"
            "    private OrderService orders;\n"
            "    public void submit() { orders.place(); }\n"
            "}\n"
        ),
    })
    try:
        resolve_aop_advice(store)
        aspect = paths["AuditAspect.java"].as_posix()
        sql = {
            _short(n.qualified_name)
            for n in store.get_impact_radius_sql([aspect], max_depth=3)["impacted_nodes"]
        }
        nx = {
            _short(n.qualified_name)
            for n in store._get_impact_radius_networkx([aspect], max_depth=3)["impacted_nodes"]
        }
        assert "OrderService.java::OrderService.place" in sql
        assert "OrderService.java::OrderService.cancel" in sql
        assert sql == nx
    finally:
        store.close()


def test_aop_query_patterns_on_unknown_target_report_not_found(tmp_path: Path) -> None:
    _write_tree(tmp_path, ACME_TREE)
    (tmp_path / ".code-review-graph").mkdir()
    with GraphStore(tmp_path / ".code-review-graph" / "graph.db") as store:
        full_build(tmp_path, store)
    for pattern in ("advises", "advised_by"):
        result = query_graph(pattern, "NoSuchMethodAnywhere", repo_root=str(tmp_path))
        assert result["status"] == "not_found"


# ---------------------------------------------------------------------------
# Large and hostile input
# ---------------------------------------------------------------------------


def test_broad_pointcut_fans_out_once_per_method_without_self_edge(tmp_path: Path) -> None:
    methods = "\n".join(f"    public void m{i}() {{}}" for i in range(400))
    _, store = _store_sources(tmp_path, {
        "Broad.java": (
            "package com.acme.aspect;\n\n@Aspect\npublic class Broad {\n"
            "    @Before(\"execution(* com.acme..*.*(..))\")\n"
            "    public void advice() {}\n}\n"
        ),
        "Big.java": f"package com.acme.big;\n\npublic class Big {{\n{methods}\n}}\n",
    })
    try:
        first = resolve_aop_advice(store)
        assert first["calls_created"] == 400
        second = resolve_aop_advice(store)
        assert second["stale_calls_removed"] == 400
        assert len(_derived_calls(store)) == 400
        assert not any(src == tgt for src, tgt in _derived_calls(store))
    finally:
        store.close()


_PATHOLOGICAL_SCRIPT = textwrap.dedent("""\
    import sys
    from pathlib import Path
    from code_review_graph.aop_resolver import resolve_aop_advice
    from code_review_graph.graph import GraphStore
    from code_review_graph.parser import CodeParser

    root = Path(sys.argv[1])
    source = '''package com.example.deeply.nested.pkg.service.impl;

    @Aspect
    public class Evil {
        @Before("execution(* com..*..*..*..*..*..*.zz(..))")
        public void advice() {}

        public void someReasonablyNamedBusinessMethod() {}
    }
    '''
    path = root / "Evil.java"
    nodes, edges = CodeParser().parse_bytes(path, source.encode())
    store = GraphStore(root / "graph.db")
    store.store_file_nodes_edges(str(path), nodes, edges, "hash")
    print(resolve_aop_advice(store)["calls_created"])
    store.close()
""")


def test_pathological_double_dot_pointcut_resolves_in_bounded_time(tmp_path: Path) -> None:
    """Pointcut text comes from the repository being indexed (including a
    fork's pull request in the GitHub Action). Repeated ``..*`` segments must
    not turn the translated regex into catastrophic backtracking that stalls
    ``build``/``update``. Run in a child process so a regression fails this
    test instead of hanging the suite."""
    package_root = str(Path(code_review_graph.__file__).resolve().parent.parent)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (package_root, env.get("PYTHONPATH", "")) if p
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _PATHOLOGICAL_SCRIPT, str(tmp_path)],
            capture_output=True, text=True, timeout=30, env=env, cwd=str(tmp_path),
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            "resolve_aop_advice did not finish within 30s on one method and the "
            "pointcut execution(* com..*..*..*..*..*..*.zz(..))"
        )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().splitlines()[-1] == "0"
