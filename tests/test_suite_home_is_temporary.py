"""Every test runs with a temporary home directory (crg-2lp).

The installers write user-level configs under the home directory:
``~/.codex``, ``~/.continue``, ``~/.qwen``, ``~/.config/...`` and more. A test
that ran a real install without patching ``Path.home()`` itself wrote into the
home of whoever ran the suite. ``tests/conftest.py`` now points the home at a
temporary directory for every test, so forgetting the patch is harmless.
"""

from __future__ import annotations

import os
from pathlib import Path

# Read at import, which pytest does while collecting and so before any
# fixture runs: this is the real home of whoever runs the suite.
_REAL_HOME = Path(os.path.expanduser("~")).resolve()


def test_the_home_directory_is_not_the_real_one():
    assert Path.home().resolve() != _REAL_HOME
    assert Path(os.path.expanduser("~")).resolve() != _REAL_HOME


def test_xdg_folders_follow_the_temporary_home():
    from code_review_graph import skills

    home = Path.home()
    assert skills._xdg_config_home() == home / ".config"
    assert skills._xdg_data_home() == home / ".local" / "share"


def test_each_test_gets_its_own_home():
    # Checked first, so this test can never write into the real home.
    assert Path.home().resolve() != _REAL_HOME
    # Written here, it must not be seen by the next test.
    marker = Path.home() / ".codex" / "config.toml"
    assert not marker.exists()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("# written by a test\n", encoding="utf-8")
