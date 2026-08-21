"""Liquid template indexing (#348)."""

from pathlib import Path

from code_review_graph.parser import CodeParser


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
    outputs = [node for node in nodes if node.kind == "Output"]
    rendered = {
        edge.target
        for edge in edges
        if edge.kind == "REFERENCES"
    }

    assert len(file_nodes) == 1
    assert file_nodes[0].language == "liquid"
    assert file_nodes[0].line_end == 5
    assert variables == {"product_grid": (1, 1)}
    assert len(outputs) == 1
    assert rendered == {"product-card"}


def test_whitespace_liquid_tag_is_indexed(tmp_path: Path) -> None:
    template = tmp_path / "collection.liquid"
    template.write_text(
        "{% liquid\n"
        "assign featured = collection.products\n"
        "render 'featured-card'\n"
        "%}\n",
        encoding="utf-8",
    )

    nodes, edges = CodeParser(tmp_path).parse_file(template)

    assert "featured" in {node.name for node in nodes if node.kind == "Variable"}
    assert "featured-card" in {
        edge.target for edge in edges if edge.kind == "REFERENCES"
    }
