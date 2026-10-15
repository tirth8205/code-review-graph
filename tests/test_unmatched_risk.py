"""Unknown graph paths must not receive a low-risk all-clear (#983, PR #1084)."""

import json
import subprocess
import sys

import pytest

from code_review_graph import cli
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, get_db_path, incremental_update
from code_review_graph.parser import EdgeInfo, NodeInfo
from code_review_graph.tools import query, review
from code_review_graph.tools._common import _resolve_graph_file_paths, _unmatched_file_warning


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


# ---------------------------------------------------------------------------
# Edge cases added while hardening PR #1084
# ---------------------------------------------------------------------------

TOOLS = [query.get_impact_radius, review.get_review_context]
DETAILS = ['minimal', 'standard']


@pytest.fixture
def reopening_graph(graph, monkeypatch):
    """The ``graph`` fixture, but each tool call opens its own store.

    The tools close the store they are handed, so a test that calls a tool
    more than once needs a fresh connection per call.
    """
    _store, root = graph

    def open_store(_repo_root):
        return GraphStore(root / 'graph.db'), root

    monkeypatch.setattr(query, '_get_store', open_store)
    monkeypatch.setattr(review, '_get_store', open_store)
    return graph


@pytest.mark.parametrize('tool', TOOLS)
@pytest.mark.parametrize('detail', DETAILS)
def test_fully_indexed_request_reports_measured_risk_and_no_misses(graph, tool, detail):
    result = tool(changed_files=['src/app.py'], detail_level=detail)
    assert result['risk'] == 'high'
    assert result['unmatched_files'] == []
    assert 'not indexed' not in result['summary']


@pytest.mark.parametrize('tool', TOOLS)
@pytest.mark.parametrize('detail', DETAILS)
@pytest.mark.parametrize('spelling', ['src\\app.py', './src/app.py', 'ABSOLUTE'])
def test_other_spellings_of_an_indexed_file_are_not_misses(graph, tool, detail, spelling):
    _store, root = graph
    if spelling == 'ABSOLUTE':
        spelling = str(root / 'src' / 'app.py')
    result = tool(changed_files=[spelling], detail_level=detail)
    assert result['risk'] == 'high'
    assert result['unmatched_files'] == []


@pytest.mark.parametrize('tool', TOOLS)
@pytest.mark.parametrize('detail', DETAILS)
def test_absolute_path_outside_the_repository_is_a_miss(graph, tool, detail, tmp_path_factory):
    outside = str(tmp_path_factory.mktemp('elsewhere') / 'src' / 'app.py')
    result = tool(changed_files=[outside], detail_level=detail)
    assert result['risk'] == 'unknown'
    assert result['unmatched_files'] == [outside]


@pytest.mark.parametrize('tool', TOOLS)
@pytest.mark.parametrize('detail', DETAILS)
def test_duplicate_missing_paths_are_reported_once(graph, tool, detail):
    result = tool(changed_files=['ghost.py', 'ghost.py'], detail_level=detail)
    assert result['risk'] == 'unknown'
    assert result['unmatched_files'] == ['ghost.py']
    assert '1 requested file(s) are not indexed: ghost.py' in result['summary']


@pytest.mark.parametrize('tool', TOOLS)
@pytest.mark.parametrize('detail', DETAILS)
def test_missing_paths_keep_the_callers_spelling(graph, tool, detail):
    requested = ['dir with space/nöpe ü.py', 'src\\ghost.py']
    result = tool(changed_files=requested, detail_level=detail)
    assert result['risk'] == 'unknown'
    assert result['unmatched_files'] == requested
    for path in requested:
        assert path in result['summary']


@pytest.mark.parametrize('tool', TOOLS)
def test_unicode_path_with_spaces_that_is_indexed_matches(graph, tool):
    store, root = graph
    path = (root / 'dir with space' / 'ünï.py').as_posix()
    store.upsert_node(NodeInfo('File', path, path, 1, 2, 'python'))
    store.upsert_node(NodeInfo('Function', 'uni', path, 1, 2, 'python'))
    store.commit()
    result = tool(changed_files=['dir with space/ünï.py'], detail_level='minimal')
    assert result['risk'] == 'low'
    assert result['unmatched_files'] == []


