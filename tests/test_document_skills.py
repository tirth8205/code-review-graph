"""Tests for the documentation-driven skill generator."""

from pathlib import Path

from code_review_graph.skills import (
    DOCUMENTARY_SKILLS,
    generate_documentary_skills,
    render_documentary_skill_markdown,
)


def test_render_documentary_skill_markdown_contains_expected_sections():
    for slug, spec in DOCUMENTARY_SKILLS.items():
        content = render_documentary_skill_markdown(slug)

        assert content.startswith("---\n")
        assert f"name: {slug}\n" in content
        assert f"description: {spec['description']}\n" in content
        if "argument_hint" in spec:
            assert f'argument-hint: "{spec["argument_hint"]}"\n' in content
        assert f"# {spec['title']}\n" in content
        for tool_name in (
            "build_or_update_graph_tool",
            "get_docs_section_tool",
            "get_review_context_tool",
            "get_affected_flows_tool",
        ):
            if tool_name in spec["body"]:
                assert tool_name in content


def test_generate_documentary_skills_writes_three_skill_dirs(tmp_path: Path):
    skills_dir = generate_documentary_skills(tmp_path)

    assert skills_dir == tmp_path / "skills"
    assert {path.name for path in skills_dir.iterdir()} == {
        "build-graph",
        "review-delta",
        "review-pr",
    }
    for slug in DOCUMENTARY_SKILLS:
        skill_path = skills_dir / slug / "SKILL.md"
        assert skill_path.is_file()
        assert skill_path.read_text(encoding="utf-8") == render_documentary_skill_markdown(slug)


def test_checked_in_documentary_skills_match_generator(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[1]
    generated_dir = generate_documentary_skills(tmp_path)

    for slug in DOCUMENTARY_SKILLS:
        generated = (generated_dir / slug / "SKILL.md").read_text(encoding="utf-8")
        checked_in = (repo_root / "skills" / slug / "SKILL.md").read_text(encoding="utf-8")
        assert generated == checked_in
