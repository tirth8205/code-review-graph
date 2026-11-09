"""Opt-in graph context for ``detect-changes --details`` (issue #882).

The GitHub Action's comment gives a risk score, but not the graph signals
behind it. ``detect-changes --details`` adds a bounded ``details`` object to
the JSON report: why each top symbol scored what it did, the communities,
hubs and cross-boundary edges the change touches, its impact radius, its
untested hotspots, and where the graph came from. Without the flag the report
is exactly what it was.
"""

from __future__ import annotations

import json
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

from code_review_graph import cli
from code_review_graph import communities as communities_module
from code_review_graph.change_details import (
    DETAIL_SECTIONS,
    MAX_ROWS,
    build_change_details,
    parse_detail_sections,
)
from code_review_graph.changes import analyze_changes, compute_risk_score, risk_signals
from code_review_graph.communities import detect_communities, store_communities
from code_review_graph.graph import GraphStore
from code_review_graph.parser import EdgeInfo, NodeInfo

# ---------------------------------------------------------------------------
# A small graph: two Python files and a TypeScript client around one change
# ---------------------------------------------------------------------------


def _node(store: GraphStore, name: str, path: Path, language: str, *,
          line: int, is_test: bool = False) -> str:
    store.upsert_node(
        NodeInfo(
            kind="Test" if is_test else "Function",
            name=name,
            file_path=str(path),
            line_start=line,
            line_end=line + 5,
            language=language,
            parent_name=None,
            is_test=is_test,
        ),
        file_hash="h",
    )
    return f"{path}::{name}"


def _edge(store: GraphStore, kind: str, source: str, target: str, path: Path) -> None:
    store.upsert_edge(
        EdgeInfo(kind=kind, source=source, target=target, file_path=str(path), line=1)
    )


@pytest.fixture()
def graph(tmp_path):
    root = tmp_path / "repo"
    session = root / "auth" / "session.py"
    routes = root / "auth" / "routes.py"
    client = root / "web" / "client.ts"
    tests = root / "tests" / "test_session.py"
    store = GraphStore(str(tmp_path / "graph.db"))
    qn = {
        "rotate": _node(store, "rotate_token", session, "python", line=1),
        "validate": _node(store, "validate_session", session, "python", line=10),
        "login": _node(store, "login_handler", routes, "python", line=1),
        "refresh_handler": _node(store, "refresh_handler", routes, "python", line=10),
        "client": _node(store, "refresh", client, "typescript", line=1),
        "test": _node(store, "test_validate", tests, "python", line=1, is_test=True),
    }
    _edge(store, "CALLS", qn["login"], qn["rotate"], routes)
    _edge(store, "CALLS", qn["refresh_handler"], qn["rotate"], routes)
    _edge(store, "CALLS", qn["refresh_handler"], qn["validate"], routes)
    _edge(store, "CALLS", qn["client"], qn["rotate"], client)
    _edge(store, "CALLS", qn["test"], qn["validate"], tests)
    _edge(store, "TESTED_BY", qn["validate"], qn["test"], tests)
    store.commit()
    store_communities(store, [
        {"name": "auth-session", "size": 2, "members": [qn["rotate"], qn["validate"]],
         "detector": "file"},
        {"name": "auth-routes", "size": 2,
         "members": [qn["login"], qn["refresh_handler"]], "detector": "file"},
        {"name": "web-client", "size": 1, "members": [qn["client"]], "detector": "file"},
        {"name": "tests", "size": 1, "members": [qn["test"]], "detector": "file"},
    ])
    report = analyze_changes(
        store,
        [str(session)],
        changed_ranges={str(session): [(1, 15)]},
    )
    yield store, report, qn, [str(session)]
    store.close()


def _details(graph, sections=DETAIL_SECTIONS):
    store, report, _, changed = graph
    return build_change_details(store, report, sections, changed_files=changed)


# ---------------------------------------------------------------------------
# Section names
# ---------------------------------------------------------------------------


