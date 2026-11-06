"""Incremental symbol renames must invalidate unchanged alias callers.

PR #1062: an importer whose own bytes did not change can still hold edges
resolved against an export that a changed file renamed or removed, so a real
content change to a file re-parses the files that depend on it. A duplicate
notification (same bytes as the graph already holds) does not.

Every test compares the incremental graph with a fresh full build of the same
tree, through the update paths users actually reach: an explicit batch, the
git-diff ``update`` path, and watch batches (``reconcile_stale=False``).
"""
import hashlib
import itertools
import os
import subprocess
from pathlib import Path

import pytest

import code_review_graph.incremental as incremental_module
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, incremental_update
from code_review_graph.parser import CodeParser

BASE_FILES = {
    'fee.ts': 'export function calculateFee(n: number) { return n + 2; }\n',
    'barrel.ts': 'export { calculateFee as fee } from "./fee.js";\n',
    'use.ts': 'import { fee as charge } from "./barrel.js";\n'
              'export function invoice() { return charge(3); }\n',
    'client.ts': 'import { calculateFee } from "./fee.js";\n'
                 'export const clientFee = calculateFee(8);\n',
    'unrelated.ts': 'export function unrelated() { return 42; }\n',
}
RENAMED = ('fee.ts', 'barrel.ts', 'client.ts')


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ['git', '-c', 'user.email=t@test', '-c', 'user.name=t',
         '-c', 'commit.gpgsign=false', '-C', str(repo), *args],
        check=True, capture_output=True,
    )


def _repo(root: Path, files: dict[str, str], *, commit: bool = False) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
    _git(root, 'add', '.')
    if commit:
        _git(root, 'commit', '-qm', 'init')
    return root


def _rename(repo: Path, names, old='calculateFee', new='computeFee') -> None:
    for name in names:
        path = repo / name
        path.write_text(path.read_text(encoding='utf-8').replace(old, new), encoding='utf-8')


def _edges(store: GraphStore) -> set[tuple[str, str, str]]:
    return {(e.kind, e.source_qualified, e.target_qualified) for e in store.get_all_edges()}


def _assert_matches_full_build(repo: Path, store: GraphStore) -> None:
    fresh = GraphStore(repo.parent / f'{repo.name}-fresh.db')
    try:
        assert not full_build(repo, fresh)['errors']
        expected = _edges(fresh)
    finally:
        fresh.close()
    actual = _edges(store)
    assert actual == expected, (
        f'missing from incremental: {sorted(expected - actual)}\n'
        f'extra in incremental: {sorted(actual - expected)}'
    )


def _callers(store: GraphStore, repo: Path, file_name: str, symbol: str) -> set[str]:
    target = f'{(repo / file_name).as_posix()}::{symbol}'
    return {e.source_qualified for e in store.get_edges_by_target(target) if e.kind == 'CALLS'}


@pytest.fixture
def built(tmp_path):
    repo = _repo(tmp_path / 'repo', BASE_FILES)
    store = GraphStore(tmp_path / 'graph.db')
    assert not full_build(repo, store)['errors']
    yield repo, store
    store.close()


def test_renamed_export_preserves_unchanged_reexport_callers(tmp_path):
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    files = {
        'fee.ts': 'export function calculateFee(n: number) { return n + 2; }\n',
        'barrel.ts': 'export { calculateFee as fee } from "./fee.js";\n',
        'use.ts': 'import { fee as charge } from "./barrel.js";\n'
                  'export function invoice() { return charge(3); }\n'
                  'export function nested() { return [1, 2].map(x => charge(x)); }\n',
        'client.ts': 'import { calculateFee } from "./fee.js";\n'
                     'export const clientFee = calculateFee(8);\n',
        'unrelated.ts': 'export function unrelated() { return 42; }\n',
    }
    for name, text in files.items():
        (tmp_path / name).write_text(text)
    subprocess.run(['git', '-C', str(tmp_path), 'add', '.'], check=True)
    store = GraphStore(tmp_path / 'incremental.db')
    assert not full_build(tmp_path, store)['errors']
    for name in ('fee.ts', 'barrel.ts', 'client.ts'):
        (tmp_path/name).write_text(files[name].replace('calculateFee', 'computeFee'))
    result = incremental_update(tmp_path, store, changed_files=['fee.ts', 'barrel.ts', 'client.ts'])
    assert not result['errors']
    target = (tmp_path / 'fee.ts').as_posix() + '::computeFee'
    incremental = {
        e.source_qualified for e in store.get_edges_by_target(target) if e.kind == 'CALLS'
    }
    full = GraphStore(tmp_path / 'full.db')
    assert not full_build(tmp_path, full)['errors']
    rebuilt = {e.source_qualified for e in full.get_edges_by_target(target) if e.kind == 'CALLS'}
    assert len(rebuilt) == 3
    assert incremental == rebuilt
    # Replaying a notification without a content change should not parse anything.
    replay = incremental_update(tmp_path, store, changed_files=['fee.ts'])
    assert replay['files_updated'] == 0


