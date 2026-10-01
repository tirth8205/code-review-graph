"""Manifest-aware vendor exclusions, issue #1056."""

import pytest

from code_review_graph.incremental import _load_ignore_patterns, _should_ignore


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
