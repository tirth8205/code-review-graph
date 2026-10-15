"""Same-leaf classes must not overwrite each other's methods (#999, PR #1087)."""

from pathlib import Path, PureWindowsPath

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, incremental_update
from code_review_graph.parser import CodeParser

SOURCES = {
    'python': '''class Alpha:
    class Same:
        def run(self):
            self.helper()
        def helper(self):
            pass
class Beta:
    class Same:
        def run(self):
            self.helper()
        def helper(self):
            pass
''',
    'ruby': '''module Alpha
  class Same
    def run
      helper()
    end
    def helper
    end
  end
end
module Beta
  class Same
    def run
      helper()
    end
    def helper
    end
  end
end
''',
}


@pytest.mark.parametrize('language', ['python', 'ruby'])
def test_methods_and_local_calls_remain_in_their_enclosing_scope(tmp_path, language):
    path = Path('m.py' if language == 'python' else 'm.rb')
    nodes, edges = CodeParser().parse_bytes(path, SOURCES[language].encode())
    with GraphStore(tmp_path / 'graph.db') as store:
        store.store_file_nodes_edges(path.as_posix(), nodes, edges)
        methods = [n for n in store.get_nodes_by_file(path.as_posix()) if n.kind == 'Function']
        assert {n.qualified_name for n in methods} == {
            f'{path}::{scope}.Same.{method}'
            for scope in ['Alpha', 'Beta'] for method in ['run', 'helper']
        }
        for scope in ['Alpha', 'Beta']:
            run = f'{path}::{scope}.Same.run'
            assert {e.target_qualified for e in store.get_edges_by_source(run)
                    if e.kind == 'CALLS'} == {f'{path}::{scope}.Same.helper'}
            assert any(e.source_qualified == f'{path}::{scope}.Same'
                       for e in store.get_edges_by_target(run) if e.kind == 'CONTAINS')


@pytest.mark.parametrize('language', ['python', 'ruby'])
def test_unique_leaf_method_identities_keep_their_existing_spelling(language):
    path = Path('m.py' if language == 'python' else 'm.rb')
    source = SOURCES[language].split('class Beta:' if language == 'python' else 'module Beta')[0]
    parser = CodeParser()
    nodes, _ = parser.parse_bytes(path, source.encode())
    methods = [n for n in nodes if n.kind == 'Function']
    assert {parser._node_qualified(n) for n in methods} == {
        f'{path}::Same.run', f'{path}::Same.helper'
    }


@pytest.mark.parametrize('language', ['python', 'ruby'])
def test_missing_member_does_not_bind_to_the_other_class(language):
    source = SOURCES[language]
    if language == 'python':
        source = source.rsplit('        def helper(self):\n            pass\n', 1)[0]
    else:
        source = source.rsplit('    def helper\n    end\n', 1)[0] + '  end\nend\n'
    path = Path('m.py' if language == 'python' else 'm.rb')
    _, edges = CodeParser().parse_bytes(path, source.encode())
    calls = [e for e in edges if e.kind == 'CALLS' and e.source == f'{path}::Beta.Same.run']
    assert calls
    assert all(e.target != f'{path}::Alpha.Same.helper' for e in calls)


# ---------------------------------------------------------------------------
# Edge cases added while hardening PR #1087
# ---------------------------------------------------------------------------


def _parse(path, source):
    parser = CodeParser()
    if isinstance(path, str):
        path = Path(path)
    nodes, edges = parser.parse_bytes(path, source.encode())
    return parser, nodes, edges


def _methods(parser, nodes):
    return {parser._node_qualified(n) for n in nodes if n.kind in ('Function', 'Test')}


def _calls_from(edges, source):
    return {e.target for e in edges if e.kind == 'CALLS' and e.source == source}


def _contains_sources(edges, target):
    return {e.source for e in edges if e.kind == 'CONTAINS' and e.target == target}


