"""Components wrapped in a higher-order call (forwardRef, memo, ...) get Function nodes."""

from pathlib import Path

from code_review_graph.parser import CodeParser


def _parse(tmp_path: Path, name: str, source: str):
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return path, CodeParser().parse_file(path)


def _functions(nodes):
    return {node.name: node for node in nodes if node.kind == "Function"}


def test_forward_ref_component_gets_a_function_node(tmp_path):
    path, (nodes, edges) = _parse(
        tmp_path,
        "Button.tsx",
        "import { forwardRef } from 'react';\n"
        "export const Button = forwardRef<HTMLButtonElement, Props>((props, ref) => (\n"
        "  <button ref={ref} {...props} />\n"
        "));\n",
    )
    functions = _functions(nodes)
    assert "Button" in functions
    assert functions["Button"].line_start == 2
    assert functions["Button"].line_end == 4
    qualified = f"{path.as_posix()}::Button"
    assert any(edge.kind == "CONTAINS" and edge.target == qualified for edge in edges)


def test_nested_and_plain_wrappers_are_unwrapped(tmp_path):
    _path, (nodes, _edges) = _parse(
        tmp_path,
        "widgets.jsx",
        "export const Card = memo(forwardRef((props, ref) => <div ref={ref} />));\n"
        "export const Store = observer(() => <span />);\n"
        "export const Page = withRouter(function Page(props) { return <main />; });\n"
        "export const Fancy = styled(Base)((props) => ({ color: props.color }));\n",
    )
    assert {"Card", "Store", "Page", "Fancy"} <= set(_functions(nodes))


def test_calls_inside_a_wrapped_component_are_attributed_to_it(tmp_path):
    path, (_nodes, edges) = _parse(
        tmp_path,
        "Input.tsx",
        "export const Input = forwardRef((props, ref) => {\n"
        "  useImperativeHandle(ref, () => ({}));\n"
        "  return <input />;\n"
        "});\n",
    )
    calls = [edge for edge in edges if edge.kind == "CALLS"]
    assert any(
        edge.source == f"{path.as_posix()}::Input" and edge.target.endswith("useImperativeHandle")
        for edge in calls
    )


def test_calls_without_a_function_argument_stay_plain_variables(tmp_path):
    _path, (nodes, _edges) = _parse(
        tmp_path,
        "setup.ts",
        "const client = createClient({ url: 'x' });\n"
        "const Title = styled.h1`color: red;`;\n"
        "const total = sum(1, 2);\n",
    )
    assert not _functions(nodes)


def test_jsx_use_of_a_wrapped_component_targets_an_existing_node(tmp_path):
    button_path, (button_nodes, _button_edges) = _parse(
        tmp_path,
        "Button.tsx",
        "import { forwardRef } from 'react';\n"
        "export const Button = forwardRef((props, ref) => <button ref={ref} {...props} />);\n",
    )
    _toolbar_path, (_toolbar_nodes, toolbar_edges) = _parse(
        tmp_path,
        "Toolbar.tsx",
        "import { Button } from './Button';\nexport const Toolbar = () => <Button>Save</Button>;\n",
    )
    calls = [edge for edge in toolbar_edges if edge.kind == "CALLS"]
    assert any(edge.target == f"{button_path.resolve().as_posix()}::Button" for edge in calls)
    assert "Button" in _functions(button_nodes)


def _call_pairs(path: Path, edges):
    prefix = f"{path.resolve().as_posix()}::"
    return {
        (edge.source.replace(prefix, ""), edge.target.rsplit("::", 1)[-1].rsplit("/", 1)[-1])
        for edge in edges
        if edge.kind == "CALLS"
    }


def test_a_callback_taking_call_is_not_a_component_definition(tmp_path):
    """A call that takes a callback among others computes a value, it does not
    define a function. Naming the variable after the first callback would invent
    one and drop the wrapper and the other arguments."""
    path, (nodes, edges) = _parse(
        tmp_path,
        "setup.js",
        "function setup() { const result = choose(() => left(), () => right()); return result; }\n",
    )

    assert set(_functions(nodes)) == {"setup"}
    assert _call_pairs(path, edges) == {
        ("setup", "choose"),
        ("setup", "left"),
        ("setup", "right"),
    }


def test_a_two_argument_hook_keeps_its_calls_on_the_enclosing_function(tmp_path):
    path, (nodes, edges) = _parse(
        tmp_path,
        "hook.js",
        "function setup() { const value = useMemo(() => compute(), [dep]); return value; }\n",
    )

    assert set(_functions(nodes)) == {"setup"}
    assert _call_pairs(path, edges) == {("setup", "useMemo"), ("setup", "compute")}


def test_a_wrapped_component_keeps_the_wrapper_calls_in_the_enclosing_scope(tmp_path):
    """The wrapper runs where the declaration is, not inside the component it makes.

    This case used to assert ("Card", "memo"), which read the other way round: the
    component was credited with a call made before it existed, and on a declaration
    inside a function that moved the call off the function that really makes it (#972).
    """
    path, (nodes, edges) = _parse(
        tmp_path,
        "Card.jsx",
        "function setup() {\n"
        "  const Card = memo(forwardRef((props, ref) => { paint(); return null; }));\n"
        "  return Card;\n"
        "}\n",
    )

    assert set(_functions(nodes)) == {"Card", "setup"}
    assert _call_pairs(path, edges) == {
        ("setup", "memo"),
        ("setup", "forwardRef"),
        ("Card", "paint"),
    }


def test_a_value_returning_single_argument_call_is_not_a_component(tmp_path):
    """A lone function argument does not make the result callable (#972).

    `evaluate(() => compute())` has the same shape as `memo(() => paint())` and
    returns a number. Reading it as a definition invented a `result` function and
    moved `compute`'s caller off `setup`, which is the function that makes the call.
    """
    path, (nodes, edges) = _parse(
        tmp_path,
        "app.ts",
        "function compute(): number { return 1; }\n"
        "function evaluate(cb: () => number): number { return cb(); }\n"
        "export function setup(): number {\n"
        "  const result = evaluate(() => compute());\n"
        "  return result;\n"
        "}\n",
    )

    assert set(_functions(nodes)) == {"compute", "evaluate", "setup"}
    assert ("setup", "evaluate") in _call_pairs(path, edges)
    assert ("setup", "compute") in _call_pairs(path, edges)


def test_a_sibling_declarator_keeps_its_call(tmp_path):
    """One declaration can define a component AND call a function (#972).

    The caller skips its generic recursion over the whole declaration as soon as a
    function is extracted from it, so a declarator this pass does not own has to be
    walked here or its calls are lost.
    """
    path, (nodes, edges) = _parse(
        tmp_path,
        "component.tsx",
        "import { memo } from 'react';\n"
        "function nextToken(): number { return 1; }\n"
        "export function setup() {\n"
        "  const Button = memo(() => paint()), token = nextToken();\n"
        "  return [Button, token];\n"
        "}\n",
    )

    pairs = _call_pairs(path, edges)
    assert ("setup", "nextToken") in pairs
    assert ("setup", "memo") in pairs
    assert ("Button", "paint") in pairs
