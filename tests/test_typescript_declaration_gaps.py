"""TypeScript interfaces and type aliases are not test gaps (issue #1109, PR #1112).

They are stored as Class nodes but have no runtime behavior, so a changed
interface must not appear in test_gaps.

The later tests pin the edges of that rule: where the declarations can sit
(TSX, ``.mts``/``.cts``/``.d.ts``, namespaces, ambient blocks, Vue and Svelte
``<script lang="ts">``), what must stay a gap (enums, classes, components,
other languages' interfaces, merged class/interface declarations, a class of
the same name elsewhere), the real build/update path, and graphs written
before the parser recorded ``typescript_kind``.
"""

import json
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from code_review_graph.changes import analyze_changes
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, get_db_path, incremental_update
from code_review_graph.parser import CodeParser, NodeInfo
from code_review_graph.tools.review import detect_changes_func


def test_parser_tags_interfaces_and_type_aliases_only():
    source = (
        b"export interface WidgetProps { title: string }\n"
        b"export type Size = { w: number };\n"
        b"export class Widget { render() { return 1; } }\n"
    )
    nodes, _ = CodeParser().parse_bytes(Path("widget.ts"), source)
    by_name = {n.name: n for n in nodes if n.kind == "Class"}

    assert by_name["WidgetProps"].extra["typescript_kind"] == "interface_declaration"
    assert by_name["Size"].extra["typescript_kind"] == "type_alias_declaration"
    assert "typescript_kind" not in by_name["Widget"].extra


class TestTypeScriptDeclarationGaps:
    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = GraphStore(self.tmp.name)

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def _add_class(self, name: str, extra: dict, line_start: int) -> None:
        node = NodeInfo(
            kind="Class",
            name=name,
            file_path="widget.ts",
            line_start=line_start,
            line_end=line_start + 2,
            language="typescript",
            is_test=False,
            extra=extra,
        )
        self.store.upsert_node(node, file_hash="abc")
        self.store.commit()

    def test_interface_is_not_a_gap_but_a_real_class_still_is(self):
        self._add_class("WidgetProps", {"typescript_kind": "interface"}, 1)
        self._add_class("Widget", {}, 10)

        result = analyze_changes(
            self.store,
            changed_files=["widget.ts"],
            changed_ranges={"widget.ts": [(1, 20)]},
        )
        gap_names = {g["name"] for g in result["test_gaps"]}

        assert "WidgetProps" not in gap_names
        assert "Widget" in gap_names


# ---------------------------------------------------------------------------
# Parser: where the tag is set, and where it must not be
# ---------------------------------------------------------------------------


def _classes(path: str, source: str) -> dict[str, NodeInfo]:
    nodes, _ = CodeParser().parse_bytes(Path(path), source.encode("utf-8"))
    return {n.name: n for n in nodes if n.kind == "Class"}


def test_tsx_tags_declarations_but_not_components_or_classes():
    """The shape from issue #1109: a .tsx file with props, a component, a class."""
    nodes, _ = CodeParser().parse_bytes(
        Path("widget.tsx"),
        b"export interface WidgetProps { title: string }\n"
        b"export type Size = { w: number };\n"
        b"export function Widget({ title }: WidgetProps) {\n"
        b"  return <h1>{title}</h1>;\n"
        b"}\n"
        b"export class Panel extends React.Component { render() { return null; } }\n",
    )
    by_name = {n.name: n for n in nodes if n.kind != "File"}

    assert by_name["WidgetProps"].language == "tsx"
    assert by_name["WidgetProps"].extra["typescript_kind"] == "interface_declaration"
    assert by_name["Size"].extra["typescript_kind"] == "type_alias_declaration"
    assert by_name["Widget"].kind == "Function"
    assert "typescript_kind" not in by_name["Widget"].extra
    assert "typescript_kind" not in by_name["Panel"].extra
    assert "typescript_kind" not in by_name["render"].extra


