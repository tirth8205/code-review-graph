"""Identity migrations retry only failed replacements after their first scan."""

import hashlib
from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import CPP_IDENTITY_VERSION, incremental_update
from code_review_graph.parser import CodeParser, NodeInfo


@pytest.fixture
def legacy_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for index in range(8):
        (repo / f"healthy_{index}.py").write_text(f"def healthy_{index}(): pass\n")
    source = repo / "broken.cpp"
    source.write_text("void run(int value) {}\n")
    (repo / "healthy.cpp").write_text("void healthy(int value) {}\n")
    db = tmp_path / "graph.db"
    store = GraphStore(db)
    store.upsert_node(
        NodeInfo(
            kind="Function",
            name="run",
            file_path=str(source),
            line_start=1,
            line_end=1,
            language="cpp",
        ),
        file_hash=hashlib.sha256(source.read_bytes()).hexdigest(),
    )
    store.commit()
    yield repo, source, db, store
    store.close()


def failing_parser(monkeypatch, failing=True):
    calls = []
    original = CodeParser.parse_bytes

    def parse(parser, path, raw):
        calls.append(Path(path).name)
        if failing and Path(path).name == "broken.cpp":
            raise RuntimeError("persistent parse failure")
        return original(parser, path, raw)

    monkeypatch.setattr(CodeParser, "parse_bytes", parse)
    return calls


@pytest.mark.parametrize("executor", ["serial", "thread"])
def test_persistent_failure_retries_only_failed_file_and_survives_reopen(
    legacy_repo,
    monkeypatch,
    executor,
):
    repo, source, db, store = legacy_repo
    monkeypatch.setenv("CRG_SERIAL_PARSE", "1" if executor == "serial" else "0")
    monkeypatch.setenv("CRG_PARSE_EXECUTOR", "thread")
    with monkeypatch.context() as failed:
        calls = failing_parser(failed)
        first = incremental_update(repo, store, changed_files=[])
        assert first["identity_rebuild"] is True
        assert first["errors"] == [{"file": "broken.cpp", "error": "persistent parse failure"}]
        assert store.get_metadata("cpp_identity_version") is None
        assert store.get_node(f"{source.as_posix()}::run") is not None
        assert set(calls) == {path.name for path in repo.iterdir()}
        calls.clear()
        with GraphStore(db) as reopened:
            again = incremental_update(repo, reopened, changed_files=[])
        assert calls == ["broken.cpp"]
        assert again.get("identity_rebuild") is None
        assert again["files_updated"] == 0
        assert again["errors"] == first["errors"]
        assert store.get_metadata("cpp_identity_version") is None
    # Retry must ignore the unchanged content hash left on the old node.
    with monkeypatch.context() as recovered:
        calls = failing_parser(recovered, failing=False)
        final = incremental_update(repo, store, changed_files=[])
        assert calls == ["broken.cpp"]
    assert final["errors"] == []
    assert final["files_updated"] == 1
    assert store.get_metadata("cpp_identity_version") == CPP_IDENTITY_VERSION
    assert store.get_node(f"{source.as_posix()}::run") is None
    assert store.get_node(f"{source.as_posix()}::run(int)") is not None
    assert incremental_update(repo, store, changed_files=[])["files_updated"] == 0


def test_unrelated_edit_does_not_mark_pending_identity_complete(legacy_repo, monkeypatch):
    repo, _source, _db, store = legacy_repo
    monkeypatch.setenv("CRG_SERIAL_PARSE", "1")
    calls = failing_parser(monkeypatch)
    incremental_update(repo, store, changed_files=[])
    calls.clear()
    (repo / "healthy_0.py").write_text("def healthy_0(): return 1\n")
    result = incremental_update(repo, store, changed_files=["healthy_0.py"])
    assert set(calls) == {"healthy_0.py", "broken.cpp"}
    assert result["files_updated"] == 1
    assert result["errors"] == [{"file": "broken.cpp", "error": "persistent parse failure"}]
    assert store.get_metadata("cpp_identity_version") is None


def test_deleted_failed_file_finishes_identity_upgrade(legacy_repo, monkeypatch):
    repo, source, _db, store = legacy_repo
    monkeypatch.setenv("CRG_SERIAL_PARSE", "1")
    calls = failing_parser(monkeypatch)
    incremental_update(repo, store, changed_files=[])
    source.unlink()
    calls.clear()
    result = incremental_update(repo, store, changed_files=[])
    assert calls == []
    assert result.get("identity_rebuild") is None
    assert result["errors"] == []
    assert store.get_nodes_by_file(str(source)) == []
    assert store.get_metadata("cpp_identity_version") == CPP_IDENTITY_VERSION


@pytest.mark.parametrize("reconcile", [True, False])
def test_ignored_failed_file_is_removed_or_reported_pending(
    legacy_repo,
    monkeypatch,
    reconcile,
):
    repo, source, _db, store = legacy_repo
    monkeypatch.setenv("CRG_SERIAL_PARSE", "1")
    calls = failing_parser(monkeypatch)
    incremental_update(repo, store, changed_files=[])
    (repo / ".code-review-graphignore").write_text("broken.cpp\n")
    calls.clear()
    result = incremental_update(repo, store, changed_files=[], reconcile_stale=reconcile)
    assert calls == []
    if reconcile:
        assert result["errors"] == []
        assert store.get_nodes_by_file(str(source)) == []
        assert store.get_metadata("cpp_identity_version") == CPP_IDENTITY_VERSION
    else:
        assert result["errors"]
        assert result["errors"][0]["file"] == "broken.cpp"
        assert store.get_node(f"{source.as_posix()}::run") is not None
        assert store.get_metadata("cpp_identity_version") is None


@pytest.mark.parametrize("change_kind", ["committed", "reverted"])
def test_pending_identity_preserves_git_freshness_and_content_reconciliation(
    legacy_repo,
    monkeypatch,
    change_kind,
):
    import subprocess

    repo, source, _db, store = legacy_repo
    monkeypatch.setenv("CRG_SERIAL_PARSE", "1")

    def git(*args):
        return subprocess.run(
            ["git", "-c", "user.email=t@test", "-c", "user.name=t", *args],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("init", "-q")
    git("add", ".")
    git("commit", "-qm", "initial")
    initial_sha = git("rev-parse", "HEAD")
    store.set_metadata("git_head_sha", initial_sha)
    healthy = repo / "healthy_0.py"
    original = healthy.read_text()
    with monkeypatch.context() as failed:
        calls = failing_parser(failed)
        incremental_update(repo, store, changed_files=[])
        healthy.write_text("def healthy_0(): return 42\n")
        if change_kind == "committed":
            git("add", ".")
            git("commit", "-qm", "edit healthy file")
        else:
            incremental_update(repo, store, changed_files=["healthy_0.py"])
            healthy.write_text(original)
        calls.clear()
        updated = incremental_update(repo, store, base=initial_sha)
        assert set(calls) == {"healthy_0.py", "broken.cpp"}
        assert updated["files_updated"] == 1
        assert updated["errors"]
        assert updated["freshness_advanced"] is True
        assert store.get_metadata("git_head_sha") == git("rev-parse", "HEAD")
        assert store.get_metadata("cpp_identity_version") is None
    with monkeypatch.context() as recovered:
        calls = failing_parser(recovered, failing=False)
        final = incremental_update(repo, store, base=initial_sha)
        assert calls == ["broken.cpp"]
    assert final["errors"] == []
    assert final["freshness_advanced"] is True
    assert store.get_metadata("git_head_sha") == git("rev-parse", "HEAD")
    assert store.get_node(f"{source.as_posix()}::run(int)") is not None
