"""Watch batches refresh exclusion rules without re-inventorying source files (#909)."""

from unittest.mock import Mock, patch

import pytest
from watchdog.events import (
    DirModifiedEvent,
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileMovedEvent,
)

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import (
    _create_watch_handler,
    _load_ignore_patterns,
    _should_ignore,
    clear_nested_ignore_cache,
    incremental_update,
)


@pytest.fixture(autouse=True)
def isolated_ignore_cache():
    clear_nested_ignore_cache()
    yield
    clear_nested_ignore_cache()


@pytest.fixture
def store(tmp_path):
    with GraphStore(tmp_path / "graph.db") as graph:
        yield graph


def write_source(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("def retained():\n    return 1\n")
    return path


def test_first_maven_output_is_ignored_before_cache_expiry(tmp_path, store):
    module = tmp_path / "module"
    module.mkdir()
    (module / "pom.xml").write_text("<project/>")
    handler = _create_watch_handler(tmp_path, store, None)
    output = write_source(module / "target" / "generated.py")

    # A file-only delivery must suffice: native backends can omit directory events.
    with patch(
        "code_review_graph.incremental._scan_nested_output_dirs",
        side_effect=AssertionError("ordinary file event rescanned module tree"),
    ):
        handler.process([FileCreatedEvent(str(output))])
    handler.raise_if_failed()
    assert store.get_all_files() == []


@pytest.mark.parametrize("keep", [False, True])
def test_new_manifest_purges_previously_indexed_output_unless_kept(tmp_path, store, keep):
    output = write_source(tmp_path / "module" / "target" / "generated.py")
    user = write_source(tmp_path / "module" / "src" / "user.py")
    if keep:
        (tmp_path / ".code-review-graphignore").write_text("!module/target\n")
    incremental_update(tmp_path, store, changed_files=[str(output), str(user)])
    callback = Mock(return_value=None)
    handler = _create_watch_handler(tmp_path, store, callback)
    manifest = output.parent.parent / "pom.xml"
    manifest.write_text("<project/>")

    with patch(
        "code_review_graph.incremental.collect_all_files",
        side_effect=AssertionError("metadata event inventoried repository"),
    ):
        handler.process([FileCreatedEvent(str(manifest))])
    handler.raise_if_failed()
    assert bool(store.get_nodes_by_file(str(output))) is keep
    assert store.get_nodes_by_file(str(user))
    assert callback.call_count == (0 if keep else 1)


def test_ignore_edit_purges_and_removal_indexes_without_source_event(tmp_path, store):
    generated = write_source(tmp_path / "generated" / "generated.py")
    user = write_source(tmp_path / "src" / "user.py")
    incremental_update(tmp_path, store, changed_files=[str(generated), str(user)])
    callback = Mock(return_value=None)
    handler = _create_watch_handler(tmp_path, store, callback)
    config = tmp_path / ".code-review-graphignore"
    config.write_text("generated/\n")

    handler.process([FileModifiedEvent(str(config))])
    handler.raise_if_failed()
    assert store.get_nodes_by_file(str(generated)) == []
    assert store.get_nodes_by_file(str(user))
    callback.assert_called_once_with(store)

    config.unlink()
    handler.process([FileDeletedEvent(str(config))])
    handler.raise_if_failed()
    assert store.get_nodes_by_file(str(generated))
    assert callback.call_count == 2


def test_removing_keep_purges_inferred_output(tmp_path, store):
    output = write_source(tmp_path / "module" / "target" / "user.py")
    (output.parent.parent / "pom.xml").write_text("<project/>")
    config = tmp_path / ".code-review-graphignore"
    config.write_text("!module/target\n")
    incremental_update(tmp_path, store, changed_files=[output.relative_to(tmp_path).as_posix()])
    handler = _create_watch_handler(tmp_path, store, None)
    config.write_text("")

    handler.process([FileModifiedEvent(str(config))])
    handler.raise_if_failed()
    assert store.get_all_files() == []


def test_gitignore_edit_preserves_existing_explicit_update_semantics(tmp_path, store):
    user = write_source(tmp_path / "src" / "user.py")
    incremental_update(tmp_path, store, changed_files=[str(user)])
    handler = _create_watch_handler(tmp_path, store, None)
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("src/\n")
    user.write_text("def changed():\n    return 2\n")

    with (
        patch(
            "code_review_graph.incremental._scan_nested_output_dirs",
            side_effect=AssertionError("ordinary file batch rescanned module tree"),
        ),
        patch(
            "code_review_graph.incremental.collect_all_files",
            side_effect=AssertionError("ordinary file batch inventoried repository"),
        ),
    ):
        handler.process([FileModifiedEvent(str(gitignore)), FileModifiedEvent(str(user))])
    handler.raise_if_failed()
    assert any(node.name == "changed" for node in store.get_nodes_by_file(str(user)))


def test_adding_keep_indexes_existing_output_without_source_event(tmp_path, store):
    output = write_source(tmp_path / "module" / "target" / "user.py")
    (output.parent.parent / "pom.xml").write_text("<project/>")
    incremental_update(tmp_path, store, changed_files=[output.relative_to(tmp_path).as_posix()])
    assert store.get_all_files() == []
    callback = Mock(return_value=None)
    handler = _create_watch_handler(tmp_path, store, callback)
    config = tmp_path / ".code-review-graphignore"
    config.write_text("!module/target\n")

    handler.process([FileCreatedEvent(str(config))])
    handler.raise_if_failed()
    assert store.get_nodes_by_file(str(output))
    callback.assert_called_once_with(store)


def test_manifest_removal_indexes_existing_output_without_source_event(tmp_path, store):
    output = write_source(tmp_path / "module" / "target" / "user.py")
    manifest = output.parent.parent / "pom.xml"
    manifest.write_text("<project/>")
    handler = _create_watch_handler(tmp_path, store, None)
    manifest.unlink()

    handler.process([FileDeletedEvent(str(manifest))])
    handler.raise_if_failed()
    assert store.get_nodes_by_file(str(output))


def test_keep_does_not_override_explicit_exclusion(tmp_path, store):
    output = write_source(tmp_path / "module" / "target" / "user.py")
    (output.parent.parent / "pom.xml").write_text("<project/>")
    handler = _create_watch_handler(tmp_path, store, None)
    config = tmp_path / ".code-review-graphignore"
    config.write_text("!module/target\nmodule/target/\n")

    handler.process([FileCreatedEvent(str(config))])
    handler.raise_if_failed()
    assert store.get_all_files() == []


def test_cache_expiry_refreshes_snapshot_and_purges_stored_output(tmp_path, store, monkeypatch):
    output = write_source(tmp_path / "module" / "target" / "generated.py")
    user = write_source(tmp_path / "src" / "user.py")
    incremental_update(tmp_path, store, changed_files=[str(output), str(user)])
    handler = _create_watch_handler(tmp_path, store, None)
    (output.parent.parent / "pom.xml").write_text("<project/>")
    monkeypatch.setattr("code_review_graph.incremental._NESTED_IGNORE_TTL_SECONDS", 0)

    handler.process([FileModifiedEvent(str(user))])
    handler.raise_if_failed()
    assert store.get_nodes_by_file(str(output)) == []
    assert store.get_nodes_by_file(str(user))


def test_output_inference_does_not_exclude_an_existing_source_file(tmp_path):
    module = tmp_path / "module"
    module.mkdir()
    (module / "pom.xml").write_text("<project/>")
    (module / "target").write_text("#!/usr/bin/env python3\ndef source(): pass\n")

    assert not _should_ignore("module/target", _load_ignore_patterns(tmp_path))


def test_ignore_purge_removes_exact_legacy_spelling(tmp_path, store):
    output = write_source(tmp_path / "generated" / "output.py")
    user = write_source(tmp_path / "src" / "user.py")
    incremental_update(tmp_path, store, changed_files=[str(output), str(user)])
    legacy = str(output).replace("/", chr(92))
    store._conn.execute("UPDATE nodes SET file_path = ? WHERE file_path = ?", (legacy, str(output)))
    store._conn.execute("UPDATE edges SET file_path = ? WHERE file_path = ?", (legacy, str(output)))
    store._conn.commit()
    store._invalidate_cache()
    assert legacy in store.get_all_files()
    handler = _create_watch_handler(tmp_path, store, None)
    config = tmp_path / ".code-review-graphignore"
    config.write_text("generated/\n")

    handler.process([FileCreatedEvent(str(config))])
    handler.raise_if_failed()
    assert legacy not in store.get_all_files()
    assert store.get_nodes_by_file(str(user))


def test_ignore_purge_refuses_partially_foreign_graph_before_mutation(tmp_path, store):
    output = write_source(tmp_path / "generated" / "output.py")
    foreign = write_source(tmp_path.parent / (tmp_path.name + "-foreign") / "user.py")
    incremental_update(tmp_path, store, changed_files=[str(output)])
    # Preserve a legitimate File marker outside this root, as in a mixed legacy graph.
    from code_review_graph.parser import CodeParser

    nodes, _ = CodeParser(foreign.parent).parse_file(foreign)
    for node in nodes:
        store.upsert_node(node)
    store.commit()
    before = store.get_all_files()
    handler = _create_watch_handler(tmp_path, store, None)
    config = tmp_path / ".code-review-graphignore"
    config.write_text("generated/\n")

    handler.process([FileCreatedEvent(str(config))])
    with pytest.raises(RuntimeError, match="watch update failed") as failure:
        handler.raise_if_failed()
    assert "root" in str(failure.value.__cause__)
    assert store.get_all_files() == before
    assert store.get_nodes_by_file(str(output))


def test_parent_directory_modification_does_not_rescan_ordinary_batch(tmp_path, store):
    user = write_source(tmp_path / "src" / "user.py")
    handler = _create_watch_handler(tmp_path, store, None)
    with patch(
        "code_review_graph.incremental._scan_nested_output_dirs",
        side_effect=AssertionError("parent directory notification rescanned module tree"),
    ):
        handler.process([FileModifiedEvent(str(user)), DirModifiedEvent(str(user.parent))])
    handler.raise_if_failed()
    assert store.get_nodes_by_file(str(user))


@pytest.mark.parametrize("event_type", ["created", "moved"])
@pytest.mark.parametrize("explicit_ignore", [False, True])
def test_new_source_file_at_inferred_output_root_is_indexed(
    tmp_path,
    store,
    event_type,
    explicit_ignore,
):
    module = tmp_path / "module"
    module.mkdir()
    (module / "pom.xml").write_text("<project/>")
    if explicit_ignore:
        (tmp_path / ".code-review-graphignore").write_text("module/target/\n")
    handler = _create_watch_handler(tmp_path, store, None)
    target = module / "target"
    source = module / "script" if event_type == "moved" else target
    source.write_text("#!/usr/bin/env python3\ndef source(): return 1\n")
    if event_type == "moved":
        source.rename(target)
        event = FileMovedEvent(str(source), str(target))
    else:
        event = FileCreatedEvent(str(target))

    handler.process([event])
    handler.raise_if_failed()
    assert bool(store.get_nodes_by_file(str(target))) is not explicit_ignore
