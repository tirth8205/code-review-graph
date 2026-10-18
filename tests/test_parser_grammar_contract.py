"""Grammar dispatch names must exist in the bundled grammar (issue #986).

PR #1086 adds the contract and removes the names it found to be unreal
(C# ``record_struct_declaration``, Swift ``struct_declaration``, Objective-C
``category_interface``, Ruby ``method_call``) and renames PowerShell's call
dispatch from ``command_expression`` to ``command``.

The extra cases below pin what the bare ``id_for_node_kind`` check cannot see:
a supertype or hidden rule has an id yet never appears in a parse tree; a
table keyed by a language the parser never detects matches nothing; the
PowerShell rename must actually route real commands into call extraction;
and the constructs the removed names were meant for must still be extracted.
"""

import pytest
from tree_sitter_language_pack import get_language

from code_review_graph.parser import (
    _CALL_TYPES,
    _CLASS_TYPES,
    _FUNCTION_TYPES,
    _IMPORT_TYPES,
    EXTENSION_TO_LANGUAGE,
    SHEBANG_INTERPRETER_TO_LANGUAGE,
    CodeParser,
)

_TABLES = (
    ("classes", _CLASS_TYPES), ("functions", _FUNCTION_TYPES),
    ("imports", _IMPORT_TYPES), ("calls", _CALL_TYPES),
)

_DISPATCH_ENTRIES = [
    (table, language, node_type)
    for table, mapping in _TABLES
    for language, names in mapping.items()
    for node_type in names
]


def _concrete_named_kind(grammar, node_type: str) -> bool:
    """True when *node_type* is a named kind a parse tree can contain.

    ``id_for_node_kind`` also answers for supertypes (``expression`` in
    Python), which are hidden and never appear as a node's ``type``, and
    maps the empty string to the built-in ``ERROR`` symbol, so the id must
    round-trip to the same name.
    """
    kind_id = grammar.id_for_node_kind(node_type, True)
    if kind_id is None or grammar.node_kind_for_id(kind_id) != node_type:
        return False
    if kind_id in getattr(grammar, "supertypes", ()):
        return False
    return bool(grammar.node_kind_is_visible(kind_id))


@pytest.mark.parametrize(("table", "language", "node_type"), _DISPATCH_ENTRIES)
def test_dispatch_type_exists_in_grammar(table, language, node_type):
    grammar = get_language(language)
    assert grammar.id_for_node_kind(node_type, True) is not None, (
        f"{table}[{language!r}] contains nonexistent named node {node_type!r}"
    )


@pytest.mark.parametrize(("table", "language", "node_type"), _DISPATCH_ENTRIES)
def test_dispatch_type_is_a_concrete_visible_node(table, language, node_type):
    grammar = get_language(language)
    assert _concrete_named_kind(grammar, node_type), (
        f"{table}[{language!r}] names {node_type!r}, which never appears in a "
        "parse tree (missing, hidden or a supertype)"
    )


@pytest.mark.parametrize(
    ("language", "node_type", "expected"),
    [
        ("python", "call", True),
        ("python", "function_definition", True),
        ("powershell", "command", True),
        # Supertype: has an id, never a node type in a tree.
        ("python", "expression", False),
        ("python", "primary_expression", False),
        # Hidden rule and anonymous keyword.
        ("python", "_simple_statement", False),
        ("python", "def", False),
        # The five names issue #986 reported.
        ("csharp", "record_struct_declaration", False),
        ("swift", "struct_declaration", False),
        ("objc", "category_interface", False),
        ("ruby", "method_call", False),
        ("powershell", "command_expression", False),
        # Empty name resolves to ERROR's id; it must not count as real.
        ("python", "", False),
        ("powershell", "", False),
    ],
)
def test_contract_checker_rejects_unreal_kinds(language, node_type, expected):
    assert _concrete_named_kind(get_language(language), node_type) is expected


