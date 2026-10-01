"""Unknown graph paths must not receive a low-risk all-clear (#983)."""

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.parser import EdgeInfo, NodeInfo
from code_review_graph.tools import query, review
from code_review_graph.tools._common import _resolve_graph_file_paths


@pytest.fixture
def graph(tmp_path, monkeypatch):
    store = GraphStore(tmp_path / 'graph.db')
    path = (tmp_path / 'src' / 'app.py').as_posix()
    store.upsert_node(NodeInfo('File', path, path, 1, 3, 'python'))
    store.upsert_node(NodeInfo('Function', 'handle', path, 1, 3, 'python'))
    for index in range(30):
        caller = (tmp_path / f'caller{index}.py').as_posix()
        store.upsert_node(NodeInfo('Function', f'caller{index}', caller, 1, 2, 'python'))
        store.upsert_edge(EdgeInfo('CALLS', f'{caller}::caller{index}',
                                  f'{path}::handle', caller, 2))
    store.commit()
    monkeypatch.setattr(query, '_get_store', lambda _: (store, tmp_path))
    monkeypatch.setattr(review, '_get_store', lambda _: (store, tmp_path))
    monkeypatch.setattr(review, 'resolve_review_base', lambda root, base: base)
    yield store, tmp_path
    store.close()


@pytest.mark.parametrize('tool', [query.get_impact_radius, review.get_review_context])
@pytest.mark.parametrize('detail', ['minimal', 'standard'])
@pytest.mark.parametrize('path', ['src/ap.py', 'src/ghost.py', 'src/app.py::handle'])
def test_unmatched_path_is_unknown(graph, tool, detail, path):
    result = tool(changed_files=[path], detail_level=detail)
    assert result['risk'] == 'unknown'
    assert result['unmatched_files'] == [path]
    assert 'not indexed' in result['summary'].lower()


@pytest.mark.parametrize('tool', [query.get_impact_radius, review.get_review_context])
@pytest.mark.parametrize('detail', ['minimal', 'standard'])
def test_partial_match_keeps_measured_risk_and_names_miss(graph, tool, detail):
    result = tool(changed_files=['src/app.py', 'missing.py'], detail_level=detail)
    assert result['risk'] == 'high'
    assert result['unmatched_files'] == ['missing.py']


def test_compatibility_wrapper_resolves_duplicate_relative_and_absolute_paths(graph):
    store, root = graph
    absolute = (root / 'src' / 'app.py').as_posix()
    assert _resolve_graph_file_paths(store, root, ['src/app.py', absolute]) == [absolute]