@pytest.mark.parametrize("suffix", [".mts", ".cts", ".d.ts"])
def test_typescript_module_and_declaration_files_are_tagged(suffix):
    classes = _classes(
        f"types{suffix}",
        "export interface Decl { a: number }\nexport type Alias = string;\n",
    )
    assert classes["Decl"].extra["typescript_kind"] == "interface_declaration"
    assert classes["Alias"].extra["typescript_kind"] == "type_alias_declaration"


@pytest.mark.parametrize(
    ("source", "name", "kind"),
    [
        ("export default interface Props { a: string }\n",
         "Props", "interface_declaration"),
        ("export namespace NS { export interface Inner { a: number } }\n",
         "Inner", "interface_declaration"),
        ("declare global { interface Window { crg: string } }\n",
         "Window", "interface_declaration"),
        ("declare module 'x' { export interface Ambient { b: number } }\n",
         "Ambient", "interface_declaration"),
        ("export declare interface Declared { c: number }\n",
         "Declared", "interface_declaration"),
        ("export interface Box<T> { v: T }\n", "Box", "interface_declaration"),
        ("export interface Svc { run(): void; go: () => void }\n",
         "Svc", "interface_declaration"),
        ("export type Fn<T> = (x: T) => void;\n", "Fn", "type_alias_declaration"),
        ("export type Tone = 'light' | 'dark';\n", "Tone", "type_alias_declaration"),
    ],
)
def test_wrapped_and_ambient_declarations_are_tagged(source, name, kind):
    classes = _classes("decls.ts", source)
    assert classes[name].extra["typescript_kind"] == kind


def test_interface_method_signatures_do_not_become_function_gaps(tmp_path):
    """Method signatures inside an interface are not Function nodes, and a
    whole-file change to an interface with methods reports no gap at all."""
    nodes, _ = CodeParser().parse_bytes(
        Path("/r/svc.ts"),
        b"export interface Svc {\n  run(): void;\n  go: () => void;\n}\n",
    )
    assert [n.name for n in nodes if n.kind == "Function"] == []

    store = GraphStore(tmp_path / "graph.db")
    try:
        for node in nodes:
            store.upsert_node(node, file_hash="abc")
        store.commit()
        result = analyze_changes(store, changed_files=["/r/svc.ts"])
    finally:
        store.close()
    assert "Svc" in {f["name"] for f in result["changed_functions"]}
    assert result["test_gaps"] == []


def test_unicode_declaration_names_are_tagged():
    classes = _classes(
        "unicode.ts",
        "export interface \u00dcn\u00efc\u00f6d\u00e9 { a: string }\n"
        "export type \u578b = number;\n",
    )
    assert classes["\u00dcn\u00efc\u00f6d\u00e9"].extra["typescript_kind"] == (
        "interface_declaration"
    )
    assert classes["\u578b"].extra["typescript_kind"] == "type_alias_declaration"


@pytest.mark.parametrize(
    ("source", "name"),
    [
        ("export enum Color { Red, Green }\n", "Color"),
        ("export const enum Flag { A }\n", "Flag"),
        ("export class Widget { render() { return 1; } }\n", "Widget"),
        ("export namespace NS { export class C { m() { return 1; } } }\n", "C"),
    ],
)
def test_runtime_typescript_declarations_are_not_tagged(source, name):
    """Enums and classes emit runtime code, so they stay gap candidates."""
    assert "typescript_kind" not in _classes("runtime.ts", source)[name].extra


@pytest.mark.parametrize(
    ("path", "source", "name"),
    [
        ("Widget.js", "export class Widget { render() { return 1; } }\n", "Widget"),
        ("Runner.java", "public interface Runner { void run(); }\n", "Runner"),
        ("Runner.cs", "public interface IRunner { void Run(); }\n", "IRunner"),
    ],
)
def test_other_languages_are_not_tagged(path, source, name):
    """The tag is TypeScript-only, even where another grammar reuses the
    ``interface_declaration`` node type."""
    assert "typescript_kind" not in _classes(path, source)[name].extra


