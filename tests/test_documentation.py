"""Regression checks for user-facing command examples."""

import re
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
    import tomli as tomllib

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
    "docs/LLM-OPTIMIZED-REFERENCE.md",
    "docs/TROUBLESHOOTING.md",
)
RELEASE_VERSION_DOC_FILES = (
    "README.md",
    "docs/FEATURES.md",
    "docs/GITHUB_ACTION.md",
    "docs/LLM-OPTIMIZED-REFERENCE.md",
    "docs/ROADMAP.md",
    "docs/USAGE.md",
)


def current_project_version() -> str:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return pyproject["project"]["version"]


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


def test_release_facing_docs_match_project_version():
    version = current_project_version()
    previous_minor = re.escape("2.3.6")

    for doc_name in RELEASE_VERSION_DOC_FILES:
        content = (ROOT / doc_name).read_text(encoding="utf-8")
        assert version in content, f"{doc_name} does not mention current version {version}"
        assert re.search(rf"\bv{previous_minor}\b|\b{previous_minor}\b", content) is None, (
            f"{doc_name} still references previous release version 2.3.6"
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
