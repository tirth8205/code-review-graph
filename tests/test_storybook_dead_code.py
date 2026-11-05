"""Storybook consumers do not prove production use (issue #902, PR #1085)."""

from pathlib import Path

import pytest

from code_review_graph.changes import analyze_changes
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, incremental_update
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


# --- Edge cases added while hardening PR #1085 ---------------------------------


def _dead_names(store, root=None):
    return {node['name'] for node in find_dead_code(store, root=root)}


def _dead_qualified(store, root=None):
    return {node['qualified_name'] for node in find_dead_code(store, root=root)}


def test_prebuilt_graph_story_symbols_are_not_reported_dead(tmp_path):
    # A graph built before #1085 stored story symbols with is_test = 0. They
    # are demo code, not production symbols waiting for a caller.
    story = 'components/Foo.stories.tsx'
    with GraphStore(tmp_path / 'graph.db') as store:
        store.upsert_node(NodeInfo('Function', 'Primary', story, 5, 5, 'tsx', is_test=False))
        store.upsert_node(NodeInfo('Class', 'StoryHarness', story, 7, 9, 'tsx', is_test=False))
        store.commit()
        assert find_dead_code(store, root=tmp_path) == []


@pytest.mark.parametrize('kind', ['CALLS', 'IMPORTS_FROM', 'REFERENCES'])
@pytest.mark.parametrize('caller_file, alive', [
    ('components/Foo.stories.tsx', False),
    ('components/Foo.stories.js', False),
    ('pages/Home.tsx', True),
])
def test_each_story_evidence_kind_alone_does_not_keep_component_alive(
    tmp_path, kind, caller_file, alive,
):
    with GraphStore(tmp_path / 'graph.db') as store:
        store.upsert_node(NodeInfo('Function', 'Foo', 'components/Foo.tsx', 1, 3, 'tsx'))
        store.upsert_edge(EdgeInfo(kind, f'{caller_file}::Use', 'components/Foo.tsx::Foo',
                                  caller_file, 2))
        store.commit()
        assert ('Foo' not in _dead_names(store, tmp_path)) is alive


@pytest.mark.parametrize('source', ['components/Foo.tsx::Foo', 'Foo'])
@pytest.mark.parametrize('test_file, alive', [
    ('components/Foo.stories.tsx', False),
    ('components/Foo.test.tsx', True),
])
def test_story_tested_by_is_not_test_evidence_but_real_tests_are(
    tmp_path, source, test_file, alive,
):
    # ``Foo`` covers the unresolved bare-name TESTED_BY fallback.
    with GraphStore(tmp_path / 'graph.db') as store:
        store.upsert_node(NodeInfo('Function', 'Foo', 'components/Foo.tsx', 1, 3, 'tsx'))
        store.upsert_edge(EdgeInfo('TESTED_BY', source, f'{test_file}::check', test_file, 4))
        store.commit()
        assert ('Foo' not in _dead_names(store, tmp_path)) is alive


@pytest.mark.parametrize('caller_file, alive', [
    ('Widget.stories.tsx', False),
    ('app.tsx', True),
])
def test_class_qualified_story_call_does_not_keep_method_alive(tmp_path, caller_file, alive):
    with GraphStore(tmp_path / 'graph.db') as store:
        store.upsert_node(NodeInfo('Class', 'Widget', 'Widget.tsx', 1, 9, 'tsx'))
        store.upsert_node(NodeInfo('Function', 'label', 'Widget.tsx', 2, 4, 'tsx',
                                   parent_name='Widget'))
        store.upsert_edge(EdgeInfo('CALLS', f'{caller_file}::Use', 'Widget::label',
                                  caller_file, 3))
        store.commit()
        assert ('label' not in _dead_names(store, tmp_path)) is alive


