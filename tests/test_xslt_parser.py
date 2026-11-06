"""XSLT stylesheets: templates as nodes, and the links between them as edges.

``.xsl`` and ``.xslt`` files gave nothing, which leaves a gap in projects that
transform XML, as many C# ones do. See: #1052
"""

from __future__ import annotations

from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, get_db_path
from code_review_graph.parser import CodeParser, normalize_file_path
from code_review_graph.postprocessing import run_post_processing
from code_review_graph.refactor import find_dead_code
from code_review_graph.tools.query import query_graph, semantic_search_nodes

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE = FIXTURES / "sample_xslt.xsl"
COMMON = FIXTURES / "sample_xslt_common.xsl"
QN = normalize_file_path(FIXTURE)
XSL_NS = 'xmlns:xsl="http://www.w3.org/1999/XSL/Transform"'


def _stylesheet(body: str, ns: str = XSL_NS) -> bytes:
    return f'<xsl:stylesheet version="1.0" {ns}>\n{body}\n</xsl:stylesheet>'.encode()


def _parse(source: bytes, path: Path | None = None):
    return CodeParser().parse_bytes(path or Path("/repo/a.xsl"), source)


def _templates(nodes):
    return {node.name: node for node in nodes if node.kind == "Function"}


def _calls(edges):
    return [edge for edge in edges if edge.kind == "CALLS"]


def _call_pairs(edges, source=None):
    return {
        (edge.source, edge.target) for edge in _calls(edges)
        if source is None or edge.source == source
    }


@pytest.fixture(scope="module")
def parsed():
    return CodeParser().parse_file(FIXTURE)


@pytest.mark.parametrize("name", ["a.xsl", "a.xslt", "A.XSL"])
def test_xslt_extensions_are_detected(name):
    assert CodeParser().detect_language(Path(name)) == "xslt"


def test_file_node_and_language(parsed):
    nodes, _ = parsed

    assert nodes[0].kind == "File"
    assert {node.language for node in nodes} == {"xslt"}


def test_named_match_and_function_templates_are_nodes(parsed):
    names = set(_templates(parsed[0]))

    assert {
        "match=/",
        "page.header",
        "match=section | appendix",
        "match=title mode=toc",
        "match=para[@role='note'] mode=toc",
        "match=para mode=toc",
        "match=footer mode=plain",
        "f:clean",
    } <= names


def test_template_kinds_and_details_are_recorded(parsed):
    templates = _templates(parsed[0])

    assert templates["page.header"].extra["xslt_kind"] == "named_template"
    assert templates["page.header"].params == "(title, level)"
    assert templates["f:clean"].extra["xslt_kind"] == "function"
    note = templates["match=para[@role='note'] mode=toc"]
    assert note.extra["xslt_kind"] == "match_template"
    assert note.extra["match"] == "para[@role='note']"
    assert note.extra["mode"] == "toc"
    assert note.extra["priority"] == "2"


def test_template_lines_cover_the_element(parsed):
    lines = FIXTURE.read_text().splitlines()
    header = _templates(parsed[0])["page.header"]

    assert '<xsl:template name="page.header">' in lines[header.line_start - 1]
    assert "</xsl:template>" in lines[header.line_end - 1]


def test_same_match_and_mode_keep_both_templates(parsed):
    """Priorities let two templates share a pattern, so neither may be lost."""
    nodes, edges = parsed
    para = [node for node in nodes if node.name == "match=para mode=toc"]
    parser = CodeParser()

    assert len(para) == 2
    assert len({parser._node_qualified(node) for node in para}) == 2
    assert {
        edge.target for edge in edges
        if edge.kind == "CONTAINS" and "match=para mode=toc" in edge.target
    } == {parser._node_qualified(node) for node in para}


def test_contains_edges_link_the_file_to_each_template(parsed):
    nodes, edges = parsed
    parser = CodeParser()

    contained = {edge.target for edge in edges if edge.kind == "CONTAINS"}
    assert {
        parser._node_qualified(node) for node in nodes if node.kind == "Function"
    } == contained


