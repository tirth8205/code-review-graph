"""Manifest-aware vendor exclusions, issue #1056."""

import pytest
from watchdog.events import FileCreatedEvent, FileDeletedEvent

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import (
    _create_watch_handler,
    _load_ignore_patterns,
    _should_ignore,
    clear_nested_ignore_cache,
)
from code_review_graph.parser import CodeParser


@pytest.mark.parametrize("path", ["vendor/supplier.py", "src/com/acme/vendor/Supplier.java"])
def test_non_composer_vendor_source_is_visible(tmp_path, path):
    assert not _should_ignore(path, _load_ignore_patterns(tmp_path))


@pytest.mark.parametrize("path", ["vendor/autoload.php", "backend/vendor/package/lib.php"])
def test_composer_dependencies_are_still_excluded(tmp_path, path):
    (tmp_path / "composer.json").write_text("{}", encoding="utf-8")
    assert _should_ignore(path, _load_ignore_patterns(tmp_path))


def test_explicit_vendor_ignore_still_works_without_composer(tmp_path):
    (tmp_path / ".code-review-graphignore").write_text("vendor/\n", encoding="utf-8")
    assert _should_ignore("src/vendor/generated.py", _load_ignore_patterns(tmp_path))


def test_other_dependency_defaults_are_unchanged(tmp_path):
    patterns = _load_ignore_patterns(tmp_path)
    assert _should_ignore("packages/app/node_modules/lib/index.js", patterns)
    assert _should_ignore("src/lib/__pycache__/app.pyc", patterns)


@pytest.mark.parametrize("manifest", ["go.mod", "go.work", "Gemfile", "Rakefile", "app.gemspec"])
def test_go_and_ruby_vendor_dependencies_remain_excluded(tmp_path, manifest):
    (tmp_path / manifest).write_text("", encoding="utf-8")
    assert _should_ignore("vendor/package/lib/source.rb", _load_ignore_patterns(tmp_path))


@pytest.mark.parametrize("manifest", ["composer.json", "go.mod", "Gemfile"])
def test_nested_dependency_vendor_is_excluded_in_watch(tmp_path, manifest):
    clear_nested_ignore_cache()
    module = tmp_path / "api"
    module.mkdir()
    (module / manifest).write_text("", encoding="utf8")
    with GraphStore(tmp_path / "graph.db") as store:
        handler = _create_watch_handler(tmp_path, store, None)
        source = module / "vendor" / "pkg" / "generated.py"
        source.parent.mkdir(parents=True)
        source.write_text("def dependency(): pass\n", encoding="utf8")
        handler.process([FileCreatedEvent(str(source))])
        handler.raise_if_failed()
        assert not store.get_all_files()


def test_nested_go_vendor_is_not_an_import_target(tmp_path):
    clear_nested_ignore_cache()
    module = tmp_path / "server"
    module.mkdir()
    (module / "go.mod").write_text("module example.com/server\n", encoding="utf8")
    vendor = module / "vendor/github.com/ext/lib"
    vendor.mkdir(parents=True)
    (vendor / "lib.go").write_text("package lib\nfunc Run() {}\n", encoding="utf8")
    assert not CodeParser(tmp_path)._path_is_indexed(vendor / "lib.go", tmp_path)
    source = module / "main.go"
    source.write_text('package main\nimport "github.com/ext/lib"\nfunc main() { lib.Run() }\n')
    _, edges = CodeParser(tmp_path).parse_file(source)
    assert not any("/vendor/" in e.target for e in edges if e.kind == "IMPORTS_FROM")


def test_removing_root_manifest_invalidates_nested_scan(tmp_path):
    clear_nested_ignore_cache()
    manifest = tmp_path / "Gemfile"
    manifest.write_text("", encoding="utf8")
    module = tmp_path / "vendor/mod"
    module.mkdir(parents=True)
    (module / "pom.xml").write_text("<project/>", encoding="utf8")
    output = module / "target/generated.py"
    output.parent.mkdir()
    output.write_text("def generated(): pass\n", encoding="utf8")
    user = module / "src/user.py"
    user.parent.mkdir()
    user.write_text("def retained(): pass\n", encoding="utf8")
    with GraphStore(tmp_path / "graph.db") as store:
        handler = _create_watch_handler(tmp_path, store, None)
        manifest.unlink()
        handler.process([FileDeletedEvent(str(manifest))])
        handler.raise_if_failed()
        assert not store.get_nodes_by_file(str(output))
        assert store.get_nodes_by_file(str(user))
        assert _should_ignore("vendor/mod/target/generated.py", _load_ignore_patterns(tmp_path))