@pytest.mark.parametrize('path, source, expected', [
    ('m.py', '', set()),
    ('m.rb', '', set()),
    ('m.py', 'def run():\n    pass\n', {'m.py::run'}),
    ('m.rb', 'def run\nend\n', {'m.rb::run'}),
], ids=['empty-python', 'empty-ruby', 'functions-only-python', 'functions-only-ruby'])
def test_files_without_classes_are_unaffected(path, source, expected):
    parser, nodes, _ = _parse(path, source)
    assert [n.kind for n in nodes if n.kind == 'File'] == ['File']
    assert _methods(parser, nodes) == expected


DEEP = {
    'python': '''class A:
    class B:
        class Same:
            def run(self):
                self.helper()
            def helper(self):
                pass
class C:
    class Same:
        def run(self):
            self.helper()
        def helper(self):
            pass
''',
    'ruby': '''module A
  module B
    class Same
      def run
        helper()
      end
      def helper
      end
    end
  end
end
module C
  class Same
    def run
      helper()
    end
    def helper
    end
  end
end
''',
}


@pytest.mark.parametrize('language', ['python', 'ruby'])
def test_deeper_enclosing_chains_keep_methods_apart(language):
    path = 'm.py' if language == 'python' else 'm.rb'
    parser, nodes, edges = _parse(path, DEEP[language])
    assert _methods(parser, nodes) == {
        f'{path}::A.B.Same.run', f'{path}::A.B.Same.helper',
        f'{path}::C.Same.run', f'{path}::C.Same.helper',
    }
    assert _calls_from(edges, f'{path}::A.B.Same.run') == {f'{path}::A.B.Same.helper'}
    assert _calls_from(edges, f'{path}::C.Same.run') == {f'{path}::C.Same.helper'}


RUBY_NESTED_USER = '''module Admin
  class User
    def save
      validate()
    end
    def validate
    end
  end
end
'''
RUBY_TOP_LEVEL_USER = '''class User
  def save
    validate()
    self.validate
  end
  def validate
  end
end
'''


@pytest.mark.parametrize('nested_first', [
    pytest.param(True, id='nested-first'),
    pytest.param(False, id='nested-last'),
])
def test_top_level_ruby_class_calls_stay_in_that_class(nested_first):
    """A top-level class sharing its leaf with a nested one keeps its own calls.

    ``User#save`` calling ``validate`` (implicitly or through ``self``) means
    ``User#validate``, never ``Admin::User#validate``, whichever class the
    file declares first.
    """
    source = (
        RUBY_NESTED_USER + RUBY_TOP_LEVEL_USER if nested_first
        else RUBY_TOP_LEVEL_USER + RUBY_NESTED_USER
    )
    parser, nodes, edges = _parse('m.rb', source)
    assert _methods(parser, nodes) == {
        'm.rb::Admin.User.save', 'm.rb::Admin.User.validate',
        'm.rb::User.save', 'm.rb::User.validate',
    }
    assert _calls_from(edges, 'm.rb::Admin.User.save') == {'m.rb::Admin.User.validate'}
    assert _calls_from(edges, 'm.rb::User.save') == {'m.rb::User.validate'}


def test_top_level_python_class_self_calls_stay_in_that_class():
    source = '''class Beta:
    class Same:
        def run(self):
            self.helper()
        def helper(self):
            pass
class Same:
    def run(self):
        self.helper()
    def helper(self):
        pass
'''
    parser, nodes, edges = _parse('m.py', source)
    assert _methods(parser, nodes) == {
        'm.py::Beta.Same.run', 'm.py::Beta.Same.helper',
        'm.py::Same.run', 'm.py::Same.helper',
    }
    assert _calls_from(edges, 'm.py::Same.run') == {'m.py::Same.helper'}
    assert _calls_from(edges, 'm.py::Beta.Same.run') == {'m.py::Beta.Same.helper'}


