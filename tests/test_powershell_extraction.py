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


@pytest.mark.parametrize("name,source", [
    ("app.py", b"def helper(): pass\ndef caller(): helper()\n"),
    ("app.sh", b"helper() { echo ok; }\ncaller() { helper; }\ncaller\n"),
])
def test_python_and_bash_still_extract_functions_and_calls(name, source):
    nodes, edges = CodeParser().parse_bytes(Path(name), source)
    assert {"helper", "caller"} <= {n.name for n in nodes if n.kind == "Function"}
    assert any(e.kind == "CALLS" and e.target.endswith("helper") for e in edges)