def test_parse_detail_sections():
    assert parse_detail_sections("") == ()
    assert parse_detail_sections("  ") == ()
    assert parse_detail_sections("all") == DETAIL_SECTIONS
    assert parse_detail_sections("graph, risk,graph") == ("risk", "graph")
    assert parse_detail_sections("RISK") == ("risk",)
    with pytest.raises(ValueError, match="bogus"):
        parse_detail_sections("risk,bogus")


def test_only_the_sections_asked_for_are_built(graph):
    details = _details(graph, ("risk", "graph"))
    assert details["sections"] == ["risk", "graph"]
    assert set(details) == {"sections", "risk", "graph"}


# ---------------------------------------------------------------------------
# risk: the signals behind each score
# ---------------------------------------------------------------------------


def test_risk_signals_add_up_to_the_risk_score(graph):
    store, report, qn, _ = graph
    for name in ("rotate", "validate", "login", "client"):
        node = store.get_node(qn[name])
        signals = risk_signals(store, node)
        assert list(signals) == [
            "flows", "cross_community", "tests", "security", "callers", "churn",
        ]
        total = sum(s["score"] for s in signals.values())
        assert round(min(total, 1.0), 4) == compute_risk_score(store, node)


def test_risk_section_explains_each_top_symbol(graph):
    _, report, qn, _ = graph
    risk = _details(graph)["risk"]
    by_name = {entry["qualified_name"]: entry for entry in risk}
    priorities = {p["qualified_name"]: p["risk_score"] for p in report["review_priorities"]}
    assert set(by_name) == set(priorities)
    for name, entry in by_name.items():
        assert entry["risk_score"] == priorities[name]
    rotate = by_name[qn["rotate"]]["signals"]
    assert rotate["tests"] == {"score": 0.3, "count": 0}
    assert rotate["callers"]["count"] == 3
    # login_handler, refresh_handler and the TypeScript client sit in other
    # communities.
    assert rotate["cross_community"]["count"] == 3
    validate = by_name[qn["validate"]]["signals"]
    assert validate["tests"]["count"] == 1
    assert validate["tests"]["score"] < rotate["tests"]["score"]


# ---------------------------------------------------------------------------
# communities, hubs, coupling
# ---------------------------------------------------------------------------


def test_communities_section(graph):
    section = _details(graph)["communities"]
    assert section["in_graph"] == 4
    assert section["touched_total"] == 1
    assert section["unassigned"] == 0
    assert section["touched"] == [
        {"id": section["touched"][0]["id"], "name": "auth-session", "size": 2, "changed": 2},
    ]


def test_hubs_section_ranks_changed_symbols_by_connections(graph):
    _, _, qn, _ = graph
    section = _details(graph)["hubs"]
    # Five production symbols have connections; the test is not ranked.
    assert section["ranked"] == 5
    # Equal degrees tie on the name, so the order is stable run to run.
    assert [n["qualified_name"] for n in section["nodes"]] == [qn["rotate"], qn["validate"]]
    rotate, validate = section["nodes"]
    # Three callers.
    assert (rotate["degree"], rotate["rank"]) == (3, 1)
    # Called by refresh_handler and the test, and tested by it.
    assert (validate["degree"], validate["rank"]) == (3, 1)
    # Callers in auth-routes and web-client.
    assert rotate["communities_linked"] == 2


def test_coupling_section_finds_cross_community_and_cross_language_edges(graph):
    _, _, qn, _ = graph
    section = _details(graph)["coupling"]
    assert section["cross_language"] == 1
    # login -> rotate, refresh_handler -> rotate, refresh_handler -> validate,
    # client -> rotate. The test's call and TESTED_BY edge are not coupling.
    assert section["cross_community"] == 4
    assert section["total"] == 4
    first = section["edges"][0]
    assert first["source"] == qn["client"]
    assert first["target"] == qn["rotate"]
    assert first["reasons"] == ["cross-language", "cross-community"]
    assert (first["source_language"], first["target_language"]) == ("typescript", "python")
    assert (first["source_community"], first["target_community"]) == (
        "web-client", "auth-session",
    )


