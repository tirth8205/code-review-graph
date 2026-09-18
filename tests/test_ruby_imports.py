"""Ruby require/require_relative/autoload/load resolution.

One test per Ruby import form. Every assertion names the exact IMPORTS_FROM
target and requires it to be a file that exists in the repository.

The three defects being fixed, measured on jekyll: 0 of 227 ruby
IMPORTS_FROM edges resolved to a File node while 88 named a real in-repo
``.rb`` file; 49 ``autoload`` statements (the whole internal module graph)
produced no edge at all; and 34 of the 227 edges (15%) sat on lines that
were not requires, because the extractor tested ``"require" in text`` over
the whole call node.
"""

from pathlib import Path

import pytest

from code_review_graph.parser import CodeParser, normalize_file_path


def _write(root: Path, rel: str, text: str = "# frozen_string_literal: true\n") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _targets(edges) -> list[str]:
    return [e.target for e in edges if e.kind == "IMPORTS_FROM"]


def _want(path: Path) -> str:
    return normalize_file_path(path.resolve())


def _scopes(edges) -> list[object]:
    return [
        e.extra.get("import_scope")
        for e in edges if e.kind == "IMPORTS_FROM"
    ]


def _parse(repo: Path, rel: str, source: str):
    path = _write(repo, rel, source)
    parser = CodeParser(repo_root=str(repo))
    _nodes, edges = parser.parse_file(path)
    return edges


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A gem-shaped repository: gemspec require_paths plus lib/ and test/."""
    _write(
        tmp_path, "thing.gemspec",
        'Gem::Specification.new do |s|\n'
        '  s.name = "thing"\n'
        '  s.require_paths = ["lib"]\n'
        'end\n',
    )
    _write(tmp_path, "lib/thing.rb")
    _write(tmp_path, "lib/thing/filters.rb")
    _write(tmp_path, "lib/thing/version.rb")
    _write(tmp_path, "lib/thing/commands/serve/servlet.rb")
    _write(tmp_path, "test/helper.rb")
    return tmp_path


class TestRubyImportForms:
    def test_require_resolves_through_gemspec_require_paths(self, repo):
        """`require "thing/filters"` -> lib/thing/filters.rb."""
        edges = _parse(repo, "lib/thing/site.rb", 'require "thing/filters"\n')
        assert _targets(edges) == [_want(repo / "lib/thing/filters.rb")]
        assert Path(_targets(edges)[0]).is_file()

    def test_require_relative_resolves_against_the_requiring_file(self, repo):
        edges = _parse(
            repo, "test/site_test.rb", 'require_relative "../lib/thing"\n',
        )
        assert _targets(edges) == [_want(repo / "lib/thing.rb")]

    def test_require_relative_with_explicit_dot_slash(self, repo):
        _write(repo, "lib/thing/app/helper.rb")
        edges = _parse(
            repo, "lib/thing/main.rb", "require_relative './app/helper'\n",
        )
        assert _targets(edges) == [_want(repo / "lib/thing/app/helper.rb")]

    def test_require_relative_keeps_an_explicit_rb_extension(self, repo):
        edges = _parse(
            repo, "test/site_test.rb", 'require_relative "helper.rb"\n',
        )
        assert _targets(edges) == [_want(repo / "test/helper.rb")]

    def test_autoload_resolves_its_second_argument_like_require(self, repo):
        """jekyll declares its whole internal module graph with 49 autoloads."""
        edges = _parse(
            repo, "lib/thing.rb", 'autoload :Filters, "thing/filters"\n',
        )
        assert _targets(edges) == [_want(repo / "lib/thing/filters.rb")]

    def test_autoload_on_a_constant_receiver_resolves(self, repo):
        edges = _parse(
            repo, "lib/thing.rb", 'Thing.autoload :Version, "thing/version"\n',
        )
        assert _targets(edges) == [_want(repo / "lib/thing/version.rb")]

    def test_load_resolves_the_literal_path_with_its_extension(self, repo):
        edges = _parse(repo, "lib/thing.rb", 'load "thing/version.rb"\n')
        assert _targets(edges) == [_want(repo / "lib/thing/version.rb")]

    def test_gem_require_stays_a_bare_string(self, repo):
        """139 of jekyll's 227 edges name a gem or stdlib feature."""
        source = (
            'require "nokogiri"\n'
            'require "liquid"\n'
            'require "json"\n'
            'require "forwardable"\n'
        )
        edges = _parse(repo, "lib/thing/site.rb", source)
        assert _targets(edges) == ["nokogiri", "liquid", "json", "forwardable"]

    def test_test_directory_is_a_load_root(self, repo):
        edges = _parse(repo, "test/site_test.rb", 'require "helper"\n')
        assert _targets(edges) == [_want(repo / "test/helper.rb")]

    def test_spec_directory_is_a_load_root(self, tmp_path):
        _write(tmp_path, "lib/thing.rb")
        _write(tmp_path, "spec/spec_helper.rb")
        edges = _parse(tmp_path, "spec/thing_spec.rb", 'require "spec_helper"\n')
        assert _targets(edges) == [_want(tmp_path / "spec/spec_helper.rb")]

    def test_rails_app_subdirectories_are_load_roots(self, tmp_path):
        _write(tmp_path, "config/application.rb")
        _write(tmp_path, "app/models/user.rb")
        edges = _parse(
            tmp_path, "app/controllers/users_controller.rb",
            'require "models/user"\n',
        )
        assert _targets(edges) == [_want(tmp_path / "app/models/user.rb")]

    def test_load_path_unshift_adds_a_root(self, tmp_path):
        _write(tmp_path, "extras/widget.rb")
        _write(
            tmp_path, "Rakefile",
            '$LOAD_PATH.unshift File.expand_path("extras", __dir__)\n',
        )
        edges = _parse(tmp_path, "run.rb", 'require "widget"\n')
        assert _targets(edges) == [_want(tmp_path / "extras/widget.rb")]

    def test_a_require_into_an_unindexed_directory_stays_bare(self, tmp_path):
        """``**/vendor/**`` is a default ignore, so nothing in it is a node.

        Naming a file no build indexes turns a visibly external bare string
        into a confident path that resolves to nothing.
        """
        _write(tmp_path, "vendor/extra/widget.rb")
        _write(
            tmp_path, "Rakefile",
            '$LOAD_PATH.unshift File.expand_path("vendor/extra", __dir__)\n',
        )
        edges = _parse(tmp_path, "run.rb", 'require "widget"\n')
        assert _targets(edges) == ["widget"]


