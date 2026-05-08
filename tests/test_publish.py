"""Tests for the opt-in understand-quickly publish flow."""

from __future__ import annotations

import io
import json
import re
from pathlib import Path
from unittest import mock

import pytest

from code_review_graph import publish as publish_mod
from code_review_graph.graph import GraphStore
from code_review_graph.parser import NodeInfo


@pytest.fixture
def store(tmp_path: Path) -> GraphStore:
    db_path = tmp_path / "test.db"
    s = GraphStore(db_path)
    s.upsert_node(
        NodeInfo(
            kind="File",
            name="auth.py",
            file_path="src/auth.py",
            line_start=1,
            line_end=10,
            language="python",
            parent_name=None,
            params=None,
            return_type=None,
            modifiers=None,
            is_test=False,
            extra={},
        )
    )
    return s


def test_build_publish_payload_embeds_metadata(store: GraphStore, tmp_path: Path):
    payload = publish_mod.build_publish_payload(store, tmp_path)
    assert "nodes" in payload and "edges" in payload
    md = payload["metadata"]
    assert md["tool"] == "code-review-graph"
    assert md["tool_version"]  # non-empty
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", md["generated_at"])
    # commit is omitted when not in a git repo (tmp_path has no .git)
    assert "commit" not in md or re.fullmatch(r"[0-9a-f]{40}", md["commit"])


def test_write_publish_json_writes_to_disk(store: GraphStore, tmp_path: Path):
    out = tmp_path / ".code-review-graph" / "graph.json"
    publish_mod.write_publish_json(store, tmp_path, out)
    assert out.exists()
    data = json.loads(out.read_text())
    assert data["metadata"]["tool"] == "code-review-graph"


def test_publish_without_token_skips_dispatch(
    store: GraphStore, tmp_path: Path, monkeypatch, capsys
):
    monkeypatch.delenv("UNDERSTAND_QUICKLY_TOKEN", raising=False)
    out = tmp_path / "graph.json"
    with mock.patch.object(publish_mod.urlrequest, "urlopen") as m_open:
        publish_mod.publish(store, tmp_path, out, publish_to_uq=True)
    m_open.assert_not_called()
    captured = capsys.readouterr().out
    assert "UNDERSTAND_QUICKLY_TOKEN not set" in captured


def test_publish_with_token_fires_dispatch(
    store: GraphStore, tmp_path: Path, monkeypatch, capsys
):
    monkeypatch.setenv("UNDERSTAND_QUICKLY_TOKEN", "ghp_fake")

    fake_resp = mock.MagicMock()
    fake_resp.__enter__.return_value = fake_resp
    fake_resp.status = 204
    fake_resp.getcode.return_value = 204

    with mock.patch.object(
        publish_mod, "_git_origin_owner_repo", return_value=("looptech-ai", "demo")
    ), mock.patch.object(publish_mod.urlrequest, "urlopen", return_value=fake_resp) as m_open:
        out = tmp_path / "graph.json"
        publish_mod.publish(store, tmp_path, out, publish_to_uq=True)

    m_open.assert_called_once()
    sent = m_open.call_args[0][0]
    assert sent.full_url == publish_mod.DISPATCH_URL
    body = json.loads(sent.data.decode())
    assert body == {
        "event_type": "sync-entry",
        "client_payload": {"id": "looptech-ai/demo"},
    }
    assert sent.headers["Authorization"] == "Bearer ghp_fake"
    captured = capsys.readouterr().out
    assert "dispatch sent" in captured


def test_publish_dispatch_failure_is_soft(
    store: GraphStore, tmp_path: Path, monkeypatch, capsys
):
    monkeypatch.setenv("UNDERSTAND_QUICKLY_TOKEN", "ghp_fake")
    from urllib import error as urlerror

    err = urlerror.HTTPError(
        publish_mod.DISPATCH_URL, 422, "Unprocessable", {}, io.BytesIO(b"")
    )
    out = tmp_path / "graph.json"
    with mock.patch.object(
        publish_mod, "_git_origin_owner_repo", return_value=("looptech-ai", "demo")
    ), mock.patch.object(publish_mod.urlrequest, "urlopen", side_effect=err):
        # Must not raise — soft-fail and inform the user.
        publish_mod.publish(store, tmp_path, out, publish_to_uq=True)
    captured = capsys.readouterr().out
    assert "dispatch failed" in captured
    assert "npx @understand-quickly/cli add" in captured
    assert out.exists()


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/looptech-ai/demo.git", ("looptech-ai", "demo")),
        ("https://github.com/looptech-ai/demo", ("looptech-ai", "demo")),
        ("git@github.com:looptech-ai/demo.git", ("looptech-ai", "demo")),
        ("git@github.com:looptech-ai/demo", ("looptech-ai", "demo")),
    ],
)
def test_origin_owner_repo_parsing(url: str, expected: tuple[str, str], tmp_path: Path):
    with mock.patch.object(
        publish_mod.subprocess, "check_output", return_value=(url + "\n").encode()
    ):
        assert publish_mod._git_origin_owner_repo(tmp_path) == expected
