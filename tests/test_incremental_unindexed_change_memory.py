"""An update must not load changed files the graph never indexes.

PR #1062 hashes every changed path before looking for its dependents, so a
tracked data file in the diff (a dataset, a model checkpoint, a fixture
database) is read into memory in full on every ``update``, even though it has
no language, holds no graph rows and cannot have dependents. ``git diff`` keeps
an uncommitted change to such a file in the change list, so the PostToolUse
hook's ``update --skip-flows`` paid that cost again after every edit.
"""
import subprocess
import tracemalloc
from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, incremental_update

DATA_BYTES = 64 * 1024 * 1024


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ['git', '-c', 'user.email=t@test', '-c', 'user.name=t',
         '-c', 'commit.gpgsign=false', '-C', str(repo), *args],
        check=True, capture_output=True,
    )


def test_update_does_not_read_changed_unindexed_data_file(tmp_path):
    repo = (tmp_path / 'repo').resolve()
    (repo / 'data').mkdir(parents=True)
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    (repo / 'app.py').write_text('def f():\n    return 1\n', encoding='utf-8')
    data = repo / 'data' / 'fixture.sqlite'
    with data.open('wb') as handle:
        handle.truncate(DATA_BYTES)
    _git(repo, 'add', '.')
    _git(repo, 'commit', '-qm', 'init')

    store = GraphStore(tmp_path / 'graph.db')
    try:
        assert not full_build(repo, store)['errors']
        assert not store.get_nodes_by_file(str(data))

        # A local change to the tracked data file stays in ``git diff HEAD``
        # next to the source edit the hook is reacting to.
        with data.open('ab') as handle:
            handle.write(b'x')
        (repo / 'app.py').write_text('def f():\n    return 2\n', encoding='utf-8')

        tracemalloc.start()
        try:
            result = incremental_update(repo, store, base='HEAD')
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
    finally:
        store.close()

    assert 'data/fixture.sqlite' in result['changed_files']
    assert result['files_updated'] == 1
    assert not result['errors']
    assert peak < DATA_BYTES // 2, (
        f'update peaked at {peak / 2**20:.1f} MiB while the only indexed change '
        f'is a two-line file; the {DATA_BYTES // 2**20} MiB data file was loaded'
    )
