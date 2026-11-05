"""Regression checks for user-facing command examples."""

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
README_FILES = (
    "README.md",
    "README.hi-IN.md",
    "README.ja-JP.md",
    "README.ko-KR.md",
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
    "code_review_graph/docs/LLM-OPTIMIZED-REFERENCE.md",
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


def test_github_action_references_use_current_supported_majors():
    """Keep active workflows and copy-paste examples on supported majors."""
    files = [
        ROOT / "action.yml",
        ROOT / "README.md",
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


def test_schema_version_is_current_everywhere_it_is_written_down():
    """The places CI's schema-sync job does not reach.

    schema-sync compares ``migrations.py`` against the VS Code extension only.
    The schema version is also written down in prose that ships — ``docs/`` is
    in the sdist — and in the Action's cache key, and each of those drifted
    behind the migrations at least once.
    """
    from code_review_graph.migrations import LATEST_VERSION

    schema_doc = (ROOT / "docs/schema.md").read_text(encoding="utf-8")
    assert f"The current schema version is {LATEST_VERSION}," in schema_doc, (
        f"docs/schema.md does not state schema version {LATEST_VERSION}"
    )
    assert f"| {LATEST_VERSION} |" in schema_doc, (
        f"docs/schema.md has no migration table row for v{LATEST_VERSION}"
    )

    troubleshooting = (ROOT / "docs/TROUBLESHOOTING.md").read_text(encoding="utf-8")
    assert f"this build understands v{LATEST_VERSION})" in troubleshooting, (
        "the sample newer-database error in docs/TROUBLESHOOTING.md names a "
        f"version other than v{LATEST_VERSION}"
    )

    for name in ("action.yml", "docs/GITHUB_ACTION.md"):
        content = (ROOT / name).read_text(encoding="utf-8")
        segments = set(re.findall(r"schema(\d+)", content))
        assert segments == {str(LATEST_VERSION)}, (
            f"{name} names cache schema segment(s) {sorted(segments)}; "
            f"expected only schema{LATEST_VERSION}"
        )
