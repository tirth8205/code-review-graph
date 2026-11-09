"""Re-parsing an unchanged importer must not wipe its flows and communities.

PR #1062 made a real content change to a file re-parse every file that
imports it (up to ``_MAX_DEPENDENT_FILES``). Re-parsing replaces the
importer's node rows, which gives them new ids and clears ``community_id``.
The update path that ``code-review-graph update`` and the
``build_or_update_graph`` MCP tool share then re-traces flows and re-detects
communities only for ``result["changed_files"]``, so the importer's flows keep
pointing at node ids that no longer exist and its nodes drop out of every
community. A comment-only edit to a widely imported module would empty the
flows and community membership of all its importers.
"""
import subprocess
from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, incremental_update
from code_review_graph.tools.build import build_or_update_graph
from code_review_graph.tools.flows_tools import get_flow
from code_review_graph.tools.review import get_affected_flows_func

FILES = {
    'fee.ts': 'export function calculateFee(n: number) { return n + 2; }\n',
    # use.ts imports fee.ts, so it is a dependent of fee.ts. Its own bytes
    # never change in this test.
    'use.ts': (
        "import { calculateFee } from './fee';\n"
        'export function main() { return step1(); }\n'
        'function step1() { return step2(); }\n'
        'function step2() { return helper(); }\n'
        'function helper() { return calculateFee(3); }\n'
    ),
    'other.ts': (
        'export function runOther() { return a1(); }\n'
        'function a1() { return a2(); }\n'
        'function a2() { return 1; }\n'
    ),
}
IMPORTER_FUNCTIONS = {'main', 'step1', 'step2', 'helper'}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ['git', '-c', 'user.email=t@test', '-c', 'user.name=t',
         '-c', 'commit.gpgsign=false', '-C', str(repo), *args],
        check=True, capture_output=True,
    )


def _repo(root: Path) -> Path:
    root.mkdir(parents=True)
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    for name, text in FILES.items():
        (root / name).write_text(text, encoding='utf-8')
    _git(root, 'add', '.')
    _git(root, 'commit', '-qm', 'init')
    return root


def _flow_step_names(repo: Path) -> set[str]:
    flow = get_flow(flow_name='main', repo_root=str(repo))
    assert flow['status'] == 'ok', flow
    return {step['name'] for step in flow['flow']['steps']}


def _importer_community_ids(repo: Path) -> dict[str, int | None]:
    store = GraphStore(repo / '.code-review-graph' / 'graph.db')
    try:
        rows = store._conn.execute(
            "SELECT name, community_id FROM nodes "
            "WHERE file_path = ? AND kind = 'Function'",
            (str(repo / 'use.ts'),),
        ).fetchall()
    finally:
        store.close()
    return {row['name']: row['community_id'] for row in rows}


def test_comment_edit_to_imported_module_keeps_importer_flows_and_communities(
    tmp_path, monkeypatch,
):
    monkeypatch.delenv('CRG_DATA_DIR', raising=False)
    repo = _repo((tmp_path / 'repo').resolve())

    built = build_or_update_graph(full_rebuild=True, repo_root=str(repo))
    assert built['status'] == 'ok'
    assert IMPORTER_FUNCTIONS <= _flow_step_names(repo)
    assert all(cid is not None for cid in _importer_community_ids(repo).values())

    # A comment-only edit to the imported module: use.ts is untouched.
    (repo / 'fee.ts').write_text(
        '// pricing helpers\n' + FILES['fee.ts'], encoding='utf-8',
    )
    _git(repo, 'commit', '-qam', 'comment')

    updated = build_or_update_graph(repo_root=str(repo))
    assert updated['status'] == 'ok'
    assert updated['build_type'] == 'incremental'

    # The flow that runs through use.ts must still list use.ts's functions.
    assert IMPORTER_FUNCTIONS <= _flow_step_names(repo)
    # Reviewing use.ts must still find the flow it belongs to.
    affected = get_affected_flows_func(changed_files=['use.ts'], repo_root=str(repo))
    assert affected['status'] == 'ok'
    assert 'main' in {flow['name'] for flow in affected['affected_flows']}
    # use.ts's functions must still belong to a community.
    communities = _importer_community_ids(repo)
    assert set(communities) == IMPORTER_FUNCTIONS
    assert all(cid is not None for cid in communities.values()), communities


def _node_ids(store: GraphStore, path: Path) -> dict[str, int]:
    rows = store._conn.execute(
        'SELECT qualified_name, id FROM nodes WHERE file_path = ?', (path.as_posix(),),
    ).fetchall()
    return {row['qualified_name']: row['id'] for row in rows}


def test_forced_reparse_keeps_importer_node_ids_at_every_postprocess_level(
    tmp_path, monkeypatch,
):
    """`update --skip-flows` re-traces nothing, so the ids themselves must stay."""
    monkeypatch.delenv('CRG_DATA_DIR', raising=False)
    repo = _repo((tmp_path / 'repo').resolve())
    built = build_or_update_graph(full_rebuild=True, repo_root=str(repo))
    assert built['status'] == 'ok'
    db = repo / '.code-review-graph' / 'graph.db'
    with GraphStore(db) as store:
        before = _node_ids(store, repo / 'use.ts')
    assert before

    (repo / 'fee.ts').write_text('// pricing helpers\n' + FILES['fee.ts'], encoding='utf-8')
    _git(repo, 'commit', '-qam', 'comment')
    updated = build_or_update_graph(repo_root=str(repo), postprocess='minimal')

    assert updated['files_updated'] == 2
    with GraphStore(db) as store:
        assert _node_ids(store, repo / 'use.ts') == before
    assert IMPORTER_FUNCTIONS <= _flow_step_names(repo)
    assert all(cid is not None for cid in _importer_community_ids(repo).values())


