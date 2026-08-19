"""Focused regressions for the August PR-sweep follow-ups (#866)."""

from pathlib import Path
from unittest.mock import MagicMock

from code_review_graph.daemon_cli import _configure_utf8_stdio, _handle_stop
from code_review_graph.parser import CodeParser
from code_review_graph.tools.review import get_affected_flows_func


def test_affected_flows_empty_result_includes_truncated(monkeypatch, tmp_path):
    store = MagicMock()
    monkeypatch.setattr(
        "code_review_graph.tools.review._get_store",
        lambda _root: (store, tmp_path),
    )

    result = get_affected_flows_func(changed_files=[], repo_root=str(tmp_path))

    assert result["truncated"] is False


def test_js_specifier_resolves_jsx_source(tmp_path: Path) -> None:
    caller = tmp_path / "app.mts"
    caller.write_text('import "./foo.js"\n', encoding="utf-8")
    (tmp_path / "foo.jsx").write_text("export {}\n", encoding="utf-8")

    resolved = CodeParser()._resolve_module_to_file(
        "./foo.js", str(caller), "typescript"
    )

    assert resolved == (tmp_path / "foo.jsx").as_posix()


def test_mjs_specifier_resolves_mts_source(tmp_path: Path) -> None:
    caller = tmp_path / "app.mts"
    caller.write_text('import "./foo.mjs"\n', encoding="utf-8")
    (tmp_path / "foo.mts").write_text("export {}\n", encoding="utf-8")

    resolved = CodeParser()._resolve_module_to_file(
        "./foo.mjs", str(caller), "typescript"
    )

    assert resolved == (tmp_path / "foo.mts").as_posix()


def test_cjs_specifier_resolves_cts_source(tmp_path: Path) -> None:
    caller = tmp_path / "app.cts"
    caller.write_text('import "./foo.cjs"\n', encoding="utf-8")
    (tmp_path / "foo.cts").write_text("export {}\n", encoding="utf-8")

    resolved = CodeParser()._resolve_module_to_file(
        "./foo.cjs", str(caller), "typescript"
    )

    assert resolved == (tmp_path / "foo.cts").as_posix()


def test_daemon_stop_treats_windows_race_oserror(monkeypatch, capsys):
    monkeypatch.setattr("code_review_graph.daemon.is_daemon_running", lambda: True)
    monkeypatch.setattr("code_review_graph.daemon.read_pid", lambda: 123)
    monkeypatch.setattr(
        "code_review_graph.daemon_cli.os.kill",
        lambda *_args: (_ for _ in ()).throw(OSError(87, "The parameter is incorrect.")),
    )
    cleared = []
    monkeypatch.setattr("code_review_graph.daemon.clear_pid", lambda: cleared.append(True))

    _handle_stop(MagicMock())

    assert cleared == [True]
    assert "already gone" in capsys.readouterr().out


def test_daemon_utf8_stdio_reconfigures_legacy_streams(monkeypatch):
    calls = []

    class Stream:
        encoding = "cp1252"

        def reconfigure(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(
        "sys.stdout", Stream(), raising=False
    )
    monkeypatch.setattr(
        "sys.stderr", Stream(), raising=False
    )

    _configure_utf8_stdio()

    assert calls == [{"encoding": "utf-8"}, {"encoding": "utf-8"}]
