"""Tests for the content-addressed vstore (code_review_graph.vstore)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from code_review_graph.vstore import (
    get,
    list_entries,
    put,
    tree_hash,
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@test")
    _git(root, "config", "user.name", "test")
    (root / "src.py").write_text("x = 1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


def _make_data_dir(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    (data / "embeddings").mkdir(parents=True)
    (data / "graph.db").write_bytes(b"graph-bytes")
    (data / "embeddings" / "vectors.npz").write_bytes(b"embedding-bytes")
    return data


def test_tree_hash_is_stable_hex_and_moves_with_commit(repo: Path) -> None:
    first = tree_hash(repo)
    assert len(first) == 40
    assert all(c in "0123456789abcdef" for c in first)
    assert tree_hash(repo) == first
    (repo / "src.py").write_text("x = 2\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "change")
    assert tree_hash(repo) != first


def test_tree_hash_fails_loud_outside_a_repo(tmp_path: Path) -> None:
    empty = tmp_path / "plain"
    empty.mkdir()
    with pytest.raises(RuntimeError, match="tree hash unavailable"):
        tree_hash(empty)


def test_put_get_roundtrip_is_byte_identical(repo: Path, tmp_path: Path) -> None:
    store = tmp_path / "store"
    data = _make_data_dir(tmp_path)
    receipt = put(store, repo, data, embedding_model="local/minilm")
    assert receipt.tree_hash == tree_hash(repo)
    assert receipt.embedding_model == "local/minilm"
    assert receipt.files == {
        "graph.db": receipt.files["graph.db"],
        "embeddings/vectors.npz": receipt.files["embeddings/vectors.npz"],
    }
    assert [r.tree_hash for r in list_entries(store)] == [receipt.tree_hash]

    dest = tmp_path / "dest"
    out = get(store, receipt.tree_hash, dest)
    assert out == receipt
    assert (dest / "graph.db").read_bytes() == b"graph-bytes"
    assert (dest / "embeddings" / "vectors.npz").read_bytes() == b"embedding-bytes"


def test_put_is_idempotent_and_atomic(repo: Path, tmp_path: Path) -> None:
    store = tmp_path / "store"
    data = _make_data_dir(tmp_path)
    first = put(store, repo, data)
    second = put(store, repo, data)
    assert first.tree_hash == second.tree_hash
    entries = list(store.iterdir())
    assert [p.name for p in entries] == [first.tree_hash]
    assert not any(p.name.startswith(".staging-") for p in entries)


def test_get_rejects_tampered_bundle(repo: Path, tmp_path: Path) -> None:
    store = tmp_path / "store"
    data = _make_data_dir(tmp_path)
    receipt = put(store, repo, data)
    stored = store / receipt.tree_hash / "bundle" / "graph.db"
    stored.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="tampered"):
        get(store, receipt.tree_hash, tmp_path / "dest")


def test_get_rejects_missing_entry(repo: Path, tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="no vstore entry"):
        get(tmp_path / "store", "0" * 40, tmp_path / "dest")


def test_receipt_carries_commit_and_model_metadata(repo: Path, tmp_path: Path) -> None:
    store = tmp_path / "store"
    data = _make_data_dir(tmp_path)
    receipt = put(store, repo, data, embedding_model="local/minilm")
    raw = json.loads((store / receipt.tree_hash / "receipt.json").read_text(encoding="utf-8"))
    assert raw["schema_version"] == 1
    assert raw["head"] == _git(repo, "rev-parse", "HEAD")
    assert raw["files"]["graph.db"]


def test_seed_helper_restores_bundle_and_reports_miss(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_review_graph.cli import _seed_build_from_vstore

    store = tmp_path / "store"
    put(store, repo, _make_data_dir(tmp_path), embedding_model="local/minilm")

    target = tmp_path / "crg-data"
    monkeypatch.setenv("CRG_DATA_DIR", str(target))

    assert _seed_build_from_vstore(store, repo, quiet=False) is True
    assert (target / "graph.db").read_bytes() == b"graph-bytes"
    assert (target / "embeddings" / "vectors.npz").read_bytes() == b"embedding-bytes"

    assert _seed_build_from_vstore(tmp_path / "empty-store", repo, quiet=True) is False


def test_seed_helper_fails_loud_on_tampered_bundle(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_review_graph.cli import _seed_build_from_vstore

    store = tmp_path / "store"
    put(store, repo, _make_data_dir(tmp_path))
    digest = tree_hash(repo)
    (store / digest / "bundle" / "graph.db").write_bytes(b"tampered")
    monkeypatch.setenv("CRG_DATA_DIR", str(tmp_path / "target"))
    with pytest.raises(ValueError, match="tampered"):
        _seed_build_from_vstore(store, repo, quiet=True)


def test_retain_max_entries_keeps_newest(repo: Path, tmp_path: Path) -> None:
    import os
    import time

    from code_review_graph.vstore import retain

    store = tmp_path / "store"
    digests = []
    for i in range(3):
        (repo / "src.py").write_text(f"x = {i}\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", f"c{i}")
        data = tmp_path / f"data{i}"
        data.mkdir()
        (data / "graph.db").write_bytes(f"db{i}".encode())
        digests.append(put(store, repo, data).tree_hash)
        os.utime(store / digests[-1], (time.time() + i * 100,) * 2)

    removed = retain(store, max_entries=2)

    assert removed == [digests[0]]
    assert [r.tree_hash for r in list_entries(store)] == digests[1:]


def test_retain_ttl_drops_old_entries(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os
    import time

    from code_review_graph.vstore import retain

    store = tmp_path / "store"
    data = _make_data_dir(tmp_path)
    old = put(store, repo, data)
    entry = store / old.tree_hash
    week_ago = time.time() - 8 * 86400
    os.utime(entry, (week_ago, week_ago))

    removed = retain(store, ttl_days=7)

    assert removed == [old.tree_hash]
    assert list_entries(store) == []


def test_retain_requires_a_policy(repo: Path, tmp_path: Path) -> None:
    from code_review_graph.vstore import retain

    with pytest.raises(ValueError, match="requires max_entries"):
        retain(tmp_path / "store")