def test_git_diff_update_preserves_reexport_callers(tmp_path):
    """The ``update`` path: no explicit list, diff against the stored anchor."""
    repo = _repo(tmp_path / 'repo', BASE_FILES, commit=True)
    store = GraphStore(tmp_path / 'graph.db')
    try:
        assert not full_build(repo, store)['errors']
        anchor = store.get_metadata('git_head_sha')
        assert anchor
        _rename(repo, RENAMED)
        _git(repo, 'commit', '-qam', 'rename')

        result = incremental_update(repo, store, base=anchor)

        assert not result['errors']
        assert sorted(result['changed_files']) == sorted(RENAMED)
        assert _callers(store, repo, 'fee.ts', 'computeFee') == {
            str(repo / 'client.ts'), f'{repo / "use.ts"}::invoice',
        }
        _assert_matches_full_build(repo, store)
        # Idempotent: the next update from the new anchor parses nothing.
        again = incremental_update(repo, store, base=store.get_metadata('git_head_sha'))
        assert again['files_updated'] == 0
    finally:
        store.close()


@pytest.mark.parametrize('order', list(itertools.permutations(RENAMED)))
def test_watch_batches_one_file_at_a_time_reach_full_build_parity(built, order):
    """Watch delivers one save per batch and never reconciles stale files.

    Whichever file the user saves first, the graph equals a full build of the
    tree on disk after every save, not only once all three are saved: the user
    can query between saves.
    """
    repo, store = built
    for name in order:
        _rename(repo, [name])
        result = incremental_update(repo, store, changed_files=[name], reconcile_stale=False)
        assert not result['errors']
        _assert_matches_full_build(repo, store)


def test_mcp_update_tool_reports_reexport_callers_after_rename(tmp_path):
    """The MCP/CLI path: build_or_update_graph, then query_graph callers_of."""
    from code_review_graph.tools.build import build_or_update_graph
    from code_review_graph.tools.query import query_graph

    repo = _repo(tmp_path / 'repo', BASE_FILES, commit=True)
    built_result = build_or_update_graph(
        full_rebuild=True, repo_root=str(repo), postprocess='none',
    )
    assert built_result['status'] == 'ok'
    _rename(repo, RENAMED)
    _git(repo, 'commit', '-qam', 'rename')

    updated = build_or_update_graph(repo_root=str(repo), postprocess='none')
    callers = query_graph('callers_of', 'computeFee', repo_root=str(repo))

    assert updated['status'] == 'ok'
    assert updated['build_type'] == 'incremental'
    assert sorted(r['qualified_name'].rsplit('/', 1)[-1] for r in callers['results']) == [
        'client.ts', 'use.ts::invoice',
    ]

def test_watch_replay_of_unchanged_file_reparses_nothing(built):
    repo, store = built
    _rename(repo, RENAMED)
    first = incremental_update(repo, store, changed_files=list(RENAMED), reconcile_stale=False)
    assert first['files_updated'] == 4  # three renamed files plus use.ts

    replay = incremental_update(repo, store, changed_files=['fee.ts'], reconcile_stale=False)

    assert replay['files_updated'] == 0
    assert replay['dependent_files'] == []
    _assert_matches_full_build(repo, store)


def test_unchanged_seed_does_not_invalidate_dependents_of_changed_sibling(built):
    """A duplicate notification for fee.ts must not drag its importers along."""
    repo, store = built
    (repo / 'unrelated.ts').write_text('export function unrelated() { return 43; }\n')

    result = incremental_update(
        repo, store, changed_files=['fee.ts', 'unrelated.ts'], reconcile_stale=False,
    )

    assert result['files_updated'] == 1
    assert result['dependent_files'] == []


