"""Edge-case tests for PR #779: skill templates must match the exported MCP schema.

These go beyond the PR's own regression test:
- byte-identical generated vs bundled skills (drift in either direction fails)
- every backticked ``*_tool`` reference in every skill resolves to a tool
  actually registered with ``@mcp.tool()`` in main.py (catches typos and
  references to tools that later get renamed or removed)
- no skill references any exported tool by its bare (un-suffixed) name
  for the full tool surface, not just the six names the PR renamed
- generate_skills is robust to unicode/space paths and regenerates
  (overwrites) stale content
"""

import ast
import re
from pathlib import Path

from code_review_graph.skills import _SKILLS, generate_skills

REPO_ROOT = Path(__file__).parents[1]
SKILL_NAMES = ["explore-codebase", "review-changes", "debug-issue", "refactor-safely"]

_BACKTICK = re.compile(r"`([^`]+)`")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _exported_tool_names() -> set[str]:
    """Collect function names registered via @mcp.tool() in main.py."""
    src = (REPO_ROOT / "code_review_graph" / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            target = dec.func if isinstance(dec, ast.Call) else dec
            if isinstance(target, ast.Attribute) and target.attr == "tool":
                names.add(node.name)
    return names


def _backticked_identifiers(markdown: str) -> list[str]:
    """Leading identifier of every backticked span, e.g. `foo(task="x")` -> foo."""
    out = []
    for span in _BACKTICK.findall(markdown):
        m = _IDENT.match(span)
        if m:
            out.append(m.group(0))
    return out


def _all_skill_files(tmp_path: Path) -> list[Path]:
    generated = generate_skills(tmp_path)
    files = []
    for name in SKILL_NAMES:
        files.append(generated / name / "SKILL.md")
        files.append(REPO_ROOT / "skills" / name / "SKILL.md")
    return files


def test_exported_schema_is_nonempty_and_contains_renamed_tools():
    exported = _exported_tool_names()
    assert len(exported) >= 20, exported
    for tool in [
        "get_minimal_context_tool",
        "list_graph_stats_tool",
        "get_community_tool",
        "list_flows_tool",
        "get_flow_tool",
        "find_large_functions_tool",
    ]:
        assert tool in exported


def test_generated_and_bundled_skills_byte_identical(tmp_path):
    """The sdist ships skills/; the installer generates from _SKILLS.

    Any divergence between the two copies is the exact bug class this
    PR fixed, so guard it with strict equality rather than token lists.
    """
    generated = generate_skills(tmp_path)
    for name in SKILL_NAMES:
        gen = (generated / name / "SKILL.md").read_text(encoding="utf-8")
        bundled = (REPO_ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
        assert gen == bundled, f"generated and bundled {name}/SKILL.md diverged"


def test_every_tool_reference_resolves_to_exported_schema(tmp_path):
    exported = _exported_tool_names()
    for skill_file in _all_skill_files(tmp_path):
        content = skill_file.read_text(encoding="utf-8")
        for ident in _backticked_identifiers(content):
            if ident.endswith("_tool"):
                assert ident in exported, (
                    f"{skill_file} references `{ident}` which is not a registered MCP tool"
                )


def test_no_bare_name_of_any_exported_tool(tmp_path):
    """Broader than the PR's legacy list: derive bare names from the schema."""
    bare_names = {name.removesuffix("_tool") for name in _exported_tool_names()}
    for skill_file in _all_skill_files(tmp_path):
        content = skill_file.read_text(encoding="utf-8")
        for ident in _backticked_identifiers(content):
            assert ident not in bare_names, (
                f"{skill_file} references stale bare tool name `{ident}`"
            )


def test_generate_skills_unicode_and_space_path(tmp_path):
    target = tmp_path / "üñí code (v2)" / "deep" / "nested"
    out = generate_skills(target)
    assert out == target / ".claude" / "skills"
    for name in SKILL_NAMES:
        content = (out / name / "SKILL.md").read_text(encoding="utf-8")
        assert "get_minimal_context_tool" in content
        assert content.startswith("---\n")


def test_generate_skills_overwrites_stale_content(tmp_path):
    out = generate_skills(tmp_path)
    stale = out / "debug-issue" / "SKILL.md"
    stale.write_text("Use `get_flow` and `get_minimal_context`.\n", encoding="utf-8")
    out2 = generate_skills(tmp_path)
    assert out2 == out
    refreshed = stale.read_text(encoding="utf-8")
    assert "`get_flow`" not in refreshed
    assert "get_flow_tool" in refreshed


def test_skills_dict_covers_exactly_four_known_skills():
    assert sorted(f.removesuffix(".md") for f in _SKILLS) == sorted(SKILL_NAMES)
