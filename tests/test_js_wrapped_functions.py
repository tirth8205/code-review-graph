"""Components wrapped in a higher-order call (forwardRef, memo, ...) get Function nodes (#972)."""

import time
from collections import Counter
from pathlib import Path, PureWindowsPath

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, incremental_update
from code_review_graph.parser import CodeParser
from code_review_graph.tools.query import query_graph


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


def test_a_method_that_shares_a_wrapper_name_is_not_a_component(tmp_path):
    """Only `React.*` members are wrappers. `pool.connect(cb)` and `utils.memo(fn)` call
    methods that happen to share a wrapper's name; indexing them invented a `session`
    function and took `logErr`'s caller away from `handle` (#972)."""
    path, (nodes, edges) = _parse(
        tmp_path,
        "db.js",
        "import { logErr } from './log';\n"
        "export function handle(pool) {\n"
        "  const session = pool.connect(function (err) { logErr(err); });\n"
        "  return session;\n"
        "}\n"
        "export function open(utils) {\n"
        "  const cached = utils.memo(() => compute());\n"
        "  return cached;\n"
        "}\n",
    )

    assert set(_functions(nodes)) == {"handle", "open"}
    pairs = _call_pairs(path, edges)
    assert ("handle", "logErr") in pairs
    assert ("open", "compute") in pairs
    assert not {caller for caller, _ in pairs} & {"session", "cached"}


def test_a_curried_chain_past_the_depth_limit_is_not_followed(tmp_path):
    """The wrapper name follows a curried callee no deeper than the unwrapping does, so a
    pathological chain parses instead of raising RecursionError."""
    source = "export const S = connect(a)" + "(b)" * 1000 + "(() => { paint(); });\n"
    _path, (nodes, _edges) = _parse(tmp_path, "deep.js", source)

    assert "S" not in _functions(nodes)


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


_FORK_REF_TEST = (
    "import * as React from 'react';\n"
    "import useForkRef from './useForkRef';\n"
    "\n"
    "describe('useForkRef', () => {\n"
    "  const Probe = React.forwardRef(function Probe(props, ref) {\n"
    "    return <div ref={useForkRef(ref, null)} />;\n"
    "  });\n"
    "\n"
    "  it('forks if only one of the branches requires a ref', () => {\n"
    "    const Component = React.forwardRef(function Component(props, ref) {\n"
    "      const handleRef = useForkRef(handleOwnRef, ref);\n"
    "      return <div ref={handleRef} />;\n"
    "    });\n"
    "    render(<Component />);\n"
    "  });\n"
    "\n"
    "  it('does nothing if none of the forked branches requires a ref', () => {\n"
    "    const Outer = React.memo(forwardRef((props, ref) => {\n"
    "      return React.cloneElement(props.children, { ref: useForkRef(null, ref) });\n"
    "    }));\n"
    "    render(<Outer><Probe /></Outer>);\n"
    "  });\n"
    "});\n"
)


def test_a_component_built_in_a_test_body_keeps_its_calls_on_the_test(tmp_path):
    """A test that builds its own probe component still tests what the probe calls.

    material-ui builds probes as `React.forwardRef(function Component(props, ref) {
    ... useForkRef(handleOwnRef, ref) ... })` inside `it()`. Indexing the probe moved
    the call onto it, TESTED_BY followed the call, and `tests_for(useForkRef)` came
    back empty (#972). In a test's body, `describe` included, a wrapped component is a
    fixture and its calls stay the test's. One at module scope is still indexed.
    """
    repo = tmp_path.resolve()
    (repo / "src").mkdir()
    production = repo / "src" / "useForkRef.ts"
    production.write_text(
        "export default function useForkRef(...refs) { return refs; }\n",
        encoding="utf-8",
    )
    test_file = repo / "src" / "useForkRef.test.tsx"
    test_file.write_text(
        "export const Frame = React.memo(() => <div>{useForkRef(null, null)}</div>);\n"
        + _FORK_REF_TEST,
        encoding="utf-8",
    )

    nodes, edges = CodeParser(repo).parse_file(test_file)

    tested = {
        edge.target.rsplit("::", 1)[-1]
        for edge in edges
        if edge.kind == "TESTED_BY" and edge.source == f"{production.as_posix()}::useForkRef"
    }
    assert tested == {
        "describe:useForkRef@L5",
        "it:forks if only one of the branches requires a ref@L10",
        "it:does nothing if none of the forked branches requires a ref@L18",
    }
    assert set(_functions(nodes)) == {"Frame"}

    graph_dir = repo / ".code-review-graph"
    graph_dir.mkdir()
    with GraphStore(graph_dir / "graph.db") as store:
        assert full_build(repo, store)["errors"] == []
    result = query_graph("tests_for", f"{production.as_posix()}::useForkRef", repo_root=str(repo))
    assert result["status"] == "ok"
    assert {node["name"] for node in result["results"]} == tested