def test_leaf_edit_does_not_reparse_the_files_it_imports(built):
    """Dependents are importers; the importer's own targets are not re-parsed."""
    repo, store = built
    (repo / 'use.ts').write_text(BASE_FILES['use.ts'] + '// trailing note\n')

    result = incremental_update(repo, store, changed_files=['use.ts'])

    assert result['files_updated'] == 1
    assert result['dependent_files'] == []


def test_body_only_seed_edit_leaves_graph_identical_to_full_build(built):
    repo, store = built
    (repo / 'fee.ts').write_text('// pricing\n' + BASE_FILES['fee.ts'])

    result = incremental_update(repo, store, changed_files=['fee.ts'])

    assert not result['errors']
    assert sorted(result['dependent_files']) == ['client.ts', 'use.ts']
    # Listed is not enough: the old update listed them too and skipped them.
    assert result['files_updated'] == 3
    _assert_matches_full_build(repo, store)


def test_deleted_seed_in_watch_batch_reparses_its_importers(built):
    """Importers of a deleted file fall back to unresolved edges, as a build does."""
    repo, store = built
    (repo / 'fee.ts').unlink()

    result = incremental_update(repo, store, changed_files=['fee.ts'], reconcile_stale=False)

    assert not result['errors']
    assert sorted(result['dependent_files']) == ['client.ts', 'use.ts']
    assert not store.get_nodes_by_file(str(repo / 'fee.ts'))
    _assert_matches_full_build(repo, store)


def test_unreadable_seed_still_invalidates_its_dependents(built, monkeypatch):
    """A seed whose bytes cannot be hashed is treated as changed, not as a replay."""
    repo, store = built
    _rename(repo, RENAMED)
    blocked = (repo / 'fee.ts').resolve()
    real_read_bytes = Path.read_bytes

    def flaky_read_bytes(self):
        if self.resolve() == blocked:
            raise PermissionError(13, 'Permission denied', str(self))
        return real_read_bytes(self)

    monkeypatch.setattr(Path, 'read_bytes', flaky_read_bytes)
    monkeypatch.setenv('CRG_SERIAL_PARSE', '1')
    result = incremental_update(
        repo, store, changed_files=list(RENAMED), reconcile_stale=False,
    )
    assert [e['file'] for e in result['errors']] == ['fee.ts']
    assert sorted(result['dependent_files']) == ['client.ts', 'use.ts']

    monkeypatch.setattr(Path, 'read_bytes', real_read_bytes)
    retry = incremental_update(repo, store, changed_files=['fee.ts'], reconcile_stale=False)
    assert not retry['errors']
    _assert_matches_full_build(repo, store)


def test_paths_with_spaces_and_unicode_keep_reexport_callers(tmp_path):
    files = {
        f'src dir/ü {name}': text.replace('"./', '"./ü ')
        for name, text in BASE_FILES.items()
    }
    repo = _repo(tmp_path / 'repo ü', files)
    store = GraphStore(tmp_path / 'graph.db')
    try:
        assert not full_build(repo, store)['errors']
        changed = [f'src dir/ü {name}' for name in RENAMED]
        _rename(repo, changed)

        result = incremental_update(repo, store, changed_files=changed)

        assert not result['errors']
        assert sorted(Path(p).as_posix() for p in result['dependent_files']) == [
            'src dir/ü client.ts', 'src dir/ü use.ts',
        ]
        _assert_matches_full_build(repo, store)
    finally:
        store.close()


def test_absolute_changed_paths_keep_reexport_callers(built):
    """Hooks and editors may report absolute paths; they name the same files."""
    repo, store = built
    _rename(repo, RENAMED)

    result = incremental_update(repo, store, changed_files=[str(repo / n) for n in RENAMED])

    assert not result['errors']
    _assert_matches_full_build(repo, store)


