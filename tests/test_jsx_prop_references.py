"""JSX attribute values that are bare identifiers produce REFERENCES edges (#1235)."""

import tempfile
from pathlib import Path

from code_review_graph.parser import CodeParser


def _parse(tmp_path: Path, source: str, name: str = "App.tsx"):
    p = tmp_path / name
    p.write_text(source, encoding="utf-8")
    return CodeParser().parse_file(p)


def _ref_targets(edges) -> set:
    return {e.target.rsplit("::", 1)[-1] for e in edges if e.kind == "REFERENCES"}


def test_jsx_prop_handler_emits_reference(tmp_path):
    src = (
        "function handleSave() {\n"
        "  console.log('save')\n"
        "}\n"
        "\n"
        "export function App() {\n"
        "  return <button onClick={handleSave}>save</button>\n"
        "}\n"
    )
    _, edges = _parse(tmp_path, src)
    refs = [e for e in edges if e.kind == "REFERENCES" and e.target.endswith("handleSave")]
    assert len(refs) == 1
    assert refs[0].source.endswith("App")


def test_jsx_prop_on_self_closing_component(tmp_path):
    src = (
        "function handleOwner(o) { return o }\n"
        "export function App() {\n"
        "  return <Picker onSelect={handleOwner} />\n"
        "}\n"
    )
    _, edges = _parse(tmp_path, src)
    assert "handleOwner" in _ref_targets(edges)


def test_jsx_prop_non_identifier_and_unknown_names_skipped(tmp_path):
    src = (
        "function handleSave() {}\n"
        "export function App({ onClose }) {\n"
        "  return <button onClick={() => handleSave()} onBlur={onClose} title={'x'}>x</button>\n"
        "}\n"
    )
    _, edges = _parse(tmp_path, src)
    targets = _ref_targets(edges)
    # arrow fn is a CALLS edge, not REFERENCES; onClose is a param, not defined/imported
    assert "handleSave" not in targets
    assert "onClose" not in targets


def test_jsx_child_expression_is_not_a_reference(tmp_path):
    """{label} as a child (not an attribute value) must not emit an edge."""
    src = (
        "function label() { return 'x' }\n"
        "export function App() {\n"
        "  return <div>{label}</div>\n"
        "}\n"
    )
    _, edges = _parse(tmp_path, src)
    assert "label" not in _ref_targets(edges)


def test_jsx_handler_not_reported_dead(tmp_path):
    from code_review_graph.graph import GraphStore
    from code_review_graph.refactor import find_dead_code

    src = (
        "function handleSave() {}\n"
        "function unusedHelper() {}\n"
        "export function App() {\n"
        "  return <button onClick={handleSave}>save</button>\n"
        "}\n"
    )
    p = tmp_path / "App.tsx"
    p.write_text(src, encoding="utf-8")
    nodes, edges = CodeParser().parse_file(p)
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = GraphStore(Path(tmp_dir) / "graph.db")
        try:
            store.store_file_nodes_edges(str(p), nodes, edges, "")
            dead = {d["name"] for d in find_dead_code(store)}
            assert "handleSave" not in dead, dead
            assert "unusedHelper" in dead, dead
        finally:
            store.close()