def test_dispatch_tables_are_keyed_by_detected_loadable_languages():
    detectable = set(EXTENSION_TO_LANGUAGE.values())
    detectable |= set(SHEBANG_INTERPRETER_TO_LANGUAGE.values())
    for table, mapping in _TABLES:
        for language, names in mapping.items():
            assert language in detectable, (
                f"{table} has an entry for {language!r}, which the parser never detects"
            )
            get_language(language)  # raises if the grammar is not bundled
            duplicates = sorted({n for n in names if names.count(n) > 1})
            assert not duplicates, f"{table}[{language!r}] repeats {duplicates}"


def _spy_call_dispatch(monkeypatch) -> list[tuple[str, str]]:
    seen: list[tuple[str, str]] = []
    original = CodeParser._extract_calls

    def spy(self, child, source, language, *args, **kwargs):
        if language == "powershell":
            seen.append((child.type, child.text.decode("utf-8", errors="replace")))
        return original(self, child, source, language, *args, **kwargs)

    monkeypatch.setattr(CodeParser, "_extract_calls", spy)
    return seen


_PS_SOURCE = """\
function Get-Foo {
    param($x)
    Invoke-Bar -Name $x
}

Invoke-Command -ScriptBlock { Get-Inner 1 }
Get-Foo 2
"""


def test_powershell_commands_reach_call_extraction(tmp_path, monkeypatch):
    """Every PowerShell command, nested or not, is handed to call extraction.

    On staging the dispatch named ``command_expression``, so no PowerShell
    node ever reached ``_extract_calls``.
    """
    seen = _spy_call_dispatch(monkeypatch)
    path = tmp_path / "scripts dir" / "dépl oy.ps1"
    path.parent.mkdir()
    path.write_text(_PS_SOURCE, encoding="utf-8")

    nodes, _edges = CodeParser().parse_file(path)

    assert {kind for kind, _ in seen} == {"command"}
    heads = sorted(text.split()[0] for _, text in seen)
    assert heads == ["Get-Foo", "Get-Inner", "Invoke-Bar", "Invoke-Command"]
    assert any(n.kind == "File" for n in nodes)


def test_powershell_long_script_dispatches_every_command(tmp_path, monkeypatch):
    """A flat 2,000-command script: none is skipped by call dispatch."""
    seen = _spy_call_dispatch(monkeypatch)
    count = 2000
    path = tmp_path / "many.ps1"
    path.write_text(
        "".join(f"Invoke-Step{i} -Index {i}\n" for i in range(count)),
        encoding="utf-8",
    )

    CodeParser().parse_file(path)

    assert len(seen) == count
    assert seen[-1][1].startswith(f"Invoke-Step{count - 1} ")


def test_csharp_record_struct_still_extracted_as_class(tmp_path):
    """``record struct`` parses as ``record_declaration``; dropping the
    unreal ``record_struct_declaration`` must not lose it."""
    path = tmp_path / "Money.cs"
    path.write_text(
        "namespace App {\n"
        "    public record struct Money(decimal Amount);\n"
        "    public readonly record struct Point(int X) {\n"
        "        public int Twice() => X * 2;\n"
        "    }\n"
        "    public record Person(string Name);\n"
        "}\n",
        encoding="utf-8",
    )

    nodes, edges = CodeParser().parse_file(path)

    classes = {n.name for n in nodes if n.kind == "Class"}
    assert {"Money", "Point", "Person"} <= classes
    twice = [n for n in nodes if n.kind == "Function" and n.name == "Twice"]
    assert len(twice) == 1 and twice[0].parent_name == "Point"


def test_objc_category_still_extracted_as_class(tmp_path):
    """A category parses as ``class_interface``/``class_implementation``;
    dropping the unreal ``category_interface`` must not lose it."""
    path = tmp_path / "NSString+Shout.m"
    path.write_text(
        "#import <Foundation/Foundation.h>\n"
        "@interface NSString (Shout)\n"
        "- (NSString *)shout;\n"
        "@end\n"
        "@implementation NSString (Shout)\n"
        "- (NSString *)shout { return [self uppercaseString]; }\n"
        "@end\n",
        encoding="utf-8",
    )

    nodes, edges = CodeParser().parse_file(path)

    assert any(n.kind == "Class" and n.name == "NSString" for n in nodes)
    shout = [n for n in nodes if n.kind == "Function" and n.name == "shout"]
    assert shout and all(n.parent_name == "NSString" for n in shout)
    calls = {e.target for e in edges if e.kind == "CALLS"}
    assert "uppercaseString" in calls


