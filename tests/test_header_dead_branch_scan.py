"""Header classification and dead-guard checks over long preprocessor chains.

Regression coverage for PR #1099 (issue #977). A ``.h`` file is classified as
C or C++ by ``CodeParser._has_cpp_header_evidence``. It used to call
``_is_in_static_dead_guard`` for every syntax node, and each call walked the
whole ancestor chain. tree-sitter nests an ``#if`` / ``#elif`` chain as a
right-leaning spine, so on generated AUTOSAR MemMap headers the scan was
quadratic in the number of branches and a single file took minutes.

The scan now carries the dead-branch state down the tree and checks each
parent-child edge once, and ``_is_in_static_dead_guard`` checks each edge of
the ancestor chain once. These tests pin four things: the scan does no
per-node ancestor walk, it makes one Python-level edge check per node, the
#977 header stays inside a wall-clock budget, and every ``#if 0`` /
``#elif 0`` / ``#else`` / ``if false`` decision is the same as before the
change.

The scan still reads ``node.prev_named_sibling`` once per node, which
tree-sitter resolves from the tree root, so total cost is not yet linear in
the chain length. The budget below is set to catch a Python-level per-node
ancestor walk (seconds at this size), not that remaining native cost.
"""

import time
from pathlib import Path

import pytest