@pytest.mark.parametrize('tool', TOOLS)
def test_repo_relative_stored_paths_still_match(graph, tool):
    """Graphs may store repo-relative paths (see the resolver docstring)."""
    store, _root = graph
    store.upsert_node(NodeInfo('File', 'lib/util.py', 'lib/util.py', 1, 2, 'python'))
    store.upsert_node(NodeInfo('Function', 'helper', 'lib/util.py', 1, 2, 'python'))
    store.commit()
    result = tool(changed_files=['lib/util.py'], detail_level='minimal')
    assert result['risk'] == 'low'
    assert result['unmatched_files'] == []


def test_warning_names_at_most_five_paths():
    missing = [f'gone{index}.py' for index in range(7)]
    warning = _unmatched_file_warning(missing)
    assert warning.startswith('  - 7 requested file(s) are not indexed: ')
    assert 'gone4.py (+2 more)' in warning
    assert 'gone5.py' not in warning and 'gone6.py' not in warning
    assert _unmatched_file_warning([]) == ''


@pytest.mark.parametrize('tool', TOOLS)
@pytest.mark.parametrize('detail', DETAILS)
def test_many_misses_are_counted_in_full_in_the_summary(graph, tool, detail):
    missing = [f'gone{index}.py' for index in range(7)]
    result = tool(changed_files=['src/app.py', *missing], detail_level=detail)
    assert result['risk'] == 'high'
    assert '7 requested file(s) are not indexed' in result['summary']
    assert '(+2 more)' in result['summary']


@pytest.mark.parametrize('tool', TOOLS)
@pytest.mark.parametrize('detail', DETAILS)
def test_unmatched_paths_report_bounded_sample_and_full_count(graph, tool, detail):
    missing = [f'assets/missing_{index}.json' for index in range(250)]
    result = tool(changed_files=['src/app.py', *missing], detail_level=detail)
    assert result['unmatched_files_total'] == len(missing)
    assert result['unmatched_files_truncated'] is True
    assert result['unmatched_files'] == missing[:len(result['unmatched_files'])]
    assert 0 < len(result['unmatched_files']) <= (5 if detail == 'minimal' else 200)
    assert result['risk'] == 'high'


@pytest.mark.parametrize('tool', TOOLS)
def test_unmatched_paths_respect_explicit_response_bounds(graph, tool):
    bound = {'max_results': 2} if tool is query.get_impact_radius else {'max_files': 2}
    result = tool(changed_files=['one.py', 'two.py', 'three.py'], **bound)
    assert result['unmatched_files'] == ['one.py', 'two.py']
    assert result['unmatched_files_total'] == 3
    assert result['unmatched_files_truncated'] is True


@pytest.mark.parametrize('tool', TOOLS)
def test_minimal_response_does_not_grow_with_the_number_of_misses(reopening_graph, tool):
    """A minimal response must stay bounded however many paths miss.

    An auto-detected diff carries every changed docs, lockfile, asset and
    data file, none of which is indexed. ``detail_level="minimal"`` is the
    ~100-token answer the agent workflow is built on, and every response
    list carries a fixed ceiling (see tests/test_token_budget.py), so the
    misses may be counted in full but must not be echoed in full.
    """
    def size(count):
        missing = [f'assets/locales/messages_{index:05d}.json' for index in range(count)]
        result = tool(changed_files=['src/app.py', *missing], detail_level='minimal')
        assert f'{count} requested file(s) are not indexed' in result['summary']
        return len(json.dumps(result, default=str))

    # Both counts sit above any sensible display cap, so a capped list costs
    # the same at either size and only the reported counts differ.
    fewer, more = size(1_000), size(3_000)
    assert more - fewer < 500, (
        f'minimal response grew from {fewer} to {more} characters between '
        f'1,000 and 3,000 unindexed paths'
    )


@pytest.mark.parametrize('tool', TOOLS)
@pytest.mark.parametrize('detail', DETAILS)
def test_repeated_calls_give_the_same_answer(reopening_graph, tool, detail):
    request = ['src/app.py', 'missing.py']
    first = tool(changed_files=list(request), detail_level=detail)
    second = tool(changed_files=list(request), detail_level=detail)
    for key in ('risk', 'unmatched_files', 'summary'):
        assert first[key] == second[key]


def test_empty_request_keeps_the_no_changes_answer(reopening_graph):
    impact = query.get_impact_radius(changed_files=[])
    assert impact['status'] == 'ok'
    assert impact['summary'] == 'No changed files detected.'
    assert 'unmatched_files' not in impact
    context = review.get_review_context(changed_files=[])
    assert context['status'] == 'ok'
    assert context['summary'] == 'No changes detected. Nothing to review.'
    assert 'unmatched_files' not in context


