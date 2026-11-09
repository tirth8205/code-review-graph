"""The opt-in context history log and the ``context-history`` command.

Issue #813. The maintainer set the first version in the issue thread: off by
default, a local JSONL file under the graph's data directory, one append at
the ``attach_context_savings`` site, and a small command that prints a
per-day or per-tool trend. Nothing leaves the machine. These tests pin that,
plus the two promises a log like this has to keep: it never holds file names
or source text, and it cannot grow without bound.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from code_review_graph import cli
from code_review_graph import context_history as ch
from code_review_graph.context_history import CallInfo
from code_review_graph.context_savings import attach_context_savings

LOG_NAME = "context-history.jsonl"
AGREED_KEYS = {"v", "ts", "tool", "baseline_tokens", "returned_tokens", "saved_tokens"}
OPTIONAL_KEYS = {"detail_level", "files", "depth"}


def _log_lines(data_dir: Path) -> list[dict]:
    path = data_dir / LOG_NAME
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _enable(monkeypatch) -> None:
    monkeypatch.setenv("CRG_CONTEXT_HISTORY", "1")


def _attach(data_dir: Path, **call) -> dict:
    return attach_context_savings(
        {"summary": "x" * 40},
        original_tokens=100,
        call=CallInfo(data_dir=data_dir, tool=call.pop("tool", "detect_changes_tool"), **call),
    )


# ---------------------------------------------------------------------------
# Off unless asked for
# ---------------------------------------------------------------------------


class TestOffByDefault:
    def test_nothing_is_written_when_the_variable_is_unset(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CRG_CONTEXT_HISTORY", raising=False)
        result = _attach(tmp_path)

        assert "context_savings" in result
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "nonsense"])
    def test_values_that_do_not_turn_it_on(self, value, tmp_path, monkeypatch):
        monkeypatch.setenv("CRG_CONTEXT_HISTORY", value)
        _attach(tmp_path)

        assert list(tmp_path.iterdir()) == []

    @pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
    def test_values_that_turn_it_on(self, value, tmp_path, monkeypatch):
        monkeypatch.setenv("CRG_CONTEXT_HISTORY", value)
        _attach(tmp_path)

        assert len(_log_lines(tmp_path)) == 1

    def test_read_only_mode_writes_nothing(self, tmp_path, monkeypatch):
        _enable(monkeypatch)
        monkeypatch.setenv("CRG_READ_ONLY", "1")
        _attach(tmp_path)

        assert list(tmp_path.iterdir()) == []

    def test_a_call_without_call_info_is_never_logged(self, tmp_path, monkeypatch):
        _enable(monkeypatch)
        monkeypatch.chdir(tmp_path)
        attach_context_savings({"summary": "x"}, original_tokens=100)

        assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# What one line holds
# ---------------------------------------------------------------------------


class TestOneLine:
    def test_one_line_per_call_with_the_agreed_fields(self, tmp_path, monkeypatch):
        _enable(monkeypatch)
        result = _attach(
            tmp_path, tool="get_impact_radius_tool",
            detail_level="minimal", files=3, depth=2,
        )

        [entry] = _log_lines(tmp_path)
        assert set(entry) == AGREED_KEYS | OPTIONAL_KEYS
        assert entry["v"] == 1
        assert entry["tool"] == "get_impact_radius_tool"
        assert entry["baseline_tokens"] == 100
        assert entry["saved_tokens"] == result["context_savings"]["saved_tokens"]
        assert entry["returned_tokens"] == 100 - entry["saved_tokens"]
        assert (entry["detail_level"], entry["files"], entry["depth"]) == ("minimal", 3, 2)
        stamp = datetime.strptime(entry["ts"], "%Y-%m-%dT%H:%M:%SZ")
        assert abs(stamp - datetime.now(timezone.utc).replace(tzinfo=None)) < timedelta(
            minutes=5
        )

    def test_args_the_call_did_not_have_are_left_out(self, tmp_path, monkeypatch):
        _enable(monkeypatch)
        _attach(tmp_path, tool="get_architecture_overview_tool")

        [entry] = _log_lines(tmp_path)
        assert set(entry) == AGREED_KEYS

    def test_free_text_detail_level_is_never_copied(self, tmp_path, monkeypatch):
        """``detail_level`` comes from the client as any string. Only the two
        values the tools act on are written, so nothing a client types lands
        in the file."""
        _enable(monkeypatch)
        _attach(tmp_path, detail_level="sk-live-not-a-real-key")

        text = (tmp_path / LOG_NAME).read_text(encoding="utf-8")
        assert "sk-live" not in text
        assert _log_lines(tmp_path)[0]["detail_level"] == "standard"

    def test_counts_that_are_not_whole_numbers_are_left_out(self, tmp_path, monkeypatch):
        _enable(monkeypatch)
        _attach(tmp_path, files="three", depth=True)

        assert set(_log_lines(tmp_path)[0]) == AGREED_KEYS

    def test_no_estimate_means_no_line(self, tmp_path, monkeypatch):
        """The log mirrors the response: no ``context_savings``, no line."""
        _enable(monkeypatch)
        result = attach_context_savings(
            {"summary": "x"}, original_tokens=0,
            call=CallInfo(data_dir=tmp_path, tool="detect_changes_tool"),
        )

        assert "context_savings" not in result
        assert list(tmp_path.iterdir()) == []

    def test_a_missing_data_dir_is_not_created(self, tmp_path, monkeypatch):
        _enable(monkeypatch)
        missing = tmp_path / "gone" / ".code-review-graph"
        result = _attach(missing)

        assert "context_savings" in result
        assert not missing.exists()

    def test_a_write_failure_does_not_break_the_tool(self, tmp_path, monkeypatch, caplog):
        _enable(monkeypatch)

        def refuse(*_args, **_kwargs):
            raise PermissionError("read-only file system")

        monkeypatch.setattr(ch.os, "open", refuse)
        result = _attach(tmp_path)

        assert "context_savings" in result
        assert "context history" in caplog.text.lower()

    def test_the_file_is_private_to_the_user(self, tmp_path, monkeypatch):
        if os.name == "nt":
            pytest.skip("POSIX file modes")
        _enable(monkeypatch)
        _attach(tmp_path)

        assert (tmp_path / LOG_NAME).stat().st_mode & 0o077 == 0


# ---------------------------------------------------------------------------
# Bounded size
# ---------------------------------------------------------------------------


class TestBoundedSize:
    def test_the_log_rolls_over_and_never_passes_twice_the_limit(self, tmp_path, monkeypatch):
        _enable(monkeypatch)
        monkeypatch.setattr(ch, "MAX_BYTES", 600)
        for _ in range(200):
            _attach(tmp_path, detail_level="minimal", files=12, depth=2)

        names = sorted(p.name for p in tmp_path.iterdir() if p.name != "context-history.lock")
        assert names == [LOG_NAME, LOG_NAME + ".1"]
        lock = tmp_path / "context-history.lock"
        assert not lock.exists() or lock.stat().st_size == 0
        one_line = len((tmp_path / LOG_NAME).read_text(encoding="utf-8").splitlines()[0]) + 1
        for name in names:
            assert (tmp_path / name).stat().st_size < 600 + one_line
        # Both files are still read.
        entries, skipped = ch.read_history(tmp_path)
        assert skipped == 0
        assert len(entries) > 600 // one_line

    def test_the_default_limit_is_small(self):
        assert ch.MAX_BYTES <= 2 * 1024 * 1024

    @pytest.mark.skipif(sys.platform == "win32", reason="the guard uses flock")
    def test_two_writers_rolling_over_at_once_keep_the_old_log(self, tmp_path, monkeypatch):
        # Both writers see a full log. Without one lock around the size check,
        # the roll-over and the append, the second renames the first one's
        # new one-line log over .1 and the whole older log is lost.
        _enable(monkeypatch)
        monkeypatch.setattr(ch, "MAX_BYTES", 600)
        full = "x" * 700 + "\n"
        (tmp_path / LOG_NAME).write_text(full, encoding="utf-8")

        real_replace = os.replace
        other = threading.Thread(target=_attach, args=(tmp_path,))

        def slow_first_replace(src, dst):
            # The first writer pauses mid roll-over while the second one runs.
            if not other.is_alive() and other.ident is None:
                other.start()
                other.join(timeout=1.0)
            real_replace(src, dst)

        monkeypatch.setattr(ch.os, "replace", slow_first_replace)
        _attach(tmp_path)
        other.join(timeout=5)

        assert (tmp_path / (LOG_NAME + ".1")).read_text(encoding="utf-8") == full
        assert len(_log_lines(tmp_path)) == 2


# ---------------------------------------------------------------------------
# Reading and summing
# ---------------------------------------------------------------------------


def _line(ts: str, tool: str, baseline: int, returned: int) -> str:
    return json.dumps({
        "v": 1, "ts": ts, "tool": tool,
        "baseline_tokens": baseline, "returned_tokens": returned,
        "saved_tokens": max(0, baseline - returned),
    })


class TestRead:
    def test_older_file_first_and_bad_lines_skipped(self, tmp_path):
        (tmp_path / (LOG_NAME + ".1")).write_text(
            _line("2026-11-01T10:00:00Z", "a", 10, 5) + "\n", encoding="utf-8",
        )
        (tmp_path / LOG_NAME).write_text(
            "not json\n"
            + json.dumps({"v": 99, "ts": "2026-11-02T00:00:00Z"}) + "\n"
            + json.dumps({"v": 1, "ts": "x", "tool": "b"}) + "\n"
            + _line("2026-11-02T10:00:00Z", "b", 20, 5) + "\n",
            encoding="utf-8",
        )

        entries, skipped = ch.read_history(tmp_path)

        assert [e["tool"] for e in entries] == ["a", "b"]
        assert skipped == 3

    def test_since_drops_older_lines(self, tmp_path):
        (tmp_path / LOG_NAME).write_text(
            _line("2026-10-01T10:00:00Z", "old", 10, 5) + "\n"
            + _line("2026-11-02T10:00:00Z", "new", 10, 5) + "\n",
            encoding="utf-8",
        )

        entries, _ = ch.read_history(
            tmp_path, since=datetime(2026, 11, 1, tzinfo=timezone.utc),
        )

        assert [e["tool"] for e in entries] == ["new"]

    def test_no_log_is_an_empty_history(self, tmp_path):
        assert ch.read_history(tmp_path / "missing") == ([], 0)


class TestSummary:
    ENTRIES = [
        {"ts": "2026-11-01T10:00:00Z", "tool": "a", "baseline_tokens": 100,
         "returned_tokens": 10, "saved_tokens": 90},
        {"ts": "2026-11-01T11:00:00Z", "tool": "b", "baseline_tokens": 50,
         "returned_tokens": 40, "saved_tokens": 10},
        {"ts": "2026-11-02T09:00:00Z", "tool": "a", "baseline_tokens": 200,
         "returned_tokens": 70, "saved_tokens": 130},
    ]

    def test_by_day_in_date_order(self):
        rows = ch.summarise(self.ENTRIES, by="day")

        assert [r["key"] for r in rows] == ["2026-11-01", "2026-11-02"]
        assert rows[0] == {
            "key": "2026-11-01", "calls": 2, "baseline_tokens": 150,
            "returned_tokens": 50, "saved_tokens": 100, "largest_returned": 40,
        }

    def test_by_tool_busiest_first(self):
        rows = ch.summarise(self.ENTRIES, by="tool")

        assert [r["key"] for r in rows] == ["a", "b"]
        assert rows[0]["calls"] == 2
        assert rows[0]["largest_returned"] == 70

    def test_total(self):
        total = ch.total(self.ENTRIES)

        assert total["calls"] == 3
        assert total["saved_tokens"] == 230
        assert total["largest_returned"] == 70

    def test_saved_is_net_so_a_reply_bigger_than_the_files_shows(self):
        """Each response floors its own saving at 0. Summing those would hide
        the call that cost more than reading the files, which is the spike
        the log is for, and the columns would not add up."""
        entries = [
            {"ts": "2026-11-01T10:00:00Z", "tool": "big", "baseline_tokens": 1000,
             "returned_tokens": 3000, "saved_tokens": 0},
            {"ts": "2026-11-01T10:05:00Z", "tool": "small", "baseline_tokens": 1000,
             "returned_tokens": 100, "saved_tokens": 900},
        ]
        rows = {r["key"]: r for r in ch.summarise(entries, by="tool")}
        totals = ch.total(entries)

        assert rows["big"]["saved_tokens"] == -2000
        assert totals["saved_tokens"] == totals["baseline_tokens"] - totals["returned_tokens"]
        table = ch.format_table(list(rows.values()), totals, by="tool")
        assert "-2,000 (-200%)" in table
        assert "negative Saved" in table


# ---------------------------------------------------------------------------
# Real tools on a real graph
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    )


SECRET_BODY = "TOKEN_FROM_SOURCE_9f1c"


@pytest.fixture
def built_repo(tmp_path):
    from code_review_graph.tools.build import build_or_update_graph

    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "core.py").write_text(
        "def helper():\n    return 1\n\n\ndef run():\n    return helper()\n",
        encoding="utf-8",
    )
    (repo / "pkg" / "api.py").write_text(
        "from pkg.core import run\n\n\ndef serve():\n    return run()\n",
        encoding="utf-8",
    )
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    result = build_or_update_graph(full_rebuild=True, repo_root=str(repo))
    assert result["status"] == "ok", result
    # A working-tree change whose source text must never reach the log.
    (repo / "pkg" / "core.py").write_text(
        f"def helper():\n    return '{SECRET_BODY}'\n\n\ndef run():\n    return helper()\n",
        encoding="utf-8",
    )
    return repo


class TestRealTools:
    def test_each_instrumented_tool_writes_one_line(self, built_repo, monkeypatch):
        from code_review_graph.tools import (
            detect_changes_func,
            get_architecture_overview_func,
            get_impact_radius,
            get_review_context,
        )

        _enable(monkeypatch)
        root = str(built_repo)
        changed = ["pkg/core.py"]
        get_impact_radius(changed_files=changed, repo_root=root, detail_level="minimal")
        get_review_context(changed_files=changed, repo_root=root)
        detect_changes_func(changed_files=changed, repo_root=root, detail_level="minimal")
        get_architecture_overview_func(repo_root=root, detail_level="minimal")

        data_dir = built_repo / ".code-review-graph"
        entries = _log_lines(data_dir)
        assert [e["tool"] for e in entries] == [
            "get_impact_radius_tool",
            "get_review_context_tool",
            "detect_changes_tool",
            "get_architecture_overview_tool",
        ]
        assert entries[0]["detail_level"] == "minimal"
        assert entries[1]["detail_level"] == "standard"
        assert all(e.get("files") == 1 for e in entries[:3])
        assert all(e["baseline_tokens"] > 0 for e in entries)

        text = (data_dir / LOG_NAME).read_text(encoding="utf-8")
        for leak in (SECRET_BODY, "core.py", "pkg", str(built_repo), "helper"):
            assert leak not in text

    def test_with_the_option_off_the_tools_write_nothing(self, built_repo, monkeypatch):
        from code_review_graph.tools import detect_changes_func, get_impact_radius

        monkeypatch.delenv("CRG_CONTEXT_HISTORY", raising=False)
        data_dir = built_repo / ".code-review-graph"
        before = sorted(p.name for p in data_dir.iterdir())
        get_impact_radius(changed_files=["pkg/core.py"], repo_root=str(built_repo))
        detect_changes_func(changed_files=["pkg/core.py"], repo_root=str(built_repo))

        assert sorted(p.name for p in data_dir.iterdir()) == before
        assert LOG_NAME not in before

    def test_cli_mirrors_are_labelled_as_cli_commands(self, built_repo, monkeypatch, capsys):
        _enable(monkeypatch)
        argv = [
            "code-review-graph", "impact", "--files", "pkg/core.py", "--repo", str(built_repo),
        ]
        with patch.object(sys, "argv", argv):
            cli.main()
        capsys.readouterr()

        entries = _log_lines(built_repo / ".code-review-graph")
        assert [e["tool"] for e in entries] == ["impact"]

    def test_cli_detect_changes_is_logged(self, built_repo, monkeypatch, capsys):
        _enable(monkeypatch)
        argv = ["code-review-graph", "detect-changes", "--brief", "--repo", str(built_repo)]
        with patch.object(sys, "argv", argv):
            cli.main()
        capsys.readouterr()

        [entry] = _log_lines(built_repo / ".code-review-graph")
        assert entry["tool"] == "detect-changes"
        assert entry["files"] == 1
        assert "detail_level" not in entry


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


def _run(argv: list[str], capsys) -> tuple[int, str]:
    code = 0
    with patch.object(sys, "argv", ["code-review-graph", *argv]):
        try:
            cli.main()
        except SystemExit as exc:
            code = int(exc.code or 0)
    return code, capsys.readouterr().out


@pytest.fixture
def repo_with_log(tmp_path):
    repo = tmp_path / "repo"
    data_dir = repo / ".code-review-graph"
    data_dir.mkdir(parents=True)
    (repo / ".git").mkdir()
    today = datetime.now(timezone.utc)
    stamps = [
        (today - timedelta(days=1)).strftime("%Y-%m-%dT10:00:00Z"),
        today.strftime("%Y-%m-%dT00:00:01Z"),
    ]
    (data_dir / LOG_NAME).write_text(
        _line(stamps[0], "detect_changes_tool", 12000, 800) + "\n"
        + _line(stamps[1], "get_impact_radius_tool", 3000, 2900) + "\n"
        + _line(stamps[1], "detect_changes_tool", 9000, 600) + "\n"
        + _line("2020-01-01T00:00:00Z", "ancient_tool", 5, 1) + "\n",
        encoding="utf-8",
    )
    return repo, [s[:10] for s in stamps]


class TestCommand:
    def test_per_day_trend(self, repo_with_log, capsys, monkeypatch):
        monkeypatch.delenv("CRG_CONTEXT_HISTORY", raising=False)
        repo, days = repo_with_log
        code, out = _run(["context-history", "--repo", str(repo), "--days", "30"], capsys)

        assert code == 0
        assert "Day (UTC)" in out
        assert days[0] in out and days[1] in out
        assert "2020-01-01" not in out
        [total_line] = [line for line in out.splitlines() if line.startswith("Total")]
        # 12,000 + 3,000 + 9,000 in, 800 + 2,900 + 600 out; the 2020 line is outside.
        assert total_line.split()[1:4] == ["3", "24,000", "4,300"]
        assert "Recording is off" in out

    def test_per_tool_trend(self, repo_with_log, capsys):
        repo, _ = repo_with_log
        code, out = _run(["context-history", "--repo", str(repo), "--by", "tool"], capsys)

        assert code == 0
        lines = out.splitlines()
        tool_rows = [line for line in lines if line.startswith(("detect", "get_", "ancient"))]
        assert tool_rows[0].startswith("detect_changes_tool")
        assert any(line.startswith("ancient_tool") for line in tool_rows)

    def test_json(self, repo_with_log, capsys, monkeypatch):
        _enable(monkeypatch)
        repo, _ = repo_with_log
        code, out = _run(
            ["context-history", "--repo", str(repo), "--by", "tool", "--json"], capsys,
        )

        assert code == 0
        data = json.loads(out)
        assert data["recording"] is True
        assert data["by"] == "tool"
        assert data["total"]["calls"] == 4
        assert data["rows"][0]["key"] == "detect_changes_tool"

    def test_no_log_says_how_to_turn_it_on_and_creates_nothing(
        self, tmp_path, capsys, monkeypatch,
    ):
        monkeypatch.delenv("CRG_CONTEXT_HISTORY", raising=False)
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)

        code, out = _run(["context-history", "--repo", str(repo)], capsys)

        assert code == 0
        assert "CRG_CONTEXT_HISTORY=1" in out
        assert not (repo / ".code-review-graph").exists()

    def test_repo_inside_the_project_reads_the_project_log(self, repo_with_log, capsys):
        # ``impact --repo repo/src`` logs under the project root, so the
        # reader must look there too, not under repo/src.
        repo, _ = repo_with_log
        sub = repo / "src"
        sub.mkdir()

        code, out = _run(["context-history", "--repo", str(sub), "--json"], capsys)

        assert code == 0
        data = json.loads(out)
        assert data["log"] == str(repo.resolve() / ".code-review-graph" / LOG_NAME)
        assert data["total"]["calls"] == 4
        assert not (sub / ".code-review-graph").exists()

    def test_days_must_be_positive(self, tmp_path, capsys):
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        with pytest.raises(SystemExit) as exc:
            with patch.object(
                sys, "argv",
                ["code-review-graph", "context-history", "--repo", str(repo), "--days", "0"],
            ):
                cli.main()
        assert exc.value.code == 2
