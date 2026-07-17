"""Regression checks for user-facing command examples."""

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
README_FILES = (
    "README.md",
    "README.hi-IN.md",
    "README.ja-JP.md",
    "README.ko-KR.md",
    "README.ur-PK.md",
    "README.zh-CN.md",
)
OPTIONAL_GROUPS = (
    "embeddings",
    "google-embeddings",
    "communities",
    "enrichment",
    "eval",
    "wiki",
    "all",
)
USER_DOC_FILES = README_FILES + (
    "docs/COMMANDS.md",
    "docs/FAQ.md",
    "docs/LLM-OPTIMIZED-REFERENCE.md",
    "docs/TROUBLESHOOTING.md",
)


def test_pip_extra_examples_use_cross_shell_double_quotes():
    """Extras must survive zsh globbing without breaking Windows cmd.exe."""
    for readme_name in README_FILES:
        content = (ROOT / readme_name).read_text(encoding="utf-8")
        for group in OPTIONAL_GROUPS:
            command = f'pip install "code-review-graph[{group}]"'
            assert command in content, f"{readme_name} is missing {command}"


def test_current_user_docs_have_no_unquoted_pip_extras():
    pattern = re.compile(r"pip install code-review-graph\[[A-Za-z0-9-]+\]")
    for doc_name in USER_DOC_FILES:
        content = (ROOT / doc_name).read_text(encoding="utf-8")
        assert pattern.search(content) is None, f"unquoted pip extras in {doc_name}"


def test_readme_language_navigation_is_reciprocal():
    """Every translated README must link to every available translation."""
    for readme_name in README_FILES:
        content = (ROOT / readme_name).read_text(encoding="utf-8")
        for target_name in README_FILES:
            assert f'href="{target_name}"' in content, (
                f"{readme_name} is missing the language link to {target_name}"
            )


def test_current_urdu_readme_preserves_canonical_code_blocks():
    """Commands and configuration examples must stay verbatim across translation."""
    pattern = re.compile(r"(?ms)^( {0,3})```[^\n]*\n(.*?)^\1```[ \t]*$")
    english = (ROOT / "README.md").read_text(encoding="utf-8")
    urdu = (ROOT / "README.ur-PK.md").read_text(encoding="utf-8")

    english_blocks = [body for _indent, body in pattern.findall(english)]
    urdu_blocks = [body for _indent, body in pattern.findall(urdu)]
    assert urdu_blocks == english_blocks


def test_github_action_references_use_current_supported_majors():
    """Keep active workflows and copy-paste examples on supported majors."""
    files = [
        ROOT / "action.yml",
        ROOT / "README.md",
        ROOT / "README.ur-PK.md",
        ROOT / "docs/GITHUB_ACTION.md",
        *(ROOT / ".github/workflows").glob("*.yml"),
    ]
    expected_majors = {"checkout": "7", "cache": "6"}
    for path in files:
        content = path.read_text(encoding="utf-8")
        for action, expected in expected_majors.items():
            for actual in re.findall(rf"actions/{action}@v(\d+)", content):
                assert actual == expected, (
                    f"{path.relative_to(ROOT)} uses actions/{action}@v{actual}; "
                    f"expected v{expected}"
                )


def test_codebuddy_install_docs_cover_project_artifacts():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    usage = (ROOT / "docs/USAGE.md").read_text(encoding="utf-8")

    assert "install --platform codebuddy" in readme
    for artifact in (
        ".mcp.json",
        "CODEBUDDY.md",
        ".codebuddy/settings.json",
        ".codebuddy/skills/<name>/SKILL.md",
    ):
        assert artifact in usage