@pytest.mark.xfail(
    strict=True,
    reason=(
        'pre-existing, not introduced by #1084: get_files_matching() builds an '
        'unescaped LIKE suffix, so an empty path resolves to every file in the '
        'graph and the request reads as a low-risk all-clear'
    ),
)
@pytest.mark.parametrize('tool', TOOLS)
def test_empty_string_path_is_not_an_all_clear(graph, tool):
    result = tool(changed_files=['src/app.py', ''], detail_level='minimal')
    assert result['risk'] != 'low'


# ---------------------------------------------------------------------------
# The same contract through a real build, update and Git change discovery
# ---------------------------------------------------------------------------


def _git(repo, *args):
    result = subprocess.run(
        ['git', *args], cwd=str(repo), capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return result


@pytest.fixture
def built_repo(tmp_path, monkeypatch):
    monkeypatch.delenv('CRG_DATA_DIR', raising=False)
    repo = tmp_path / 'repo'
    (repo / 'src').mkdir(parents=True)
    _git(repo, 'init', '-q')
    _git(repo, 'config', 'user.email', 'test@test.com')
    _git(repo, 'config', 'user.name', 'Test')
    (repo / 'src' / '__init__.py').write_text('', encoding='utf-8')
    (repo / 'src' / 'app.py').write_text('def handle(x):\n    return x\n', encoding='utf-8')
    for index in range(8):
        (repo / 'src' / f'caller{index}.py').write_text(
            'from src.app import handle\n\n\n'
            f'def caller{index}():\n    return handle({index})\n',
            encoding='utf-8',
        )
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-q', '-m', 'init')
    store = GraphStore(get_db_path(repo))
    try:
        full_build(repo, store)
    finally:
        store.close()
    return repo


MEASURED = {'low', 'medium', 'high'}


@pytest.mark.parametrize('tool', TOOLS)
def test_file_added_after_the_build_is_unknown_until_the_graph_is_updated(built_repo, tool):
    repo = built_repo
    (repo / 'src' / 'fresh.py').write_text('def fresh():\n    return 1\n', encoding='utf-8')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-q', '-m', 'add fresh')

    stale = tool(changed_files=['src/fresh.py'], repo_root=str(repo), detail_level='minimal')
    assert stale['risk'] == 'unknown'
    assert stale['unmatched_files'] == ['src/fresh.py']

    store = GraphStore(get_db_path(repo))
    try:
        incremental_update(repo, store, base='HEAD~1')
    finally:
        store.close()
    fresh = tool(changed_files=['src/fresh.py'], repo_root=str(repo), detail_level='minimal')
    assert fresh['risk'] in MEASURED
    assert fresh['unmatched_files'] == []


@pytest.mark.parametrize('tool', TOOLS)
@pytest.mark.parametrize('detail', DETAILS)
def test_auto_detected_change_to_unindexed_files_only_is_unknown(built_repo, tool, detail):
    repo = built_repo
    (repo / 'README.md').write_text('# docs\n', encoding='utf-8')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-q', '-m', 'docs only')

    result = tool(repo_root=str(repo), detail_level=detail)
    assert result['status'] == 'ok'
    assert result['risk'] == 'unknown'
    assert result['unmatched_files'] == ['README.md']


@pytest.mark.parametrize('tool', TOOLS)
@pytest.mark.parametrize('detail', DETAILS)
def test_auto_detected_mixed_change_keeps_measured_risk(built_repo, tool, detail):
    repo = built_repo
    (repo / 'README.md').write_text('# docs\n', encoding='utf-8')
    (repo / 'src' / 'app.py').write_text('def handle(x):\n    return x + 1\n', encoding='utf-8')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-q', '-m', 'code and docs')

    result = tool(repo_root=str(repo), detail_level=detail)
    assert result['risk'] in MEASURED
    assert result['unmatched_files'] == ['README.md']


def test_cli_impact_reports_unknown_for_a_missing_path(built_repo, monkeypatch, capsys):
    argv = ['code-review-graph', 'impact', '--files', 'src/ap.py', '--repo', str(built_repo)]
    monkeypatch.setattr(sys, 'argv', argv)
    cli.main()
    result = json.loads(capsys.readouterr().out)
    assert result['risk'] == 'unknown'
    assert result['unmatched_files'] == ['src/ap.py']
