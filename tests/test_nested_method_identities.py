"""Same-leaf classes must not overwrite each other's methods (#999)."""

from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore
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