def test_call_template_links_to_the_named_template(parsed):
    _, edges = parsed

    assert (f"{QN}::match=/", f"{QN}::page.header") in _call_pairs(edges)
    assert (f"{QN}::f:clean", f"{QN}::page.header") in _call_pairs(edges)


def test_call_template_to_another_file_stays_a_bare_name(parsed):
    _, edges = parsed

    assert (f"{QN}::match=/", "shared.footer") in _call_pairs(edges)


def test_apply_templates_links_by_the_last_step_of_select(parsed):
    _, edges = parsed

    # ``report/section`` reaches a pattern that lists ``section``.
    assert (f"{QN}::match=/", f"{QN}::match=section | appendix") in _call_pairs(edges)
    assert (
        f"{QN}::match=/", f"{QN}::match=footer mode=plain",
    ) in _call_pairs(edges)


def test_apply_templates_only_links_inside_its_mode(parsed):
    _, edges = parsed
    from_section = _call_pairs(edges, f"{QN}::match=section | appendix")

    assert (
        f"{QN}::match=section | appendix", f"{QN}::match=title mode=toc",
    ) in from_section
    assert not any(target.endswith("::match=/") for _, target in from_section)


def test_apply_templates_marks_its_edges(parsed):
    _, edges = parsed
    edge = next(
        edge for edge in _calls(edges)
        if edge.target == f"{QN}::match=title mode=toc"
    )

    assert edge.extra["xslt_call"] == "apply-templates"
    assert edge.extra["select"] == "title"
    assert edge.extra["mode"] == "toc"
    assert FIXTURE.read_text().splitlines()[edge.line - 1].strip().startswith(
        '<xsl:apply-templates select="title"',
    )


def test_a_mode_without_select_links_to_every_template_in_that_mode(parsed):
    _, edges = parsed
    targets = {
        target for _, target in _call_pairs(edges, f"{QN}::match=section | appendix")
    }

    assert f"{QN}::match=para[@role='note'] mode=toc" in targets
    assert f"{QN}::match=para mode=toc" in targets


def test_current_mode_means_the_enclosing_templates_mode(parsed):
    _, edges = parsed

    assert any(
        edge.source == f"{QN}::match=footer mode=plain"
        and edge.target == f"{QN}::match=footer mode=plain"
        and edge.extra["mode"] == "plain"
        for edge in _calls(edges)
    )


def test_an_unknowable_dispatch_adds_no_edge_and_is_counted(parsed):
    """A bare apply-templates could reach anything; the graph says it cannot tell."""
    nodes, edges = parsed
    templates = _templates(nodes)
    note = f"{QN}::match=para[@role='note'] mode=toc"

    assert _call_pairs(edges, note) == set()
    assert templates["match=para[@role='note'] mode=toc"].extra["dynamic_apply_count"] == 1
    assert templates["match=/"].extra["dynamic_apply_count"] == 1


def test_apply_with_no_local_match_keeps_a_bare_target(parsed):
    source = _stylesheet(
        '<xsl:template match="a"><xsl:apply-templates select="b" mode="m"/></xsl:template>',
    )

    _, edges = _parse(source)

    assert [edge.target for edge in _calls(edges)] == ["match=b mode=m"]


@pytest.mark.parametrize("select", [".", "..", "*", "node()", "$nodes", "@*|node()", "a|*"])
def test_selects_that_name_nothing_keep_no_bare_target(select):
    source = _stylesheet(
        f'<xsl:template match="a"><xsl:apply-templates select="{select}"/></xsl:template>',
    )

    nodes, edges = _parse(source)

    assert _calls(edges) == []
    assert _templates(nodes)["match=a"].extra["dynamic_apply_count"] == 1


def test_select_dot_with_a_mode_links_inside_that_mode():
    source = _stylesheet(
        """<xsl:template match="a"><xsl:apply-templates select="." mode="m"/></xsl:template>
<xsl:template match="b" mode="m"/>""",
    )

    _, edges = _parse(source)

    assert [edge.target for edge in _calls(edges)] == ["/repo/a.xsl::match=b mode=m"]