def test_two_edges_between_the_same_symbols_are_one_link(graph):
    store, _, qn, _ = graph
    routes = Path(qn["login"].split("::")[0])
    store.upsert_edge(EdgeInfo(
        kind="CALLS", source=qn["login"], target=qn["rotate"],
        file_path=str(routes), line=99,
    ))
    store.commit()
    assert sum(
        1 for e in store.get_edges_by_target(qn["rotate"])
        if e.source_qualified == qn["login"]
    ) == 2
    section = _details(graph)["coupling"]
    assert section["cross_community"] == 4
    assert section["total"] == 4


def test_a_changed_test_is_never_a_hub(tmp_path):
    store = GraphStore(str(tmp_path / "graph.db"))
    try:
        path = tmp_path / "test_big.py"
        test = _node(store, "test_everything", path, "python", line=1, is_test=True)
        prod = _node(store, "helper", tmp_path / "lib.py", "python", line=1)
        _edge(store, "CALLS", test, prod, path)
        store.commit()
        report = analyze_changes(store, [str(path)], changed_ranges={str(path): [(1, 5)]})
        assert report["changed_functions"], "the test itself is the change"
        hubs = build_change_details(
            store, report, ("hubs",), changed_files=[str(path)],
        )["hubs"]
    finally:
        store.close()
    assert hubs == {"ranked": 1, "total": 0, "nodes": []}


# ---------------------------------------------------------------------------
# impact, gaps, graph
# ---------------------------------------------------------------------------


def test_impact_section(graph):
    _, _, qn, _ = graph
    section = _details(graph)["impact"]
    assert section["depth"] >= 1
    assert section["impacted_nodes"] >= 3
    assert section["impacted_files"] >= 2
    files = {row["file_path"] for row in section["top_files"]}
    assert any(path.endswith("routes.py") for path in files)
    callers = {row["qualified_name"]: row for row in section["callers"]}
    assert callers[qn["rotate"]]["total"] == 3
    assert set(callers[qn["rotate"]]["callers"]) == {
        qn["login"], qn["refresh_handler"], qn["client"],
    }


def test_gaps_section_ranks_test_gaps_by_what_depends_on_them(graph):
    _, report, qn, _ = graph
    section = _details(graph)["gaps"]
    assert section["total"] == report["test_gaps_total"]
    hot = section["hotspots"][0]
    assert hot["qualified_name"] == qn["rotate"]
    assert hot["callers"] == 3
    assert hot["coverage"] == "none"
    assert qn["validate"] not in {h["qualified_name"] for h in section["hotspots"]}


def test_gap_with_a_name_longer_than_the_report_keeps_its_counts(tmp_path):
    # The report cuts names to 256 characters; the counts must still come
    # from the real node, not from a lookup by the cut name.
    from code_review_graph.flows import store_flows

    store = GraphStore(str(tmp_path / "graph.db"))
    try:
        path = tmp_path / "long.py"
        target = _node(store, "f" * 300, path, "python", line=1)
        for i in range(2):
            caller = _node(store, f"caller_{i}", tmp_path / f"c{i}.py", "python", line=1)
            _edge(store, "CALLS", caller, target, tmp_path / f"c{i}.py")
        store.commit()
        node = store.get_node(target)
        store_flows(store, [{
            "name": "flow", "entry_point_id": node.id, "depth": 0, "node_count": 1,
            "file_count": 1, "criticality": 0.5, "path": [node.id],
        }])
        report = analyze_changes(store, [str(path)], changed_ranges={str(path): [(1, 6)]})
        section = build_change_details(
            store, report, ("gaps",), changed_files=[str(path)],
        )["gaps"]
    finally:
        store.close()
    assert len(target) > 256
    hot = section["hotspots"][0]
    assert hot["qualified_name"] == target[:256]
    assert hot["callers"] == 2
    assert hot["flows"] == 1