from code_review_graph import parser as parser_module
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, incremental_update
from code_review_graph.parser import (
    CodeParser,
    _is_in_static_dead_guard,
    _is_statically_false_condition,
    _node_is_in_child,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _issue_977_memmap_header(sections: int, newline: str = "\n") -> bytes:
    """Return the synthetic MemMap header produced by the generator in #977."""
    module = "SynMod"
    lines = [
        "/* synthetic MemMap-style section-mapping header */",
        "#if defined (START_WITH_IF)",
    ]
    for index in range(sections):
        for kind, pragma in (
            ("START", f'#pragma section bss=".module_section_{index}"'),
            ("STOP", "#pragma section"),
        ):
            macro = f"{module}_{kind}_SEC_M{index}"
            lines.append(f"#elif defined {macro}")
            lines.append(f"   #undef {macro}")
            lines.append(f"   {pragma}")
            lines.append("")
    lines.append("#endif")
    return newline.join(lines).encode()


def _file_language(nodes) -> str:
    return next(node.language for node in nodes if node.kind == "File")


def _forbid_ancestor_walk(monkeypatch) -> None:
    def unexpected_ancestor_walk(node) -> bool:
        raise AssertionError("header evidence scan walked a node's ancestors")

    monkeypatch.setattr(
        parser_module, "_is_in_static_dead_guard", unexpected_ancestor_walk,
    )


def _named_nodes(root) -> list:
    found = []
    pending = [root]
    while pending:
        node = pending.pop()
        found.append(node)
        pending.extend(node.named_children)
    return found


@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_issue_977_memmap_header_parses_without_per_node_ancestor_walk(
    tmp_path: Path, monkeypatch, newline: str,
) -> None:
    source = _issue_977_memmap_header(200, newline)
    _forbid_ancestor_walk(monkeypatch)

    nodes, edges = CodeParser().parse_bytes(tmp_path / "memmap.h", source)

    assert [node.kind for node in nodes] == ["File"]
    assert _file_language(nodes) == "c"
    assert edges == []


def test_header_scan_checks_each_parent_child_edge_once(monkeypatch) -> None:
    tree_parser = CodeParser()._get_parser("cpp")
    assert tree_parser is not None
    tree = tree_parser.parse(_issue_977_memmap_header(150))
    named_node_count = len(_named_nodes(tree.root_node))

    calls = []
    edge_check = parser_module._is_static_dead_guard_child

    def counting_edge_check(parent, child) -> bool:
        calls.append((parent.type, child.type))
        return edge_check(parent, child)

    monkeypatch.setattr(
        parser_module, "_is_static_dead_guard_child", counting_edge_check,
    )
    assert CodeParser._has_cpp_header_evidence(tree.root_node) is False
    # One Python-level check per child edge of a visited node: never more than
    # the tree has nodes, however deep the #elif spine is. This counts only the
    # edge checks; the wall-clock budget test below covers total cost.
    assert 0 < len(calls) < named_node_count


def test_issue_977_header_scan_stays_inside_wall_clock_budget() -> None:
    """The 400-branch #977 header must classify in well under a second.

    With a per-node ancestor walk this scan took several seconds at this size
    (about 14 seconds for a full parse before #1099); without it, it takes a
    few hundredths of a second. The budget leaves wide room for slow CI.
    """
    tree_parser = CodeParser()._get_parser("cpp")
    assert tree_parser is not None
    root = tree_parser.parse(_issue_977_memmap_header(200)).root_node

    best = float("inf")
    for _ in range(3):
        start = time.perf_counter()
        assert CodeParser._has_cpp_header_evidence(root) is False
        best = min(best, time.perf_counter() - start)

    assert best < 2.0, f"header evidence scan took {best:.3f}s for 400 branches"


@pytest.mark.parametrize(
    ("source", "language"),
    [
        pytest.param(
            "#if defined(X)\ntypedef int a;\n#elif 0\nclass Dead {};\n#endif\n",
            "c",
            id="elif-0-branch-is-dead",
        ),
        pytest.param(
            "#if 0\ntypedef int a;\n#else\nclass Live {};\n#endif\n",
            "cpp",
            id="else-of-if-0-is-live",
        ),
        pytest.param(
            "#if 0\ntypedef int a;\n#elif 1\nclass Live {};\n#endif\n",
            "cpp",
            id="elif-after-if-0-is-live",
        ),
        pytest.param(
            "#if defined(X)\ntypedef int a;\n#elif 0\ntypedef int b;\n"
            "#else\nnamespace live {}\n#endif\n",
            "cpp",
            id="else-of-elif-0-is-live",
        ),
        pytest.param(
            "#if 0\n#if 1\nclass Dead {};\n#endif\n#endif\ntypedef int v;\n",
            "c",
            id="live-guard-nested-in-if-0-is-dead",
        ),
        pytest.param(
            "#if 0\n#if 0\n#else\nclass Dead {};\n#endif\n#endif\ntypedef int v;\n",
            "c",
            id="else-nested-in-if-0-is-dead",
        ),
        pytest.param(
            "#if 1\n#if 0\nclass Dead {};\n#endif\ntypedef int v;\n#endif\n",
            "c",
            id="if-0-nested-in-live-guard-is-dead",
        ),
        pytest.param(
            "#if 0\ntypedef int dead;\n#endif\nclass Live {};\n",
            "cpp",
            id="dead-state-does-not-leak-to-next-sibling",
        ),
        pytest.param(
            "class Live {};\n#if 0\ntypedef int dead;\n#endif\n",
            "cpp",
            id="evidence-before-dead-block",
        ),
        pytest.param(
            "/* ünïçødé 日本 */\n#if 0 /* ünï */\n"
            "class Dead {};\n#endif\ntypedef int v; /* 日本 */\n",
            "c",
            id="unicode-comments-dead",
        ),
        pytest.param(
            "/* ünïçødé 日本 */\n#if 0 /* ünï */\n"
            "class Dead {};\n#else /* 日本 */\nclass Live {};\n#endif\n",
            "cpp",
            id="unicode-comments-live-else",
        ),
        pytest.param(
            "#if 0\r\nclass Dead {};\r\n#endif\r\ntypedef int v;\r\n",
            "c",
            id="crlf-dead",
        ),
        pytest.param(
            "#if 0\r\ntypedef int a;\r\n#else\r\nclass Live {};\r\n#endif\r\n",
            "cpp",
            id="crlf-live-else",
        ),
        pytest.param(
            "void f(void) {\n#if 0\nauto g = [](){};\n#endif\n}\n",
            "c",
            id="dead-lambda-inside-function-body",
        ),
        pytest.param("", "c", id="empty"),
        pytest.param("\n\n   \n", "c", id="whitespace-only"),
        pytest.param("/* only a comment */\n", "c", id="comment-only"),
    ],
)
def test_header_language_honours_dead_preprocessor_branches(
    tmp_path: Path, source: str, language: str,
) -> None:
    nodes, _ = CodeParser().parse_bytes(tmp_path / "guarded.h", source.encode())

    assert _file_language(nodes) == language


def _deep_chain(head: str, branch: str, tail: str, branches: int) -> bytes:
    body = "".join(branch.format(index=index) for index in range(branches))
    return (head + body + tail).encode()


@pytest.mark.parametrize(
    ("source", "language"),
    [
        pytest.param(
            _deep_chain(
                "#if 0\n",
                "#elif 0\ntypedef int dead_{index};\n",
                "#else\nclass Live {};\n#endif\n",
                150,
            ),
            "cpp",
            id="else-under-long-dead-elif-chain-is-live",
        ),
        pytest.param(
            _deep_chain(
                "#if defined(A)\n",
                "#elif defined(X{index})\n#undef X{index}\n",
                "#elif 0\nclass Dead {};\n#endif\n",
                150,
            ),
            "c",
            id="elif-0-at-end-of-long-chain-is-dead",
        ),
        pytest.param(
            _deep_chain(
                "#if defined(A)\n",
                "#elif defined(X{index})\n#undef X{index}\n",
                "#elif defined(LAST)\nclass Live {};\n#endif\n",
                150,
            ),
            "cpp",
            id="live-branch-at-end-of-long-chain",
        ),
    ],
)
def test_long_chain_classification_reaches_the_deepest_branch(
    tmp_path: Path, monkeypatch, source: bytes, language: str,
) -> None:
    _forbid_ancestor_walk(monkeypatch)

    nodes, _ = CodeParser().parse_bytes(tmp_path / "chain.h", source)

    assert _file_language(nodes) == language


def _reference_is_in_static_dead_guard(node) -> bool:
    """The two-walk algorithm ``_is_in_static_dead_guard`` used before #1099."""
    cursor = node.parent
    while cursor is not None:
        if cursor.type == "if_statement":
            condition = cursor.child_by_field_name("condition")
            consequence = cursor.child_by_field_name("consequence")
            if (
                condition is not None
                and consequence is not None
                and _is_statically_false_condition(condition)
                and _node_is_in_child(node, consequence)
            ):
                return True
        cursor = cursor.parent

    preproc = node.parent
    while preproc is not None:
        if preproc.type in ("preproc_if", "preproc_elif"):
            condition = preproc.child_by_field_name("condition")
            if (
                condition is not None
                and condition.type == "number_literal"
                and condition.text == b"0"
            ):
                alternative = preproc.child_by_field_name("alternative")
                if not (
                    alternative is not None
                    and _node_is_in_child(node, alternative)
                ):
                    return True
        preproc = preproc.parent
    return False


_EQUIVALENCE_SOURCES = [
    ("go", (FIXTURES / "sample_dead_guard.go").read_bytes(), True),
    ("typescript", (FIXTURES / "sample_dead_guard.ts").read_bytes(), True),
    ("c", (FIXTURES / "sample_dead_guard.c").read_bytes(), True),
    (
        "go",
        b"func f() {\n  if false { a() } else if false { b() } else { c() }\n"
        b"  if true { if false { d() } else { e() } }\n}\n",
        True,
    ),
    (
        "typescript",
        b"function f() {\n  if (false) g(); else h();\n"
        b"  if (0) { if (true) { x(); } }\n  if ((false)) { y(); }\n"
        b"  if (1) { z(); }\n}\n",
        True,
    ),
    ("javascript", b"if (0) { a(); } else { b(); }\nif (false) c();\n", True),
    (
        "c",
        b"void f(void) {\n#if 0\n  a();\n#elif 0\n  b();\n#elif 1\n  c();\n"
        b"#else\n  d();\n#endif\n#if 1\n#if 0\n  e();\n#else\n  g();\n#endif\n"
        b"#endif\n}\n",
        True,
    ),
    (
        "cpp",
        _deep_chain(
            "#if 0\nclass A {};\n",
            "#elif 0\nvoid dead_{index}();\n",
            "#else\nclass Live { void m(); };\n#endif\n",
            60,
        ),
        True,
    ),
    ("cpp", _issue_977_memmap_header(30), False),
]


@pytest.mark.parametrize(
    ("language", "source", "has_dead_branch"),
    _EQUIVALENCE_SOURCES,
    ids=[f"{entry[0]}-{index}" for index, entry in enumerate(_EQUIVALENCE_SOURCES)],
)
def test_single_walk_dead_guard_matches_previous_two_walk_result(
    language: str, source: bytes, has_dead_branch: bool,
) -> None:
    tree_parser = CodeParser()._get_parser(language)
    assert tree_parser is not None
    tree = tree_parser.parse(source)

    nodes = _named_nodes(tree.root_node)
    dead = [node for node in nodes if _reference_is_in_static_dead_guard(node)]
    mismatches = [
        (node.type, node.start_point, node.text[:40])
        for node in nodes
        if _is_in_static_dead_guard(node) != _reference_is_in_static_dead_guard(node)
    ]

    assert mismatches == []
    assert bool(dead) is has_dead_branch


def test_cpp_callable_declarations_under_if_0_stay_unindexed(tmp_path: Path) -> None:
    source = b"""#if 0
void dead_decl(int);
#else
void live_decl(int);
#endif
class Widget {
#if 0
  void dead_member();
#endif
  void live_member();
};
"""
    nodes, _ = CodeParser().parse_bytes(tmp_path / "Widget.hpp", source)

    functions = sorted(node.name for node in nodes if node.kind == "Function")
    assert functions == ["live_decl", "live_member"]


def test_c_calls_in_dead_elif_chain_are_dropped(tmp_path: Path) -> None:
    source = b"""void live(void);
void dead0(void);
void dead1(void);
void caller(void) {
#if 0
  dead0();
#elif 0
  dead1();
#else
  live();
#endif
}
"""
    _, edges = CodeParser().parse_bytes(tmp_path / "caller.c", source)

    targets = sorted(edge.target.split("::")[-1] for edge in edges if edge.kind == "CALLS")
    assert targets == ["live"]


def test_memmap_header_build_and_update_flip_language_with_live_evidence(
    tmp_path: Path,
) -> None:
    header = tmp_path / "memmap.h"
    header.write_bytes(_issue_977_memmap_header(60))
    (tmp_path / "main.c").write_text(
        "int helper(void);\nint use(void) { return helper(); }\n",
        encoding="utf-8",
    )

    with GraphStore(tmp_path / "graph.db") as store:
        result = full_build(tmp_path, store)
        assert result["errors"] == []
        assert result["files_parsed"] == 2
        stored = store.get_nodes_by_file(str(header))
        assert [node.kind for node in stored] == ["File"]
        assert stored[0].language == "c"

        header.write_bytes(
            _issue_977_memmap_header(60).replace(
                b"#endif", b"#else\nclass Live {};\n#endif",
            )
        )
        updated = incremental_update(tmp_path, store, changed_files=["memmap.h"])
        assert updated["errors"] == []
        stored = store.get_nodes_by_file(str(header))
        assert {node.language for node in stored} == {"cpp"}
        assert any(node.kind == "Class" and node.name == "Live" for node in stored)

        # Back to the plain header, twice: the second update must be a no-op.
        header.write_bytes(_issue_977_memmap_header(60))
        for _ in range(2):
            reverted = incremental_update(tmp_path, store, changed_files=["memmap.h"])
            assert reverted["errors"] == []
            stored = store.get_nodes_by_file(str(header))
            assert [node.kind for node in stored] == ["File"]
            assert stored[0].language == "c"


# ---------------------------------------------------------------------------
# Audit additions for PR #1099.
# ---------------------------------------------------------------------------


def _reference_has_cpp_header_evidence(root) -> bool:
    """``_has_cpp_header_evidence`` as it was before #1099: per-node walk."""
    pending = [root]
    while pending:
        node = pending.pop()
        if node.type == "ERROR" or _reference_is_in_static_dead_guard(node):
            continue
        previous = node.prev_named_sibling
        if (
            previous is not None
            and previous.type == "ERROR"
            and previous.end_byte == node.start_byte
        ):
            continue
        if node.type in parser_module._CPP_HEADER_EVIDENCE_TYPES:
            return True
        if node.type == "enum_specifier" and any(
            child.type in ("class", "struct") for child in node.children
        ):
            return True
        if (
            node.type == "type_qualifier"
            and node.text in parser_module._CPP_HEADER_EVIDENCE_QUALIFIERS
        ):
            return True
        pending.extend(node.named_children)
    return False


_RANDOM_HEADER_OPENERS = (
    "#if 0", "#if 0", "#if 1", "#if defined(X{n})", "#ifdef X{n}", "#ifndef X{n}",
)
_RANDOM_HEADER_BRANCHES = (
    "#elif 0", "#elif 0", "#elif 1", "#elif defined(Y{n})", "#elifdef Y{n}", "#else",
)
_RANDOM_HEADER_LEAVES = (
    "typedef int t{n};",
    "#pragma section",
    "#undef X{n}",
    "int f{n}(void);",
    "class C{n} {{}};",
    "namespace n{n} {{}}",
    "template <typename T> struct S{n};",
    "enum class E{n} {{ A }};",
    "consteval int k{n}();",
    "struct P{n} {{\n#if 0\npublic:\n#endif\n  int x;\n}};",
    "void g{n}(void) {{\n#if 0\n  auto l = [](){{}};\n#endif\n  if (0) {{ h(); }}\n}}",
    "@class Forward{n};",
)


def _random_header(rng, depth: int = 0) -> str:
    lines = []
    for _ in range(rng.randint(1, 3)):
        if depth < 3 and rng.random() < 0.55:
            n = rng.randint(0, 99)
            lines.append(rng.choice(_RANDOM_HEADER_OPENERS).format(n=n))
            lines.append(_random_header(rng, depth + 1))
            seen_else = False
            for _ in range(rng.randint(0, 3)):
                branch = rng.choice(_RANDOM_HEADER_BRANCHES).format(n=n)
                if seen_else:
                    break
                seen_else = branch == "#else"
                lines.append(branch)
                lines.append(_random_header(rng, depth + 1))
            lines.append("#endif")
        else:
            lines.append(rng.choice(_RANDOM_HEADER_LEAVES).format(n=rng.randint(0, 99)))
    return "\n".join(lines)


@pytest.mark.parametrize("grammar", ["cpp", "c"])
def test_top_down_scan_matches_per_node_scan_on_random_guard_nesting(
    grammar: str,
) -> None:
    """The flag carried down the tree must give the old per-node answer.

    ``test_single_walk_dead_guard_matches_previous_two_walk_result`` checks the
    ancestor-walk helper, but ``_has_cpp_header_evidence`` no longer calls it:
    it carries its own flag. This compares that scan, and the helper on every
    node, with the pre-#1099 algorithm over seeded random nestings of
    ``#if 0`` / ``#if 1`` / ``#ifdef`` / ``#elif 0`` / ``#elifdef`` / ``#else``
    around C++-only syntax, C syntax, error recovery and ``if (0)``. Production
    classifies ``.h`` files with the C++ grammar; the C grammar run covers the
    helper on the trees ``.c`` files produce.
    """
    import random

    rng = random.Random(1099)
    tree_parser = CodeParser()._get_parser(grammar)
    assert tree_parser is not None
    outcomes = set()
    dead_nodes = 0
    for _ in range(200):
        source = _random_header(rng)
        if rng.random() < 0.3:
            source = source.replace("\n", "\r\n")
        tree = tree_parser.parse(source.encode())
        expected = _reference_has_cpp_header_evidence(tree.root_node)
        assert CodeParser._has_cpp_header_evidence(tree.root_node) is expected, source
        outcomes.add(expected)
        for node in _named_nodes(tree.root_node):
            reference = _reference_is_in_static_dead_guard(node)
            dead_nodes += reference
            assert _is_in_static_dead_guard(node) is reference, (
                source, node.type, node.start_point,
            )
    # The corpus must exercise dead branches and, under the C++ grammar,
    # both verdicts, or it proves nothing.
    assert dead_nodes > 0
    if grammar == "cpp":
        assert outcomes == {True, False}


@pytest.mark.parametrize(
    ("source", "language"),
    [
        pytest.param(
            "#ifndef MEMMAP_H\n#define MEMMAP_H\n#if 0\nclass Dead {};\n#endif\n"
            "typedef int v;\n#endif\n",
            "c",
            id="if-0-inside-include-guard-is-dead",
        ),
        pytest.param(
            "#ifndef MEMMAP_H\n#define MEMMAP_H\n#if 0\ntypedef int a;\n#else\n"
            "class Live {};\n#endif\n#endif\n",
            "cpp",
            id="else-of-if-0-inside-include-guard-is-live",
        ),
        pytest.param(
            "#pragma once\n#if 0\nnamespace dead {}\n#endif\nint f(void);\n",
            "c",
            id="if-0-after-pragma-once-is-dead",
        ),
        pytest.param(
            "#ifdef X\ntypedef int a;\n#elif 0\nclass Dead {};\n#endif\n",
            "c",
            id="elif-0-under-ifdef-is-dead",
        ),
        pytest.param(
            "#ifndef X\ntypedef int a;\n#elif 0\ntypedef int b;\n#else\n"
            "class Live {};\n#endif\n",
            "cpp",
            id="else-after-elif-0-under-ifndef-is-live",
        ),
        pytest.param(
            "#if 0\n#ifdef X\nclass Dead {};\n#else\nnamespace dead {}\n#endif\n#endif\n"
            "typedef int v;\n",
            "c",
            id="ifdef-and-else-nested-in-if-0-are-dead",
        ),
        pytest.param(
            "#if 0\ntypedef int a;\n#elifdef X\nclass Live {};\n#endif\n",
            "cpp",
            id="elifdef-after-if-0-is-live",
        ),
        pytest.param(
            "struct S {\n#if 0\npublic:\n#endif\n  int x;\n};\n",
            "c",
            id="dead-access-specifier-inside-struct-body",
        ),
        pytest.param(
            "struct S {\n#if 0\n  int y;\n#else\npublic:\n#endif\n  int x;\n};\n",
            "cpp",
            id="live-access-specifier-inside-struct-body",
        ),
        pytest.param(
            "#if 0\nenum class Dead { A };\n#endif\ntypedef int v;\n",
            "c",
            id="dead-scoped-enum",
        ),
        pytest.param(
            "#if 0\nconsteval int dead();\n#endif\ntypedef int v;\n",
            "c",
            id="dead-consteval-qualifier",
        ),
        pytest.param(
            "#if defined(A)\ntypedef int a;\n#elif 0\ntemplate <typename T> struct D;\n"
            "#endif\n",
            "c",
            id="dead-template-under-elif-0",
        ),
        pytest.param(
            "#if 0\nclass Dead {};\n",
            "c",
            id="unterminated-if-0-is-dead",
        ),
        pytest.param(
            "#if 0\nclass Dead {};\n#else\nclass Live {};\n",
            "cpp",
            id="unterminated-if-0-else-is-live",
        ),
    ],
)
def test_header_language_with_include_guards_and_every_evidence_kind(
    tmp_path: Path, source: str, language: str,
) -> None:
    nodes, _ = CodeParser().parse_bytes(tmp_path / "guarded.h", source.encode())

    assert _file_language(nodes) == language


@pytest.mark.parametrize(
    ("tail", "language"),
    [
        pytest.param("#endif\n#endif\n", "c", id="dead-free"),
        pytest.param("#else\nclass Live {};\n#endif\n#endif\n", "cpp", id="live-at-end"),
        pytest.param(
            "#elif 0\nclass Dead {};\n#endif\n#endif\n", "c", id="dead-at-end",
        ),
    ],
)
def test_include_guarded_memmap_chain_is_classified_without_ancestor_walk(
    tmp_path: Path, monkeypatch, tail: str, language: str,
) -> None:
    """Real MemMap headers sit inside an include guard or ``#pragma once``."""
    chain = _issue_977_memmap_header(150).decode().rsplit("#endif", 1)[0]
    source = "#ifndef SYNMOD_MEMMAP_H\n#define SYNMOD_MEMMAP_H\n" + chain + tail
    _forbid_ancestor_walk(monkeypatch)

    nodes, _ = CodeParser().parse_bytes(tmp_path / "SynMod_MemMap.h", source.encode())

    assert _file_language(nodes) == language