def test_three_level_reexport_chain_keeps_callers(tmp_path):
    files = {
        'fee.ts': 'export function calculateFee(n: number) { return n + 2; }\n',
        'b1.ts': 'export { calculateFee as fee } from "./fee.js";\n',
        'b2.ts': 'export { fee } from "./b1.js";\n',
        'b3.ts': 'export { fee } from "./b2.js";\n',
        'use.ts': 'import { fee } from "./b3.js";\n'
                  'export function invoice() { return fee(3); }\n',
    }
    repo = _repo(tmp_path / 'repo', files)
    store = GraphStore(tmp_path / 'graph.db')
    try:
        assert not full_build(repo, store)['errors']
        _rename(repo, ['fee.ts', 'b1.ts'])

        result = incremental_update(repo, store, changed_files=['fee.ts', 'b1.ts'])

        assert not result['errors']
        assert _callers(store, repo, 'fee.ts', 'computeFee') == {f'{repo / "use.ts"}::invoice'}
        _assert_matches_full_build(repo, store)
    finally:
        store.close()


def test_python_reexport_rename_matches_full_build(tmp_path):
    """Neighbouring language: Python aliases resolve through the same update.

    Python resolves ``charge`` to ``pkg/barrel.py::charge``, so parity alone
    holds even without the fix; the re-parse count is what pins it here.
    """
    files = {
        'pkg/__init__.py': '',
        'pkg/fee.py': 'def calculate_fee(n):\n    return n + 2\n',
        'pkg/barrel.py': 'from pkg.fee import calculate_fee as fee\n',
        'pkg/use.py': 'from pkg.barrel import fee as charge\n\n\n'
                      'def invoice():\n    return charge(3)\n',
    }
    repo = _repo(tmp_path / 'repo', files)
    store = GraphStore(tmp_path / 'graph.db')
    try:
        assert not full_build(repo, store)['errors']
        _rename(repo, ['pkg/fee.py', 'pkg/barrel.py'], 'calculate_fee', 'compute_fee')

        result = incremental_update(repo, store, changed_files=['pkg/fee.py', 'pkg/barrel.py'])

        assert not result['errors']
        assert sorted(result['dependent_files']) == ['pkg/barrel.py', 'pkg/use.py']
        assert result['files_updated'] == 3
        _assert_matches_full_build(repo, store)
    finally:
        store.close()


def test_many_dependents_use_the_parallel_parse_path(tmp_path, monkeypatch):
    """Eight or more files to parse take the executor path, not the serial loop."""
    monkeypatch.setenv('CRG_PARSE_EXECUTOR', 'thread')
    monkeypatch.delenv('CRG_SERIAL_PARSE', raising=False)
    files = dict(BASE_FILES)
    for i in range(10):
        files[f'm/m{i}.ts'] = (
            'import { fee } from "../barrel.js";\n'
            f'export function f{i}() {{ return fee({i}); }}\n'
        )
    repo = _repo(tmp_path / 'repo', files)
    store = GraphStore(tmp_path / 'graph.db')
    try:
        assert not full_build(repo, store)['errors']
        _rename(repo, RENAMED)

        result = incremental_update(repo, store, changed_files=list(RENAMED))

        assert not result['errors']
        assert result['files_updated'] >= 8
        assert len(_callers(store, repo, 'fee.ts', 'computeFee')) == 12
        _assert_matches_full_build(repo, store)
    finally:
        store.close()


def test_forced_dependent_reparse_is_bounded_by_the_dependent_cap(tmp_path, monkeypatch):
    """A widely imported file re-parses at most the capped dependent set."""
    monkeypatch.setattr(incremental_module, '_MAX_DEPENDENT_FILES', 3)
    files = {'hub.ts': 'export function h(n: number) { return n; }\n'}
    for i in range(6):
        files[f'm{i}.ts'] = f'import {{ h }} from "./hub.js";\nexport const v{i} = h({i});\n'
    repo = _repo(tmp_path / 'repo', files)
    store = GraphStore(tmp_path / 'graph.db')
    try:
        assert not full_build(repo, store)['errors']
        (repo / 'hub.ts').write_text('// note\n' + files['hub.ts'])

        result = incremental_update(repo, store, changed_files=['hub.ts'], reconcile_stale=False)

        assert not result['errors']
        assert len(result['dependent_files']) == 3
        assert result['files_updated'] == 4
    finally:
        store.close()


