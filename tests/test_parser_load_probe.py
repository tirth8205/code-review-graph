"""Failure-mode tests for bounded tree-sitter parser loading."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from code_review_graph import parser as parser_module
from code_review_graph.parser import CodeParser


@pytest.fixture(autouse=True)
def _clear_probe_cache():
    parser_module._clear_parser_probe_cache()
    yield
    parser_module._clear_parser_probe_cache()


class _FakeLanguagePack:
    def __init__(self, failures: dict[str, Exception] | None = None) -> None:
        self.failures = failures or {}
        self.calls: list[str] = []

    def get_parser(self, grammar: str):
        self.calls.append(grammar)
        failure = self.failures.get(grammar)
        if failure is not None:
            raise failure
        return object()


def _completed(returncode: int = 0) -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode)


def test_successful_probe_runs_once_across_parser_instances(monkeypatch):
    probe_calls: list[str] = []
    language_pack = _FakeLanguagePack()

    def fake_run(command, **_kwargs):
        probe_calls.append(command[-1])
        return _completed()

    monkeypatch.setattr(parser_module.subprocess, "run", fake_run)
    monkeypatch.setattr(
        parser_module.importlib,
        "import_module",
        lambda _name: language_pack,
    )

    assert all(CodeParser()._get_parser("python") is not None for _ in range(4))
    assert probe_calls == ["python"]
    assert language_pack.calls == ["python"] * 4


def test_probe_timeout_skips_only_the_failing_grammar(monkeypatch):
    probe_calls: list[str] = []
    language_pack = _FakeLanguagePack()

    def fake_run(command, **kwargs):
        grammar = command[-1]
        probe_calls.append(grammar)
        if grammar == "tsx":
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return _completed()

    monkeypatch.setattr(parser_module.subprocess, "run", fake_run)
    monkeypatch.setattr(
        parser_module.importlib,
        "import_module",
        lambda _name: language_pack,
    )

    parser = CodeParser()
    assert parser._get_parser("tsx") is None
    assert parser._get_parser("python") is not None
    assert CodeParser()._get_parser("tsx") is None
    assert probe_calls == ["tsx", "python"]
    assert language_pack.calls == ["python"]


def test_nonzero_probe_skips_only_the_failing_grammar(monkeypatch):
    probe_calls: list[str] = []
    language_pack = _FakeLanguagePack()

    def fake_run(command, **_kwargs):
        grammar = command[-1]
        probe_calls.append(grammar)
        return _completed(1 if grammar == "verilog" else 0)

    monkeypatch.setattr(parser_module.subprocess, "run", fake_run)
    monkeypatch.setattr(
        parser_module.importlib,
        "import_module",
        lambda _name: language_pack,
    )

    parser = CodeParser()
    assert parser._get_parser("verilog") is None
    assert parser._get_parser("rust") is not None
    assert probe_calls == ["verilog", "rust"]
    assert language_pack.calls == ["rust"]


def test_nonzero_probe_logs_the_subprocess_failure_reason(monkeypatch, caplog):
    def fake_run(_command, **_kwargs):
        return SimpleNamespace(
            returncode=1,
            stderr=(
                b"Traceback (most recent call last):\n"
                b"ModuleNotFoundError: No module named "
                b"'tree_sitter_language_pack'\n"
            ),
        )

    monkeypatch.setattr(parser_module.subprocess, "run", fake_run)

    with caplog.at_level("WARNING"):
        assert not parser_module._parser_load_probe_succeeds("java")

    assert (
        "Skipping unavailable tree-sitter parser for java: "
        "ModuleNotFoundError: No module named 'tree_sitter_language_pack'"
        in caplog.text
    )


def test_probe_can_load_language_pack_from_user_site(tmp_path, monkeypatch):
    """Regression for --user installs hidden by Python's isolated mode."""
    base_executable = getattr(sys, "_base_executable", sys.executable)
    env = os.environ.copy()
    env["PYTHONUSERBASE"] = str(tmp_path / "user-base")
    user_site_result = subprocess.run(
        [
            base_executable,
            "-c",
            "import site; print(site.ENABLE_USER_SITE); "
            "print(site.getusersitepackages())",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    enabled, user_site = user_site_result.stdout.splitlines()
    if enabled != "True":
        pytest.skip("base interpreter has user-site packages disabled")

    user_site_path = Path(user_site)
    package_dir = user_site_path / "tree_sitter_language_pack"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text(
        "def get_parser(grammar):\n"
        "    assert grammar == 'user-site-only'\n"
        "    return object()\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("PYTHONUSERBASE", env["PYTHONUSERBASE"])
    monkeypatch.setattr(parser_module.sys, "executable", base_executable)

    assert parser_module._run_parser_load_probe("user-site-only", 5.0)


def test_expected_parent_load_failure_is_cached(monkeypatch):
    probe_calls: list[str] = []
    language_pack = _FakeLanguagePack({"zig": LookupError("missing grammar")})

    def fake_run(command, **_kwargs):
        probe_calls.append(command[-1])
        return _completed()

    monkeypatch.setattr(parser_module.subprocess, "run", fake_run)
    monkeypatch.setattr(
        parser_module.importlib,
        "import_module",
        lambda _name: language_pack,
    )

    assert CodeParser()._get_parser("zig") is None
    assert CodeParser()._get_parser("zig") is None
    assert probe_calls == ["zig"]
    assert language_pack.calls == ["zig"]


def test_unexpected_parent_load_failure_still_surfaces(monkeypatch):
    language_pack = _FakeLanguagePack({"tsx": RuntimeError("native loader bug")})
    monkeypatch.setattr(
        parser_module.subprocess,
        "run",
        lambda *_args, **_kwargs: _completed(),
    )
    monkeypatch.setattr(
        parser_module.importlib,
        "import_module",
        lambda _name: language_pack,
    )

    with pytest.raises(RuntimeError, match="native loader bug"):
        CodeParser()._get_parser("tsx")
