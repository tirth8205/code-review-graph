"""HTML pages: inline scripts as code, id-anchored blocks as DocSection nodes.

A hand-written page mixes prose with a small script. Both were invisible: a
``.html`` file gave nothing at all. See: #521
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, get_db_path
from code_review_graph.parser import CodeParser, normalize_file_path
from code_review_graph.postprocessing import run_post_processing
from code_review_graph.tools.query import query_graph, semantic_search_nodes

FIXTURE = Path(__file__).parent / "fixtures" / "sample_html.html"


def _parse(source: bytes, path: Path | None = None):
    target = path or Path("/repo/page.html")
    return CodeParser().parse_bytes(target, source)


def _sections(nodes):
    return {node.name: node for node in nodes if node.kind == "DocSection"}


def _named(nodes, name):
    return next(node for node in nodes if node.name == name)


@pytest.mark.parametrize("name", ["page.html", "page.htm", "PAGE.HTML"])
def test_html_extensions_are_detected(name):
    assert CodeParser().detect_language(Path(name)) == "html"


def test_file_node_and_every_node_are_tagged_html():
    nodes, _ = CodeParser().parse_file(FIXTURE)

    assert nodes[0].kind == "File"
    assert {node.language for node in nodes} == {"html"}


def test_inline_scripts_give_functions_classes_and_calls():
    nodes, edges = CodeParser().parse_file(FIXTURE)
    names = {node.name for node in nodes if node.kind in ("Function", "Class")}

    assert {"openSearch", "renderTabs", "Tracker", "update"} <= names
    file_path = normalize_file_path(FIXTURE)
    assert any(
        edge.kind == "CALLS"
        and edge.source == f"{file_path}::openSearch"
        and edge.target == f"{file_path}::renderTabs"
        for edge in edges
    )


def test_a_script_block_can_call_a_function_from_another_block():
    """Classic scripts on one page share one global scope."""
    nodes, edges = CodeParser().parse_file(FIXTURE)
    file_path = normalize_file_path(FIXTURE)

    assert any(
        edge.kind == "CALLS"
        and edge.source == f"{file_path}::Tracker.update"
        and edge.target == f"{file_path}::renderTabs"
        for edge in edges
    )


def test_module_script_names_stay_inside_the_module():
    source = b"""<script type="module">function inner() {}</script>
<script>inner();</script>"""
    _, edges = _parse(source)

    calls = [edge.target for edge in edges if edge.kind == "CALLS"]
    assert calls == ["inner"]


def test_a_module_import_shadows_a_classic_global(tmp_path):
    util = tmp_path / "util.js"
    util.write_text("export function helper() {}\n")
    source = b"""<script>function helper() {}</script>
<script type="module">import { helper } from './util.js'; helper();</script>"""

    _, edges = CodeParser(repo_root=tmp_path).parse_bytes(tmp_path / "page.html", source)

    calls = [edge.target for edge in edges if edge.kind == "CALLS"]
    assert calls == [f"{normalize_file_path(util)}::helper"]


def test_member_call_resolves_to_a_member_assigned_function_on_the_page():
    source = b"""<script>
