"""C/C++ parse-throughput prefilters (issue #802, PR #876)."""

import time
from pathlib import Path

import pytest

from code_review_graph.parser import _CPP_QT_STRUCTURAL_MACRO_REPLACEMENTS, CodeParser


def _guard_cpp_parser(monkeypatch):
    original_parser = CodeParser._get_parser

    def guarded_parser(parser_self, language):
        if language == "cpp":
            raise AssertionError("plain C header must not probe the C++ parser")
        return original_parser(parser_self, language)

    monkeypatch.setattr(CodeParser, "_get_parser", guarded_parser)


def test_plain_c_header_skips_speculative_cpp_parser(monkeypatch):
    source = b"struct point { int x; int y; };\nint distance(struct point p);\n"

    _guard_cpp_parser(monkeypatch)

    nodes, _ = CodeParser().parse_bytes(Path("point.h"), source)

    assert "point" in {node.name for node in nodes}


@pytest.mark.parametrize(
    "source",
    [
        b"",
        b"struct flags { unsigned a : 3; unsigned b : 5; };\n",
        b"int a[3];\nint m[2][2] = {{0}};\nint (*handlers[3])(void);\n",
        b"/* a public class using a template */\nint f(void); // namespace class\n",
        b"static const char *word = \"class using\";\nstatic const char quote = '\"';\n",
    ],
    ids=["empty", "bitfield", "arrays", "comments", "strings"],
)
def test_c_only_text_is_not_mistaken_for_cpp(monkeypatch, source):
    _guard_cpp_parser(monkeypatch)

    nodes, _ = CodeParser().parse_bytes(Path("plain.h"), source)

    assert next(node.language for node in nodes if node.kind == "File") == "c"


def test_cpp_header_still_uses_speculative_parser():
    source = b"namespace sample { struct Wrapper { class Inner {}; }; }\n"

    nodes, _ = CodeParser().parse_bytes(Path("wrapper.h"), source)

    assert {node.name for node in nodes} >= {"Wrapper", "Inner"}


# Each shape carries one kind of evidence and none of the others.
@pytest.mark.parametrize(
    "source",
    [
        b"struct Widget { public: void reset(); };\n",
        b"struct Widget { private: int x; };\n",
        b"struct Widget { protected: int x; };\n",
        b"using Id = int;\n",
        b"using std::string;\n",
        b"struct Parent {}; struct Child : Parent { int run() { return 1; } };\n",
        b"struct Child /* note */ final : Parent {};\n",
        b"union Mixed : Parent { int x; };\n",
        b"enum struct Colour { Red };\n",
        b"enum /* note */ struct Colour { Red };\n",
        b"enum class Colour { Red };\n",
        b"consteval int one() { return 1; }\n",
        b"constinit int one = 1;\n",
        b"auto one = [](int x) { return x; };\n",
        b"auto one = [] { return 1; };\n",
        b"auto one = [] /* note */ { return 1; };\n",
        b"auto one = [] // note\n{ return 1; };\n",
        b"auto one = [x] mutable { return x; };\n",
        b"auto one = []<typename T>(T x) { return x; };\n",
        b"auto one = [] [[nodiscard]] () { return 1; };\n",
        b"template <typename T> concept Any = true;\n",
        b"void one() noexcept;\n",
        b"auto one() -> int;\n",
        b"struct Widget { Q_SIGNALS: void changed(); };\n",
    ],
)
def test_every_kind_of_cpp_evidence_still_promotes_the_header(source):
    nodes, _ = CodeParser().parse_bytes(Path("shape.h"), source)

    assert next(node.language for node in nodes if node.kind == "File") == "cpp"


def test_base_class_header_keeps_its_structure():
    source = b"struct Parent {}; struct Child : Parent { int run() { return 1; } };\n"

    nodes, edges = CodeParser().parse_bytes(Path("derived.h"), source)

    assert {node.name: node.kind for node in nodes if node.name == "Parent"} == {"Parent": "Class"}
    assert [node.parent_name for node in nodes if node.name == "run"] == ["Child"]
    assert any(edge.kind == "INHERITS" and edge.target == "Parent" for edge in edges)


@pytest.mark.parametrize(
    "source",
    [b"struct " * 50_000, b"enum " * 50_000, b"/* " * 100_000, b"] " * 100_000, b'" ' * 100_001],
    ids=["struct", "enum", "open-comments", "brackets", "quotes"],
)
def test_text_check_stays_fast_on_hostile_input(source):
    started = time.monotonic()

    CodeParser._has_cpp_header_text_evidence(source)

    assert time.monotonic() - started < 2.0


def test_qt_mask_skips_sources_without_structural_macros():
    source = b"int ordinary_function(int value) { return value + 1; }\n"

    assert CodeParser._mask_cpp_qt_macros(source) is source


def test_every_masked_qt_macro_is_caught_by_the_fast_path():
    assert all(
        key.startswith((b"Q_", b"QT_")) for key in _CPP_QT_STRUCTURAL_MACRO_REPLACEMENTS
    )