# --- Edge cases added while hardening #972 for staging ---------------------------


def _all_pairs(path: Path, edges, kinds=("CALLS", "REFERENCES")):
    prefix = f"{path.resolve().as_posix()}::"
    return {
        (
            edge.kind,
            edge.source.replace(prefix, "").rsplit("/", 1)[-1],
            edge.target.replace(prefix, "").rsplit("::", 1)[-1].rsplit("/", 1)[-1],
        )
        for edge in edges
        if edge.kind in kinds
    }


def test_let_and_var_declarations_are_unwrapped_like_const(tmp_path):
    """`let` and `var` are a `lexical_declaration` / `variable_declaration` like `const`."""
    path, (nodes, edges) = _parse(
        tmp_path,
        "legacy.jsx",
        "let Card = memo(() => paint());\nvar Input = forwardRef((props, ref) => focus());\n",
    )

    functions = _functions(nodes)
    assert set(functions) == {"Card", "Input"}
    assert functions["Input"].params == "(props, ref)"
    assert ("Card", "paint") in _call_pairs(path, edges)
    assert ("Input", "focus") in _call_pairs(path, edges)


def test_a_wrapper_without_a_function_literal_is_not_a_definition(tmp_path):
    """`memo()`, `memo(Button)` and `memo(...parts)` do not receive a function literal.

    `memo(Button)` is the most common form of all, and the component is `Button`, which
    already has its own node. None of these become a definition, and the wrapper calls
    and the reference to `Button` stay where the declaration is.
    """
    path, (nodes, edges) = _parse(
        tmp_path,
        "memoized.jsx",
        "function Button() { return null; }\n"
        "function setup(parts) {\n"
        "  const Empty = memo();\n"
        "  const NoRef = forwardRef();\n"
        "  const MemoButton = memo(Button);\n"
        "  const Spread = memo(...parts);\n"
        "  return [Empty, NoRef, MemoButton, Spread];\n"
        "}\n",
    )

    assert set(_functions(nodes)) == {"Button", "setup"}
    assert _all_pairs(path, edges) == {
        ("CALLS", "setup", "memo"),
        ("CALLS", "setup", "forwardRef"),
        ("REFERENCES", "setup", "Button"),
    }


def test_shapes_the_unwrapping_does_not_read_stay_plain_calls(tmp_path):
    """Forms outside the documented ones keep their staging behaviour.

    No declarator name (`export default memo(fn)`, destructuring), a value that is not
    an arrow or function expression (generator, parenthesised), an expression around
    the call (`as FC`), a tagged template instead of an argument list
    (`styled(Base)\\`...\\``) and a member callee that is not `React` (`styled.div(fn)`):
    none is a definition, and every call stays in the enclosing scope.
    """
    path, (nodes, edges) = _parse(
        tmp_path,
        "shapes.tsx",
        "export default memo(() => first());\n"
        "export function setup() {\n"
        "  const { A } = memo(() => second());\n"
        "  const [B] = forwardRef(() => third());\n"
        "  const C = memo(function* () { yield fourth(); });\n"
        "  const D = memo((() => fifth()));\n"
        "  const E = memo(() => sixth()) as FC;\n"
        "  const F = styled(Base)`color: red;`;\n"
        "  const G = styled.div(() => seventh());\n"
        "  return [A, B, C, D, E, F, G];\n"
        "}\n",
    )

    assert set(_functions(nodes)) == {"setup"}
    module = path.resolve().as_posix()
    pairs = _call_pairs(path, edges)
    assert {caller for caller, _ in pairs} == {module, "setup"}
    assert (module, "first") in pairs
    inner = ("second", "third", "fourth", "fifth", "sixth", "seventh")
    assert {("setup", callee) for callee in inner} <= pairs


def test_a_component_wrapped_inside_another_has_the_outer_as_its_caller(tmp_path):
    """A wrapped component declared in another one's body: the inner wrapper runs in the
    outer component, the inner component's body belongs to the inner one, and the outer
    wrapper stays at module scope."""
    path, (nodes, edges) = _parse(
        tmp_path,
        "nested.jsx",
        "export const Outer = memo(() => {\n"
        "  const Inner = forwardRef(() => paint());\n"
        "  return <Inner />;\n"
        "});\n",
    )

    functions = _functions(nodes)
    assert set(functions) == {"Outer", "Inner"}
    assert (functions["Outer"].line_start, functions["Outer"].line_end) == (1, 4)
    assert (functions["Inner"].line_start, functions["Inner"].line_end) == (2, 2)
    assert _call_pairs(path, edges) == {
        (path.resolve().as_posix(), "memo"),
        ("Outer", "forwardRef"),
        ("Outer", "Inner"),
        ("Inner", "paint"),
    }


