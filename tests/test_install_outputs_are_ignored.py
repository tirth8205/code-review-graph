"""What ``install`` writes inside this checkout must not show up in ``git status``.

A contributor who tries ``code-review-graph install`` in a fresh clone of this
repository should not be left with untracked files. Every path an installer
writes inside the repository is either a file the repository tracks
(``AGENTS.md``, ``CLAUDE.md``, ``GEMINI.md``, ``.mcp.json``) or a path the root
``.gitignore`` lists, the way ``.claude/``, ``.qoder/`` and ``QODER.md``
already were.

Found by following CONTRIBUTING.md from a fresh clone: installing for Cursor
left ``.cursor/mcp.json`` and ``.cursorrules`` untracked.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from code_review_graph import cli, skills

REPO = Path(__file__).resolve().parents[1]

# The user-level folders some installers look for or write into. All of them
# live under the throwaway HOME below, never the real one.
_HOME_DIRS = (
    ".codex",
    ".cursor",
    ".codeium/windsurf",
    ".continue",
    ".gemini/antigravity",
    ".qwen",
    ".kiro",
    ".copilot",
    ".config/opencode",
    ".config/mimocode",
    ".config/zed",
    "Library/Application Support/Zed",
    ".dsh",
    ".hermes",
)


def _git(*args: str, cwd: Path = REPO, stdin: str | None = None) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd, input=stdin, capture_output=True, text=True, check=True,
    ).stdout


def _checkout_or_skip() -> None:
    """Skip unless this file runs from a git checkout of the repository."""
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    try:
        top = _git("rev-parse", "--show-toplevel").strip()
    except subprocess.CalledProcessError:
        pytest.skip("not a git checkout (an sdist, for example)")
    if Path(top).resolve() != REPO:
        pytest.skip("this tree is inside some other git repository")


@pytest.fixture
def no_global_excludes(tmp_path) -> list[str]:
    """Git options that leave the user's own global ignore file out of it.

    A contributor whose global excludes already list ``.cursor/`` would
    otherwise pass this test while a fresh clone elsewhere fails it.
    """
    empty = tmp_path / "no-global-excludes"
    empty.write_text("", encoding="utf-8")
    return ["-c", f"core.excludesFile={empty}"]


@pytest.fixture
def tracked() -> set[str]:
    _checkout_or_skip()
    return set(_git("ls-files", "-z").split("\0")) - {""}


@pytest.fixture
def scratch_clone(tmp_path, monkeypatch, tracked) -> Path:
    """A git repo holding this checkout's top-level and .github files.

    Those are the files an installer reads or merges into, so each platform
    behaves as it would in a real clone. HOME points at an empty folder so no
    installer touches the real one.
    """
    home = tmp_path / "home"
    for relative in _HOME_DIRS:
        (home / relative).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setattr(Path, "home", lambda: home)

    clone = tmp_path / "clone"
    clone.mkdir()
    _git("init", "-q", cwd=clone)
    for relative in sorted(tracked):
        if "/" in relative and not relative.startswith(".github/"):
            continue
        target = clone / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / relative, target)
    return clone


class _Args:
    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)


def test_no_tracked_file_is_ignored(tracked, no_global_excludes):
    """A pattern added for an installer must never hide a file we ship."""
    ignored = _git(*no_global_excludes, "ls-files", "-ci", "--exclude-standard", "-z")
    assert [path for path in ignored.split("\0") if path] == []


@pytest.mark.parametrize("platform", sorted(skills.PLATFORMS))
def test_install_leaves_nothing_untracked(platform, scratch_clone, tracked, no_global_excludes):
    before = {p for p in scratch_clone.rglob("*") if p.is_file()}

    cli._handle_init(_Args(
        repo=str(scratch_clone), platform=platform, dry_run=False, yes=True,
        no_instructions=False, no_skills=False, no_hooks=False,
    ))

    written = sorted(
        path.relative_to(scratch_clone).as_posix()
        for path in scratch_clone.rglob("*")
        if path.is_file() and path not in before
        and ".git" not in path.relative_to(scratch_clone).parts
    )
    if platform == "cursor":
        # Proof the check sees what was written, not an empty list.
        assert ".cursor/mcp.json" in written, written
    new = [path for path in written if path not in tracked]
    # --no-index: ask what the root .gitignore says about each path, whether
    # or not it exists in this checkout.
    ignored = subprocess.run(
        ["git", *no_global_excludes, "check-ignore", "--no-index", "--stdin", "-z"],
        cwd=REPO, input="\0".join(new), capture_output=True, text=True,
    ).stdout
    missing = sorted(set(new) - set(ignored.split("\0")))
    assert missing == [], (
        f"install --platform {platform} writes {missing} inside the repository, "
        "and the root .gitignore does not list them. Add them there."
    )