def test_graph_section_reports_provenance(graph):
    store = graph[0]
    section = _details(graph)["graph"]
    stats = store.get_stats()
    assert section["nodes"] == stats.total_nodes
    assert section["edges"] == stats.total_edges
    assert section["communities"] == 4
    assert section["community_detector"] == "file"
    assert section["igraph_installed"] is communities_module.IGRAPH_AVAILABLE
    assert isinstance(section["version"], str) and section["version"]
    assert isinstance(section["schema_version"], int)


def test_details_lists_are_bounded(tmp_path):
    store = GraphStore(str(tmp_path / "graph.db"))
    try:
        path = tmp_path / "big.py"
        names = []
        for i in range(MAX_ROWS * 3):
            target = _node(store, f"fn_{i}", path, "python", line=i * 10 + 1)
            caller = _node(store, f"caller_{i}", tmp_path / f"c{i}.py", "python", line=1)
            _edge(store, "CALLS", caller, target, tmp_path / f"c{i}.py")
            names.append(target)
        store.commit()
        report = analyze_changes(
            store, [str(path)], changed_ranges={str(path): [(1, MAX_ROWS * 40)]},
        )
        details = build_change_details(
            store, report, DETAIL_SECTIONS, changed_files=[str(path)],
        )
    finally:
        store.close()
    assert len(details["risk"]) <= MAX_ROWS
    assert len(details["hubs"]["nodes"]) <= MAX_ROWS
    assert len(details["coupling"]["edges"]) <= MAX_ROWS
    assert len(details["impact"]["top_files"]) <= MAX_ROWS
    assert len(details["impact"]["callers"]) <= MAX_ROWS
    assert len(details["gaps"]["hotspots"]) <= MAX_ROWS
    assert details["gaps"]["total"] == MAX_ROWS * 3


# ---------------------------------------------------------------------------
# The community detector is recorded where the graph section can read it
# ---------------------------------------------------------------------------


def _two_file_graph(tmp_path) -> GraphStore:
    store = GraphStore(str(tmp_path / "graph.db"))
    a = tmp_path / "pkg" / "a.py"
    b = tmp_path / "other" / "b.py"
    qa = [_node(store, f"a{i}", a, "python", line=i * 10 + 1) for i in range(3)]
    qb = [_node(store, f"b{i}", b, "python", line=i * 10 + 1) for i in range(3)]
    for left, right in zip(qa, qa[1:]):
        _edge(store, "CALLS", left, right, a)
    for left, right in zip(qb, qb[1:]):
        _edge(store, "CALLS", left, right, b)
    store.commit()
    return store


def test_file_based_detection_is_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(communities_module, "IGRAPH_AVAILABLE", False)
    store = _two_file_graph(tmp_path)
    try:
        count = store_communities(store, detect_communities(store))
        assert count > 0
        assert store.get_metadata("community_detector") == "file"
    finally:
        store.close()


def test_leiden_detection_is_recorded(tmp_path, monkeypatch):
    def fake_leiden(nodes, edges, min_size, adj=None):
        return [{
            "name": "all", "level": 0, "size": len(nodes), "cohesion": 1.0,
            "dominant_language": "python", "description": "",
            "members": [n.qualified_name for n in nodes],
        }]

    monkeypatch.setattr(communities_module, "IGRAPH_AVAILABLE", True)
    # igraph loads on first use (#1139); pretend it loaded.
    monkeypatch.setattr(communities_module, "_load_igraph", lambda: True)
    monkeypatch.setattr(communities_module, "_detect_leiden", fake_leiden)
    store = _two_file_graph(tmp_path)
    try:
        store_communities(store, detect_communities(store))
        assert store.get_metadata("community_detector") == "leiden"
    finally:
        store.close()