def test_a_wrapped_component_in_a_class_method_belongs_to_the_class(tmp_path):
    """Inside a method the component is qualified under the class, as a directly
    assigned function is, and the wrapper call stays on the method."""
    path, (nodes, edges) = _parse(
        tmp_path,
        "page.jsx",
        "class Page {\n"
        "  render() {\n"
        "    const Row = memo(() => paint());\n"
        "    return Row;\n"
        "  }\n"
        "}\n",
    )

    functions = _functions(nodes)
    assert set(functions) == {"render", "Row"}
    assert functions["Row"].parent_name == "Page"
    prefix = path.resolve().as_posix()
    assert any(
        edge.kind == "CONTAINS"
        and edge.source == f"{prefix}::Page"
        and edge.target == f"{prefix}::Page.Row"
        for edge in edges
    )
    assert _call_pairs(path, edges) == {("Page.render", "memo"), ("Page.Row", "paint")}


def test_unicode_names_and_a_path_with_spaces(tmp_path):
    """Identifiers and paths are decoded as UTF-8: the qualified name keeps both."""
    folder = tmp_path / "dir with spaces é"
    folder.mkdir()
    path, (nodes, edges) = _parse(
        folder,
        "Ünïcødé Card.jsx",
        "export const Kärtchen = memo(() => paint());\n"
        "export const Opt = React?.memo(async () => { await draw(); });\n",
    )

    assert set(_functions(nodes)) == {"Kärtchen", "Opt"}
    qualified = f"{path.as_posix()}::Kärtchen"
    assert any(edge.kind == "CONTAINS" and edge.target == qualified for edge in edges)
    assert {("Kärtchen", "paint"), ("Opt", "draw")} <= _call_pairs(path, edges)


def test_a_syntax_error_in_a_wrapped_component_does_not_raise(tmp_path):
    """Half-typed source reaches the parser from watch mode. It must not raise, and
    the complete declarations before the broken one keep their nodes and calls."""
    path, (nodes, edges) = _parse(
        tmp_path,
        "broken.tsx",
        "export const Good = memo(() => paint());\n"
        "export function after() { return tidy(); }\n"
        "export const Broken = memo((props) => {\n  draw(\n"
        "export const Half = forwardRef<HTMLDivElement,\n",
    )

    functions = _functions(nodes)
    assert {"Good", "after"} <= set(functions)
    assert (functions["Good"].line_start, functions["Good"].line_end) == (1, 1)
    assert {("Good", "paint"), ("after", "tidy")} <= _call_pairs(path, edges)


def test_a_windows_style_path_keeps_test_bodies_and_module_components_apart(tmp_path):
    """The test-body check compares qualified names, which normalise the separators.

    With a backslash path a probe built in `it()` is still the test's, and a component
    at module scope of the same test file is still indexed under a POSIX identity.
    """
    nodes, edges = CodeParser().parse_bytes(
        PureWindowsPath(r"C:\repo\src\Probe.test.tsx"),
        b"export const Frame = React.memo(() => <div>{useForkRef(null)}</div>);\n"
        b"describe('d', () => {\n"
        b"  it('t', () => {\n"
        b"    const P = React.forwardRef((p, r) => useForkRef(r));\n"
        b"    render(<P />);\n"
        b"  });\n"
        b"});\n",
    )

    assert {node.name for node in nodes if node.kind == "Function"} == {"Frame"}
    assert all("\\" not in node.file_path for node in nodes)
    useforkref_callers = {
        edge.source.rsplit("::", 1)[-1]
        for edge in edges
        if edge.kind == "CALLS" and edge.target == "useForkRef"
    }
    assert useforkref_callers == {"Frame", "it:t@L3"}


def test_probes_in_every_test_runner_body_stay_the_tests(tmp_path):
    """`beforeEach`, `it.each(...)(...)` and `test()` bodies are test bodies too.

    Their probes are not indexed and every TESTED_BY edge staging mints is still
    minted, so `tests_for(target)` keeps the hooks and tests that exercise it.
    """
    repo = tmp_path.resolve()
    test_file = repo / "probe.test.jsx"
    test_file.write_text(
        "describe('d', () => {\n"
        "  beforeEach(() => {\n"
        "    const P = memo(() => target());\n"
        "  });\n"
        "  it.each([1])('e %s', () => {\n"
        "    const Q = forwardRef(() => target());\n"
        "  });\n"
        "  test('t', () => {\n"
        "    const R = React.memo(() => target());\n"
        "  });\n"
        "});\n",
        encoding="utf-8",
    )

    nodes, edges = CodeParser(repo).parse_file(test_file)

    assert not _functions(nodes)
    assert not {"P", "Q", "R"} & {node.name for node in nodes}
    tested_by = {
        edge.target.rsplit("::", 1)[-1]
        for edge in edges
        if edge.kind == "TESTED_BY" and edge.source == "target"
    }
    assert tested_by == {"beforeEach@L2", "describe:d@L1", "test:t@L8"}


