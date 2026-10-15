"""C/C++ parse-throughput prefilters (#802)."""

from pathlib import Path

from code_review_graph.parser import CodeParser


def test_plain_c_header_skips_speculative_cpp_parser(monkeypatch):
    source = b"struct point { int x; int y; };\nint distance(struct point p);\n"

    original_parser = CodeParser._get_parser

    def guarded_parser(parser_self, language):
        if language == "cpp":
            raise AssertionError("plain C header must not probe the C++ parser")
        return original_parser(parser_self, language)

    monkeypatch.setattr(CodeParser, "_get_parser", guarded_parser)

    nodes, _ = CodeParser().parse_bytes(Path("point.h"), source)

    assert "point" in {node.name for node in nodes}


def test_cpp_header_still_uses_speculative_parser():
    source = b"namespace sample { struct Wrapper { class Inner {}; }; }\n"

    nodes, _ = CodeParser().parse_bytes(Path("wrapper.h"), source)

    assert {node.name for node in nodes} >= {"Wrapper", "Inner"}


def test_qt_mask_skips_sources_without_structural_macros():
    source = b"int ordinary_function(int value) { return value + 1; }\n"

    assert CodeParser._mask_cpp_qt_macros(source) is source
