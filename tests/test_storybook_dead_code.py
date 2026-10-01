"""Storybook consumers do not prove production use (issue #902)."""

from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.parser import CodeParser, EdgeInfo, NodeInfo, is_test_file
from code_review_graph.refactor import find_dead_code


@pytest.mark.parametrize('extension', ['js', 'jsx', 'ts', 'tsx'])
def test_story_filename_is_nonproduction(extension):
    path = Path(f'components/Foo.stories.{extension}')
    assert is_test_file(path)
    nodes, _ = CodeParser().parse_bytes(path, b'export function Story() { return 1; }')
    assert nodes and all(node.is_test for node in nodes)


@pytest.mark.parametrize('path', ['stories/Foo.tsx', 'Foo.stories.py', 'Foo.stories.tsx.bak'])
def test_story_like_production_paths_remain_production(path):
    assert not is_test_file(path)


@pytest.mark.parametrize('production_caller', [False, True])
def test_only_story_callers_do_not_keep_component_alive(tmp_path, production_caller):
    with GraphStore(tmp_path / 'graph.db') as store:
        component = 'components/Foo.tsx'
        story = 'components/Foo.stories.tsx'
        store.upsert_node(NodeInfo('Function', 'Foo', component, 1, 3, 'tsx'))
        # False metadata models a graph built before stories were classified.
        store.upsert_node(NodeInfo('File', story, story, 1, 5, 'tsx', is_test=False))
        store.upsert_edge(EdgeInfo('IMPORTS_FROM', story, component, story, 1))
        store.upsert_edge(EdgeInfo('CALLS', story, f'{component}::Foo', story, 3))
        if production_caller:
            store.upsert_edge(EdgeInfo('CALLS', 'app.tsx::App', f'{component}::Foo',
                                      'app.tsx', 3))
        store.commit()
        dead = {node['name'] for node in find_dead_code(store, root=tmp_path)}
        assert ('Foo' in dead) is not production_caller


def test_story_call_does_not_hide_bare_production_caller_fallback(tmp_path):
    with GraphStore(tmp_path / 'graph.db') as store:
        store.upsert_node(NodeInfo('Function', 'Foo', 'Foo.tsx', 1, 3, 'tsx'))
        store.upsert_node(NodeInfo('File', 'app.tsx', 'app.tsx', 1, 3, 'tsx'))
        store.upsert_node(NodeInfo('Function', 'App', 'app.tsx', 1, 3, 'tsx'))
        store.upsert_edge(EdgeInfo('CALLS', 'Foo.stories.tsx', 'Foo.tsx::Foo',
                                  'Foo.stories.tsx', 2))
        store.upsert_edge(EdgeInfo('CALLS', 'app.tsx::App', 'Foo', 'app.tsx', 2))
        store.commit()
        assert not any(n['name'] == 'Foo' for n in find_dead_code(store, root=tmp_path))


def test_story_member_calls_do_not_keep_class_alive(tmp_path):
    with GraphStore(tmp_path / 'graph.db') as store:
        store.upsert_node(NodeInfo('Class', 'Widget', 'Widget.tsx', 1, 5, 'tsx'))
        store.upsert_edge(EdgeInfo('CALLS', 'Widget.stories.tsx', 'Widget.tsx::Widget.render',
                                  'Widget.stories.tsx', 3))
        store.commit()
        assert any(n['name'] == 'Widget' for n in find_dead_code(store, root=tmp_path))
