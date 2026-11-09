import io
import json
import os
import subprocess
import sys

import pytest

from code_review_graph import hook_update


def test_payload_cwd_selects_repo_root(monkeypatch, tmp_path):
    repo = tmp_path / "repo-b"
    nested = repo / "packages" / "app"
    nested.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    monkeypatch.setattr(hook_update.sys, "stdin", io.StringIO(json.dumps({"cwd": str(nested)})))
    assert hook_update._hook_repo() == repo


def test_missing_or_invalid_payload_cwd_fails_closed(monkeypatch, tmp_path):
    repo = tmp_path / "repo-b"
    repo.mkdir()
    for payload in (
        {},
        {"cwd": str(tmp_path / "missing")},
        {"cwd": "~/repo"},
        {"cwd": str(tmp_path)},
    ):
        monkeypatch.setattr(hook_update.sys, "stdin", io.StringIO(json.dumps(payload)))
        assert hook_update._hook_repo() is None


def test_update_uses_payload_repo_and_ignores_invalid_payload(monkeypatch, tmp_path):
    repo_a = tmp_path / "repo-a"
    repo = tmp_path / "repo-b"
    nested = repo / "nested"
    repo_a.mkdir()
    nested.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo_a)], check=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    calls = []
    monkeypatch.setattr(
        hook_update,
        "_update_repo",
        lambda root: calls.append(root),
    )
    monkeypatch.chdir(repo_a)
    monkeypatch.setattr(hook_update.sys, "stdin", io.StringIO(json.dumps({"cwd": str(nested)})))
    hook_update.run_update_hook()
    assert calls == [repo]

    calls.clear()
    monkeypatch.setattr(hook_update.sys, "stdin", io.StringIO(json.dumps({})))
    hook_update.run_update_hook()
    assert calls == []


def test_status_failure_keeps_output_but_not_a_failed_hook(monkeypatch, tmp_path, capfd):
    """No graph is normal: the old hook exited 0 here, with no traceback."""
    repo = tmp_path / "repo-b"
    repo.mkdir()
    executable = tmp_path / "code-review-graph"
    executable.write_text("#!/bin/sh\necho 'No graph found' >&2\nexit 1\n")
    executable.chmod(0o755)
    monkeypatch.setattr(hook_update, "_hook_repo", lambda: repo)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")

    hook_update.run_status_hook()

    assert "No graph found" in capfd.readouterr().err


def test_status_with_no_executable_does_not_raise(monkeypatch, tmp_path, capsys):
    repo = tmp_path / "repo-b"
    repo.mkdir()
    monkeypatch.setattr(hook_update, "_hook_repo", lambda: repo)
    monkeypatch.setenv("PATH", str(tmp_path))

    hook_update.run_status_hook()

    assert capsys.readouterr().err.startswith("Error: ")


def test_status_hook_in_a_repo_with_no_graph_exits_clean(tmp_path):
    """The real command, end to end, in a repository nobody has built."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "code-review-graph"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" -m code_review_graph "$@"\n')
    shim.chmod(0o755)
    proc = subprocess.run(
        [sys.executable, "-m", "code_review_graph", "hook-status"],
        input=json.dumps({"cwd": str(repo)}),
        text=True,
        capture_output=True,
        env={**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
    )
    assert proc.returncode == 0, proc.stderr
    assert "Traceback" not in proc.stderr


@pytest.mark.parametrize(
    "outcome",
    [
        pytest.param(lambda **kw: {"status": "error", "summary": "boom"}, id="error-result"),
        pytest.param(RuntimeError("boom"), id="runtime-error"),
    ],
)
def test_update_reports_a_failed_update(monkeypatch, tmp_path, capsys, outcome):
    from code_review_graph.tools import build

    def fake(**kwargs):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome(**kwargs)

    monkeypatch.setattr(build, "build_or_update_graph", fake)
    with pytest.raises(SystemExit) as caught:
        hook_update._update_repo(tmp_path)
    assert caught.value.code == 1
    assert "boom" in capsys.readouterr().err


def test_status_runs_executable_shim_and_emits_child_output(monkeypatch, tmp_path, capfd):
    repo = tmp_path / "repo-b"
    repo.mkdir()
    executable = tmp_path / "code-review-graph"
    executable.write_text("#!/bin/sh\nprintf 'Nodes: 7\\n'\n")
    executable.chmod(0o755)
    monkeypatch.setattr(hook_update, "_hook_repo", lambda: repo)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")

    hook_update.run_status_hook()

    assert "Nodes: 7" in capfd.readouterr().out


def test_update_calls_real_builder_with_payload_repo(monkeypatch, tmp_path):
    pytest.importorskip("tree_sitter_language_pack")
    repo = tmp_path / "repo-b"
    nested = repo / "nested"
    nested.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    from code_review_graph.tools import build

    calls = []
    original = build.build_or_update_graph
    monkeypatch.setattr(
        build,
        "build_or_update_graph",
        lambda **kwargs: calls.append(kwargs) or {"files_updated": 0},
    )
    monkeypatch.setattr(hook_update.sys, "stdin", io.StringIO(json.dumps({"cwd": str(nested)})))
    hook_update.run_update_hook()
    assert calls == [{"full_rebuild": False, "repo_root": str(repo), "postprocess": "minimal"}]
    assert build.build_or_update_graph is not original


def test_generated_command_preserves_executable_failure(monkeypatch, tmp_path):
    from code_review_graph.skills import generate_codex_hooks_config

    config = generate_codex_hooks_config(tmp_path)
    command = config["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
    executable = tmp_path / "code-review-graph"
    executable.write_text("#!/bin/sh\nexit 7\n")
    executable.chmod(0o755)
    result = subprocess.run(
        ["/bin/bash", "-c", command],
        cwd=tmp_path,
        env={**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"]},
        input=json.dumps({"cwd": str(tmp_path)}),
        text=True,
    )
    assert result.returncode == 7


def test_update_hook_builds_the_payload_repo_not_the_cwd(tmp_path):
    """The real command, end to end, launched from a different repository."""
    launcher = tmp_path / "launcher"
    target = tmp_path / "target"
    for repo in (launcher, target):
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    nested = target / "pkg"
    nested.mkdir()
    proc = subprocess.run(
        [sys.executable, "-m", "code_review_graph", "hook-update"],
        cwd=launcher,
        input=json.dumps({"cwd": str(nested)}),
        text=True,
        capture_output=True,
        env={**os.environ, "HOME": str(tmp_path)},
    )
    assert proc.returncode == 0, proc.stderr
    assert (target / ".code-review-graph" / "graph.db").exists()
    assert not (launcher / ".code-review-graph").exists()
