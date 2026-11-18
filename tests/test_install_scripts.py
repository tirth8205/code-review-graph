"""Syntax and content lint for the one-line installer scripts.

These tests guard the frictionless-install entry point: ``install.sh``
(POSIX sh) and ``install.ps1`` (PowerShell). They run on every platform.
The sh syntax check uses ``sh -n``; the PowerShell check is skipped when no
``pwsh`` / ``powershell`` binary is available.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO_ROOT / "install.sh"
INSTALL_PS1 = REPO_ROOT / "install.ps1"

# Official Astral installer URLs the scripts must pin to.
ASTRAL_SH_URL = "https://astral.sh/uv/install.sh"
ASTRAL_PS1_URL = "https://astral.sh/uv/install.ps1"


def test_install_scripts_exist():
    assert INSTALL_SH.is_file(), "install.sh missing at repo root"
    assert INSTALL_PS1.is_file(), "install.ps1 missing at repo root"


def test_install_sh_is_posix_syntax_valid():
    """`sh -n install.sh` must parse without errors (no bashisms allowed)."""
    sh = shutil.which("sh")
    if not sh:
        pytest.skip("no POSIX sh available")
    result = subprocess.run(
        [sh, "-n", str(INSTALL_SH)],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=30,
    )
    assert result.returncode == 0, f"sh -n failed: {result.stderr}"


def test_install_sh_has_no_obvious_bashisms():
    """Lightweight guard against the most common bash-only constructs."""
    text = INSTALL_SH.read_text()
    assert text.startswith("#!/bin/sh"), "install.sh must use the /bin/sh shebang"
    # `[[ ... ]]`, arrays, and `function name {` are bash-only.
    assert "[[" not in text, "double-bracket test is a bashism"
    assert "function " not in text, "the `function` keyword is a bashism"


def test_install_sh_pins_official_uv_installer():
    text = INSTALL_SH.read_text()
    assert ASTRAL_SH_URL in text, "install.sh must pin the official Astral uv installer"
    # The CLI install path and fallbacks must be present.
    assert "uv tool install code-review-graph" in text
    assert "pipx install code-review-graph" in text
    assert "pip" in text and "--user" in text


def test_install_ps1_pins_official_uv_installer():
    text = INSTALL_PS1.read_text()
    assert ASTRAL_PS1_URL in text, "install.ps1 must pin the official Astral uv installer"
    assert "uv tool install code-review-graph" in text
    assert "pipx install code-review-graph" in text
    assert "--user code-review-graph" in text


def test_install_ps1_syntax_valid_if_powershell_available():
    """Parse install.ps1 with PowerShell when present; skip otherwise."""
    pwsh = shutil.which("pwsh") or shutil.which("powershell")
    if not pwsh:
        pytest.skip("no PowerShell (pwsh/powershell) available")
    # Tokenize the script; a parse error returns a non-zero exit code.
    cmd = (
        "$ErrorActionPreference='Stop';"
        "$errors=$null;"
        "[void][System.Management.Automation.PSParser]::Tokenize("
        f"(Get-Content -Raw '{INSTALL_PS1}'), [ref]$errors);"
        "if ($errors.Count -gt 0) { $errors | ForEach-Object { $_.Message }; exit 1 }"
    )
    result = subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-Command", cmd],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=60,
    )
    assert result.returncode == 0, f"PowerShell parse failed: {result.stdout}{result.stderr}"