@pytest.mark.parametrize(
    ("path", "source", "language"),
    [
        (
            "Comp.vue",
            "<template><div/></template>\n"
            "<script lang=\"ts\">\n"
            "export interface VProps { a: string }\n"
            "export type VSize = number;\n"
            "export class VC { m() { return 1; } }\n"
            "</script>\n",
            "vue",
        ),
        (
            "Comp.svelte",
            "<script lang=\"ts\">\n"
            "  export interface VProps { a: string }\n"
            "  export type VSize = number;\n"
            "  export class VC { m() { return 1; } }\n"
            "</script>\n"
            "<div/>\n",
            "svelte",
        ),
    ],
)
def test_single_file_component_typescript_declarations_are_tagged(path, source, language):
    """Vue and Svelte relabel script nodes with their own language; the tag
    is set while the block is parsed as TypeScript and must survive that."""
    classes = _classes(path, source)
    assert classes["VProps"].language == language
    assert classes["VProps"].extra["typescript_kind"] == "interface_declaration"
    assert classes["VSize"].extra["typescript_kind"] == "type_alias_declaration"
    assert "typescript_kind" not in classes["VC"].extra


# ---------------------------------------------------------------------------
# Real build and update path
# ---------------------------------------------------------------------------

_WIDGET = (
    "export interface WidgetProps { title: string }\n"
    "export type Size = { w: number };\n"
    "export enum Tone { Light, Dark }\n"
    "export function Widget({ title }: WidgetProps) {\n"
    "  return <h1>{title}</h1>;\n"
    "}\n"
    "export class Panel { render() { return 1; } }\n"
)
_WIDGET_EDITED = _WIDGET.replace("title: string }", "title: string; sub?: string }")
_RUNTIME_GAPS = {"Tone", "Widget", "Panel", "render"}
_DECLARATIONS = {"WidgetProps", "Size"}


class TestBuildAndUpdatePath:
    """Parse -> SQLite -> analyze_changes, through full_build and
    incremental_update, in a repository path with spaces and non-ASCII."""

    @pytest.fixture
    def repo(self, tmp_path):
        root = tmp_path / "my repo \u00fc"
        (root / "src dir").mkdir(parents=True)
        (root / ".git").mkdir()
        (root / "src dir" / "widget.tsx").write_text(_WIDGET, encoding="utf-8")
        store = GraphStore(root / "graph.db")
        try:
            yield root, store
        finally:
            store.close()

    @staticmethod
    def _build(root: Path, store: GraphStore) -> None:
        with patch(
            "code_review_graph.incremental.get_all_tracked_files",
            return_value=["src dir/widget.tsx"],
        ):
            result = full_build(root, store)
        assert result["errors"] == []

    @staticmethod
    def _widget_path(root: Path, store: GraphStore) -> str:
        (path,) = [
            f for f in store.get_all_files() if f.endswith("widget.tsx")
        ]
        return path

    def _gaps(self, root: Path, store: GraphStore, ranges: bool = True) -> dict:
        path = self._widget_path(root, store)
        return analyze_changes(
            store,
            changed_files=[path],
            changed_ranges={path: [(1, 7)]} if ranges else None,
        )

    def test_full_build_drops_declarations_but_keeps_runtime_gaps(self, repo):
        root, store = repo
        self._build(root, store)
        result = self._gaps(root, store)

        assert {g["name"] for g in result["test_gaps"]} == _RUNTIME_GAPS
        # Other analyses still see the declarations: they are changed nodes
        # with a risk score, only the gap report leaves them out.
        changed = {f["name"]: f for f in result["changed_functions"]}
        assert _DECLARATIONS <= set(changed)
        assert all("risk_score" in changed[n] for n in _DECLARATIONS)
        assert f"{len(_RUNTIME_GAPS)} test gap(s)" in result["summary"]
        untested_line = next(
            line for line in result["summary"].splitlines() if "Untested:" in line
        )
        assert not any(name in untested_line for name in _DECLARATIONS)

    def test_whole_file_fallback_also_drops_declarations(self, repo):
        """No line ranges: every node of the changed file is a candidate."""
        root, store = repo
        self._build(root, store)
        result = self._gaps(root, store, ranges=False)
        assert {g["name"] for g in result["test_gaps"]} == _RUNTIME_GAPS

    def test_change_touching_only_an_interface_reports_no_gap(self, repo):
        root, store = repo
        self._build(root, store)
        path = self._widget_path(root, store)
        result = analyze_changes(
            store, changed_files=[path], changed_ranges={path: [(1, 2)]},
        )
        assert {f["name"] for f in result["changed_functions"]} == _DECLARATIONS
        assert result["test_gaps"] == []

    def test_graph_from_an_earlier_release_heals_when_the_file_is_reparsed(self, repo):
        """A graph built before this change has no ``typescript_kind``.

        Until the file is parsed again its declarations are still reported
        (the documented limitation). The normal edit -> update path reparses
        the file, and from then on they drop out; running the update or a
        full rebuild again changes nothing.
        """
        root, store = repo
        self._build(root, store)
        path = self._widget_path(root, store)
        for node in store.get_nodes_by_file(path):
            if node.extra.get("typescript_kind"):
                legacy = {k: v for k, v in node.extra.items() if k != "typescript_kind"}
                store._conn.execute(
                    "UPDATE nodes SET extra = ? WHERE id = ?",
                    (json.dumps(legacy), node.id),
                )
        store.commit()

        stale = {g["name"] for g in self._gaps(root, store)["test_gaps"]}
        assert stale == _RUNTIME_GAPS | _DECLARATIONS

        (root / "src dir" / "widget.tsx").write_text(_WIDGET_EDITED, encoding="utf-8")
        incremental_update(root, store, changed_files=["src dir/widget.tsx"])
        assert {g["name"] for g in self._gaps(root, store)["test_gaps"]} == _RUNTIME_GAPS

        incremental_update(root, store, changed_files=["src dir/widget.tsx"])
        assert {g["name"] for g in self._gaps(root, store)["test_gaps"]} == _RUNTIME_GAPS

        self._build(root, store)
        assert {g["name"] for g in self._gaps(root, store)["test_gaps"]} == _RUNTIME_GAPS


