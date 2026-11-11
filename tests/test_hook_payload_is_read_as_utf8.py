"""Hook payloads are read as UTF-8 bytes, whatever code page stdin has.

On Windows a text-mode stdin decodes a pipe with the ANSI code page, but
hosts write the event JSON as UTF-8. A ``cwd`` under a folder such as
``José`` then named a folder that does not exist, so the enrich hook and the
edit hook quietly did nothing. The same bug was fixed once in hook_update on
int/877; this pins it for both readers.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

from code_review_graph import enrich, hook_update


def _cp1252_stdin(payload: dict) -> io.TextIOWrapper:
    """A stdin like Windows gives: UTF-8 bytes behind an ANSI-code-page reader."""
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return io.TextIOWrapper(io.BytesIO(raw), encoding="cp1252")


def test_enrich_hook_reads_the_payload_as_utf8(tmp_path, monkeypatch):
    cwd = tmp_path / "José" / "repo"
    cwd.mkdir(parents=True)
    seen: list[Path] = []

    def find_project_root(start: Path) -> Path:
        seen.append(start)
        return start

    monkeypatch.setattr("code_review_graph.incremental.find_project_root", find_project_root)
    monkeypatch.setattr(
        enrich.sys,
        "stdin",
        _cp1252_stdin(
            {"tool_name": "Grep", "tool_input": {"pattern": "x"}, "cwd": str(cwd)}
        ),
    )

    enrich.run_hook()

    assert seen == [cwd]


def test_edit_hook_reads_the_payload_as_utf8(tmp_path, monkeypatch):
    cwd = str(tmp_path / "José" / "repo")
    monkeypatch.setattr(hook_update.sys, "stdin", _cp1252_stdin({"cwd": cwd}))

    assert hook_update._read_payload() == {"cwd": cwd}
