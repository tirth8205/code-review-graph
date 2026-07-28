"""Scoped/static ``Type::method`` calls are tracked as callers in Rust (#567).

In Rust, ``Type::method()`` / ``Self::method()`` / ``Type::new()`` is the
dominant call form for associated functions and constructors.  These used to be
stored as a ``CALLS`` edge whose target was the intermediate ``Type::method``
string, which matched no node key, so ``callers_of`` / ``get_impact_radius``
reported zero callers.  The post-build scoped resolver rewrites the resolvable
two-segment targets to the defining node.
"""

from __future__ import annotations

from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build
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
            "src/mailer.rs": (
                "pub struct Mailer;\n"
                "impl Mailer {\n"
                "    pub fn dispatch(to: &str) -> bool { true }\n"
                "}\n"
            ),
            "src/signup.rs": (
                "use crate::mailer::Mailer;\n"
                "pub fn register(email: &str) -> bool {\n"
                "    Mailer::dispatch(email)\n"
                "}\n"
            ),
        },
    )

    result = query_graph("callers_of", "dispatch", repo_root=str(tmp_path))
    assert result["status"] == "ok"
    assert [r["name"] for r in result["results"]] == ["register"]


def test_scoped_call_edge_is_tagged_inferred(tmp_path: Path) -> None:
    store = _build(
        tmp_path,
        {
            "src/mailer.rs": (
                "pub struct Mailer;\n"
                "impl Mailer {\n"
                "    pub fn dispatch(to: &str) -> bool { true }\n"
                "}\n"
            ),
            "src/signup.rs": (
                "use crate::mailer::Mailer;\n"
                "pub fn register(email: &str) -> bool {\n"
                "    Mailer::dispatch(email)\n"
                "}\n"
            ),
        },
    )
    resolved = [
        c for c in _calls(store)
        if c["target_qualified"].endswith("Mailer.dispatch")
    ]
    assert len(resolved) == 1
    assert resolved[0]["confidence_tier"] == "INFERRED"


def test_constructor_new_call_resolves(tmp_path: Path) -> None:
    # ``Type::new()`` is the idiomatic Rust constructor form.
    _build(
        tmp_path,
        {
            "src/mailer.rs": (
                "pub struct Mailer;\n"
                "impl Mailer {\n"
                "    pub fn build() -> Mailer { Mailer }\n"
                "}\n"
            ),
            "src/app.rs": (
                "use crate::mailer::Mailer;\n"
                "pub fn boot() -> Mailer {\n"
                "    Mailer::build()\n"
                "}\n"
            ),
        },
    )
    result = query_graph("callers_of", "build", repo_root=str(tmp_path))
    assert result["status"] == "ok"
    assert [r["name"] for r in result["results"]] == ["boot"]


def test_self_scoped_call_resolves_to_enclosing_type(tmp_path: Path) -> None:
    # ``Self::helper()`` inside an impl must resolve to the enclosing type's
    # method so the caller shows up (this same-file case is resolved from
    # lexical evidence during parsing; the resolver leaves already-resolved
    # targets alone). What matters is that the call is tracked as a caller.
    store = _build(
        tmp_path,
        {
            "src/worker.rs": (
                "pub struct Worker;\n"
                "impl Worker {\n"
                "    pub fn helper(n: u32) -> u32 { n + 1 }\n"
                "    pub fn run(&self) -> u32 { Self::helper(41) }\n"
                "}\n"
            ),
        },
    )
    resolved = [
        c for c in _calls(store)
        if c["target_qualified"].endswith("Worker.helper")
    ]
    assert len(resolved) == 1
    assert resolved[0]["source_qualified"].endswith("Worker.run")
    # The dangling ``Self::helper`` / ``Worker::helper`` form must not survive.
    targets = {c["target_qualified"] for c in _calls(store)}
    assert "Self::helper" not in targets
    assert "Worker::helper" not in targets

    result = query_graph("callers_of", "helper", repo_root=str(tmp_path))
    assert [r["name"] for r in result["results"]] == ["run"]


