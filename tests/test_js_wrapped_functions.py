"""Components wrapped in a higher-order call (forwardRef, memo, ...) get Function nodes (#972)."""

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


def test_a_directly_assigned_function_keeps_its_sibling_declarators(tmp_path):
    """The same holds when the function is assigned directly (#972).

    Before, `const a = () => x(), b = y()` extracted `a` and skipped the whole
    declaration, so the call to `y` was lost. The other declarators are now walked in
    the enclosing scope whichever form the function has.
    """
    path, (nodes, edges) = _parse(
        tmp_path,
        "setup.js",
        "function setup() {\n  const a = () => x(), b = y();\n  return [a, b];\n}\n",
    )

    assert set(_functions(nodes)) == {"setup", "a"}
    assert _call_pairs(path, edges) == {("a", "x"), ("setup", "y")}


def test_a_member_expression_wrapper_is_unwrapped(tmp_path):
    """`React.memo(fn)` names the wrapper through a member expression: the wrapper is
    the property, and type arguments between callee and call do not hide it."""
    path, (nodes, edges) = _parse(
        tmp_path,
        "fields.tsx",
        "import React from 'react';\n"
        "export function setup() {\n"
        "  const Card = React.memo((props) => { paint(); return null; });\n"
        "  const Input = React.forwardRef<HTMLInputElement, Props>((props, ref) => {\n"
        "    focus();\n"
        "    return null;\n"
        "  });\n"
        "  return [Card, Input];\n"
        "}\n",
    )

    functions = _functions(nodes)
    assert set(functions) == {"setup", "Card", "Input"}
    assert functions["Input"].params == "(props, ref)"
    assert (functions["Input"].line_start, functions["Input"].line_end) == (4, 7)
    assert _call_pairs(path, edges) == {
        ("setup", "memo"),
        ("setup", "forwardRef"),
        ("Card", "paint"),
        ("Input", "focus"),
    }


def test_an_unknown_inner_wrapper_is_not_unwrapped(tmp_path):
    """`memo(wrap(fn))`: `wrap` is not a component wrapper, so `memo` does not receive a
    function and the declaration is not a definition. Every call stays on `setup`."""
    path, (nodes, edges) = _parse(
        tmp_path,
        "Card.jsx",
        "function setup() {\n  const Card = memo(wrap(() => paint()));\n  return Card;\n}\n",
    )

    assert set(_functions(nodes)) == {"setup"}
    assert _call_pairs(path, edges) == {
        ("setup", "memo"),
        ("setup", "wrap"),
        ("setup", "paint"),
    }


def test_wrappers_nested_past_the_depth_limit_are_not_unwrapped(tmp_path):
    """The outermost wrapper plus `_JS_WRAPPER_MAX_DEPTH` nested ones are unwrapped;
    one more and the declaration is left to the generic walk."""
    limit = CodeParser._JS_WRAPPER_MAX_DEPTH + 1
    within = "memo(" * limit + "() => paint()" + ")" * limit
    beyond = "memo(" * (limit + 1) + "() => draw()" + ")" * (limit + 1)
    path, (nodes, edges) = _parse(
        tmp_path,
        "deep.jsx",
        "function setup() {\n"
        f"  const Within = {within};\n"
        f"  const Beyond = {beyond};\n"
        "  return [Within, Beyond];\n"
        "}\n",
    )

    assert set(_functions(nodes)) == {"setup", "Within"}
    assert _call_pairs(path, edges) == {
        ("setup", "memo"),
        ("Within", "paint"),
        ("setup", "draw"),
    }
    memo_calls = [edge for edge in edges if edge.kind == "CALLS" and edge.target == "memo"]
    assert len(memo_calls) == limit + (limit + 1)


def _reference_pairs(path: Path, edges):
    prefix = f"{path.resolve().as_posix()}::"
    return {
        (edge.source.replace(prefix, ""), edge.target.rsplit("::", 1)[-1])
        for edge in edges
        if edge.kind == "REFERENCES"
    }


def test_the_rest_of_a_wrapper_call_stays_in_the_enclosing_scope(tmp_path):
    """Only the wrapped function is the component (#972).

    The inner call of a curried wrapper and the type arguments are evaluated where the
    declaration is. Skipping them lost the `styled` and `connect` calls, and the
    references that keep `Base`, `mapState` and `Props` from looking unused.
    """
    path, (nodes, edges) = _parse(
        tmp_path,
        "wrapped.tsx",
        "interface Props { id: string }\n"
        "function Base() { return null; }\n"
        "function mapState(state) { return state; }\n"
        "export function setup() {\n"
        "  const Box = styled(Base)(() => theme());\n"
        "  const Page = connect(mapState, (d) => bind(d))(() => render());\n"
        "  const Button = forwardRef<HTMLButtonElement, Props>(() => paint());\n"
        "  return [Box, Page, Button];\n"
        "}\n",
    )

    assert set(_functions(nodes)) == {"Base", "mapState", "setup", "Box", "Page", "Button"}
    assert _call_pairs(path, edges) == {
        ("setup", "styled"),
        ("setup", "connect"),
        ("setup", "bind"),
        ("setup", "forwardRef"),
        ("Box", "theme"),
        ("Page", "render"),
        ("Button", "paint"),
    }
    assert _reference_pairs(path, edges) == {
        ("setup", "Base"),
        ("setup", "mapState"),
        ("setup", "Props"),
    }


