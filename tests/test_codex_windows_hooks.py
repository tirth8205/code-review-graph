"""Codex ``commandWindows`` hooks, run the way Codex runs them on Windows.

Codex hands ``commandWindows`` to the user's shell, as
``<powershell> -NoProfile -Command <hook>``, or to ``%COMSPEC% /C "<hook>"``
when it found no shell. Every test here runs the generated hook through each of
those launchers this machine has, with the event JSON on stdin, and checks what
reached the CLI and what the hook returned.

From #877 (issue #620). Windows only: the string contract the launchers rely on
is pinned on every platform in tests/test_skills.py.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from code_review_graph.skills import generate_codex_hooks_config

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="runs the hooks through Windows shells"
)

_EVENTS = {"hook-update": "PostToolUse", "hook-status": "SessionStart"}

_RECORDER = """\
import os
import sys
from pathlib import Path

out = Path(os.environ["CRG_FAKE_RECORD"])
out.with_suffix(".stdin").write_bytes(sys.stdin.buffer.read())
out.with_suffix(".argv").write_text("\\n".join(sys.argv[1:]), encoding="utf-8")
sys.exit(int(os.environ.get("CRG_FAKE_EXIT", "0")))
"""


def _launchers() -> list:
    params = []
    for name in ("powershell", "pwsh"):
        exe = shutil.which(name)
        if exe:
            params.append(pytest.param(("powershell", exe), id=name))
    params.append(pytest.param(("cmd", os.environ.get("COMSPEC", "cmd.exe")), id="cmd"))
    return params


def _hook(subcommand: str) -> str:
    event = _EVENTS[subcommand]
    config = generate_codex_hooks_config(Path.cwd())
    return config["hooks"][event][0]["hooks"][0]["commandWindows"]


def _run(
    launcher: tuple[str, str], hook: str, *, cwd: Path, path: str, payload: bytes
) -> subprocess.CompletedProcess[bytes]:
    kind, exe = launcher
    args: str | list[str]
    if kind == "cmd":
        # Codex passes the hook to cmd.exe raw, inside one more pair of quotes.
        args = f'"{exe}" /C "{hook}"'
    else:
        args = [exe, "-NoProfile", "-Command", hook]
    return subprocess.run(
        args,
        input=payload,
        cwd=cwd,
        env={**os.environ, "PATH": path},
        capture_output=True,
        timeout=180,
    )


def _windows_dirs() -> list[str]:
    root = os.environ.get("SystemRoot", r"C:\Windows")
    return [
        os.path.join(root, "System32"),
        os.path.join(root, "System32", "WindowsPowerShell", "v1.0"),
    ]


def _path_without_the_cli() -> str:
    names = ("code-review-graph.exe", "code-review-graph.cmd", "code-review-graph.bat")
    return os.pathsep.join(
        entry
        for entry in os.environ.get("PATH", "").split(os.pathsep)
        if entry and not any((Path(entry) / name).exists() for name in names)
    )


def _fake_cli(tmp_path: Path, body: str) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "code-review-graph.cmd").write_text(
        f"@{body} %*\r\n@exit /b %ERRORLEVEL%\r\n", encoding="utf-8"
    )
    return bin_dir


def _recording_cli(tmp_path: Path, monkeypatch, exit_code: int = 0) -> tuple[Path, Path]:
    recorder = tmp_path / "recorder.py"
    recorder.write_text(_RECORDER, encoding="utf-8")
    record = tmp_path / "record"
    monkeypatch.setenv("CRG_FAKE_RECORD", str(record))
    monkeypatch.setenv("CRG_FAKE_EXIT", str(exit_code))
    return _fake_cli(tmp_path, f'"{sys.executable}" "{recorder}"'), record


def _real_cli(tmp_path: Path) -> Path:
    """The real CLI, run from this checkout, behind a ``code-review-graph`` name."""
    return _fake_cli(tmp_path, f'"{sys.executable}" -m code_review_graph')


def _git_repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    return path


def _payload(cwd: Path, padding: int = 0) -> bytes:
    event = {
        "hook_event_name": "PostToolUse",
        "cwd": str(cwd),
        "tool_name": "Write",
        "tool_input": {"content": "caf\u00e9 \u2713 " + "x" * padding},
    }
    return json.dumps(event, ensure_ascii=False).encode("utf-8")


@pytest.mark.parametrize("launcher", _launchers())
@pytest.mark.parametrize("subcommand", sorted(_EVENTS))
def test_the_event_json_reaches_the_cli_byte_for_byte(
    launcher, subcommand, tmp_path, monkeypatch
):
    bin_dir, record = _recording_cli(tmp_path, monkeypatch)
    payload = _payload(tmp_path, padding=4 * 1024 * 1024)

    proc = _run(
        launcher,
        _hook(subcommand),
        cwd=tmp_path,
        path=str(bin_dir) + os.pathsep + os.environ["PATH"],
        payload=payload,
    )

    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    assert record.with_suffix(".argv").read_text(encoding="utf-8") == subcommand
    assert record.with_suffix(".stdin").read_bytes() == payload


@pytest.mark.parametrize("launcher", _launchers())
def test_a_failing_cli_fails_the_hook(launcher, tmp_path, monkeypatch):
    """As in the POSIX form, a failed update is reported, not hidden."""
    bin_dir, _ = _recording_cli(tmp_path, monkeypatch, exit_code=3)

    proc = _run(
        launcher,
        _hook("hook-update"),
        cwd=tmp_path,
        path=str(bin_dir) + os.pathsep + os.environ["PATH"],
        payload=_payload(tmp_path),
    )

    assert proc.returncode != 0


@pytest.mark.parametrize("launcher", _launchers())
@pytest.mark.parametrize("subcommand", sorted(_EVENTS))
def test_without_the_cli_a_large_payload_is_drained_and_the_hook_succeeds(
    launcher, subcommand, tmp_path
):
    path = _path_without_the_cli()
    proc = _run(
        launcher,
        _hook(subcommand),
        cwd=tmp_path,
        path=path,
        payload=_payload(tmp_path, padding=8 * 1024 * 1024),
    )

    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    assert b"not recognized" not in proc.stderr


@pytest.mark.parametrize("launcher", _launchers())
def test_update_builds_the_graph_of_the_payload_repo(launcher, tmp_path):
    repo = _git_repo(tmp_path / "repo")
    bin_dir = _real_cli(tmp_path)

    proc = _run(
        launcher,
        _hook("hook-update"),
        cwd=tmp_path,
        path=str(bin_dir) + os.pathsep + os.environ["PATH"],
        payload=_payload(repo),
    )

    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    assert (repo / ".code-review-graph" / "graph.db").exists()


@pytest.mark.parametrize("launcher", _launchers())
@pytest.mark.parametrize("subcommand", sorted(_EVENTS))
def test_outside_a_repo_the_hook_succeeds_and_writes_nothing(
    launcher, subcommand, tmp_path
):
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    bin_dir = _real_cli(tmp_path)

    proc = _run(
        launcher,
        _hook(subcommand),
        cwd=outside,
        path=str(bin_dir) + os.pathsep + os.environ["PATH"],
        payload=_payload(outside),
    )

    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    assert not (outside / ".code-review-graph").exists()


@pytest.mark.parametrize("launcher", _launchers())
@pytest.mark.parametrize("subcommand", sorted(_EVENTS))
def test_without_git_the_hook_succeeds_and_writes_nothing(
    launcher, subcommand, tmp_path
):
    repo = _git_repo(tmp_path / "repo")
    bin_dir = _real_cli(tmp_path)
    path = os.pathsep.join([str(bin_dir), *_windows_dirs()])
    assert shutil.which("git", path=path) is None

    proc = _run(launcher, _hook(subcommand), cwd=repo, path=path, payload=_payload(repo))

    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    assert not (repo / ".code-review-graph").exists()