def test_graph_damaged_by_an_earlier_release_heals_when_an_import_it_uses_changes(built):
    """Upgrade path: stale importer edges written by the old update.

    The old update re-parsed only the changed files, leaving use.ts pointing at
    ``fee.ts::calculateFee``. A replay does not repair that (use.ts's own hash
    still matches). Because the stale edge names a symbol fee.ts no longer has,
    use.ts is reached through the file it imports (barrel.ts), not through fee.ts;
    a real change there repairs it. ``build`` repairs it unconditionally.
    """
    repo, store = built
    _rename(repo, RENAMED)
    parser = CodeParser(repo)
    for name in RENAMED:
        raw = (repo / name).read_bytes()
        nodes, edges = parser.parse_bytes(repo / name, raw)
        store.store_file_nodes_edges(
            str(repo / name), nodes, edges, hashlib.sha256(raw).hexdigest(),
        )
    store.commit()
    assert _callers(store, repo, 'fee.ts', 'computeFee') == {str(repo / 'client.ts')}

    replay = incremental_update(repo, store, changed_files=list(RENAMED))
    assert replay['files_updated'] == 0

    (repo / 'barrel.ts').write_text('// touched\n' + (repo / 'barrel.ts').read_text())
    healed = incremental_update(repo, store, changed_files=['barrel.ts'])

    assert not healed['errors']
    _assert_matches_full_build(repo, store)


@pytest.mark.skipif(os.name == 'nt', reason='POSIX permission bits')
def test_permission_error_on_seed_hash_does_not_crash_update(built):
    """Real chmod 000 on the seed: reported as an error, dependents still re-parsed."""
    repo, store = built
    if hasattr(os, 'geteuid') and os.geteuid() == 0:
        pytest.skip('root can read mode-000 files')
    _rename(repo, RENAMED)
    seed = repo / 'fee.ts'
    seed.chmod(0)
    try:
        result = incremental_update(
            repo, store, changed_files=list(RENAMED), reconcile_stale=False,
        )
    finally:
        seed.chmod(0o644)
    assert [e['file'] for e in result['errors']] == ['fee.ts']
    # client.ts is reached only through fee.ts, so this fails if an unreadable
    # seed is treated as a replay; use.ts alone is also reached via barrel.ts.
    assert sorted(result['dependent_files']) == ['client.ts', 'use.ts']


@pytest.fixture
def committed(tmp_path):
    repo = _repo(tmp_path / 'repo', BASE_FILES, commit=True)
    store = GraphStore(tmp_path / 'graph.db')
    assert not full_build(repo, store)['errors']
    yield repo, store
    store.close()


def test_commit_hook_update_after_watch_saves_reparses_nothing(committed):
    """Watch indexes each save; the post-commit ``update`` then sees the same bytes.

    git still lists the renamed files against the stored anchor, but the graph
    already holds them, so nothing is re-parsed and nothing is invalidated.
    """
    repo, store = committed
    for name in RENAMED:
        _rename(repo, [name])
        saved = incremental_update(repo, store, changed_files=[name], reconcile_stale=False)
        assert not saved['errors']
    anchor = store.get_metadata('git_head_sha')
    _git(repo, 'commit', '-qam', 'rename')

    result = incremental_update(repo, store, base=anchor)

    assert not result['errors']
    assert sorted(result['changed_files']) == sorted(RENAMED)
    assert result['dependent_files'] == []
    assert result['files_updated'] == 0
    _assert_matches_full_build(repo, store)


def test_revert_found_only_by_content_scan_reparses_dependents(committed):
    """Rename indexed by watch, then ``git checkout .``: git diff is empty.

    Only the content scan reports the three files. use.ts was re-pointed at
    ``computeFee`` by the watch batch, so it must be re-pointed back.
    """
    repo, store = committed
    anchor = store.get_metadata('git_head_sha')
    _rename(repo, RENAMED)
    saved = incremental_update(repo, store, changed_files=list(RENAMED), reconcile_stale=False)
    assert not saved['errors']
    for name in RENAMED:
        (repo / name).write_text(BASE_FILES[name], encoding='utf-8')

    result = incremental_update(repo, store, base=anchor)

    assert not result['errors']
    assert sorted(result['changed_files']) == sorted(RENAMED)
    assert 'use.ts' in result['dependent_files']
    assert _callers(store, repo, 'fee.ts', 'calculateFee') == {
        str(repo / 'client.ts'), f'{repo / "use.ts"}::invoice',
    }
    _assert_matches_full_build(repo, store)


