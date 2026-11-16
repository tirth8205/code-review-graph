"""C# tests marked only by a framework attribute are recognised as tests.

xUnit (`[Fact]`, `[Theory]`) and MSTest (`[TestMethod]`, `[DataTestMethod]`)
give a test method no naming convention to match, so before this the methods
stayed `Function` nodes: `tests_for` returned nothing for the code they call
and `detect_changes` listed that code as untested.
"""

import hashlib
from pathlib import Path

import pytest

from code_review_graph.changes import analyze_changes
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import CSHARP_IDENTITY_VERSION
from code_review_graph.parser import CodeParser
from code_review_graph.scoped_resolver import resolve_scoped_calls
from code_review_graph.tools.query import query_graph

pytestmark = pytest.mark.skipif(
    CodeParser()._get_parser("csharp") is None,
    reason="csharp tree-sitter grammar not installed",
)

_CORE = (
    "namespace Shop.Core;\n"
    "public class Basket {\n"
    "    public int Count() { return 0; }\n"
    "}\n"
)


def _test_file(attribute: str, using: str = "Xunit") -> str:
    return (
        f"using {using};\n"
        "using Shop.Core;\n"
        "namespace Shop.Core.Tests;\n"
        "public class BasketChecks {\n"
        f"    {attribute}\n"
        "    public void Empty_basket_counts_zero() {\n"
        "        var basket = new Basket();\n"
        "        var n = basket.Count();\n"
        "    }\n"
        "}\n"
    )


def _parse(tmp_path: Path, source: str, name: str = "BasketChecks.cs"):
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return CodeParser().parse_file(path)


def _kind(nodes, name: str = "Empty_basket_counts_zero") -> str:
    return next(n.kind for n in nodes if n.name == name)


def _build(root: Path, attribute: str) -> tuple[GraphStore, Path, Path]:
    store = GraphStore(root / ".code-review-graph" / "graph.db")
    parser = CodeParser(root)
    core = root / "Basket.cs"
    core.write_text(_CORE, encoding="utf-8")
    checks = root / "BasketChecks.cs"
    checks.write_text(_test_file(attribute), encoding="utf-8")
    for path in (core, checks):
        nodes, edges = parser.parse_file(path)
        store.store_file_nodes_edges(
            str(path), nodes, edges, hashlib.sha256(path.read_bytes()).hexdigest(),
        )
    store.set_metadata("csharp_identity_version", CSHARP_IDENTITY_VERSION)
    resolve_scoped_calls(store, root)
    store.resolve_bare_call_targets()
    store.commit()
    return store, core, checks


@pytest.mark.parametrize(
    "attribute",
    [
        "[Fact]",
        "[Theory]",
        "[TestMethod]",
        "[DataTestMethod]",
        "[TestCase(1)]",
        "[TestCaseSource(nameof(Cases))]",
        '[Fact(Skip = "slow")]',
        "[Fact, Trait(\"kind\", \"unit\")]",
        "[Xunit.Fact]",
        "[FactAttribute]",
        "[Microsoft.VisualStudio.TestTools.UnitTesting.TestMethodAttribute]",
    ],
)
def test_framework_attribute_marks_a_test(tmp_path, attribute):
    nodes, _ = _parse(tmp_path, _test_file(attribute))
    assert _kind(nodes) == "Test"


@pytest.mark.parametrize(
    "attribute",
    ["[Obsolete]", "[Factory]", "[TheoryData]", "[TestInitialize]", "[Attribute]", ""],
)
def test_other_attributes_do_not_mark_a_test(tmp_path, attribute):
    # The file name carries no test marker either, so only the attribute decides.
    nodes, _ = _parse(tmp_path, _test_file(attribute), name="BasketChecks.cs")
    assert _kind(nodes) == "Function"


def test_attribute_names_are_csharp_only(tmp_path):
    """`@Fact` on a Java method is not a JUnit test marker."""
    if CodeParser()._get_parser("java") is None:
        pytest.skip("java tree-sitter grammar not installed")
    path = tmp_path / "Basket.java"
    path.write_text(
        "class Basket {\n    @Fact\n    void emptyBasketCountsZero() {}\n}\n",
        encoding="utf-8",
    )
    nodes, _ = CodeParser().parse_file(path)
    assert _kind(nodes, "emptyBasketCountsZero") == "Function"


@pytest.mark.parametrize("attribute", ["[Fact]", "[Theory]", "[TestMethod]"])
def test_tests_for_finds_the_attributed_test(tmp_path, attribute):
    store, core, checks = _build(tmp_path, attribute)
    store.close()
    result = query_graph(
        "tests_for", f"{core}::Shop.Core.Basket.Count", repo_root=str(tmp_path),
    )
    assert result.get("status") == "ok"
    found = {r["qualified_name"]: r for r in result.get("results", [])}
    test_qn = f"{checks}::Shop.Core.Tests.BasketChecks.Empty_basket_counts_zero"
    assert test_qn in found
    assert found[test_qn].get("inferred_by") != "naming_convention"


def test_covered_method_is_not_reported_untested(tmp_path):
    store, core, _checks = _build(tmp_path, "[Fact]")
    try:
        result = analyze_changes(store, [str(core)])
        assert "Count" not in {gap["name"] for gap in result["test_gaps"]}
    finally:
        store.close()