def test_a_helper_in_a_test_file_is_not_a_test_body(tmp_path):
    """Only a test's own body is exempt. A plain helper function in a test file is not a
    test, so a component it builds is indexed like anywhere else."""
    repo = tmp_path.resolve()
    test_file = repo / "helpers.test.jsx"
    test_file.write_text(
        "function renderProbe() {\n  const Probe = memo(() => target());\n  return Probe;\n}\n",
        encoding="utf-8",
    )

    nodes, edges = CodeParser(repo).parse_file(test_file)

    assert set(_functions(nodes)) == {"renderProbe", "Probe"}
    assert _call_pairs(test_file, edges) == {("renderProbe", "memo"), ("Probe", "target")}


def test_name_collisions_match_a_directly_assigned_function(tmp_path):
    """Wrapped components share the identity rules of directly assigned functions.

    Two functions that each declare `const Card` give both the same `<file>::Card`
    identity, as `const Row = () => ...` always has. A wrapped declaration must not
    invent a different scheme for the same collision.
    """
    wrapped_path, (wrapped_nodes, wrapped_edges) = _parse(
        tmp_path,
        "wrapped.jsx",
        "function one() { const Card = memo(() => paint()); return Card; }\n"
        "function two() { const Card = memo(() => draw()); return Card; }\n",
    )
    direct_path, (direct_nodes, direct_edges) = _parse(
        tmp_path,
        "direct.jsx",
        "function one() { const Card = () => paint(); return Card; }\n"
        "function two() { const Card = () => draw(); return Card; }\n",
    )

    def identities(nodes):
        return sorted(
            (node.kind, node.name, node.line_start) for node in nodes if node.kind == "Function"
        )

    assert identities(wrapped_nodes) == identities(direct_nodes)
    body_calls = {("Card", "paint"), ("Card", "draw")}
    assert body_calls <= _call_pairs(wrapped_path, wrapped_edges)
    assert body_calls <= _call_pairs(direct_path, direct_edges)
    assert _call_pairs(wrapped_path, wrapped_edges) - body_calls == {
        ("one", "memo"),
        ("two", "memo"),
    }


def test_each_part_of_a_wrapper_call_is_recorded_once(tmp_path):
    """The wrapper calls, the inner call of a curried wrapper, the type arguments, the
    annotation and memo's comparator are each walked once, in the enclosing scope.

    The symbols are imported so that every one of them yields an edge; a walk that
    visited the rest of a wrapper call twice would record each of these twice."""
    path, (nodes, edges) = _parse(
        tmp_path,
        "Card.tsx",
        "import { Base, mapState, eq, El, Item, Props } from './deps';\n"
        "export function setup() {\n"
        "  const Box = styled(Base)((p) => paint());\n"
        "  const Linked = connect(mapState)((p) => draw());\n"
        "  const Card: FC<Props> = memo(forwardRef<El, Item>((p, r) => fill()), eq),"
        " token = nextToken();\n"
        "  return [Box, Linked, Card, token];\n"
        "}\n",
    )

    prefix = f"{path.resolve().as_posix()}::"
    recorded = Counter(
        (
            edge.kind,
            edge.source.replace(prefix, ""),
            edge.target.rsplit("::", 1)[-1].rsplit("/", 1)[-1],
            edge.line,
        )
        for edge in edges
        if edge.kind in ("CALLS", "REFERENCES")
    )

    assert set(_functions(nodes)) == {"setup", "Box", "Linked", "Card"}
    assert recorded == Counter(
        {
            ("CALLS", "setup", "styled", 3): 1,
            ("REFERENCES", "setup", "Base", 3): 1,
            ("CALLS", "Box", "paint", 3): 1,
            ("CALLS", "setup", "connect", 4): 1,
            ("REFERENCES", "setup", "mapState", 4): 1,
            ("CALLS", "Linked", "draw", 4): 1,
            ("CALLS", "setup", "memo", 5): 1,
            ("CALLS", "setup", "forwardRef", 5): 1,
            ("REFERENCES", "setup", "Props", 5): 1,
            ("REFERENCES", "setup", "El", 5): 1,
            ("REFERENCES", "setup", "Item", 5): 1,
            ("REFERENCES", "setup", "eq", 5): 1,
            ("CALLS", "Card", "fill", 5): 1,
            ("CALLS", "setup", "nextToken", 5): 1,
        }
    )


_BUTTON_SOURCE = (
    "import { forwardRef } from 'react';\n"
    "import { track } from './track';\n"
    "export const Button = forwardRef<HTMLButtonElement, Props>((props, ref) => {\n"
    "  track('click');\n"
    "  return <button ref={ref} {...props} />;\n"
    "});\n"
)


