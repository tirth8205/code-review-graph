"""Incremental symbol renames must invalidate unchanged alias callers."""
import subprocess

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, incremental_update


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
    target = str(tmp_path/'fee.ts') + '::computeFee'
    incremental = {e.source_qualified for e in store.get_edges_by_target(target) if e.kind == 'CALLS'}
    full = GraphStore(tmp_path / 'full.db')
    assert not full_build(tmp_path, full)['errors']
    rebuilt = {e.source_qualified for e in full.get_edges_by_target(target) if e.kind == 'CALLS'}
    assert len(rebuilt) == 3
    assert incremental == rebuilt
    # Replaying a notification without a content change should not parse anything.
    replay = incremental_update(tmp_path, store, changed_files=['fee.ts'])
    assert replay['files_updated'] == 0