@pytest.mark.parametrize('path', ['update', 'watch'])
def test_moved_seed_file_keeps_reexport_callers(committed, path):
    """fee.ts moves to lib/fee.ts; barrel.ts and client.ts follow, use.ts does not."""
    repo, store = committed
    anchor = store.get_metadata('git_head_sha')
    (repo / 'lib').mkdir()
    _git(repo, 'mv', 'fee.ts', 'lib/fee.ts')
    for name in ('barrel.ts', 'client.ts'):
        p = repo / name
        p.write_text(
            p.read_text(encoding='utf-8').replace('"./fee.js"', '"./lib/fee.js"'),
            encoding='utf-8',
        )

    if path == 'update':
        _git(repo, 'commit', '-qam', 'move')
        result = incremental_update(repo, store, base=anchor)
    else:
        result = incremental_update(
            repo, store,
            changed_files=['fee.ts', 'lib/fee.ts', 'barrel.ts', 'client.ts'],
            reconcile_stale=False,
        )

    assert not result['errors']
    assert _callers(store, repo, 'lib/fee.ts', 'calculateFee') == {
        str(repo / 'client.ts'), f'{repo / "use.ts"}::invoice',
    }
    _assert_matches_full_build(repo, store)


def test_watch_save_reads_the_saved_file_at_most_twice(built, monkeypatch):
    """The seed hash taken for the dependents check is reused by the parse loop.

    One read to hash, one to parse, as before this change. unrelated.ts has no
    importers, so no other file's parse reads it.
    """
    repo, store = built
    monkeypatch.setenv('CRG_SERIAL_PARSE', '1')
    (repo / 'unrelated.ts').write_text('export function unrelated() { return 43; }\n')
    reads: list[str] = []
    real_read_bytes = Path.read_bytes

    def counting_read_bytes(self):
        reads.append(Path(self).name)
        return real_read_bytes(self)

    monkeypatch.setattr(Path, 'read_bytes', counting_read_bytes)
    result = incremental_update(
        repo, store, changed_files=['unrelated.ts'], reconcile_stale=False,
    )

    assert not result['errors']
    assert result['files_updated'] == 1
    assert reads.count('unrelated.ts') <= 2, reads


def test_deleted_seed_through_update_keeps_importer_edges(committed):
    """The watch path is covered above; this is ``update`` after ``git rm``."""
    repo, store = committed
    anchor = store.get_metadata('git_head_sha')
    _git(repo, 'rm', '-q', 'fee.ts')
    _git(repo, 'commit', '-qm', 'drop fee')

    result = incremental_update(repo, store, base=anchor)

    assert not result['errors']
    _assert_matches_full_build(repo, store)


def test_failed_dependent_reparse_is_retried_by_the_next_update(committed, monkeypatch):
    repo, store = committed
    monkeypatch.setenv('CRG_SERIAL_PARSE', '1')
    _rename(repo, RENAMED)
    blocked = (repo / 'use.ts').resolve()
    real_read_bytes = Path.read_bytes

    def flaky_read_bytes(self):
        if self.resolve() == blocked:
            raise PermissionError(13, 'Permission denied', str(self))
        return real_read_bytes(self)

    monkeypatch.setattr(Path, 'read_bytes', flaky_read_bytes)
    first = incremental_update(repo, store, changed_files=list(RENAMED), reconcile_stale=False)
    assert [e['file'] for e in first['errors']] == ['use.ts']

    monkeypatch.setattr(Path, 'read_bytes', real_read_bytes)
    incremental_update(repo, store, base=store.get_metadata('git_head_sha'))

    _assert_matches_full_build(repo, store)


def test_unindexed_changed_file_is_not_read(committed, monkeypatch):
    """A committed binary asset is listed by git but was never read before this change."""
    repo, store = committed
    blob = repo / 'assets' / 'model.bin'
    blob.parent.mkdir()
    blob.write_bytes(b'\x00\x01' * 4096)
    reads: list[str] = []
    real_read_bytes = Path.read_bytes

    def counting_read_bytes(self):
        reads.append(Path(self).name)
        return real_read_bytes(self)

    monkeypatch.setattr(Path, 'read_bytes', counting_read_bytes)
    result = incremental_update(
        repo, store, changed_files=['assets/model.bin'], reconcile_stale=False,
    )

    assert not result['errors']
    assert result['files_updated'] == 0
    assert 'model.bin' not in reads