def _write_issue_repo(repo: Path) -> None:
    (repo / "src").mkdir()
    (repo / "src" / "track.ts").write_text(
        "export function track(name: string) { return name; }\n",
        encoding="utf-8",
    )
    (repo / "src" / "Button.tsx").write_text(_BUTTON_SOURCE, encoding="utf-8")
    (repo / "src" / "Toolbar.tsx").write_text(
        "import { Button } from './Button';\nexport const Toolbar = () => <Button>Save</Button>;\n",
        encoding="utf-8",
    )
    (repo / "src" / "Button.test.tsx").write_text(
        "import { Button } from './Button';\nit('renders', () => { render(<Button />); });\n",
        encoding="utf-8",
    )
    (repo / ".code-review-graph").mkdir()


def _dangling_calls(store: GraphStore) -> set[str]:
    known = {node.qualified_name for node in store.get_all_nodes(exclude_files=False)}
    return {
        edge.target_qualified
        for edge in store.get_all_edges()
        if edge.kind == "CALLS"
        and "::" in edge.target_qualified
        and edge.target_qualified not in known
    }


def test_issue_971_end_to_end_through_the_graph(tmp_path, monkeypatch):
    """The issue's repository, built and queried: the JSX use of `Button` lands on a node.

    `callers_of(Button)` answers `Toolbar`, `tests_for(Button)` answers the test that
    renders it, the call inside the component is the component's, and no CALLS edge
    in the repository targets a qualified name that has no node.
    """
    monkeypatch.delenv("CRG_DATA_DIR", raising=False)
    repo = tmp_path.resolve()
    _write_issue_repo(repo)
    button = f"{(repo / 'src' / 'Button.tsx').as_posix()}::Button"

    with GraphStore(repo / ".code-review-graph" / "graph.db") as store:
        assert full_build(repo, store)["errors"] == []
        node = store.get_node(button)
        assert node is not None and node.kind == "Function"
        assert (node.line_start, node.line_end) == (3, 6)
        assert _dangling_calls(store) == set()

    callers = query_graph("callers_of", button, repo_root=str(repo))
    assert callers["status"] == "ok"
    assert {result["name"] for result in callers["results"]} == {"Toolbar", "it:renders@L2"}
    tests = query_graph("tests_for", button, repo_root=str(repo))
    assert {result["name"] for result in tests["results"]} == {"it:renders@L2"}
    track = f"{(repo / 'src' / 'track.ts').as_posix()}::track"
    track_callers = query_graph("callers_of", track, repo_root=str(repo))
    assert {result["name"] for result in track_callers["results"]} == {"Button"}


def test_an_incremental_update_replaces_a_renamed_wrapped_component(tmp_path, monkeypatch):
    """Editing the file re-parses it like a full build: the old component and its edges
    go, the renamed one arrives, and a second update with no change is a no-op."""
    monkeypatch.delenv("CRG_DATA_DIR", raising=False)
    repo = tmp_path.resolve()
    _write_issue_repo(repo)
    button_file = repo / "src" / "Button.tsx"
    prefix = button_file.as_posix()

    with GraphStore(repo / ".code-review-graph" / "graph.db") as store:
        full_build(repo, store)
        assert store.get_node(f"{prefix}::Button") is not None

        button_file.write_text(
            _BUTTON_SOURCE.replace("const Button = forwardRef", "const PrimaryButton = memo"),
            encoding="utf-8",
        )
        incremental_update(repo, store, changed_files=["src/Button.tsx"])

        assert store.get_node(f"{prefix}::Button") is None
        renamed = store.get_node(f"{prefix}::PrimaryButton")
        assert renamed is not None and renamed.kind == "Function"
        assert not [
            edge
            for edge in store.get_edges_by_source(f"{prefix}::Button")
            if edge.file_path == prefix
        ]
        sources = {
            edge.source_qualified
            for edge in store.get_all_edges()
            if edge.file_path == prefix and edge.kind == "CALLS"
        }
        assert sources == {prefix, f"{prefix}::PrimaryButton"}
        before = (len(store.get_all_nodes(exclude_files=False)), len(store.get_all_edges()))

        second = incremental_update(repo, store, changed_files=[])
        assert second["files_updated"] == 0
        after = (len(store.get_all_nodes(exclude_files=False)), len(store.get_all_edges()))
        assert after == before


