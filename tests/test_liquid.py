"""Liquid template indexing. See: #878, #348."""

from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, get_db_path
from code_review_graph.parser import CodeParser, normalize_file_path
from code_review_graph.search import hybrid_search, rebuild_fts_index
from code_review_graph.tools import get_impact_radius, query_graph

FIXTURE = Path(__file__).parent / "fixtures" / "sample.liquid"


def _parse(tmp_path: Path, text: str, name: str = "page.liquid"):
    template = tmp_path / name
    template.parent.mkdir(parents=True, exist_ok=True)
    template.write_text(text, encoding="utf-8")
    return CodeParser(tmp_path).parse_file(template)


def _names(nodes, kind: str) -> list[str]:
    return [node.name for node in nodes if node.kind == kind]


def _targets(edges) -> list[str]:
    return [edge.target for edge in edges if edge.kind == "IMPORTS_FROM"]


def test_liquid_file_has_file_assignment_and_render_nodes(tmp_path: Path) -> None:
    template = tmp_path / "snippets" / "product-grid.liquid"
    template.parent.mkdir()
    template.write_text(
        "{% assign product_grid = products %}\n"
        "{{ product_grid }}\n"
        "{% render 'product-card', product: product %}\n"
        "{% comment %}{% render 'commented-out' %}{% endcomment %}\n",
        encoding="utf-8",
    )

    parser = CodeParser(tmp_path)
    nodes, edges = parser.parse_file(template)

    assert parser.detect_language(template) == "liquid"
    file_nodes = [node for node in nodes if node.kind == "File"]
    variables = {
        node.name: (node.line_start, node.line_end)
        for node in nodes
        if node.kind == "Variable"
    }

    assert len(file_nodes) == 1
    assert file_nodes[0].language == "liquid"
    assert file_nodes[0].line_end == 5
    assert variables == {"product_grid": (1, 1)}
    assert _names(nodes, "Output") == ["output:product_grid"]
    assert _targets(edges) == ["product-card"]


def test_whitespace_liquid_tag_is_indexed(tmp_path: Path) -> None:
    nodes, edges = _parse(
        tmp_path,
        "{% liquid\n"
        "assign featured = collection.products\n"
        "render 'featured-card'\n"
        "%}\n",
    )

    variables = [node for node in nodes if node.kind == "Variable"]
    assert [(node.name, node.line_start) for node in variables] == [("featured", 2)]
    assert _targets(edges) == ["featured-card"]
    assert [edge.line for edge in edges if edge.kind == "IMPORTS_FROM"] == [3]


@pytest.mark.parametrize(
    "literal",
    ["raw", "comment", "doc", "schema", "javascript", "stylesheet"],
)
def test_literal_blocks_are_not_read_as_live_code(tmp_path: Path, literal: str) -> None:
    nodes, edges = _parse(
        tmp_path,
        f"{{%- {literal} -%}}\n"
        "{% assign ghost = 1 %}\n"
        "{{ ghost_output }}\n"
        "{% render 'ghost-snippet' %}\n"
        f"{{%- end{literal} -%}}\n"
        "{{ real_output }}\n",
    )

    assert _names(nodes, "Variable") == []
    assert _names(nodes, "Output") == ["output:real_output"]
    assert _targets(edges) == []
    # Masking must not move what follows.
    assert [n.line_start for n in nodes if n.kind == "Output"] == [6]


def test_inline_comment_tag_is_not_read_as_live_code(tmp_path: Path) -> None:
    nodes, edges = _parse(
        tmp_path,
        "{% # {{ ghost_output }} render 'ghost-snippet' %}\n"
        "{% # assign ghost = 1\n"
        "   # second comment line %}\n"
        "{{ real_output }}\n",
    )

    assert _names(nodes, "Variable") == []
    assert _names(nodes, "Output") == ["output:real_output"]
    assert _targets(edges) == []


def test_comment_lines_inside_a_liquid_tag_are_skipped(tmp_path: Path) -> None:
    nodes, edges = _parse(
        tmp_path,
        "{% liquid\n"
        "  # assign ghost_var = 1\n"
        "  # render 'ghost-snippet'\n"
        "  comment\n"
        "    assign other_ghost = 1\n"
        "  endcomment\n"
        "  assign real_var = 2\n"
        "%}\n",
    )

    assert _names(nodes, "Variable") == ["real_var"]
    assert _targets(edges) == []


def test_quoted_text_is_not_read_as_a_directive(tmp_path: Path) -> None:
    nodes, edges = _parse(
        tmp_path,
        "{% assign label = \"render 'ghost' and assign phantom = 1\" %}\n"
        "{% assign shown = \"{{ not_output }}\" %}\n"
        "{% liquid\n"
        "  assign inner = \"render 'other-ghost'\"\n"
        "%}\n",
    )

    assert _names(nodes, "Variable") == ["label", "shown", "inner"]
    assert _names(nodes, "Output") == []
    assert _targets(edges) == []


def test_include_and_double_quotes_are_references(tmp_path: Path) -> None:
    _nodes, edges = _parse(
        tmp_path,
        "{% include 'single-include' %}\n"
        "{% include \"double-include\" %}\n"
        "{% render \"double-render\", item: item %}\n"
        "{% render dynamic_name %}\n",
    )

    assert _targets(edges) == ["single-include", "double-include", "double-render"]
    directives = [edge.extra["liquid_directive"] for edge in edges if edge.kind == "IMPORTS_FROM"]
    assert directives == ["include", "include", "render"]


@pytest.mark.parametrize("text", ["", "<p>plain</p>\n", "{{ }}\n{{- -}}\n"])
def test_file_with_nothing_to_index_has_only_its_file_node(tmp_path: Path, text: str) -> None:
    nodes, edges = _parse(tmp_path, text)

    assert [node.kind for node in nodes] == ["File"]
    assert edges == []