app.handle = function () {};
function start() { app.handle(); }
</script>"""
    _, edges = _parse(source)

    calls = [
        edge.target for edge in edges
        if edge.kind == "CALLS" and edge.source == "/repo/page.html::start"
    ]
    assert calls == ["/repo/page.html::app.handle"]


def test_line_numbers_are_those_of_the_html_file():
    nodes, edges = CodeParser().parse_file(FIXTURE)
    lines = FIXTURE.read_text().splitlines()

    for name, text in (
        ("openSearch", "function openSearch"),
        ("renderTabs", "function renderTabs"),
        ("Tracker", "class Tracker"),
    ):
        assert text in lines[_named(nodes, name).line_start - 1]
    call = next(
        edge for edge in edges
        if edge.kind == "CALLS" and edge.target.endswith("::renderTabs")
    )
    assert "renderTabs(" in lines[call.line - 1]


def test_id_anchored_blocks_become_sections_with_contains_edges():
    nodes, edges = CodeParser().parse_file(FIXTURE)
    file_path = normalize_file_path(FIXTURE)
    sections = _sections(nodes)

    assert set(sections) == {"#cover", "#s1", "#appendix"}
    assert sections["#s1"].extra["html_id"] == "s1"
    lines = FIXTURE.read_text().splitlines()
    assert 'id="s1"' in lines[sections["#s1"].line_start - 1]
    assert "</section>" in lines[sections["#s1"].line_end - 1]
    for name in sections:
        assert any(
            edge.kind == "CONTAINS"
            and edge.source == file_path
            and edge.target == f"{file_path}::{name}"
            for edge in edges
        )


def test_sections_carry_only_their_first_heading():
    nodes, _ = CodeParser().parse_file(FIXTURE)
    sections = _sections(nodes)

    assert sections["#cover"].extra["docstring"] == "Project overview"
    assert sections["#s1"].extra["docstring"] == "Scope of the work"
    assert "Prose" not in str(sections["#s1"].extra)


def test_a_heading_inside_a_nested_section_belongs_to_that_section():
    source = b"""<div id="outer"><div id="inner"><h2>Inner</h2></div></div>"""
    sections = _sections(_parse(source)[0])

    assert "docstring" not in sections["#outer"].extra
    assert sections["#inner"].extra["docstring"] == "Inner"


def test_long_headings_are_capped():
    source = b'<div id="a"><h2>' + b"word " * 200 + b"</h2></div>"
    sections = _sections(_parse(source)[0])

    assert len(sections["#a"].extra["docstring"]) <= 200


def test_headings_keep_their_entities():
    source = b"""<div id="a"><h2>Caf&eacute; au lait</h2></div>
<div id="b"><h2>&amp;</h2></div>"""
    sections = _sections(_parse(source)[0])

    assert sections["#a"].extra["docstring"] == "Caf\u00e9 au lait"
    assert sections["#b"].extra["docstring"] == "&"


def test_elements_outside_the_whitelist_are_not_sections():
    source = b"""<div class="x"></div><h2 id="h"></h2><span id="s"></span>
<section id=""></section><article id="   "></article>"""

    assert _sections(_parse(source)[0]) == {}


def test_unquoted_and_case_varied_attributes():
    source = b"""<DIV ID=one></DIV><Section Id='two'></Section><ARTICLE iD="three"></ARTICLE>"""

    assert set(_sections(_parse(source)[0])) == {"#one", "#two", "#three"}


def test_uppercase_script_tag_and_type():
    source = b"""<SCRIPT TYPE="TEXT/JAVASCRIPT">function shout() {}</SCRIPT>"""

    assert any(node.name == "shout" for node in _parse(source)[0])


def test_duplicate_ids_keep_one_node_and_say_so():
    source = b"""<div id="dup"><h2>First</h2></div>
<section id="dup"><h2>Second</h2></section>"""
    sections = _sections(_parse(source)[0])

    assert list(sections) == ["#dup"]
    assert sections["#dup"].line_start == 1
    assert sections["#dup"].extra["docstring"] == "First"
    assert sections["#dup"].extra["html_id_occurrences"] == 2


def test_template_expressions_are_not_anchors():
    source = b"""<div id="{{ item.id }}"></div><div id="row-{% if a %}1{% endif %}"></div>