def test_impact_radius_of_definition_file_includes_caller(tmp_path: Path) -> None:
    _build(
        tmp_path,
        {
            "src/mailer.rs": (
                "pub struct Mailer;\n"
                "impl Mailer {\n"
                "    pub fn dispatch(to: &str) -> bool { true }\n"
                "}\n"
            ),
            "src/signup.rs": (
                "use crate::mailer::Mailer;\n"
                "pub fn register(email: &str) -> bool {\n"
                "    Mailer::dispatch(email)\n"
                "}\n"
            ),
        },
    )
    impact = get_impact_radius(
        changed_files=["src/mailer.rs"], repo_root=str(tmp_path)
    )
    assert impact["status"] == "ok"
    impacted = {n["name"] for n in impact["impacted_nodes"]}
    assert "register" in impacted


def test_unresolved_external_scoped_call_is_left_untouched(tmp_path: Path) -> None:
    # ``Vec::new`` / ``String::from`` are stdlib types with no in-graph node —
    # the edge must stay a raw, directly-extracted target.
    store = _build(
        tmp_path,
        {
            "src/app.rs": (
                "pub fn make() -> Vec<u8> {\n"
                "    Vec::new()\n"
                "}\n"
            ),
        },
    )
    external = [c for c in _calls(store) if c["target_qualified"] == "Vec::new"]
    assert len(external) == 1
    assert external[0]["confidence_tier"] == "EXTRACTED"


def test_case_sensitive_identifiers_are_not_conflated(tmp_path: Path) -> None:
    # Rust is case-sensitive: a call to ``Mailer::send`` must NOT resolve to a
    # differently-cased definition ``Mailer::Send``. The edge must stay a raw,
    # directly-extracted target rather than a bogus resolved caller.
    store = _build(
        tmp_path,
        {
            "src/mailer.rs": (
                "pub struct Mailer;\n"
                "impl Mailer {\n"
                "    pub fn Send(to: &str) -> bool { true }\n"
                "}\n"
            ),
            "src/signup.rs": (
                "use crate::mailer::Mailer;\n"
                "pub fn register(email: &str) -> bool {\n"
                "    Mailer::send(email)\n"
                "}\n"
            ),
        },
    )
    dangling = [c for c in _calls(store) if c["target_qualified"] == "Mailer::send"]
    assert len(dangling) == 1
    assert dangling[0]["confidence_tier"] == "EXTRACTED"
    # And nothing falsely resolved onto the capital-S ``Send`` definition.
    assert not any(
        c["target_qualified"].endswith("Mailer.Send")
        and c["confidence_tier"] == "INFERRED"
        for c in _calls(store)
    )


def test_case_sensitive_matching_definition_resolves(tmp_path: Path) -> None:
    # The exact-case sibling of the previous test: ``Mailer::Send`` resolves.
    _build(
        tmp_path,
        {
            "src/mailer.rs": (
                "pub struct Mailer;\n"
                "impl Mailer {\n"
                "    pub fn Send(to: &str) -> bool { true }\n"
                "}\n"
            ),
            "src/signup.rs": (
                "use crate::mailer::Mailer;\n"
                "pub fn register(email: &str) -> bool {\n"
                "    Mailer::Send(email)\n"
                "}\n"
            ),
        },
    )
    result = query_graph("callers_of", "Send", repo_root=str(tmp_path))
    assert result["status"] == "ok"
    assert [r["name"] for r in result["results"]] == ["register"]