def test_trim_markers_and_multi_line_output(tmp_path: Path) -> None:
    nodes, edges = _parse(
        tmp_path,
        "{%- assign trimmed = 1 -%}\n"
        "{{- product.title -}}\n"
        "{{\n"
        "  product.price\n"
        "  | money\n"
        "}}\n"
        "{%- render 'trimmed-card' -%}\n",
    )

    outputs = {
        node.name: (node.line_start, node.line_end)
        for node in nodes
        if node.kind == "Output"
    }
    assert outputs == {
        "output:product.title": (2, 2),
        "output:product.price | money": (3, 6),
    }
    assert _names(nodes, "Variable") == ["trimmed"]
    assert _targets(edges) == ["trimmed-card"]


def test_output_names_do_not_depend_on_the_line(tmp_path: Path) -> None:
    text = "{{ product.title }}\n{{ product.price }}\n"
    before, _ = _parse(tmp_path, text)
    after, _ = _parse(tmp_path, "{% comment %}new line{% endcomment %}\n" + text)

    assert _names(before, "Output") == _names(after, "Output")


def test_repeated_names_keep_every_line(tmp_path: Path) -> None:
    nodes, _edges = _parse(
        tmp_path,
        "{% assign ratio = 1 %}\n"
        "{{ ratio }}\n"
        "{% assign ratio = 2 %}\n"
        "{{ ratio }}\n",
    )

    variable = next(node for node in nodes if node.kind == "Variable")
    output = next(node for node in nodes if node.kind == "Output")
    assert (variable.name, variable.line_start, variable.extra["lines"]) == ("ratio", 1, [1, 3])
    assert (output.name, output.line_start, output.extra["lines"]) == ("output:ratio", 2, [2, 4])
    assert len(nodes) == 3


def test_long_outputs_stay_distinct(tmp_path: Path) -> None:
    shared = "a" * 150
    nodes, _edges = _parse(tmp_path, f"{{{{ {shared}1 }}}}\n{{{{ {shared}2 }}}}\n")

    names = _names(nodes, "Output")
    assert len(set(names)) == 2
    assert all(len(name) < 150 for name in names)


def test_render_links_to_the_snippet_file_node(tmp_path: Path) -> None:
    snippet = tmp_path / "theme" / "snippets" / "card.liquid"
    snippet.parent.mkdir(parents=True)
    snippet.write_text("<div></div>\n", encoding="utf-8")

    _nodes, edges = _parse(
        tmp_path,
        "{% render 'card' %}\n"
        "{% include 'card.liquid' %}\n"
        "{% render 'missing' %}\n"
        "{% render '../../escape' %}\n",
        name="theme/sections/main.liquid",
    )

    assert _targets(edges) == [
        normalize_file_path(snippet),
        normalize_file_path(snippet),
        "missing",
        "../../escape",
    ]


def test_snippet_lookup_stays_inside_the_repository(tmp_path: Path) -> None:
    outside = tmp_path / "snippets" / "card.liquid"
    outside.parent.mkdir()
    outside.write_text("<div></div>\n", encoding="utf-8")
    repo = tmp_path / "repo"
    template = repo / "sections" / "main.liquid"
    template.parent.mkdir(parents=True)
    template.write_text("{% render 'card' %}\n", encoding="utf-8")

    _nodes, edges = CodeParser(repo).parse_file(template)

    assert _targets(edges) == ["card"]


def test_who_renders_a_snippet_end_to_end(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    (root / ".git").mkdir()
    (root / "snippets").mkdir()
    (root / "sections").mkdir()
    snippet = root / "snippets" / "card.liquid"
    snippet.write_text("{{ product.title }}\n", encoding="utf-8")
    page = root / "sections" / "main.liquid"
    page.write_text("{% render 'card' %}\n", encoding="utf-8")

    store = GraphStore(get_db_path(root))
    try:
        full_build(root, store)
    finally:
        store.close()

    importers = query_graph("importers_of", "snippets/card.liquid", str(root))
    assert [row["file"] for row in importers["results"]] == [normalize_file_path(page)]

    impact = get_impact_radius(["snippets/card.liquid"], repo_root=str(root))
    assert normalize_file_path(page) in impact["impacted_files"]


def test_sample_fixture_is_indexed() -> None:
    nodes, edges = CodeParser(FIXTURE.parent).parse_file(FIXTURE)

    assert _names(nodes, "Variable") == ["heading", "items"]
    assert _names(nodes, "Output") == ["output:heading", "output:item.title | escape"]
    assert _targets(edges) == ["product-card"]


def test_outputs_stay_out_of_search_unless_asked_for(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    (root / ".git").mkdir()
    (root / "page.liquid").write_text(
        "{% assign product_card = 1 %}\n"
        + "".join(f"{{{{ product.field_{index} }}}}\n" for index in range(100)),
        encoding="utf-8",
    )
    (root / "app.py").write_text("def product_lookup():\n    return 1\n", encoding="utf-8")

    store = GraphStore(get_db_path(root))
    try:
        full_build(root, store)
        rebuild_fts_index(store)

        kinds = {hit["kind"] for hit in hybrid_search(store, "product", limit=10)}
        outputs = hybrid_search(store, "product", kind="Output", limit=10)
        names = {node.name for node in store.search_nodes("product", limit=200)}
        count = store.count_search_nodes("product")
    finally:
        store.close()

    assert "Output" not in kinds
    assert {"Variable", "Function"} <= kinds
    assert len(outputs) == 10
    assert {"product_card", "product_lookup"} <= names
    assert not any(name.startswith("output:") for name in names)
    assert count == len(names)