def test_many_forced_importers_keep_their_ids_through_the_parallel_path(tmp_path, monkeypatch):
    monkeypatch.delenv('CRG_DATA_DIR', raising=False)
    monkeypatch.delenv('CRG_SERIAL_PARSE', raising=False)
    repo = (tmp_path / 'repo').resolve()
    repo.mkdir()
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    (repo / 'fee.ts').write_text(FILES['fee.ts'], encoding='utf-8')
    importers = [repo / f'use{index}.ts' for index in range(12)]
    for path in importers:
        path.write_text(
            "import { calculateFee } from './fee';\n"
            'export function run() { return calculateFee(1); }\n',
            encoding='utf-8',
        )
    _git(repo, 'add', '.')
    _git(repo, 'commit', '-qm', 'init')

    db = tmp_path / 'graph.db'
    with GraphStore(db) as store:
        assert not full_build(repo, store)['errors']
        before = {path: _node_ids(store, path) for path in importers}
        (repo / 'fee.ts').write_text(
            FILES['fee.ts'].replace('calculateFee', 'computeFee'), encoding='utf-8',
        )
        result = incremental_update(repo, store, changed_files=['fee.ts'], reconcile_stale=False)

        assert not result['errors']
        assert result['files_updated'] == 13
        assert {path: _node_ids(store, path) for path in importers} == before


def test_importer_edited_during_the_update_is_stored_whole(tmp_path, monkeypatch):
    """The hash checked first is not the bytes parsed: nodes must follow the bytes."""
    monkeypatch.setenv('CRG_SERIAL_PARSE', '1')
    repo = _repo((tmp_path / 'repo').resolve())
    use = (repo / 'use.ts').resolve()
    edited = (FILES['use.ts'] + 'export function added() { return 1; }\n').encode()
    real_read_bytes = Path.read_bytes
    reads = {'count': 0}

    def racing_read_bytes(self):
        if self.resolve() == use:
            reads['count'] += 1
            if reads['count'] > 1:
                return edited
        return real_read_bytes(self)

    with GraphStore(tmp_path / 'graph.db') as store:
        assert not full_build(repo, store)['errors']
        (repo / 'fee.ts').write_text('// note\n' + FILES['fee.ts'], encoding='utf-8')
        monkeypatch.setattr(Path, 'read_bytes', racing_read_bytes)
        result = incremental_update(repo, store, changed_files=['fee.ts'], reconcile_stale=False)
        monkeypatch.setattr(Path, 'read_bytes', real_read_bytes)

        assert not result['errors']
        assert reads['count'] >= 2
        names = {node.name for node in store.get_nodes_by_file(str(use))}
        assert 'added' in names


def test_update_post_processes_the_importers_it_reparsed(tmp_path, monkeypatch):
    monkeypatch.delenv('CRG_DATA_DIR', raising=False)
    repo = _repo((tmp_path / 'repo').resolve())
    # Not postprocess='none': a pending marker recomputes all derived data.
    assert build_or_update_graph(
        full_rebuild=True, repo_root=str(repo), postprocess='minimal',
    )['status'] == 'ok'
    (repo / 'fee.ts').write_text('// note\n' + FILES['fee.ts'], encoding='utf-8')
    _git(repo, 'commit', '-qam', 'comment')
    seen = {}

    def capture(store, build_result, postprocess, **kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr('code_review_graph.tools.build._run_postprocess', capture)
    assert build_or_update_graph(repo_root=str(repo))['status'] == 'ok'

    assert sorted(seen['changed_files']) == ['fee.ts', 'use.ts']


def test_full_build_clears_a_pending_importer_retry(tmp_path, monkeypatch):
    monkeypatch.setenv('CRG_SERIAL_PARSE', '1')
    repo = _repo((tmp_path / 'repo').resolve())
    use = (repo / 'use.ts').resolve()
    real_read_bytes = Path.read_bytes

    def blocked_read_bytes(self):
        if self.resolve() == use:
            raise PermissionError(13, 'Permission denied', str(self))
        return real_read_bytes(self)

    with GraphStore(tmp_path / 'graph.db') as store:
        assert not full_build(repo, store)['errors']
        (repo / 'fee.ts').write_text('// note\n' + FILES['fee.ts'], encoding='utf-8')
        monkeypatch.setattr(Path, 'read_bytes', blocked_read_bytes)
        result = incremental_update(repo, store, changed_files=['fee.ts'], reconcile_stale=False)
        monkeypatch.setattr(Path, 'read_bytes', real_read_bytes)
        assert [error['file'] for error in result['errors']] == ['use.ts']
        assert store.get_metadata('dependents_reparse_pending') == '["use.ts"]'

        assert not full_build(repo, store)['errors']
        assert store.get_metadata('dependents_reparse_pending') == '[]'