def test_import_suffix_match_selects_correct_module(tmp_path: Path) -> None:
    # Two same-named types with the same method in different modules; the
    # imported one must win by a multi-segment path-suffix match, not by a
    # single shared segment.
    store = _build(
        tmp_path,
        {
            "src/billing/mailer.rs": (
                "pub struct Mailer;\n"
                "impl Mailer {\n"
                "    pub fn go(to: &str) -> bool { true }\n"
                "}\n"
            ),
            "src/shipping/mailer.rs": (
                "pub struct Mailer;\n"
                "impl Mailer {\n"
                "    pub fn go(to: &str) -> bool { true }\n"
                "}\n"
            ),
            "src/app.rs": (
                "use crate::shipping::mailer::Mailer;\n"
                "pub fn run() -> bool {\n"
                "    Mailer::go(\"x\")\n"
                "}\n"
            ),
        },
    )
    resolved = [
        c for c in _calls(store)
        if c["confidence_tier"] == "INFERRED" and c["target_qualified"].endswith(
            "Mailer.go"
        )
    ]
    assert len(resolved) == 1
    assert "shipping/mailer.rs" in resolved[0]["target_qualified"]
    assert "billing/mailer.rs" not in resolved[0]["target_qualified"]


def test_unrelated_import_path_does_not_resolve(tmp_path: Path) -> None:
    # The imported module matches neither same-named definition's path, so the
    # ambiguous call must be left unresolved rather than pick an unrelated one.
    store = _build(
        tmp_path,
        {
            "src/billing/mailer.rs": (
                "pub struct Mailer;\n"
                "impl Mailer {\n"
                "    pub fn go(to: &str) -> bool { true }\n"
                "}\n"
            ),
            "src/shipping/mailer.rs": (
                "pub struct Mailer;\n"
                "impl Mailer {\n"
                "    pub fn go(to: &str) -> bool { true }\n"
                "}\n"
            ),
            "src/app.rs": (
                "use crate::warehouse::mailer::Mailer;\n"
                "pub fn run() -> bool {\n"
                "    Mailer::go(\"x\")\n"
                "}\n"
            ),
        },
    )
    assert not any(c["confidence_tier"] == "INFERRED" for c in _calls(store))
    dangling = [c for c in _calls(store) if c["target_qualified"] == "Mailer::go"]
    assert len(dangling) == 1


def test_partial_suffix_from_unrelated_module_does_not_resolve(
    tmp_path: Path,
) -> None:
    """A coincidental queue/mailer suffix is not the imported Rust module."""
    store = _build(
        tmp_path,
        {
            "src/order/queue/mailer.rs": (
                "pub struct Mailer;\n"
                "impl Mailer {\n"
                "    pub fn go(to: &str) -> bool { true }\n"
                "}\n"
            ),
            "src/billing/legacy/mailer.rs": (
                "pub struct Mailer;\n"
                "impl Mailer {\n"
                "    pub fn go(to: &str) -> bool { true }\n"
                "}\n"
            ),
            "src/app.rs": (
                "use crate::warehouse::queue::mailer::Mailer;\n"
                "pub fn run() -> bool {\n"
                "    Mailer::go(\"x\")\n"
                "}\n"
            ),
        },
    )

    assert not any(c["confidence_tier"] == "INFERRED" for c in _calls(store))
    dangling = [c for c in _calls(store) if c["target_qualified"] == "Mailer::go"]
    assert len(dangling) == 1


def test_multi_segment_module_path_is_left_untouched(tmp_path: Path) -> None:
    # A fully-qualified ``crate::mailer::Mailer::dispatch`` is a multi-segment
    # path; resolving it by its last two segments would be unsound, so it stays
    # an unresolved, directly-extracted edge.
    store = _build(
        tmp_path,
        {
            "src/mailer.rs": (
                "pub struct Mailer;\n"
                "impl Mailer {\n"
                "    pub fn dispatch(to: &str) -> bool { true }\n"
                "}\n"
            ),
            "src/app.rs": (
                "pub fn run() -> bool {\n"
                "    crate::mailer::Mailer::dispatch(\"x\")\n"
                "}\n"
            ),
        },
    )
    multi = [
        c for c in _calls(store)
        if c["target_qualified"] == "crate::mailer::Mailer::dispatch"
    ]
    assert len(multi) == 1
    assert multi[0]["confidence_tier"] == "EXTRACTED"