def test_leiden_falling_back_for_lack_of_edges_is_recorded_as_file(tmp_path, monkeypatch):
    # Leiden builds its graph before it finds there is nothing to join.
    fake_igraph = types.SimpleNamespace(Graph=lambda n, directed: None)
    monkeypatch.setattr(communities_module, "IGRAPH_AVAILABLE", True)
    monkeypatch.setattr(communities_module, "ig", fake_igraph)
    store = GraphStore(str(tmp_path / "graph.db"))
    try:
        path = tmp_path / "pkg" / "a.py"
        for i in range(3):
            _node(store, f"a{i}", path, "python", line=i * 10 + 1)
        store.commit()
        store_communities(store, detect_communities(store))
        assert store.get_metadata("community_detector") == "file"
    finally:
        store.close()


def test_community_dicts_from_detect_carry_no_internal_keys(tmp_path, monkeypatch):
    # The detector tag is for store_communities; the dicts detect returns are
    # otherwise exactly what they were.
    monkeypatch.setattr(communities_module, "IGRAPH_AVAILABLE", False)
    store = _two_file_graph(tmp_path)
    try:
        for comm in detect_communities(store):
            assert "member_qns" not in comm
            assert comm["detector"] == "file"
    finally:
        store.close()


# ---------------------------------------------------------------------------
# The CLI: off by default, opt in with --details
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@example.com",
         "-c", "user.name=Test", "-c", "commit.gpgsign=false", *args],
        check=True, capture_output=True, stdin=subprocess.DEVNULL, timeout=30,
    )


@pytest.fixture()
def cli_repo(tmp_path, monkeypatch):
    monkeypatch.delenv("CRG_DATA_DIR", raising=False)
    monkeypatch.delenv("CRG_REPO_ROOT", raising=False)
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    app = repo / "src" / "app.py"
    app.write_text(
        "def greet(name):\n    return 'hello ' + name\n\n\n"
        "def main():\n    return greet('x')\n",
        encoding="utf-8",
    )
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "initial")
    app.write_text(
        "def greet(name):\n    return 'hi ' + name.upper()\n\n\n"
        "def main():\n    return greet('x')\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "change greet")

    from code_review_graph.incremental import full_build, get_db_path

    store = GraphStore(get_db_path(repo))
    try:
        full_build(repo, store)
    finally:
        store.close()
    return repo


def _detect(repo: Path, capsys, *extra: str) -> dict:
    argv = ["code-review-graph", "detect-changes", "--repo", str(repo), *extra]
    with patch.object(sys, "argv", argv):
        cli.main()
    out = capsys.readouterr().out
    return json.loads(out[out.index("{"):])


def test_detect_changes_has_no_details_by_default(cli_repo, capsys):
    result = _detect(cli_repo, capsys)
    assert "details" not in result
    assert {f["name"] for f in result["changed_functions"]} >= {"greet"}


def test_detect_changes_details_all(cli_repo, capsys):
    plain = _detect(cli_repo, capsys)
    rich = _detect(cli_repo, capsys, "--details", "all")
    details = rich.pop("details")
    assert details["sections"] == list(DETAIL_SECTIONS)
    assert set(details) == {"sections", *DETAIL_SECTIONS}
    assert details["risk"][0]["qualified_name"].endswith("app.py::greet")
    # Everything else in the report is what it was; the token-savings figure
    # moves because the response it measures is now larger.
    rich.pop("context_savings")
    plain.pop("context_savings")
    assert rich == plain


def test_detect_changes_rejects_an_unknown_section(cli_repo, capsys):
    argv = ["code-review-graph", "detect-changes", "--repo", str(cli_repo),
            "--details", "risk,bogus"]
    with patch.object(sys, "argv", argv), pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    assert "bogus" in capsys.readouterr().err


def test_detect_changes_details_needs_the_json_output(cli_repo, capsys):
    argv = ["code-review-graph", "detect-changes", "--repo", str(cli_repo),
            "--brief", "--details", "risk"]
    with patch.object(sys, "argv", argv), pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    assert "--brief" in capsys.readouterr().err