<div id="<%= x %>"></div><div id="${name}"></div>"""

    assert _sections(_parse(source)[0]) == {}


def test_section_count_is_bounded(monkeypatch):
    monkeypatch.setattr(CodeParser, "_MAX_HTML_SECTIONS", 3)
    source = b"".join(b'<div id="s%d"></div>' % index for index in range(10))
    nodes, _ = _parse(source)

    assert set(_sections(nodes)) == {"#s0", "#s1", "#s2"}
    assert nodes[0].extra["html_truncated"] is True


def test_oversized_page_keeps_only_its_file_node(monkeypatch):
    monkeypatch.setattr(CodeParser, "_MAX_HTML_BYTES", 10)
    nodes, edges = _parse(b'<div id="a"><script>function f() {}</script></div>')

    assert [node.kind for node in nodes] == ["File"]
    assert nodes[0].extra["html_truncated"] is True
    assert edges == []


@pytest.mark.parametrize(
    "script_type",
    [
        "application/json",
        "application/ld+json",
        "text/x-template",
        "text/html",
        "importmap",
    ],
)
def test_scripts_that_hold_data_are_not_parsed_as_code(script_type):
    source = (
        f'<script type="{script_type}">function nope() {{}}</script>'
    ).encode()

    assert not any(node.name == "nope" for node in _parse(source)[0])


@pytest.mark.parametrize(
    "opening",
    [
        "<script>",
        '<script type="module">',
        '<script type="text/javascript; charset=utf-8">',
        '<script type="application/javascript">',
        '<script language="javascript">',
    ],
)
def test_scripts_that_hold_javascript_are_parsed(opening):
    source = f"{opening}function yes() {{}}</script>".encode()

    assert any(node.name == "yes" for node in _parse(source)[0])


def test_typescript_scripts_use_the_typescript_grammar():
    source = b"""<script lang="ts">function typed(a: number): string { return ""; }
interface Shape { size: number }</script>"""
    nodes, _ = _parse(source)

    assert _named(nodes, "typed").params is not None
    assert any(node.name == "Shape" for node in nodes)


def test_empty_and_external_scripts_add_no_code_nodes():
    source = b"""<script></script><script src="a.js"></script><script>   </script>"""
    nodes, _ = _parse(source)

    assert [node.kind for node in nodes] == ["File"]


def test_external_script_links_to_the_local_file(tmp_path):
    (tmp_path / "js").mkdir()
    helper = tmp_path / "js" / "app.js"
    helper.write_text("function fromApp() {}\n")
    page = tmp_path / "page.html"
    source = b"""<script src="js/app.js?v=2"></script>
<script src="https://cdn.example.com/lib.js"></script>
<script src="//cdn.example.com/other.js"></script>
<script src="{{ url_for('static', filename='x.js') }}"></script>
<script src="missing.js"></script>"""
    parser = CodeParser(repo_root=tmp_path)

    _, edges = parser.parse_bytes(page, source)

    targets = [edge.target for edge in edges if edge.kind == "IMPORTS_FROM"]
    assert targets == [normalize_file_path(helper), "missing.js"]
    assert all(edge.source == normalize_file_path(page) for edge in edges)


def test_root_relative_script_resolves_against_the_repo_root(tmp_path):
    (tmp_path / "static").mkdir()
    helper = tmp_path / "static" / "site.js"
    helper.write_text("")
    (tmp_path / "docs").mkdir()
    page = tmp_path / "docs" / "page.html"

    _, edges = CodeParser(repo_root=tmp_path).parse_bytes(
        page, b'<script src="/static/site.js"></script>',
    )

    assert [edge.target for edge in edges if edge.kind == "IMPORTS_FROM"] == [
        normalize_file_path(helper),
    ]


def test_root_relative_script_path_is_canonical(tmp_path):
    (tmp_path / "static").mkdir()
    site = tmp_path / "site.js"
    site.write_text("")

    _, edges = CodeParser(repo_root=tmp_path).parse_bytes(
        tmp_path / "page.html", b'<script src="/static/../site.js"></script>',
    )

    assert [edge.target for edge in edges if edge.kind == "IMPORTS_FROM"] == [
        normalize_file_path(site.resolve()),
    ]


def test_root_relative_script_that_was_forgotten_stays_bare(tmp_path):
    site = tmp_path / "site.js"
    site.write_text("")
    parser = CodeParser(repo_root=tmp_path)
    parser.exclude_files({str(site)})

    _, edges = parser.parse_bytes(tmp_path / "page.html", b'<script src="/site.js"></script>')

    assert [edge.target for edge in edges if edge.kind == "IMPORTS_FROM"] == ["/site.js"]


def test_module_import_inside_a_script_resolves_from_the_page(tmp_path):
    helper = tmp_path / "util.js"
    helper.write_text("export function helper() {}\n")
    page = tmp_path / "page.html"
    source = b"""<script type="module">