@pytest.mark.parametrize('subclass_file, alive', [
    ('Widget.stories.tsx', False),
    ('FancyWidget.tsx', True),
])
def test_story_subclass_does_not_keep_base_class_alive(tmp_path, subclass_file, alive):
    with GraphStore(tmp_path / 'graph.db') as store:
        store.upsert_node(NodeInfo('Class', 'Widget', 'Widget.tsx', 1, 9, 'tsx'))
        # The bare-name INHERITS lookup joins on the subclass node's language.
        store.upsert_node(NodeInfo('Class', 'Demo', subclass_file, 1, 3, 'tsx',
                                   is_test=is_test_file(subclass_file)))
        store.upsert_edge(EdgeInfo('INHERITS', f'{subclass_file}::Demo', 'Widget',
                                  subclass_file, 1))
        store.commit()
        assert ('Widget' not in _dead_names(store, tmp_path)) is alive


def test_windows_style_story_paths_are_excluded(tmp_path):
    assert is_test_file('C:\\repo\\components\\Foo.stories.tsx')
    assert not is_test_file('C:\\repo\\components\\Foo.tsx')
    component = 'C:/repo/components/Foo.tsx'
    story = 'C:/repo/components/Foo.stories.tsx'
    with GraphStore(tmp_path / 'graph.db') as store:
        store.upsert_node(NodeInfo('Function', 'Foo', component, 1, 3, 'tsx'))
        store.upsert_node(NodeInfo('Function', 'Primary', story, 5, 5, 'tsx', is_test=False))
        store.upsert_edge(EdgeInfo('CALLS', f'{story}::Primary', f'{component}::Foo', story, 5))
        store.upsert_edge(EdgeInfo('IMPORTS_FROM', story, f'{component}::Foo', story, 1))
        store.commit()
        # Rows written by a Windows build before path normalisation (#774)
        # kept native separators; the story check must still recognise them.
        for table, columns in (
            ('nodes', ('file_path', 'qualified_name')),
            ('edges', ('file_path', 'source_qualified', 'target_qualified')),
        ):
            assignments = ', '.join(f"{c} = replace({c}, '/', char(92))" for c in columns)
            store._conn.execute(f'UPDATE {table} SET {assignments}')  # nosec B608
        store.commit()
        assert _dead_names(store) == {'Foo'}


def test_member_call_scan_sees_production_caller_among_many_story_rows(tmp_path):
    with GraphStore(tmp_path / 'graph.db') as store:
        store.upsert_node(NodeInfo('Class', 'Widget', 'Widget.tsx', 1, 9, 'tsx'))
        for i in range(2000):
            store.upsert_edge(EdgeInfo('CALLS', f'Widget.stories.tsx::Story{i}',
                                      'Widget.tsx::Widget.render', 'Widget.stories.tsx', i + 1))
        store.commit()
        assert 'Widget' in _dead_names(store, tmp_path)
        store.upsert_edge(EdgeInfo('CALLS', 'app.tsx::App', 'Widget.tsx::Widget.render',
                                  'app.tsx', 3))
        store.commit()
        assert 'Widget' not in _dead_names(store, tmp_path)


def test_production_call_to_base_method_still_keeps_override_alive(tmp_path):
    # Polymorphic dispatch: an application call to Animal.speak keeps
    # Dog.speak alive. The story filter must not drop that path.
    with GraphStore(tmp_path / 'graph.db') as store:
        store.upsert_node(NodeInfo('Class', 'Animal', 'animal.ts', 1, 5, 'typescript'))
        store.upsert_node(NodeInfo('Function', 'speak', 'animal.ts', 2, 4, 'typescript',
                                   parent_name='Animal'))
        store.upsert_node(NodeInfo('Class', 'Dog', 'dog.ts', 1, 5, 'typescript'))
        store.upsert_node(NodeInfo('Function', 'speak', 'dog.ts', 2, 4, 'typescript',
                                   parent_name='Dog'))
        store.upsert_edge(EdgeInfo('INHERITS', 'dog.ts::Dog', 'Animal', 'dog.ts', 1))
        store.upsert_edge(EdgeInfo('CALLS', 'Animal.stories.ts::Use', 'animal.ts::Animal.speak',
                                  'Animal.stories.ts', 3))
        store.upsert_edge(EdgeInfo('CALLS', 'app.ts::main', 'animal.ts::Animal.speak',
                                  'app.ts', 3))
        store.commit()
        dead = _dead_qualified(store, tmp_path)
        assert 'animal.ts::Animal.speak' not in dead
        assert 'dog.ts::Dog.speak' not in dead