# ---------------------------------------------------------------------------
# Name collisions, missing metadata and scale
# ---------------------------------------------------------------------------


class TestCollisionsAndScale:
    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = GraphStore(self.tmp.name)

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def _store_parsed(self, path: str, source: str) -> None:
        nodes, _ = CodeParser().parse_bytes(Path(path), source.encode("utf-8"))
        for node in nodes:
            self.store.upsert_node(node, file_hash="abc")
        self.store.commit()

    def _gap_names(self, path: str, start: int, end: int) -> set[str]:
        result = analyze_changes(
            self.store,
            changed_files=[path],
            changed_ranges={path: [(start, end)]},
        )
        return {g["name"] for g in result["test_gaps"]}

    def test_class_of_the_same_name_in_another_file_is_still_a_gap(self):
        self._store_parsed("/r/types.ts", "export interface Widget { a: string }\n")
        self._store_parsed("/r/widget.ts", "export class Widget { render() { return 1; } }\n")

        assert self._gap_names("/r/types.ts", 1, 1) == set()
        assert self._gap_names("/r/widget.ts", 1, 1) == {"Widget", "render"}

    def test_interface_merged_into_a_class_keeps_the_class_methods_as_gaps(self):
        """Declaration merging gives the class and the interface one
        qualified name, so whichever comes last owns the stored node. The
        class's methods are separate nodes and must stay gaps either way."""
        self._store_parsed(
            "/r/after.ts",
            "export class Emitter {\n"
            "  emit() { return 1; }\n"
            "}\n"
            "export interface Emitter {\n"
            "  on(e: string): this;\n"
            "}\n",
        )
        assert "emit" in self._gap_names("/r/after.ts", 1, 6)

        self._store_parsed(
            "/r/before.ts",
            "export interface Mixed {\n"
            "  on(e: string): this;\n"
            "}\n"
            "export class Mixed {\n"
            "  emit() { return 1; }\n"
            "}\n",
        )
        # The class is stored last here, so it keeps its own class-level gap.
        assert self._gap_names("/r/before.ts", 1, 6) == {"Mixed", "emit"}

    def test_null_extra_from_an_old_row_is_still_a_gap(self):
        self._store_parsed("/r/widget.ts", "export class Widget { render() { return 1; } }\n")
        self.store._conn.execute(
            "UPDATE nodes SET extra = NULL WHERE name = 'Widget'"
        )
        self.store.commit()
        assert self._gap_names("/r/widget.ts", 1, 1) == {"Widget", "render"}

    def test_many_declarations_in_one_file_leave_only_runtime_gaps(self):
        # 402 changed nodes: under the default CRG_MAX_CHANGED_FUNCS cap of
        # 500, which is applied before the gap filter.
        lines = []
        for i in range(200):
            lines.append(f"export interface I{i} {{ v{i}: number }}")
            lines.append(f"export type T{i} = I{i} | null;")
        lines.append("export class Big { run() { return 1; } }")
        self._store_parsed("/r/big.ts", "\n".join(lines) + "\n")

        result = analyze_changes(
            self.store,
            changed_files=["/r/big.ts"],
            changed_ranges={"/r/big.ts": [(1, len(lines))]},
        )
        assert {g["name"] for g in result["test_gaps"]} == {"Big", "run"}
        assert len(result["changed_functions"]) == 402