import { helper } from "./util.js";
helper();
</script>"""

    _, edges = CodeParser(repo_root=tmp_path).parse_bytes(page, source)

    assert any(
        edge.kind == "IMPORTS_FROM" and edge.target == normalize_file_path(helper)
        for edge in edges
    )


def test_inline_handler_calls_a_function_on_the_page():
    nodes, edges = CodeParser().parse_file(FIXTURE)
    file_path = normalize_file_path(FIXTURE)
    lines = FIXTURE.read_text().splitlines()

    handler = [
        edge for edge in edges
        if edge.extra.get("html_event") == "onclick"
    ]
    assert [(edge.kind, edge.source, edge.target) for edge in handler] == [
        ("CALLS", file_path, f"{file_path}::openSearch"),
    ]
    assert "onclick" in lines[handler[0].line - 1]


def test_inline_handler_ignores_names_the_page_does_not_define():
    source = b"""<button onclick="alert(1); obj.run(); known(2)"></button>
<script>function known() {} function run() {}</script>"""
    _, edges = _parse(source)

    handler = [edge.target for edge in edges if edge.extra.get("html_event")]
    assert handler == ["/repo/page.html::known"]


def test_inline_handler_ignores_strings_comments_and_members():
    source = b"""<button onclick="console.log('known()'); /* known() */ alert(1); obj . known()">
</button>
<form onsubmit="return checked()"></form>
<script>function known() {} function checked() {}</script>"""
    _, edges = _parse(source)

    handler = [edge.target for edge in edges if edge.extra.get("html_event")]
    assert handler == ["/repo/page.html::checked"]


def test_inline_handler_cannot_reach_a_module_function():
    """A module's functions are not globals, and a handler runs in global scope."""
    source = b"""<button onclick="init()"></button>
<script type="module">function init() {}</script>"""
    _, edges = _parse(source)

    assert [edge for edge in edges if edge.extra.get("html_event")] == []


@pytest.mark.parametrize(
    "source",
    [
        b"",
        b"<div",
        b'<div id="a"',
        b'<div id="open"><p>never closed',
        b"</div></div></section>",
        b"<script>function broken( {",
        b'<div id="x"><script>function f() {}</div>',
        b"\xff\xfe<div id=\"bad\xff\"></div>",
        b"<" * 2000,
    ],
)
def test_malformed_html_does_not_raise(source):
    nodes, _ = _parse(source)

    assert nodes[0].kind == "File"


def test_unclosed_section_is_still_found():
    assert "#open" in _sections(_parse(b'<div id="open"><p>never closed')[0])


def test_deeply_nested_html_does_not_overflow_the_stack():
    source = b"<div>" * 20_000 + b'<div id="deep"></div>' + b"</div>" * 20_000

    assert "#deep" in _sections(_parse(source)[0])


def test_style_blocks_add_nothing():
    source = b"<style>function nope() {}</style>"

    assert [node.kind for node in _parse(source)[0]] == ["File"]


def test_test_pages_mark_their_nodes_and_link_tests(tmp_path):
    page = tmp_path / "tests" / "page.html"
    source = b"""<script>
function test_thing() { helper(); }
function helper() {}
</script>"""

    nodes, edges = CodeParser(repo_root=tmp_path).parse_bytes(page, source)

    assert all(node.is_test for node in nodes)
    assert any(
        edge.kind == "TESTED_BY" and edge.source.endswith("::helper")
        for edge in edges
    )