def test_a_graph_written_before_wrapped_components_gains_them(tmp_path, monkeypatch):
    """A graph built by an earlier release has no node for a wrapped component.

    That graph is simulated by building with the unwrapping switched off. The update
    skips a file whose content hash is unchanged, so the node arrives when the file is
    next edited, and the JSX edge that dangled then resolves. A full rebuild over the
    old database gives the same graph as a fresh build.
    """
    monkeypatch.delenv("CRG_DATA_DIR", raising=False)

    def graph(store, root):
        prefix = root.as_posix()
        nodes = sorted(
            n.qualified_name.replace(prefix, "<repo>")
            for n in store.get_all_nodes(exclude_files=False)
        )
        edges = sorted(
            (e.kind, e.source_qualified.replace(prefix, "<repo>"), e.target_qualified)
            for e in store.get_all_edges()
        )
        return nodes, [(k, s, t.replace(prefix, "<repo>")) for k, s, t in edges]

    edited = _BUTTON_SOURCE + "// edited\n"
    repo = (tmp_path / "old").resolve()
    repo.mkdir()
    _write_issue_repo(repo)
    button = f"{(repo / 'src' / 'Button.tsx').as_posix()}::Button"

    with GraphStore(repo / ".code-review-graph" / "graph.db") as store:
        with monkeypatch.context() as old_release:
            old_release.setattr(
                CodeParser,
                "_js_wrapped_function",
                lambda self, *a, **k: None,
                raising=False,
            )
            full_build(repo, store)
        assert store.get_node(button) is None
        assert button in _dangling_calls(store)

        (repo / "src" / "Button.tsx").write_text(edited, encoding="utf-8")
        incremental_update(repo, store, changed_files=["src/Button.tsx"])
        assert store.get_node(button) is not None
        assert _dangling_calls(store) == set()

        full_build(repo, store)
        rebuilt = graph(store, repo)

    fresh_repo = (tmp_path / "fresh").resolve()
    fresh_repo.mkdir()
    _write_issue_repo(fresh_repo)
    (fresh_repo / "src" / "Button.tsx").write_text(edited, encoding="utf-8")
    with GraphStore(fresh_repo / ".code-review-graph" / "graph.db") as fresh:
        full_build(fresh_repo, fresh)
        assert rebuilt == graph(fresh, fresh_repo)


def test_thousands_of_wrapped_components_in_one_scope(tmp_path):
    """Each wrapped declaration checks for an enclosing test by scanning the nodes so far.

    Thousands in one function are all indexed, thousands of probes in one `describe`
    are all left to their tests, and thousands in a helper that follows thousands of
    tests are indexed. The last is the worst case for the scan: the helper is not a
    test, so every check walks past every Test node before answering. The time bound
    is loose on purpose; it catches a blow-up, not a slowdown."""
    count = 2000
    in_function = (
        "function setup() {\n"
        + "".join(f"  const C{i} = memo(() => paint{i % 7}());\n" for i in range(count))
        + "}\n"
    )
    in_tests = (
        "describe('d', () => {\n"
        + "".join(
            f"  it('t{i}', () => {{ const P{i} = memo(() => target()); }});\n" for i in range(count)
        )
        + "});\n"
    )
    half = count // 2
    helper_after_tests = (
        "".join(f"it('t{i}', () => {{ run(); }});\n" for i in range(half))
        + "function helper() {\n"
        + "".join(f"  const H{i} = memo(() => fill());\n" for i in range(half))
        + "}\n"
    )
    files = {
        "many.jsx": in_function,
        "many.test.jsx": in_tests,
        "helper.test.jsx": helper_after_tests,
    }
    for name, source in files.items():
        (tmp_path / name).write_text(source, encoding="utf-8")

    parser = CodeParser(tmp_path)
    start = time.perf_counter()
    parsed = {name: parser.parse_file(tmp_path / name)[0] for name in files}
    elapsed = time.perf_counter() - start

    assert len(_functions(parsed["many.jsx"])) == count + 1
    assert not _functions(parsed["many.test.jsx"])
    assert len(_functions(parsed["helper.test.jsx"])) == half + 1
    assert elapsed < 20


# --- Cases added by the test audit of #972 ----------------------------------------


def test_a_wrapped_component_used_in_its_own_file_resolves_to_its_node(tmp_path):
    """A JSX use or a direct call in the same file targets the component's node.

    Same-file targets are resolved against the file's own nodes, so before wrapped
    components had nodes, `<Card />` stayed a bare `Card` that no node answered.
    """
    path, (_nodes, edges) = _parse(
        tmp_path,
        "Grid.jsx",
        "const Card = memo(() => paint());\n"
        "const Row = () => draw();\n"
        "export function Grid() { return <div><Card /><Row />{Card()}</div>; }\n",
    )

    prefix = path.as_posix()
    grid_targets = {
        edge.target for edge in edges if edge.kind == "CALLS" and edge.source == f"{prefix}::Grid"
    }
    assert grid_targets == {f"{prefix}::Card", f"{prefix}::Row"}