def test_swift_struct_and_ruby_receiver_calls_still_extracted(tmp_path):
    """Swift structs come from ``class_declaration`` and Ruby receiver calls
    from ``call``; the removed names were never doing that work."""
    swift = tmp_path / "Point.swift"
    swift.write_text(
        "struct Point {\n    func norm() -> Int { return 1 }\n}\n",
        encoding="utf-8",
    )
    ruby = tmp_path / "greeter.rb"
    ruby.write_text(
        "class Greeter\n  def hello\n    Foo.new.run\n  end\nend\n",
        encoding="utf-8",
    )
    parser = CodeParser()

    swift_nodes, _ = parser.parse_file(swift)
    point = [n for n in swift_nodes if n.kind == "Class" and n.name == "Point"]
    assert len(point) == 1 and point[0].extra.get("swift_kind") == "struct"

    _, ruby_edges = parser.parse_file(ruby)
    targets = {e.target.split("::")[-1].split(".")[-1] for e in ruby_edges if e.kind == "CALLS"}
    assert {"new", "run"} <= targets


_PS_PIPELINE_SOURCE = """\
class Widget {
    [void] Run() { Write-Host "hi" }
}
Get-ChildItem -Path . | Where-Object { Test-Path $_ } | Sort-Object Name
"""


def test_powershell_pipeline_stages_and_class_methods_reach_call_extraction(
    tmp_path, monkeypatch,
):
    """Every stage of a pipeline is its own ``command`` node, as is a command
    in a class method body; each one must reach call extraction."""
    seen = _spy_call_dispatch(monkeypatch)
    path = tmp_path / "pipeline.ps1"
    path.write_text(_PS_PIPELINE_SOURCE, encoding="utf-8")

    CodeParser().parse_file(path)

    heads = sorted(text.split()[0] for _, text in seen)
    assert heads == [
        "Get-ChildItem", "Sort-Object", "Test-Path", "Where-Object", "Write-Host",
    ]


def test_powershell_module_file_dispatches_calls(tmp_path, monkeypatch):
    """``.psm1`` modules are PowerShell too and take the same call dispatch."""
    seen = _spy_call_dispatch(monkeypatch)
    path = tmp_path / "Tools.psm1"
    path.write_text(
        "function Get-Foo { Write-Verbose 'x' }\n"
        "Export-ModuleMember -Function Get-Foo\n",
        encoding="utf-8",
    )

    CodeParser().parse_file(path)

    heads = sorted(text.split()[0] for _, text in seen)
    assert heads == ["Export-ModuleMember", "Write-Verbose"]


def test_powershell_half_written_script_still_parses(tmp_path, monkeypatch):
    """An unfinished script (unclosed function, dangling pipe) sends its
    recovered commands through call extraction without raising."""
    seen = _spy_call_dispatch(monkeypatch)
    path = tmp_path / "wip.ps1"
    path.write_text(
        'Write-Output "before"\nfunction Broken {\n    Get-Item $x |\n',
        encoding="utf-8",
    )

    nodes, _edges = CodeParser().parse_file(path)

    assert any(n.kind == "File" for n in nodes)
    assert {kind for kind, _ in seen} == {"command"}
    assert "Write-Output" in {text.split()[0] for _, text in seen}


def test_objc_interface_only_category_is_a_class(tmp_path):
    """The construct ``category_interface`` was meant for: a category declared
    with ``@interface`` and no ``@implementation`` in the file. It parses as
    ``class_interface``, which alone must still yield the Class node."""
    path = tmp_path / "Widget+Private.m"
    path.write_text(
        "@interface Widget (Private)\n"
        "- (void)reset;\n"
        "@end\n",
        encoding="utf-8",
    )

    nodes, edges = CodeParser().parse_file(path)

    widget = [n for n in nodes if n.kind == "Class" and n.name == "Widget"]
    assert len(widget) == 1 and widget[0].line_start == 1
    assert any(
        e.kind == "CONTAINS" and e.target.endswith("::Widget") for e in edges
    )