def test_production_type_annotation_still_keeps_type_alive(tmp_path):
    # Story files are now test code; an application function's annotation
    # must still count as a use of the type.
    with GraphStore(tmp_path / 'graph.db') as store:
        store.upsert_node(NodeInfo('Class', 'UserShape', 'user.ts', 1, 3, 'typescript'))
        store.upsert_node(NodeInfo('Function', 'loadUser', 'api.ts', 1, 3, 'typescript',
                                   params='()', return_type=': UserShape'))
        store.commit()
        assert 'UserShape' not in _dead_names(store, tmp_path)


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')


def test_full_build_and_incremental_update_on_real_files(tmp_path):
    root = tmp_path / 'rëpo with spaces'
    comp = root / 'src' / 'compönents'
    _write(comp / 'Foo.tsx', 'export function Foo() {\n  return 1;\n}\n')
    _write(comp / 'Foo.stories.tsx',
           "import { Foo } from './Foo';\n\nexport default { component: Foo };\n"
           'export const Primary = () => Foo();\n')
    _write(comp / 'Bar.tsx', 'export function Bar() {\n  return 2;\n}\n')
    _write(comp / 'Bar.stories.tsx',
           "import { Bar } from './Bar';\n\nexport const Primary = () => Bar();\n")
    _write(comp / 'Baz.tsx', 'export function Baz() {\n  return 3;\n}\n')
    _write(comp / 'Baz.test.tsx',
           "import { Baz } from './Baz';\n\ntest('baz', () => {\n  Baz();\n});\n")
    app = root / 'src' / 'app.tsx'
    _write(app, "import { Bar } from './compönents/Bar';\n\n"
                'export function main() {\n  return Bar();\n}\n')
    with GraphStore(tmp_path / 'graph.db') as store:
        assert not full_build(root, store)['errors']
        flags = store._conn.execute(
            "SELECT is_test FROM nodes WHERE file_path LIKE '%.stories.tsx'"
        ).fetchall()
        assert flags and all(row[0] for row in flags)
        # Only the story-only component; Bar has an application caller and
        # Baz has an ordinary test. Story exports are not candidates.
        assert _dead_names(store, root) == {'Foo'}
        assert _dead_names(store, root) == {'Foo'}  # idempotent

        _write(app, "import { Bar } from './compönents/Bar';\n"
                    "import { Foo } from './compönents/Foo';\n\n"
                    'export function main() {\n  return Bar() + Foo();\n}\n')
        assert not incremental_update(root, store, changed_files=['src/app.tsx'])['errors']
        assert _dead_names(store, root) == set()


def test_story_exports_are_not_reported_as_test_gaps(tmp_path):
    # Classifying stories as test/demo code also keeps their exports out of
    # the untested-production-code list in change analysis.
    root = tmp_path / 'repo'
    story = root / 'Foo.stories.tsx'
    _write(root / 'Foo.tsx', 'export function Foo() {\n  return 1;\n}\n')
    _write(story, "import { Foo } from './Foo';\n\nexport const Primary = () => Foo();\n"
                  'export function Secondary() {\n  return Foo();\n}\n')
    with GraphStore(tmp_path / 'graph.db') as store:
        assert not full_build(root, store)['errors']
        story_path = story.as_posix()
        result = analyze_changes(store, [story_path], changed_ranges={story_path: [(1, 6)]},
                                 repo_root=str(root))
        assert not [gap for gap in result['test_gaps'] if gap['file'].endswith('.stories.tsx')]