def test_decorated_async_and_nested_definitions_use_the_full_scope():
    source = '''class Alpha:
    class Same:
        @staticmethod
        def build():
            return 1
        async def fetch(self):
            await self.load()
        async def load(self):
            pass
        @property
        def value(self):
            return 1
        def run(self):
            def inner():
                return 1
            return inner()
class Beta:
    class Same:
        @staticmethod
        def build():
            return 2
        async def fetch(self):
            await self.load()
        async def load(self):
            pass
        def run(self):
            def inner():
                return 2
            return inner()
'''
    parser, nodes, edges = _parse('m.py', source)
    expected = {
        f'm.py::{scope}.Same.{name}'
        for scope in ['Alpha', 'Beta']
        for name in ['build', 'fetch', 'load', 'run', 'inner']
    } | {'m.py::Alpha.Same.value'}
    assert _methods(parser, nodes) == expected
    for scope in ['Alpha', 'Beta']:
        assert _calls_from(edges, f'm.py::{scope}.Same.fetch') == {f'm.py::{scope}.Same.load'}
        assert _calls_from(edges, f'm.py::{scope}.Same.run') == {f'm.py::{scope}.Same.inner'}
        assert _contains_sources(edges, f'm.py::{scope}.Same.build') == {f'm.py::{scope}.Same'}


def test_same_leaf_test_classes_keep_distinct_test_nodes():
    source = '''class TestAlpha:
    class Same:
        def test_run(self):
            self.check()
        def check(self):
            pass
class TestBeta:
    class Same:
        def test_run(self):
            self.check()
        def check(self):
            pass
'''
    path = 'tests/test_same.py'
    parser, nodes, edges = _parse(path, source)
    tests = {parser._node_qualified(n) for n in nodes if n.kind == 'Test'}
    assert tests == {f'{path}::TestAlpha.Same.test_run', f'{path}::TestBeta.Same.test_run'}
    for scope in ['TestAlpha', 'TestBeta']:
        assert _calls_from(edges, f'{path}::{scope}.Same.test_run') == {
            f'{path}::{scope}.Same.check'
        }


def test_ruby_singleton_methods_in_same_leaf_classes_stay_apart():
    source = '''module Alpha
  class Same
    def self.build
      create()
    end
    def self.create
    end
  end
end
module Beta
  class Same
    def self.build
      create()
    end
    def self.create
    end
  end
end
'''
    parser, nodes, edges = _parse('m.rb', source)
    assert _methods(parser, nodes) == {
        f'm.rb::{scope}.Same.{name}' for scope in ['Alpha', 'Beta'] for name in ['build', 'create']
    }
    for scope in ['Alpha', 'Beta']:
        assert _calls_from(edges, f'm.rb::{scope}.Same.build') == {f'm.rb::{scope}.Same.create'}


def test_unicode_identifiers_and_paths_with_spaces():
    source = '''class Äußere:
    class Gleich:
        def lauf(self):
            self.hilfe()
        def hilfe(self):
            pass
class Ändere:
    class Gleich:
        def lauf(self):
            self.hilfe()
        def hilfe(self):
            pass
'''
    path = 'dir with space/ünï.py'
    parser, nodes, edges = _parse(path, source)
    assert _methods(parser, nodes) == {
        f'{path}::{scope}.Gleich.{name}'
        for scope in ['Äußere', 'Ändere'] for name in ['lauf', 'hilfe']
    }
    for scope in ['Äußere', 'Ändere']:
        assert _calls_from(edges, f'{path}::{scope}.Gleich.lauf') == {
            f'{path}::{scope}.Gleich.hilfe'
        }
        assert _contains_sources(edges, f'{path}::{scope}.Gleich.lauf') == {
            f'{path}::{scope}.Gleich'
        }