def test_a_large_mode_is_not_fanned_out(monkeypatch):
    monkeypatch.setattr(CodeParser, "_MAX_XSLT_MODE_FANOUT", 2)
    source = _stylesheet(
        '<xsl:template match="root"><xsl:apply-templates mode="m"/></xsl:template>\n'
        + "\n".join(f'<xsl:template match="e{i}" mode="m"/>' for i in range(3)),
    )

    nodes, edges = _parse(source)

    assert _calls(edges) == []
    assert _templates(nodes)["match=root"].extra["dynamic_apply_count"] == 1


def test_modes_listing_several_names_and_all(tmp_path):
    source = _stylesheet(
        """<xsl:template match="root">
  <xsl:apply-templates select="x" mode="one"/>
  <xsl:apply-templates select="y" mode="other"/>
</xsl:template>
<xsl:template match="x" mode="one two"/>
<xsl:template match="y" mode="#all"/>""",
    )

    _, edges = _parse(source)

    targets = {edge.target.rsplit("::", 1)[-1] for edge in _calls(edges)}
    assert targets == {"match=x mode=one two", "match=y mode=#all"}


def test_import_and_include_link_to_the_local_file(parsed):
    _, edges = parsed
    imports = [edge for edge in edges if edge.kind == "IMPORTS_FROM"]

    assert [(edge.target, edge.extra["xslt_import"]) for edge in imports] == [
        (normalize_file_path(COMMON), "import"),
    ]
    assert imports[0].source == QN


def test_import_that_does_not_resolve_keeps_its_text(tmp_path):
    source = _stylesheet('<xsl:include href="missing/parts.xsl"/>')

    _, edges = CodeParser(repo_root=tmp_path).parse_bytes(tmp_path / "a.xsl", source)

    assert [edge.target for edge in edges if edge.kind == "IMPORTS_FROM"] == [
        "missing/parts.xsl",
    ]


def test_import_cannot_leave_the_repository(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "outside.xsl").write_text("<x/>")
    source = _stylesheet('<xsl:import href="../outside.xsl"/>')

    _, edges = CodeParser(repo_root=repo).parse_bytes(repo / "a.xsl", source)

    assert [edge.target for edge in edges if edge.kind == "IMPORTS_FROM"] == [
        "../outside.xsl",
    ]


def test_another_prefix_for_the_xslt_namespace():
    source = b"""<t:transform version="1.0" xmlns:t="http://www.w3.org/1999/XSL/Transform"
    xmlns:xsl="http://example.com/not-xslt">
  <t:template name="real"><t:call-template name="other"/></t:template>
  <xsl:template name="fake"/>
</t:transform>"""

    nodes, edges = _parse(source)

    assert set(_templates(nodes)) == {"real"}
    assert [edge.target for edge in _calls(edges)] == ["other"]


def test_a_simplified_stylesheet_links_from_the_file():
    source = b"""<html xsl:version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform">
  <xsl:call-template name="elsewhere"/>
</html>"""

    nodes, edges = _parse(source)

    assert [node.kind for node in nodes] == ["File"]
    assert _call_pairs(edges) == {("/repo/a.xsl", "elsewhere")}


def test_literal_elements_inside_a_template_are_walked():
    source = _stylesheet(
        """<xsl:template name="a"><div><span><xsl:if test="1">
<xsl:call-template name="b"/></xsl:if></span></div></xsl:template>
<xsl:template name="b"/>""",
    )

    _, edges = _parse(source)

    assert _call_pairs(edges) == {("/repo/a.xsl::a", "/repo/a.xsl::b")}


def test_templates_with_neither_name_nor_match_are_skipped():
    nodes, _ = _parse(_stylesheet("<xsl:template/><xsl:function/>"))

    assert _templates(nodes) == {}


def test_long_patterns_are_capped_in_the_name():
    pattern = "a" * 1000
    nodes, _ = _parse(_stylesheet(f'<xsl:template match="{pattern}"/>'))

    template = next(iter(_templates(nodes).values()))
    assert len(template.name) <= 200
    assert template.extra["match"] == pattern[:512]


def test_whitespace_in_a_pattern_is_collapsed():
    source = _stylesheet('<xsl:template match="a\n     | b"/>')

    assert set(_templates(_parse(source)[0])) == {"match=a | b"}


def test_escaped_text_in_a_pattern_is_read():
    source = _stylesheet('<xsl:template match="a[@n &gt; 1]"/>')

    assert set(_templates(_parse(source)[0])) == {"match=a[@n > 1]"}