def test_a_test_function_on_a_plain_page_links_what_it_calls():
    """As in a ``.js`` file, a ``test_`` function is a test wherever it lives."""
    source = b"""<script>function test_thing() { helper(); } function helper() {}</script>"""
    nodes, edges = _parse(source)

    assert _named(nodes, "test_thing").kind == "Test"
    assert any(
        edge.kind == "TESTED_BY"
        and edge.source == "/repo/page.html::helper"
        and edge.target == "/repo/page.html::test_thing"
        for edge in edges
    )


def _git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        [
            "git", "-c", "user.name=t", "-c", "user.email=t@example.com",
            "commit", "-q", "-m", "init",
        ],
        cwd=root,
        check=True,
    )


def _build(tmp_path: Path) -> Path:
    root = tmp_path.resolve()
    (root / "page.html").write_text(FIXTURE.read_text())
    (root / "helpers").mkdir()
    (root / "helpers" / "nav.js").write_text("function navigate() {}\n")
    (root / "other.html").write_text(
        '<script src="helpers/nav.js"></script><script>navigate();</script>\n',
    )
    _git_repo(root)
    store = GraphStore(get_db_path(root))
    try:
        full_build(root, store)
        run_post_processing(store)
    finally:
        store.close()
    return root


def test_graph_reaches_sections_and_scripts_from_the_file(tmp_path):
    root = _build(tmp_path)

    children = query_graph("children_of", "page.html", str(root))

    assert children["status"] == "ok"
    names = {row["name"] for row in children["results"]}
    assert {"#cover", "#s1", "#appendix", "openSearch", "renderTabs", "Tracker"} <= names


def test_graph_file_summary_lists_sections(tmp_path):
    root = _build(tmp_path)

    summary = query_graph("file_summary", "page.html", str(root))

    kinds = {row["kind"] for row in summary["results"]}
    assert {"File", "DocSection", "Function", "Class"} <= kinds


def test_heading_text_and_anchor_are_searchable(tmp_path):
    root = _build(tmp_path)

    by_heading = semantic_search_nodes("Scope", kind="DocSection", repo_root=str(root))
    by_anchor = semantic_search_nodes("appendix", kind="DocSection", repo_root=str(root))

    assert [row["name"] for row in by_heading["results"]] == ["#s1"]
    assert [row["name"] for row in by_anchor["results"]] == ["#appendix"]


def test_body_text_is_not_indexed(tmp_path):
    root = _build(tmp_path)

    found = semantic_search_nodes("Prose", kind="DocSection", repo_root=str(root))

    assert found["results"] == []


def test_callers_of_crosses_from_a_page_to_a_script_file(tmp_path):
    root = _build(tmp_path)

    callers = query_graph(
        "callers_of", "helpers/nav.js::navigate", str(root), resolution="all",
    )

    assert callers["status"] == "ok"
    assert any(row["name"].endswith("other.html") for row in callers["results"])


def test_importers_of_a_script_file_lists_its_pages(tmp_path):
    root = _build(tmp_path)

    importers = query_graph("importers_of", "helpers/nav.js", str(root))

    names = {row["importer"] for row in importers["results"]}
    assert any(name.endswith("page.html") for name in names)
    assert any(name.endswith("other.html") for name in names)


def test_handler_call_keeps_a_page_function_alive_in_callers_of(tmp_path):
    root = _build(tmp_path)

    callers = query_graph("callers_of", "page.html::openSearch", str(root))

    assert any(row["name"].endswith("page.html") for row in callers["results"])


def test_kind_filter_accepts_docsection_on_the_cli():
    from code_review_graph.cli import build_parser

    args = build_parser().parse_args(["search", "cover", "--kind", "DocSection"])

    assert args.kind == "DocSection"
