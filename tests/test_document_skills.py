"""Tests for the documentation-driven skill generator."""

import importlib.util
from pathlib import Path

import pytest

from code_review_graph import skills
from code_review_graph.skills import (
    DOCUMENTARY_SKILLS,
    generate_documentary_skills,
    render_documentary_skill_markdown,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "generate_documentary_skills.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("generate_documentary_skills", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_render_documentary_skill_markdown_contains_expected_sections():
    for slug, spec in DOCUMENTARY_SKILLS.items():
        content = render_documentary_skill_markdown(slug)
        frontmatter, _, rest = content[len("---\n"):].partition("\n---\n")

        assert content.startswith("---\n")
        assert frontmatter.splitlines()[:2] == [
            f"name: {slug}",
            f"description: {spec['description']}",
        ]
        if "argument_hint" in spec:
            assert frontmatter.splitlines()[2] == f'argument-hint: "{spec["argument_hint"]}"'
        assert rest.startswith(f"\n# {spec['title']}\n\n")
        assert content.endswith(spec["body"] + "\n")


def test_render_omits_argument_hint_when_the_skill_has_none(monkeypatch):
    spec = {"description": "Plain skill.", "title": "Plain", "body": "Body text."}
    monkeypatch.setitem(DOCUMENTARY_SKILLS, "plain", spec)

    assert render_documentary_skill_markdown("plain") == (
        "---\nname: plain\ndescription: Plain skill.\n---\n\n# Plain\n\nBody text.\n"
    )


def test_render_unknown_slug_names_the_known_skills():
    with pytest.raises(KeyError, match="unknown documentary skill 'nope'.*build-graph"):
        render_documentary_skill_markdown("nope")


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


def test_generated_files_use_lf_on_every_platform(tmp_path: Path):
    skills_dir = generate_documentary_skills(tmp_path)

    for slug in DOCUMENTARY_SKILLS:
        assert b"\r\n" not in (skills_dir / slug / "SKILL.md").read_bytes()


def test_checked_in_documentary_skills_match_generator(tmp_path: Path):
    generated_dir = generate_documentary_skills(tmp_path)

    for slug in DOCUMENTARY_SKILLS:
        generated = (generated_dir / slug / "SKILL.md").read_bytes()
        checked_in = (REPO_ROOT / "skills" / slug / "SKILL.md").read_bytes()
        assert generated == checked_in, (
            f"skills/{slug}/SKILL.md differs from DOCUMENTARY_SKILLS in skills.py; "
            "edit the dict and run scripts/generate_documentary_skills.py"
        )


def test_script_check_passes_on_the_checkout(capsys):
    script = _load_script()

    assert script.main(["--check"]) == 0
    assert capsys.readouterr().out == ""


def test_script_check_reports_a_hand_edit_and_writes_nothing(tmp_path: Path, capsys):
    script = _load_script()
    generate_documentary_skills(tmp_path)
    edited = tmp_path / "skills" / "review-pr" / "SKILL.md"
    edited.write_text("hand edit\n", encoding="utf-8")

    assert script.main(["--check", "--skills-dir", str(tmp_path / "skills")]) == 1
    assert "review-pr" in capsys.readouterr().out
    assert edited.read_text(encoding="utf-8") == "hand edit\n"


def test_script_rewrites_a_drifted_file(tmp_path: Path):
    script = _load_script()
    skills_dir = tmp_path / "skills"
    generate_documentary_skills(tmp_path)
    (skills_dir / "build-graph" / "SKILL.md").write_text("stale\n", encoding="utf-8")

    assert script.main(["--skills-dir", str(skills_dir)]) == 0
    assert script.main(["--check", "--skills-dir", str(skills_dir)]) == 0
    assert (skills_dir / "build-graph" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == skills.render_documentary_skill_markdown("build-graph")