def test_union_inside_a_predicate_does_not_split_the_pattern():
    source = _stylesheet(
        """<xsl:template name="go"><xsl:apply-templates select="b"/></xsl:template>
<xsl:template match="a[b|c]"/>
<xsl:template match="z/b[1]"/>""",
    )

    _, edges = _parse(source)

    assert {target.rsplit("::", 1)[-1] for _, target in _call_pairs(edges)} == {
        "match=z/b[1]",
    }


def test_oversized_stylesheet_keeps_only_its_file_node(monkeypatch):
    monkeypatch.setattr(CodeParser, "_MAX_XSLT_BYTES", 10)

    nodes, edges = _parse(_stylesheet('<xsl:template name="a"/>'))

    assert [node.kind for node in nodes] == ["File"]
    assert nodes[0].extra["xslt_truncated"] is True
    assert edges == []


@pytest.mark.parametrize(
    "source",
    [
        b"",
        b"not xml at all",
        b"<xsl:stylesheet",
        _stylesheet('<xsl:template name="open">'),
        _stylesheet('<xsl:template name="a"></xsl:wrong>'),
        b'\xff\xfe<xsl:stylesheet xmlns:xsl="x"/>',
        b"<" * 2000,
    ],
)
def test_malformed_stylesheets_do_not_raise(source):
    nodes, _ = _parse(source)

    assert nodes[0].kind == "File"


def test_deep_nesting_does_not_overflow_the_stack():
    body = "<div>" * 20_000 + '<xsl:call-template name="b"/>' + "</div>" * 20_000
    source = _stylesheet(f'<xsl:template name="a">{body}</xsl:template>')

    nodes, _ = _parse(source)

    assert nodes[0].kind == "File"


def _build(tmp_path: Path) -> Path:
    root = tmp_path.resolve()
    (root / "sample_xslt.xsl").write_text(FIXTURE.read_text())
    (root / "sample_xslt_common.xsl").write_text(COMMON.read_text())
    (root / ".git").mkdir()
    store = GraphStore(get_db_path(root))
    try:
        full_build(root, store)
        run_post_processing(store)
    finally:
        store.close()
    return root


def test_graph_callers_and_callees(tmp_path):
    root = _build(tmp_path)

    callers = query_graph("callers_of", "sample_xslt.xsl::page.header", str(root))
    callees = query_graph("callees_of", "sample_xslt.xsl::match=/", str(root))

    assert callers["status"] == "ok"
    assert {"match=/", "match=title mode=toc", "f:clean"} <= {
        row["name"] for row in callers["results"]
    }
    assert "page.header" in {row["name"] for row in callees["results"]}


def test_call_into_an_imported_file_resolves_after_post_processing(tmp_path):
    root = _build(tmp_path)

    callers = query_graph(
        "callers_of", "sample_xslt_common.xsl::shared.footer", str(root),
    )

    assert "match=/" in {row["name"] for row in callers["results"]}
    assert all(
        "target_resolution" not in row for row in callers["results"]
        if row["name"] == "match=/"
    )


def test_graph_importers_of(tmp_path):
    root = _build(tmp_path)

    importers = query_graph("importers_of", "sample_xslt_common.xsl", str(root))

    assert any(row["importer"].endswith("sample_xslt.xsl") for row in importers["results"])


def test_templates_are_searchable_by_name_and_pattern(tmp_path):
    root = _build(tmp_path)

    by_name = semantic_search_nodes("page.header", repo_root=str(root))
    by_pattern = semantic_search_nodes("appendix", repo_root=str(root))

    assert "page.header" in {row["name"] for row in by_name["results"]}
    assert "match=section | appendix" in {row["name"] for row in by_pattern["results"]}


def test_match_templates_are_not_called_dead(tmp_path):
    """The processor picks them from the input document, so no caller is normal."""
    root = _build(tmp_path)
    store = GraphStore(get_db_path(root))
    try:
        dead = {row["name"] for row in find_dead_code(store, root=root)}
    finally:
        store.close()

    assert "match=legacy" not in dead
    assert "match=/" not in dead