@pytest.mark.parametrize('language', ['python', 'ruby'])
def test_windows_paths_produce_posix_scoped_identities(language):
    suffix = 'py' if language == 'python' else 'rb'
    parser = CodeParser()
    nodes, edges = parser.parse_bytes(
        PureWindowsPath(rf'C:\repo\pkg\m.{suffix}'), SOURCES[language].encode(),
    )
    path = f'C:/repo/pkg/m.{suffix}'
    assert _methods(parser, nodes) == {
        f'{path}::{scope}.Same.{name}' for scope in ['Alpha', 'Beta'] for name in ['run', 'helper']
    }
    for scope in ['Alpha', 'Beta']:
        assert _contains_sources(edges, f'{path}::{scope}.Same.run') == {f'{path}::{scope}.Same'}


@pytest.mark.parametrize('language', ['python', 'ruby'])
def test_many_same_leaf_classes_in_one_file(language):
    count = 300
    if language == 'python':
        source = ''.join(
            f'class Model{i}:\n    class Meta:\n        def describe(self):\n'
            f'            self.label()\n        def label(self):\n            pass\n'
            for i in range(count)
        )
        path = 'models.py'
    else:
        source = ''.join(
            f'module Model{i}\n  class Meta\n    def describe\n      label()\n    end\n'
            f'    def label\n    end\n  end\nend\n'
            for i in range(count)
        )
        path = 'models.rb'
    parser, nodes, edges = _parse(path, source)
    methods = _methods(parser, nodes)
    assert len(methods) == 2 * count
    for i in (0, count // 2, count - 1):
        assert _calls_from(edges, f'{path}::Model{i}.Meta.describe') == {
            f'{path}::Model{i}.Meta.label'
        }


@pytest.mark.parametrize('path, source', [
    ('m.rb', '''module Alpha
  class Same
    def run
    end
  end
end
module Alpha
  class Same
    def helper
    end
  end
end
'''),
    ('m.py', '''if FLAG:
    class Same:
        def run(self):
            pass
else:
    class Same:
        def helper(self):
            pass
'''),
], ids=['ruby-reopened', 'python-conditional'])
def test_reopened_or_redefined_class_keeps_the_existing_spelling(path, source):
    """The same full scope declared twice is not a leaf collision."""
    parser, nodes, _ = _parse(path, source)
    assert _methods(parser, nodes) == {f'{path}::Same.run', f'{path}::Same.helper'}


def test_parser_reuse_does_not_leak_scopes_between_files():
    parser = CodeParser()
    first_nodes, _ = parser.parse_bytes(Path('a.py'), SOURCES['python'].encode())
    unique = SOURCES['python'].split('class Beta:')[0]
    nodes, _ = parser.parse_bytes(Path('b.py'), unique.encode())
    assert _methods(parser, nodes) == {'b.py::Same.run', 'b.py::Same.helper'}
    again, _ = parser.parse_bytes(Path('a.py'), SOURCES['python'].encode())
    assert _methods(parser, again) == _methods(parser, first_nodes) == {
        f'a.py::{scope}.Same.{name}' for scope in ['Alpha', 'Beta'] for name in ['run', 'helper']
    }


def test_syntax_error_elsewhere_keeps_scoped_identities():
    source = SOURCES['python'] + 'class Gamma:\n    def broken(self:\n'
    parser, nodes, edges = _parse('m.py', source)
    assert {
        f'm.py::{scope}.Same.{name}' for scope in ['Alpha', 'Beta'] for name in ['run', 'helper']
    } <= _methods(parser, nodes)
    assert _calls_from(edges, 'm.py::Beta.Same.run') == {'m.py::Beta.Same.helper'}


def test_ruby_bare_call_prefers_a_top_level_method_when_no_class_defines_it():
    source = '''def helper
end
module Alpha
  class Same
    def run
      helper()
    end
  end
end
module Beta
  class Same
    def run
      helper()
    end
  end
end
'''
    _, _, edges = _parse('m.rb', source)
    for scope in ['Alpha', 'Beta']:
        assert _calls_from(edges, f'm.rb::{scope}.Same.run') == {'m.rb::helper'}


@pytest.mark.parametrize('language, source', [
    ('java', b'class Alpha { class Same { void run() {} } }\n'
             b'class Beta { class Same { void run() {} } }\n'),
    ('csharp', b'class Alpha { class Same { void Run() {} } }\n'
               b'class Beta { class Same { void Run() {} } }\n'),
], ids=['java', 'csharp'])
def test_scope_index_is_limited_to_python_and_ruby(language, source):
    """Other languages (C# identity is #964's) are left to their own scheme."""
    parser = CodeParser()
    tree = parser._get_parser(language).parse(source)
    import_map = {}
    parser._index_ambiguous_method_scopes(tree.root_node, language, import_map)
    assert import_map == {}


def _write_repo(root):
    (root / 'm.py').write_text(SOURCES['python'], encoding='utf-8')
    (root / 'test_m.py').write_text(
        'from m import Alpha\n\n\ndef test_alpha_run():\n    Alpha.Same().run()\n',
        encoding='utf-8',
    )


def _graph_snapshot(store):
    nodes = {n.qualified_name for n in store.get_all_nodes(exclude_files=False)}
    edges = {
        (e.kind, e.source_qualified, e.target_qualified) for e in store.get_all_edges()
    }
    return nodes, edges


@pytest.mark.parametrize('refresh', ['edit-and-update', 'rebuild'])
def test_rows_written_with_the_old_identity_are_replaced(tmp_path, monkeypatch, refresh):
    """A graph built before #1087 is repaired when the file is re-parsed.

    ``update`` re-parses a file once its content changes; ``build`` re-parses
    everything. Either way no row or edge keeps the collapsed spelling.
    """
    monkeypatch.setenv('CRG_SERIAL_PARSE', '1')
    _write_repo(tmp_path)
    m = (tmp_path / 'm.py').as_posix()
    with GraphStore(tmp_path / 'graph.db') as store:
        # Earlier releases spelled every method with its leaf class only.
        with monkeypatch.context() as legacy:
            legacy.setattr(
                CodeParser, '_index_ambiguous_method_scopes',
                lambda self, root, language, import_map: None, raising=False,
            )
            full_build(tmp_path, store)
        old = {n.qualified_name for n in store.get_nodes_by_file(m) if n.kind == 'Function'}
        assert old == {f'{m}::Same.run', f'{m}::Same.helper'}

        if refresh == 'rebuild':
            full_build(tmp_path, store)
        else:
            with (tmp_path / 'm.py').open('a', encoding='utf-8') as handle:
                handle.write('# edited\n')
            incremental_update(tmp_path, store, changed_files=['m.py'])

        methods = {n.qualified_name for n in store.get_nodes_by_file(m) if n.kind == 'Function'}
        assert methods == {
            f'{m}::{scope}.Same.{name}' for scope in ['Alpha', 'Beta'] for name in ['run', 'helper']
        }
        nodes, edges = _graph_snapshot(store)
        test_fn = f'{(tmp_path / "test_m.py").as_posix()}::test_alpha_run'
        for kind, source, target in edges:
            if kind in ('CALLS', 'CONTAINS', 'TESTED_BY'):
                for endpoint in (source, target):
                    if endpoint.startswith(f'{m}::'):
                        assert endpoint in nodes, (kind, source, target)
            # The test calls Alpha's method; it must never be attributed to Beta.
            if kind == 'CALLS' and source == test_fn:
                assert target != f'{m}::Beta.Same.run'
            if kind == 'TESTED_BY' and target == test_fn:
                assert source != f'{m}::Beta.Same.run'


def test_full_build_twice_and_incremental_agree(tmp_path, monkeypatch):
    monkeypatch.setenv('CRG_SERIAL_PARSE', '1')
    _write_repo(tmp_path)
    with GraphStore(tmp_path / 'graph.db') as store:
        full_build(tmp_path, store)
        first = _graph_snapshot(store)
        full_build(tmp_path, store)
        assert _graph_snapshot(store) == first
        incremental_update(tmp_path, store, changed_files=['m.py', 'test_m.py'])
        assert _graph_snapshot(store) == first
