"""PowerShell grammar extraction regressions for issue #1067."""

from pathlib import Path

import pytest

from code_review_graph.parser import CodeParser

SOURCE = b'''
class Widget {
  [string] Name() { return "w" }
}
function Get-Thing {
  param([string] $Name)
  return Convert-Thing -Value $Name
}
function Convert-Thing { param([string] $Value); return $Value.ToUpper() }
function Test-Health { Get-Thing -Name "x" }
Get-Thing -Name "x"
'''


@pytest.mark.parametrize("extension", [".ps1", ".psm1"])
def test_extracts_declarations_and_method_scope(extension):
    nodes, _ = CodeParser().parse_bytes(Path("app" + extension), SOURCE)
    classes = {n.name for n in nodes if n.kind == "Class"}
    functions = {n.name: n for n in nodes if n.kind == "Function"}
    assert classes == {"Widget"}
    assert {"Get-Thing", "Convert-Thing", "Name", "Test-Health"} <= functions.keys()
    assert functions["Name"].parent_name == "Widget"


def test_extracts_internal_and_top_level_calls():
    _, edges = CodeParser().parse_bytes(Path("app.ps1"), SOURCE)
    calls = [e for e in edges if e.kind == "CALLS"]
    assert any(e.source.endswith("::Get-Thing") and e.target.endswith("Convert-Thing")
               for e in calls)
    assert any(e.source.endswith("app.ps1") and e.target.endswith("Get-Thing")
               for e in calls)


def test_approved_test_verb_is_production_code():
    nodes, _ = CodeParser().parse_bytes(Path("health.ps1"), SOURCE)
    node = next(n for n in nodes if n.name == "Test-Health")
    assert node.kind == "Function"
    assert not node.is_test


def test_approved_test_verb_in_test_file_is_still_test_code():
    nodes, _ = CodeParser(repo_root=Path("/repo")).parse_bytes(
        Path("/repo/tests/health.ps1"), SOURCE,
    )
    node = next(n for n in nodes if n.name == "Test-Health")
    assert node.is_test


@pytest.mark.parametrize("source", [
    b"function Get-Many { foreach ($i in 1..3) { Get-Leaf } }",
    b"1..10 | Measure-Object",
    b"function Get-Many { foreach ($i in -3..-1) { Get-Leaf } }",
    b"1.5..3.5 | Measure-Object",
    b".5..3.5 | Measure-Object",
    b"0x1..0x3 | Measure-Object",
    b"0b1..0b11 | Measure-Object",
    b"1e1..2e1 | Measure-Object",
    b"1kb..3kb | Measure-Object",
    b"1UL..3UL | Measure-Object",
    b"1d..3d | Measure-Object",
    b"1 .. 3 | Measure-Object",
])
def test_numeric_ranges_are_not_command_calls(source):
    _, edges = CodeParser().parse_bytes(Path("app.ps1"), source)
    targets = {e.target.rsplit("::", 1)[-1] for e in edges if e.kind == "CALLS"}
    assert not any(".." in target for target in targets)
    assert targets & {"Get-Leaf", "Measure-Object"}


@pytest.mark.parametrize("name", [
    "1..3.ps1", "build..release", "1gbus..2gbus", "1.5ul..3ul", "0b1d..0b11d",
])
def test_range_like_command_names_remain_calls(name):
    _, edges = CodeParser().parse_bytes(Path("app.ps1"), name.encode())
    assert any(e.kind == "CALLS" and e.target.endswith(name) for e in edges)


@pytest.mark.parametrize("path,expected", [("resource.psm1", False), ("tests/resource.psm1", True)])
def test_dsc_test_method_uses_file_context(path, expected):
    nodes, edges = CodeParser(repo_root=Path("/repo")).parse_bytes(
        Path("/repo") / path,
        b"class R { [bool] Test() { return Get-State } }",
    )
    node = next(n for n in nodes if n.name == "Test")
    assert node.is_test is expected
    assert (node.kind == "Test") is expected
    if not expected:
        assert all(e.kind != "TESTED_BY" for e in edges)


@pytest.mark.parametrize("name,source", [
    ("app.py", b"def helper(): pass\ndef caller(): helper()\n"),
    ("app.sh", b"helper() { echo ok; }\ncaller() { helper; }\ncaller\n"),
])
def test_python_and_bash_still_extract_functions_and_calls(name, source):
    nodes, edges = CodeParser().parse_bytes(Path(name), source)
    assert {"helper", "caller"} <= {n.name for n in nodes if n.kind == "Function"}
    assert any(e.kind == "CALLS" and e.target.endswith("helper") for e in edges)
