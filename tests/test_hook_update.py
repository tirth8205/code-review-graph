import io
import json
import os
import subprocess
from pathlib import Path

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
    for payload in ({}, {"cwd": str(tmp_path / "missing")}, {"cwd": "~/repo"}, {"cwd": str(tmp_path)}):
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


def test_status_preserves_child_failure(monkeypatch, tmp_path):
    repo = tmp_path / "repo-b"
    repo.mkdir()
    executable = tmp_path / "code-review-graph"
    executable.write_text("#!/bin/sh\nexit 9\n")
    executable.chmod(0o755)
    monkeypatch.setattr(hook_update, "_hook_repo", lambda: repo)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    with pytest.raises(subprocess.CalledProcessError) as caught:
        hook_update.run_status_hook()
    assert caught.value.returncode == 9


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

    command = generate_codex_hooks_config(tmp_path)["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
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