# ---------------------------------------------------------------------------
# A real Git diff through the MCP tool, a declaration changing kind across
# updates, declarations nested in other scopes, and types-only files
# ---------------------------------------------------------------------------

_MULTILINE_WIDGET = (
    "export interface WidgetProps {\n"
    "  title: string;\n"
    "}\n"
    "export type Size = {\n"
    "  w: number;\n"
    "};\n"
    "export class Panel {\n"
    "  render() {\n"
    "    return 1;\n"
    "  }\n"
    "}\n"
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        [
            "git", "-c", "user.email=test@example.com", "-c", "user.name=Test",
            "-c", "commit.gpgsign=false", *args,
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        text=True,
        timeout=30,
    )


class TestRealDiffThroughTheTool:
    """Build -> edit -> update -> ``detect_changes_func`` against a real Git
    diff. The diff's line ranges fall inside multi-line declaration bodies,
    so this pins the range-to-node mapping the reporter's MCP calls use, in
    both detail levels."""

    @pytest.fixture
    def repo(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CRG_DATA_DIR", raising=False)
        root = tmp_path / "ts repo \u00e9"
        (root / "src").mkdir(parents=True)
        _git(root, "init", "-q")
        (root / "src" / "widget.tsx").write_text(_MULTILINE_WIDGET, encoding="utf-8")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "init")
        store = GraphStore(get_db_path(root))
        try:
            assert full_build(root, store)["errors"] == []
        finally:
            store.close()
        return root

    @staticmethod
    def _edit_and_update(root: Path, old: str, new: str) -> None:
        path = root / "src" / "widget.tsx"
        text = path.read_text(encoding="utf-8")
        assert old in text
        path.write_text(text.replace(old, new), encoding="utf-8")
        store = GraphStore(get_db_path(root))
        try:
            incremental_update(root, store, changed_files=["src/widget.tsx"])
        finally:
            store.close()

    def test_edits_inside_declaration_bodies_report_no_gap(self, repo):
        self._edit_and_update(repo, "title: string;", "title: number;")
        self._edit_and_update(repo, "w: number;", "w: string;")

        result = detect_changes_func(base="HEAD", repo_root=str(repo))
        assert result["status"] == "ok"
        assert {f["name"] for f in result["changed_functions"]} == {"WidgetProps", "Size"}
        assert result["test_gaps"] == []
        assert result["test_gaps_total"] == 0

        minimal = detect_changes_func(
            base="HEAD", repo_root=str(repo), detail_level="minimal",
        )
        assert minimal["status"] == "ok"
        assert minimal["test_gap_count"] == 0

    def test_edit_inside_the_class_body_still_reports_its_gaps(self, repo):
        self._edit_and_update(repo, "return 1;", "return 2;")

        result = detect_changes_func(base="HEAD", repo_root=str(repo))
        assert result["status"] == "ok"
        assert {g["name"] for g in result["test_gaps"]} == {"Panel", "render"}

        minimal = detect_changes_func(
            base="HEAD", repo_root=str(repo), detail_level="minimal",
        )
        assert minimal["test_gap_count"] == 2


def test_tag_follows_a_declaration_that_changes_kind_across_updates(tmp_path):
    """An interface refactored into a class must become a gap on the next
    update, and turning it back into a type must drop it again: the tag is
    rewritten with the node, never left behind on it."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git").mkdir()
    src = root / "shape.ts"
    src.write_text("export interface Shape {\n  area(): number;\n}\n", encoding="utf-8")
    store = GraphStore(root / "graph.db")
    try:
        with patch(
            "code_review_graph.incremental.get_all_tracked_files",
            return_value=["shape.ts"],
        ):
            assert full_build(root, store)["errors"] == []

        def gaps() -> set[str]:
            (path,) = [f for f in store.get_all_files() if f.endswith("shape.ts")]
            result = analyze_changes(store, changed_files=[path])
            return {g["name"] for g in result["test_gaps"]}

        assert gaps() == set()

        src.write_text(
            "export class Shape {\n  area() { return 1; }\n}\n", encoding="utf-8",
        )
        incremental_update(root, store, changed_files=["shape.ts"])
        assert gaps() == {"Shape", "area"}

        src.write_text("export type Shape = {\n  area(): number;\n};\n", encoding="utf-8")
        incremental_update(root, store, changed_files=["shape.ts"])
        assert gaps() == set()
    finally:
        store.close()


class TestNestedAndTypesOnlyFiles:
    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = GraphStore(self.tmp.name)

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def _store_parsed(self, path: str, source: str) -> None:
        nodes, _ = CodeParser().parse_bytes(Path(path), source.encode("utf-8"))
        for node in nodes:
            self.store.upsert_node(node, file_hash="abc")
        self.store.commit()

    def test_declarations_local_to_a_function_drop_out_but_the_function_stays(self):
        self._store_parsed(
            "/r/outer.ts",
            "export function outer() {\n"
            "  interface Local { a: number }\n"
            "  type Alias = Local | null;\n"
            "  return 1;\n"
            "}\n",
        )
        result = analyze_changes(
            self.store,
            changed_files=["/r/outer.ts"],
            changed_ranges={"/r/outer.ts": [(2, 3)]},
        )
        assert {"Local", "Alias"} <= {f["name"] for f in result["changed_functions"]}
        assert {g["name"] for g in result["test_gaps"]} == {"outer"}

    @pytest.mark.parametrize(
        ("path", "source", "expected"),
        [
            (
                "/r/global.d.ts",
                "declare global {\n"
                "  interface Window { crg: string }\n"
                "}\n"
                "export type Id = string;\n"
                "export interface Box<T> { v: T }\n",
                set(),
            ),
            (
                "/r/Setup.vue",
                "<script setup lang=\"ts\">\n"
                "export interface P { a: string }\n"
                "type S = number;\n"
                "function f() { return 1; }\n"
                "</script>\n"
                "<template><div/></template>\n",
                {"f"},
            ),
            (
                "/r/Module.svelte",
                "<script context=\"module\" lang=\"ts\">\n"
                "  export interface P { a: string }\n"
                "  export function g() { return 1; }\n"
                "</script>\n"
                "<div/>\n",
                {"g"},
            ),
        ],
        ids=["d.ts", "vue-script-setup", "svelte-module-script"],
    )
    def test_whole_file_change_reports_only_runtime_code(self, path, source, expected):
        """Whole-file fallback over a types-only ``.d.ts`` and over Vue and
        Svelte script variants: declarations are changed nodes but never
        gaps, and the runtime function next to them still is."""
        self._store_parsed(path, source)
        result = analyze_changes(self.store, changed_files=[path])
        assert result["changed_functions"], "the file produced no changed nodes"
        assert {g["name"] for g in result["test_gaps"]} == expected
