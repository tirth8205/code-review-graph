"""Qoder installs the shipped workflows independently of the user's repository."""

from pathlib import Path

from code_review_graph.skills import install_qoder_skills

SHIPPED_SKILLS = {
    "build-graph",
    "debug-issue",
    "explore-codebase",
    "refactor-safely",
    "review-changes",
    "review-delta",
    "review-pr",
}


def test_qoder_install_without_repository_skills(tmp_path):
    root = tmp_path / "application"
    root.mkdir()
    destination = install_qoder_skills(root)
    assert destination == root / ".qoder" / "skills"
    assert {p.parent.name for p in destination.glob("*/SKILL.md")} == SHIPPED_SKILLS
    bundled = Path(__file__).resolve().parents[1] / "skills"
    for name in SHIPPED_SKILLS:
        assert (destination / name / "SKILL.md").read_bytes() == (
            bundled / name / "SKILL.md"
        ).read_bytes()


def test_qoder_installs_shipped_workflows_instead_of_unrelated_project_skills(tmp_path):
    unrelated = tmp_path / "skills" / "business-workflow" / "SKILL.md"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text("Private project workflow; not a code-review-graph skill.\n")
    destination = install_qoder_skills(tmp_path)
    assert destination == tmp_path / ".qoder" / "skills"
    assert {p.parent.name for p in destination.glob("*/SKILL.md")} == SHIPPED_SKILLS
    assert unrelated.read_text().startswith("Private project workflow;")