class TestRubyNonFabrication:
    def test_require_of_a_path_expression_emits_no_import_edge(self, repo):
        """The old regex grabbed the first quoted string: target was 'app'."""
        _write(repo, "lib/thing/app/helper.rb")
        edges = _parse(
            repo, "lib/thing/main.rb",
            'require File.join(__dir__, "app", "helper")\n',
        )
        assert _targets(edges) == []

    def test_require_of_a_variable_emits_no_import_edge(self, repo):
        edges = _parse(repo, "lib/thing/main.rb", "require path\n")
        assert _targets(edges) == []

    def test_require_of_an_interpolated_string_emits_no_import_edge(self, repo):
        edges = _parse(
            repo, "lib/thing/main.rb", 'require "thing/#{name}"\n',
        )
        assert _targets(edges) == []

    def test_raise_mentioning_required_is_not_an_import(self, repo):
        """jekyll: `raise "... Both are required."` became an IMPORTS_FROM edge."""
        source = 'raise "Missing --ssl_cert or --ssl_key. Both are required."\n'
        edges = _parse(repo, "lib/thing/main.rb", source)
        assert _targets(edges) == []

    def test_unrelated_call_containing_the_word_require_is_not_an_import(self, repo):
        """jekyll: MemoryProfiler.report(...) emitted target 'lib/jekyll/'."""
        source = (
            'MemoryProfiler.report(allow_files: ["lib/thing/"]) do\n'
            "  require_from_bundler\n"
            "end\n"
        )
        edges = _parse(repo, "lib/thing/main.rb", source)
        assert _targets(edges) == []

    def test_benchmark_report_label_is_not_an_import(self, repo):
        """jekyll: Benchmark.ips emitted target 'local-require'."""
        source = (
            "Benchmark.ips do |x|\n"
            '  x.report("local-require") { local_require }\n'
            "end\n"
        )
        edges = _parse(repo, "lib/thing/main.rb", source)
        assert _targets(edges) == []

    def test_require_relative_cannot_escape_the_repository_root(self, repo):
        edges = _parse(
            repo, "lib/thing/main.rb",
            'require_relative "../../../elsewhere"\n',
        )
        assert _targets(edges) == ["../../../elsewhere"]

    def test_resolution_is_case_exact(self, repo):
        edges = _parse(repo, "lib/thing/site.rb", 'require "thing/Filters"\n')
        assert _targets(edges) == ["thing/Filters"]

    def test_unknown_module_stays_a_bare_string(self, repo):
        edges = _parse(repo, "lib/thing/site.rb", 'require "thing/nope"\n')
        assert _targets(edges) == ["thing/nope"]


class TestRubyRequireAll:
    """`require_all "dir"` loads a whole directory TREE.

    jekyll declares six subsystems this way -- commands, converters,
    converters/markdown, drops, generators and tags -- and each was a
    single bare-string edge that matched no node.

    It is the same shape of question as a Go package import and gets the
    same answer: one edge naming the directory, expanded on the read path.
    The scope differs because the semantics do -- ``require_all`` loads every
    file BELOW the directory, while a Go package is exactly one directory --
    so these edges are tagged ``tree`` and Go's are tagged ``package``.
    """

    def test_require_all_names_the_directory_once(self, repo):
        _write(repo, "lib/thing/commands/build.rb")
        _write(repo, "lib/thing/commands/serve.rb")
        _write(repo, "lib/thing/commands/serve/servlet.rb")
        edges = _parse(repo, "lib/thing.rb", 'require_all "thing/commands"\n')
        assert _targets(edges) == [_want(repo / "lib/thing/commands")]
        assert _scopes(edges) == ["tree"]
        assert Path(_targets(edges)[0]).is_dir()

    def test_require_all_prefers_the_directory_over_a_same_named_file(self, repo):
        """lib/jekyll/filters.rb itself calls `require_all "jekyll/filters"`."""
        _write(repo, "lib/thing/filters/date_filters.rb")
        edges = _parse(repo, "lib/thing/filters.rb", 'require_all "thing/filters"\n')
        assert _targets(edges) == [_want(repo / "lib/thing/filters")]
        assert _scopes(edges) == ["tree"]

    def test_require_all_of_a_plain_file_resolves_like_require(self, repo):
        """The gem falls back to a plain require for a file argument."""
        edges = _parse(repo, "lib/thing.rb", 'require_all "thing/version"\n')
        assert _targets(edges) == [_want(repo / "lib/thing/version.rb")]
        assert _scopes(edges) == [None]

    def test_require_all_of_an_unknown_directory_stays_a_bare_string(self, repo):
        edges = _parse(repo, "lib/thing.rb", 'require_all "thing/nope"\n')
        assert _targets(edges) == ["thing/nope"]