def test_a_barrel_reexport_and_an_aliased_import_reach_the_wrapped_component(
    tmp_path,
    monkeypatch,
):
    """React code reaches components through barrels, aliases and default exports.

    `export * from './Card'`, `import { Card as Tile }` and a default-exported wrapped
    `Button` all resolve to the component's node, so `callers_of` answers every user
    and no CALLS edge dangles on a wrapped component.
    """
    monkeypatch.delenv("CRG_DATA_DIR", raising=False)
    repo = tmp_path.resolve()
    src = repo / "src"
    src.mkdir()
    (src / "Card.tsx").write_text(
        "import React from 'react';\n"
        "export const Card = React.memo(function Card(props) { return <div />; });\n"
        "export const Grid = () => <div><Card /></div>;\n",
        encoding="utf-8",
    )
    (src / "Button.tsx").write_text(
        "import { forwardRef } from 'react';\n"
        "const Button = forwardRef((props, ref) => <button ref={ref} />);\n"
        "export default Button;\n",
        encoding="utf-8",
    )
    (src / "index.ts").write_text("export * from './Card';\n", encoding="utf-8")
    (src / "App.tsx").write_text(
        "import { Card } from './index';\nexport function App() { return <Card />; }\n",
        encoding="utf-8",
    )
    (src / "Toolbar.tsx").write_text(
        "import Button from './Button';\n"
        "import { Card as Tile } from './Card';\n"
        "export const Toolbar = () => <div><Button /><Tile /></div>;\n",
        encoding="utf-8",
    )
    (repo / ".code-review-graph").mkdir()
    card = f"{(src / 'Card.tsx').as_posix()}::Card"
    button = f"{(src / 'Button.tsx').as_posix()}::Button"

    with GraphStore(repo / ".code-review-graph" / "graph.db") as store:
        assert full_build(repo, store)["errors"] == []
        assert not _dangling_calls(store) & {card, button}

    card_callers = query_graph("callers_of", card, repo_root=str(repo))
    assert card_callers["status"] == "ok"
    assert {result["name"] for result in card_callers["results"]} == {"App", "Grid", "Toolbar"}
    button_callers = query_graph("callers_of", button, repo_root=str(repo))
    assert {result["name"] for result in button_callers["results"]} == {"Toolbar"}


def test_a_helper_beside_tests_in_a_test_file_is_not_a_test_body(tmp_path):
    """Only the ENCLOSING function decides. A helper declared next to tests, inside the
    `describe` or after it, is not a test because tests exist elsewhere in the file, so
    a component it builds is indexed while a probe in the `it` body is not."""
    repo = tmp_path.resolve()
    test_file = repo / "grid.test.jsx"
    test_file.write_text(
        "describe('grid', () => {\n"
        "  function makeCell() { const Cell = memo(() => fill()); return Cell; }\n"
        "  it('renders', () => {\n"
        "    const Probe = memo(() => target());\n"
        "    render(makeCell());\n"
        "  });\n"
        "});\n"
        "function makeRow() { const Row = memo(() => draw()); return Row; }\n",
        encoding="utf-8",
    )

    nodes, edges = CodeParser(repo).parse_file(test_file)

    assert set(_functions(nodes)) == {"makeCell", "Cell", "makeRow", "Row"}
    pairs = _call_pairs(test_file, edges)
    assert {("Cell", "fill"), ("Row", "draw"), ("makeCell", "memo"), ("makeRow", "memo")} <= pairs
    assert ("it:renders@L3", "target") in pairs
    assert "Probe" not in {node.name for node in nodes}


def test_every_curried_and_single_wrapper_in_the_list_is_unwrapped(tmp_path):
    """The listed wrappers other than memo, forwardRef, observer, withRouter, styled and
    connect: `withTheme(fn)`, `withStyles(styles)(fn)`, `withTranslation()(fn)` and
    mobx's `inject('store')(observer(fn))`. Each wrapper call stays in the enclosing
    scope and the component's body is its own."""
    path, (nodes, edges) = _parse(
        tmp_path,
        "hocs.jsx",
        "export function setup(styles) {\n"
        "  const Themed = withTheme((props) => paint(props));\n"
        "  const Styled = withStyles(styles)((props) => draw(props));\n"
        "  const Translated = withTranslation()((props) => label(props));\n"
        "  const Injected = inject('store')(observer((props) => load(props)));\n"
        "  return [Themed, Styled, Translated, Injected];\n"
        "}\n",
    )

    assert set(_functions(nodes)) == {"setup", "Themed", "Styled", "Translated", "Injected"}
    assert _call_pairs(path, edges) == {
        ("setup", "withTheme"),
        ("setup", "withStyles"),
        ("setup", "withTranslation"),
        ("setup", "inject"),
        ("setup", "observer"),
        ("Themed", "paint"),
        ("Styled", "draw"),
        ("Translated", "label"),
        ("Injected", "load"),
    }


def test_generic_typescript_components_are_unwrapped(tmp_path):
    """`memo(<T,>(props) => ...)` in TSX and a generic named function expression keep
    their own parameters, and the type parameters do not hide the function."""
    path, (nodes, edges) = _parse(
        tmp_path,
        "List.tsx",
        "export const List = memo(<T,>(props: { items: T[] }) => <ul>{each(props.items)}</ul>);\n"
        "export const Select = forwardRef(function Select<T>(props: T, ref) {\n"
        "  return pick(props);\n"
        "});\n",
    )

    functions = _functions(nodes)
    assert set(functions) == {"List", "Select"}
    assert functions["List"].params == "(props: { items: T[] })"
    assert (functions["Select"].line_start, functions["Select"].line_end) == (2, 4)
    assert {("List", "each"), ("Select", "pick")} <= _call_pairs(path, edges)