def test_memo_takes_a_props_comparator(tmp_path):
    """`memo(Component, arePropsEqual)` is the documented React API (#972).

    The component is still the first argument. The comparator is not part of it: an
    inline one runs where the declaration is, and a named one stays referenced there.
    """
    path, (nodes, edges) = _parse(
        tmp_path,
        "Chart.jsx",
        "function arePropsEqual(prev, next) { return prev.id === next.id; }\n"
        "export function setup() {\n"
        "  const Chart = memo(function Chart({ points }) {\n"
        "    draw(points);\n"
        "    return null;\n"
        "  }, arePropsEqual);\n"
        "  const Row = memo(forwardRef((props, ref) => paint()), (a, b) => same(a, b));\n"
        "  return [Chart, Row];\n"
        "}\n",
    )

    functions = _functions(nodes)
    assert set(functions) == {"arePropsEqual", "setup", "Chart", "Row"}
    assert functions["Chart"].params == "({ points })"
    assert (functions["Chart"].line_start, functions["Chart"].line_end) == (3, 6)
    assert functions["Row"].params == "(props, ref)"
    assert _call_pairs(path, edges) == {
        ("setup", "memo"),
        ("setup", "forwardRef"),
        ("setup", "same"),
        ("Chart", "draw"),
        ("Row", "paint"),
    }
    assert _reference_pairs(path, edges) == {("setup", "arePropsEqual")}


def test_forward_ref_takes_exactly_one_argument(tmp_path):
    """Only memo has a second argument. `forwardRef(fn, extra)` is not the React API, so
    the declaration stays a plain call and its calls stay on `setup`."""
    path, (nodes, edges) = _parse(
        tmp_path,
        "Input.jsx",
        "function setup() {\n"
        "  const Input = forwardRef((props, ref) => focus(), extra);\n"
        "  return Input;\n"
        "}\n",
    )

    assert set(_functions(nodes)) == {"setup"}
    assert _call_pairs(path, edges) == {("setup", "forwardRef"), ("setup", "focus")}


def test_memo_takes_at_most_one_comparator(tmp_path):
    """The comparator is memo's only second argument. `memo(fn, eq, extra)` is not the
    React API, so the declaration stays a plain call and its calls stay on `setup`."""
    path, (nodes, edges) = _parse(
        tmp_path,
        "Chart.jsx",
        "function setup() {\n"
        "  const Chart = memo((props) => draw(), eq, extra);\n"
        "  return Chart;\n"
        "}\n",
    )

    assert set(_functions(nodes)) == {"setup"}
    assert _call_pairs(path, edges) == {("setup", "memo"), ("setup", "draw")}


def test_comments_are_not_wrapper_arguments(tmp_path):
    """Tree-sitter lists comments among a call's arguments (#972).

    Counted as arguments, a lint directive above the function turned `forwardRef(fn)`
    into a two-argument call, and a comment next to the comparator did the same to
    `memo(fn, eq)`. The rest of the call is walked without them too, so a comment
    before the function does not make the component look like the comparator and get
    walked a second time in the enclosing scope. The legacy `<!--` line comment is a
    comment node of the same kind.
    """
    path, (nodes, edges) = _parse(
        tmp_path,
        "fields.jsx",
        "function isEqual(a, b) { return a === b; }\n"
        "function setup() {\n"
        "  const Input = forwardRef(\n"
        "    // eslint-disable-next-line react/display-name\n"
        "    (props, ref) => focus(),\n"
        "  );\n"
        "  const Label = forwardRef((props, ref) => text() /* inline */);\n"
        "  const Chart = memo(/* why */ (props) => draw(), /* cmp */ isEqual);\n"
        "  const Legacy = observer(\n"
        "    <!-- legacy\n"
        "    () => render()\n"
        "  );\n"
        "  return [Input, Label, Chart, Legacy];\n"
        "}\n",
    )

    assert set(_functions(nodes)) == {"isEqual", "setup", "Input", "Label", "Chart", "Legacy"}
    assert _call_pairs(path, edges) == {
        ("setup", "forwardRef"),
        ("setup", "memo"),
        ("setup", "observer"),
        ("Input", "focus"),
        ("Label", "text"),
        ("Chart", "draw"),
        ("Legacy", "render"),
    }
    assert _reference_pairs(path, edges) == {("setup", "isEqual")}


def test_a_wrapped_declaration_keeps_its_type_annotation_reference(tmp_path):
    """`const Card: FC<Props> = memo(...)` was walked whole before it counted as a
    definition, so `Props` was referenced from the enclosing scope. It still is (#972)."""
    path, (nodes, edges) = _parse(
        tmp_path,
        "Card.tsx",
        "interface Props { id: string }\n"
        "export function setup() {\n"
        "  const Card: FC<Props> = memo(() => paint());\n"
        "  return Card;\n"
        "}\n",
    )

    assert set(_functions(nodes)) == {"setup", "Card"}
    assert _call_pairs(path, edges) == {("setup", "memo"), ("Card", "paint")}
    assert _reference_pairs(path, edges) == {("setup", "Props")}


def test_a_directly_assigned_function_does_not_walk_its_annotation(tmp_path):
    """Only the wrapped form gets its annotation walked.

    A directly assigned function has only ever had the function itself walked, so
    `Props` in `const Card: FC<Props> = () => ...` is not referenced from the
    enclosing scope. #972 leaves that as it is; this pins that the annotation walk
    added for wrapped declarations does not reach the direct form.
    """
    path, (nodes, edges) = _parse(
        tmp_path,
        "Card.tsx",
        "interface Props { id: string }\n"
        "export function setup() {\n"
        "  const Card: FC<Props> = () => paint();\n"
        "  return Card;\n"
        "}\n",
    )

    assert set(_functions(nodes)) == {"setup", "Card"}
    assert _call_pairs(path, edges) == {("Card", "paint")}
    assert _reference_pairs(path, edges) == set()
