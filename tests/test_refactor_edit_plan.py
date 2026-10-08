"""Complete read-only edit plans for native DSH commits (platform issue #1100)."""
import hashlib
import time

import pytest

from code_review_graph import refactor


def pending(monkeypatch, files):
    monkeypatch.setitem(refactor._pending_refactors, "dsh-test", {
        "created_at": time.time(),
        "edits": [{"file": str(path), "old": "old", "new": "new"} for path in files],
    })


@pytest.mark.parametrize("data", [b"old\n", b"old\r\n", b"\xef\xbb\xbfold\r\n"])
def test_plan_preserves_bytes_and_does_not_write(tmp_path, monkeypatch, data):
    path = tmp_path / "a.py"
    path.write_bytes(data)
    pending(monkeypatch, [path])
    result = refactor.get_refactor_edit_plan("dsh-test", tmp_path)
    assert result["status"] == "ok"
    assert result["files"] == [{"path": str(path.resolve()),
                               "before_sha256": hashlib.sha256(data).hexdigest(),
                               "after_content": data.replace(b"old", b"new").decode("utf-8"),
                               "edit_count": 1}]
    assert path.read_bytes() == data
    assert "dsh-test" in refactor._pending_refactors


@pytest.mark.parametrize("invalid", ["missing", "encoding", "stale", "outside"])
def test_incomplete_plan_is_rejected(tmp_path, monkeypatch, invalid):
    good = tmp_path / "a.py"
    good.write_bytes(b"old\n")
    path = tmp_path / "b.py"
    if invalid == "outside":
        path = tmp_path.parent / (tmp_path.name + "-outside.py")
    if invalid != "missing":
        data = b"\xffold" if invalid == "encoding" else b"changed" if invalid == "stale" else b"old"
        path.write_bytes(data)
    pending(monkeypatch, [good, path])
    result = refactor.get_refactor_edit_plan("dsh-test", tmp_path)
    assert result["status"] == "error"
    assert "files" not in result
    assert good.read_bytes() == b"old\n"


def test_plan_is_not_truncated(tmp_path, monkeypatch):
    paths = [tmp_path / f"{i}.py" for i in range(170)]
    for path in paths:
        path.write_bytes(b"old\n")
    pending(monkeypatch, paths)
    assert len(refactor.get_refactor_edit_plan("dsh-test", tmp_path)["files"]) == 170


def test_expired_plan_rejected(tmp_path, monkeypatch):
    pending(monkeypatch, [])
    refactor._pending_refactors["dsh-test"]["created_at"] = 0
    assert refactor.get_refactor_edit_plan("dsh-test", tmp_path)["status"] == "error"