def test_probes_under_runner_modifiers_and_nested_callbacks_stay_the_tests(tmp_path):
    """`it.only`, `describe.skip`, `afterEach`, `beforeAll`, `test.concurrent` and a
    probe built inside a callback the test passes on (`act(() => ...)`) are all test
    bodies: the probe is not indexed and the test keeps its TESTED_BY edge."""
    repo = tmp_path.resolve()
    test_file = repo / "runners.test.jsx"
    test_file.write_text(
        "describe('d', () => {\n"
        "  it.only('a', () => { const A = memo(() => target()); });\n"
        "  describe.skip('s', () => { const B = memo(() => target()); });\n"
        "  afterEach(() => { const C = memo(() => target()); });\n"
        "  beforeAll(() => { const D = memo(() => target()); });\n"
        "  it('n', () => { act(() => { const E = React.forwardRef(() => target()); }); });\n"
        "  test.concurrent('c', async () => { const G = memo(() => target()); });\n"
        "});\n",
        encoding="utf-8",
    )

    nodes, edges = CodeParser(repo).parse_file(test_file)

    assert not _functions(nodes)
    tested_by = {
        edge.target.rsplit("::", 1)[-1]
        for edge in edges
        if edge.kind == "TESTED_BY" and edge.source == "target"
    }
    assert tested_by == {
        "it:a@L2",
        "describe:s@L3",
        "afterEach@L4",
        "beforeAll@L5",
        "it:n@L6",
        "test:c@L7",
    }


def test_one_declaration_mixing_wrapped_direct_and_plain_declarators(tmp_path):
    """`const A = memo(...), B = () => ..., c = d()`: both functions are indexed, and
    the plain declarator's call and the wrapper call stay on the enclosing function."""
    path, (nodes, edges) = _parse(
        tmp_path,
        "mixed.jsx",
        "export function setup() {\n"
        "  const A = memo(() => a()), B = () => b(), c = d();\n"
        "  return [A, B, c];\n"
        "}\n",
    )

    assert set(_functions(nodes)) == {"setup", "A", "B"}
    assert _call_pairs(path, edges) == {
        ("setup", "memo"),
        ("setup", "d"),
        ("A", "a"),
        ("B", "b"),
    }


def test_the_first_call_of_a_curried_wrapper_is_not_a_component(tmp_path):
    """`connect`, `withStyles`, `inject`, `withTranslation` and `styled` are curried:
    the first call takes configuration and returns a wrapper, and only the second call,
    `connect(mapState)(Component)`, makes a component. The function in the first call
    is a selector, a style sheet or a callback, not the body of the variable it is
    assigned to, so it must not become a Function node that takes its calls away from
    the function making them. Staging records these calls on the enclosing scope.

    `const connector = connect((state) => ({...}))` is react-redux's documented
    `ConnectedProps` setup, used as is in real code (replayio/devtools)."""
    path, (nodes, edges) = _parse(
        tmp_path,
        "containers.js",
        "import { connect } from 'react-redux';\n"
        "import { withStyles } from '@material-ui/core/styles';\n"
        "import { inject } from 'mobx-react';\n"
        "import { onOpen } from './events';\n"
        "function selectUser(state) { return state.user; }\n"
        "export function open() {\n"
        "  const sock = connect(function () { onOpen(); });\n"
        "  return sock;\n"
        "}\n"
        "export function makeContainer(Profile) {\n"
        "  const enhance = connect((state) => ({ user: selectUser(state) }));\n"
        "  return enhance(Profile);\n"
        "}\n"
        "export function makeStyled(Comp) {\n"
        "  const decorate = withStyles((theme) => ({ root: { padding: theme.spacing(1) } }));\n"
        "  return decorate(Comp);\n"
        "}\n"
        "export function makeInjected(Comp) {\n"
        "  const withStore = inject((stores) => ({ user: selectUser(stores) }));\n"
        "  return withStore(Comp);\n"
        "}\n"
        "const connector = connect((state) => ({ user: selectUser(state) }));\n"
        "export default connector(Profile);\n",
    )

    assert set(_functions(nodes)) == {
        "selectUser",
        "open",
        "makeContainer",
        "makeStyled",
        "makeInjected",
    }
    pairs = _call_pairs(path, edges)
    assert {
        ("open", "onOpen"),
        ("makeContainer", "selectUser"),
        ("makeStyled", "spacing"),
        ("makeInjected", "selectUser"),
    } <= pairs
    assert not {caller for caller, _ in pairs} & {
        "sock",
        "enhance",
        "decorate",
        "withStore",
        "connector",
    }
