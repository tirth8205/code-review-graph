"""A call to an inherited method finds the ancestor that declares it, and only that.

``this.handle()`` in ``Leaf extends Alpha`` runs ``Alpha.handle``. The parser
sees one file, so it wrote ``<leaf>::Leaf.handle``, which names no node, and
``callers_of(Alpha.handle)`` found nothing. The same held for ``$this->run()``
in PHP and for ``pet.getName()`` on a ``Pet`` that inherits ``getName``.

Each test here also pins a way such a pass can invent a target: two classes with
one short name, a parent the graph cannot name, a same-package class that is
not an ancestor, Python's method order, and an update that changes the answer.
See: #984. The approach (an INHERITS walk, receiver types, Java packages) comes
from #1036 by Anthony Ala.
"""

from __future__ import annotations

import json
from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import get_db_path
from code_review_graph.parser import CodeParser
from code_review_graph.postprocessing import run_post_processing
from code_review_graph.refactor import find_dead_code
from code_review_graph.tools import query_graph
from code_review_graph.tools.build import build_or_update_graph


def _write(root: Path, files: dict[str, str]) -> None:
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _build(root: Path, files: dict[str, str]) -> GraphStore:
    root = root.resolve()
    (root / ".code-review-graph").mkdir(parents=True, exist_ok=True)
    _write(root, files)
    result = build_or_update_graph(full_rebuild=True, repo_root=str(root))
    assert result["status"] == "ok", result
    return GraphStore(get_db_path(root))


def _calls(store: GraphStore, kind: str = "CALLS") -> list[dict]:
    rows = store._conn.execute(
        "SELECT source_qualified, target_qualified, line, extra, confidence, "
        "confidence_tier FROM edges WHERE kind = ? ORDER BY file_path, line, id",
        (kind,),
    ).fetchall()
    return [
        {
            "source": row["source_qualified"],
            "target": row["target_qualified"],
            "line": row["line"],
            "extra": json.loads(row["extra"] or "{}"),
            "confidence": row["confidence"],
            "tier": row["confidence_tier"],
        }
        for row in rows
    ]


def _call_from(store: GraphStore, caller_suffix: str, method: str) -> dict:
    rows = [
        row for row in _calls(store)
        if row["source"].endswith(caller_suffix)
        and row["target"].rsplit(".", 1)[-1].rsplit("::", 1)[-1] == method
    ]
    assert len(rows) == 1, rows
    return rows[0]


TS_BASE = {
    "base.ts": (
        "export class HttpService {\n"
        "  protected onError(error: string): string {\n"
        "    return error;\n"
        "  }\n"
        "}\n"
    ),
    "api.ts": (
        "import { HttpService } from './base';\n\n"
        "export abstract class ApiService<U> extends HttpService {\n"
        "  protected unwrap(payload: U): U {\n"
        "    return payload;\n"
        "  }\n"
        "}\n"
    ),
    "export.ts": (
        "import { HttpService } from './base';\n\n"
        "export class ExportService extends HttpService {\n"
        "  run(): string {\n"
        "    return this.onError('boom');\n"
        "  }\n"
        "}\n"
    ),
    "import.ts": (
        "import { ApiService } from './api';\n\n"
        "export class ImportService extends ApiService<string> {\n"
        "  load(): string {\n"
        "    return this.onError('boom');\n"
        "  }\n"
        "}\n"
    ),
}


# -- What must bind ------------------------------------------------------------


def test_inherited_self_call_through_a_subclass_binds(tmp_path):
    store = _build(tmp_path, TS_BASE)
    root = tmp_path.resolve()
    try:
        row = _call_from(store, "ExportService.run", "onError")
    finally:
        store.close()

    assert row["target"] == f"{root}/base.ts::HttpService.onError"
    assert row["tier"] == "INFERRED"
    assert row["confidence"] == 0.8
    assert row["extra"]["inherited_endpoint"] == f"{root}/export.ts::ExportService.onError"
    assert "claimed_endpoint" not in row["extra"]
    callers = query_graph("callers_of", f"{root}/base.ts::HttpService.onError", str(root))
    assert {result["name"] for result in callers["results"]} == {"run", "load"}


def test_two_levels_up_through_a_generic_abstract_class(tmp_path):
    store = _build(tmp_path, TS_BASE)
    root = tmp_path.resolve()
    try:
        row = _call_from(store, "ImportService.load", "onError")
    finally:
        store.close()

    assert row["target"] == f"{root}/base.ts::HttpService.onError"


def test_a_method_the_class_declares_itself_stays_put(tmp_path):
    store = _build(tmp_path, {
        **TS_BASE,
        "own.ts": (
            "import { HttpService } from './base';\n\n"
            "export class OwnService extends HttpService {\n"
            "  protected onError(error: string): string {\n"
            "    return error;\n"
            "  }\n\n"
            "  run(): string {\n"
            "    return this.onError('boom');\n"
            "  }\n"
            "}\n"
        ),
    })
    root = tmp_path.resolve()
    try:
        row = _call_from(store, "OwnService.run", "onError")
    finally:
        store.close()

    assert row["target"] == f"{root}/own.ts::OwnService.onError"
    assert "inherited_endpoint" not in row["extra"]
    assert row["tier"] == "EXTRACTED"


def test_php_this_binds_to_its_own_method_and_to_an_inherited_one(tmp_path):
    """``$this`` is the calling object, not some other variable."""
    store = _build(tmp_path, {
        "src/Base.php": (
            "<?php\nnamespace App;\n\n"
            "class Base {\n    public function execute() {}\n}\n"
        ),
        "src/Repo.php": (
            "<?php\nnamespace App;\n\n"
            "class Repo extends Base {\n"
            "    public function run() {\n"
            "        $this->execute();\n"
            "        $this->own();\n"
            "    }\n"
            "    public function own() {}\n"
            "}\n"
        ),
    })
    root = tmp_path.resolve()
    try:
        inherited = _call_from(store, "Repo.run", "execute")
        own = _call_from(store, "Repo.run", "own")
    finally:
        store.close()

    assert inherited["target"] == f"{root}/src/Base.php::Base.execute"
    assert inherited["extra"]["inherited_through"] == f"{root}/src/Repo.php::Repo"
    assert inherited["tier"] == "INFERRED"
    assert own["target"] == f"{root}/src/Repo.php::Repo.own"


def test_php_interface_is_not_where_the_body_comes_from(tmp_path):
    """``implements Runner`` declares ``execute`` too, but runs nothing."""
    store = _build(tmp_path, {
        "src/Runner.php": (
            "<?php\nnamespace App;\n\n"
            "interface Runner {\n    public function execute();\n}\n"
        ),
        "src/Base.php": (
            "<?php\nnamespace App;\n\n"
            "abstract class Base {\n    public function execute() {}\n}\n"
        ),
        "src/Job.php": (
            "<?php\nnamespace App\\Jobs;\n\n"
            "use App\\Runner;\n\n"
            "class Job extends \\App\\Base implements Runner {\n"
            "    public function run() { $this->execute(); }\n"
            "}\n"
        ),
    })
    root = tmp_path.resolve()
    try:
        row = _call_from(store, "Job.run", "execute")
    finally:
        store.close()

    assert row["target"] == f"{root}/src/Base.php::Base.execute"


def test_java_test_under_another_source_root_reaches_the_ancestor(tmp_path):
    """``src/test`` imports ``owner.Pet`` by its full name; that still says
    which ``Pet``, and ``Pet`` inherits ``getName``."""
    store = _build(tmp_path, {
        **JAVA_MODEL,
        "test/owner/other/PetTests.java": (
            "package owner.other;\n\n"
            "import owner.Pet;\n\n"
            "class PetTests {\n"
            "    void name() {\n"
            "        Pet pet = new Pet();\n"
            "        pet.getName();\n"
            "    }\n"
            "}\n"
        ),
    })
    root = tmp_path.resolve()
    try:
        row = _call_from(store, "PetTests.name", "getName")
    finally:
        store.close()

    assert row["target"] == f"{root}/src/model/NamedEntity.java::NamedEntity.getName"


def test_php_this_still_reaches_a_method_of_its_own_class(tmp_path):
    store = _build(tmp_path, {
        "Repo.php": (
            "<?php\n"
            "class Repo {\n"
            "    public function run() { return $this->execute(); }\n"
            "    public function execute() { return 1; }\n"
            "}\n"
        ),
    })
    root = tmp_path.resolve()
    try:
        row = _call_from(store, "Repo.run", "execute")
    finally:
        store.close()

    assert row["target"] == f"{root}/Repo.php::Repo.execute"


JAVA_MODEL = {
    "src/model/NamedEntity.java": (
        "package model;\n\n"
        "public class NamedEntity {\n"
        "    public String getName() { return \"\"; }\n"
        "}\n"
    ),
    "src/owner/Pet.java": (
        "package owner;\n\n"
        "import model.NamedEntity;\n\n"
        "public class Pet extends NamedEntity {\n"
        "    public void addVisit(String visit) {}\n"
        "}\n"
    ),
    "src/owner/Owner.java": (
        "package owner;\n\n"
        "public class Owner {\n"
        "    public void addVisit(Pet pet, String visit) {\n"
        "        pet.addVisit(visit);\n"
        "        pet.getName();\n"
        "    }\n"
        "}\n"
    ),
}


def test_java_receiver_type_in_the_same_package_finds_the_inherited_method(tmp_path):
    store = _build(tmp_path, JAVA_MODEL)
    root = tmp_path.resolve()
    try:
        inherited = _call_from(store, "Owner.addVisit", "getName")
        direct = _call_from(store, "Owner.addVisit", "addVisit")
    finally:
        store.close()

    assert inherited["target"] == f"{root}/src/model/NamedEntity.java::NamedEntity.getName"
    assert inherited["extra"]["receiver_type"] == "Pet"
    # The parser binds ``pet`` to ``Pet`` in the same package (#1154), so the
    # call is written ``Pet.getName`` and the inherited pass moves it on.
    assert inherited["extra"]["inherited_endpoint"] == f"{root}/src/owner/Pet.java::Pet.getName"
    # It was a self-loop: ``pet.addVisit`` sent to ``Owner.addVisit``.
    assert direct["target"] == f"{root}/src/owner/Pet.java::Pet.addVisit"


def test_java_this_call_reaches_the_ancestor(tmp_path):
    store = _build(tmp_path, {
        **JAVA_MODEL,
        "src/owner/Cat.java": (
            "package owner;\n\n"
            "public class Cat extends Pet {\n"
            "    public String label() { return this.getName(); }\n"
            "}\n"
        ),
    })
    root = tmp_path.resolve()
    try:
        row = _call_from(store, "Cat.label", "getName")
    finally:
        store.close()

    assert row["target"] == f"{root}/src/model/NamedEntity.java::NamedEntity.getName"


def test_a_test_through_a_subclass_is_a_test_of_the_ancestor(tmp_path):
    """The TESTED_BY mirror moves with the call, so ``tests_for`` sees it."""
    store = _build(tmp_path, {
        "base.py": "class Base:\n    def shared(self):\n        return 1\n",
        "tests/test_child.py": (
            "from base import Base\n\n\n"
            "class TestChild(Base):\n"
            "    def test_shared(self):\n"
            "        assert self.shared() == 1\n"
        ),
    })
    root = tmp_path.resolve()
    try:
        mirrors = [
            row for row in _calls(store, "TESTED_BY")
            if row["target"].endswith("TestChild.test_shared")
        ]
    finally:
        store.close()

    assert [row["source"] for row in mirrors] == [f"{root}/base.py::Base.shared"]
    assert mirrors[0]["tier"] == "INFERRED"
    tests = query_graph("tests_for", f"{root}/base.py::Base.shared", str(root))
    assert [result["name"] for result in tests["results"]] == ["test_shared"]


# -- What must not bind --------------------------------------------------------


def test_a_homonym_class_in_another_file_does_not_bind(tmp_path):
    """Two ``Widget`` classes, two ``Base`` classes: each call keeps to its own."""
    store = _build(tmp_path, {
        "a/base.ts": "export class Base {\n  other(): void {}\n}\n",
        "b/base.ts": "export class Base {\n  handle(): void {}\n}\n",
        "alpha.ts": "export class Alpha {\n  other(): void {}\n}\n",
        "beta.ts": "export class Beta {\n  handle(): void {}\n}\n",
        "one.ts": (
            "import { Alpha } from './alpha';\n\n"
            "export class Widget extends Alpha {\n"
            "  run(): void {\n"
            "    this.handle();\n"
            "  }\n"
            "}\n"
        ),
        "two.ts": (
            "import { Beta } from './beta';\n\n"
            "export class Widget extends Beta {\n"
            "  run(): void {\n"
            "    this.handle();\n"
            "  }\n"
            "}\n"
        ),
        "three.ts": (
            "import { Base } from './a/base';\n\n"
            "export class Gadget extends Base {\n"
            "  run(): void {\n"
            "    this.handle();\n"
            "  }\n"
            "}\n"
        ),
    })
    root = tmp_path.resolve()
    try:
        rows = [row for row in _calls(store) if row["source"].endswith(".run")]
        by_file = {row["source"].split("::")[0].rsplit("/", 1)[-1]: row for row in rows}
    finally:
        store.close()

    assert by_file["two.ts"]["target"] == f"{root}/beta.ts::Beta.handle"
    assert by_file["one.ts"]["target"] == "handle"
    assert by_file["one.ts"]["tier"] == "UNRESOLVED"
    assert by_file["three.ts"]["target"] == "handle"
    assert by_file["three.ts"]["tier"] == "UNRESOLVED"


def test_same_package_class_that_is_not_an_ancestor_does_not_bind(tmp_path):
    """``A extends ext.Base`` calling ``ping()``; ``B`` in A's package has one."""
    store = _build(tmp_path, {
        "src/p/A.java": (
            "package p;\n\n"
            "import ext.Base;\n\n"
            "public class A extends Base {\n"
            "    public void run() {\n"
            "        ping();\n"
            "        this.ping();\n"
            "    }\n"
            "}\n"
        ),
        "src/p/B.java": (
            "package p;\n\n"
            "public class B {\n"
            "    public void ping() {}\n"
            "}\n"
        ),
    })
    root = tmp_path.resolve()
    try:
        rows = [row for row in _calls(store) if row["source"].endswith("A.run")]
    finally:
        store.close()

    assert len(rows) == 2
    assert all(row["target"] != f"{root}/src/p/B.java::B.ping" for row in rows)
    assert {row["tier"] for row in rows} == {"UNRESOLVED"}


def test_a_typed_receiver_whose_class_lacks_the_method_does_not_loop_back(tmp_path):
    """``h.equals(x)`` inside ``Widget.equals`` is not a call to itself."""
    store = _build(tmp_path, {
        "src/p/Helper.java": "package p;\n\npublic class Helper {\n}\n",
        "src/p/Widget.java": (
            "package p;\n\n"
            "public class Widget {\n"
            "    private Helper h;\n"
            "    public boolean equals(Object other) {\n"
            "        return h.equals(other);\n"
            "    }\n"
            "}\n"
        ),
    })
    try:
        row = _call_from(store, "Widget.equals", "equals")
    finally:
        store.close()

    assert row["target"] == "equals"
    assert row["tier"] == "UNRESOLVED"


def test_an_untyped_receiver_does_not_land_on_the_callers_own_method(tmp_path):
    store = _build(tmp_path, {
        "widget.ts": (
            "export class Widget {\n"
            "  equals(other: unknown): boolean {\n"
            "    const h = make();\n"
            "    return h.equals(other);\n"
            "  }\n"
            "}\n"
            "declare function make(): any;\n"
        ),
    })
    root = tmp_path.resolve()
    try:
        rows = [row for row in _calls(store) if row["source"].endswith("Widget.equals")]
    finally:
        store.close()

    assert all(row["target"] != f"{root}/widget.ts::Widget.equals" for row in rows)


def test_setting_aside_the_callers_own_method_does_not_crown_another(tmp_path):
    """``Math.random()`` inside ``WordMother.random``, with an imported
    ``MotherCreator.random`` too: still two guesses, never the import."""
    store = _build(tmp_path, {
        "creator.ts": "export class MotherCreator {\n  static random(): number { return 1; }\n}\n",
        "word.ts": (
            "import { MotherCreator } from './creator';\n\n"
            "export class WordMother {\n"
            "  static random(): number {\n"
            "    return MotherCreator.random() + Math.random();\n"
            "  }\n"
            "}\n"
        ),
    })
    root = tmp_path.resolve()
    try:
        rows = [
            row for row in _calls(store)
            if row["source"].endswith("WordMother.random")
            and row["extra"].get("receiver") == "Math"
        ]
    finally:
        store.close()

    assert len(rows) == 1
    assert rows[0]["target"] == "random"
    assert rows[0]["tier"] == "AMBIGUOUS"
    assert f"{root}/creator.ts::MotherCreator.random" in rows[0]["extra"]["ambiguous_targets"]


def test_python_two_parents_that_both_declare_it_do_not_bind(tmp_path):
    """C(A, B): Python runs ``Root.m`` through A, not ``B.m``; a walk that
    stops at the first level would pick B. With two declarations, decline."""
    store = _build(tmp_path, {
        "tree.py": (
            "class Root:\n    def m(self):\n        return 0\n\n\n"
            "class A(Root):\n    pass\n\n\n"
            "class B:\n    def m(self):\n        return 1\n\n\n"
            "class C(A, B):\n    def run(self):\n        return self.m()\n"
        ),
    })
    try:
        row = _call_from(store, "C.run", "m")
    finally:
        store.close()

    assert row["target"] == "m"
    assert row["tier"] == "UNRESOLVED"


def test_python_parent_outside_the_graph_stops_the_walk(tmp_path):
    store = _build(tmp_path, {
        "base.py": "class Base:\n    def other(self):\n        return 1\n",
        "child.py": (
            "from base import Base\n"
            "from somewhere import Mixin\n\n\n"
            "class Grand(Base):\n    pass\n\n\n"
            "class Child(Grand, Mixin):\n"
            "    def run(self):\n"
            "        return self.other()\n"
        ),
    })
    try:
        row = _call_from(store, "Child.run", "other")
    finally:
        store.close()

    # Mixin may declare ``other`` too, and Python would look there first.
    assert row["target"] == "other"


# -- Updates, cost and other readers -------------------------------------------


def _parse_into(store: GraphStore, root: Path, relative: str) -> None:
    """What an update does for one file: drop its rows, store a fresh parse."""
    path = root / relative
    store.remove_file_data(str(path))
    nodes, edges = CodeParser(repo_root=root).parse_file(path)
    for node in nodes:
        store.upsert_node(node)
    for edge in edges:
        store.upsert_edge(edge)
    store.commit()


CHAIN = {
    "a.ts": "export class A {\n  m(): number { return 1; }\n}\n",
    "b.ts": "import { A } from './a';\n\nexport class B extends A {\n}\n",
    "c.ts": "import { B } from './b';\n\nexport class C extends B {\n}\n",
    "d.ts": "import { C } from './c';\n\nexport class D extends C {\n}\n",
    "e.ts": (
        "import { D } from './d';\n\n"
        "export class E extends D {\n"
        "  run(): number { return this.m(); }\n"
        "}\n"
    ),
}


def _snapshot(store: GraphStore) -> list[tuple]:
    return [
        (row["source"], row["target"], row["line"], json.dumps(row["extra"], sort_keys=True),
         row["tier"], row["confidence"])
        for kind in ("CALLS", "TESTED_BY")
        for row in _calls(store, kind)
    ]


def test_an_update_moves_the_call_as_a_rebuild_would(tmp_path):
    """E -> D -> C -> B -> A. B gains ``m`` and loses it again, and only b.ts
    is parsed each time; e.ts's call follows, and equals a fresh build."""
    root = (tmp_path / "repo").resolve()
    store = _build(root, CHAIN)
    try:
        assert _call_from(store, "E.run", "m")["target"] == f"{root}/a.ts::A.m"

        _write(root, {"b.ts": (
            "import { A } from './a';\n\n"
            "export class B extends A {\n  m(): number { return 2; }\n}\n"
        )})
        _parse_into(store, root, "b.ts")
        run_post_processing(store)
        assert _call_from(store, "E.run", "m")["target"] == f"{root}/b.ts::B.m"
        updated = _snapshot(store)
    finally:
        store.close()
    fresh_root = tmp_path / "fresh"
    fresh = _build(fresh_root, {**CHAIN, "b.ts": (
        "import { A } from './a';\n\n"
        "export class B extends A {\n  m(): number { return 2; }\n}\n"
    )})
    try:
        rebuilt = _snapshot(fresh)
    finally:
        fresh.close()
    assert [row[1:] for row in updated] == [
        tuple(part.replace(str(fresh_root.resolve()), str(root)) if isinstance(part, str)
              else part for part in row[1:])
        for row in rebuilt
    ]

    store = GraphStore(get_db_path(root))
    try:
        _write(root, {"a.ts": "export class A {\n}\n", "b.ts": CHAIN["b.ts"]})
        _parse_into(store, root, "a.ts")
        _parse_into(store, root, "b.ts")
        run_post_processing(store)
        row = _call_from(store, "E.run", "m")
    finally:
        store.close()
    # Nothing declares ``m`` now: back to what a fresh parse and the settle
    # pass write, and labelled so.
    assert row["target"] == "m"
    assert row["extra"]["claimed_endpoint"] == f"{root}/e.ts::E.m"
    assert "inherited_endpoint" not in row["extra"]
    assert row["tier"] == "UNRESOLVED"


def test_a_settled_call_is_taken_up_once_the_ancestor_appears(tmp_path):
    root = tmp_path.resolve()
    store = _build(root, {**CHAIN, "a.ts": "export class A {\n}\n"})
    try:
        settled = _call_from(store, "E.run", "m")
        assert settled["extra"]["claimed_endpoint"] == f"{root}/e.ts::E.m"

        _write(root, {"a.ts": CHAIN["a.ts"]})
        _parse_into(store, root, "a.ts")
        store.refresh_target_resolution()
        row = _call_from(store, "E.run", "m")
    finally:
        store.close()

    assert row["target"] == f"{root}/a.ts::A.m"
    assert "claimed_endpoint" not in row["extra"]


def test_the_walk_writes_once_and_a_second_pass_moves_nothing(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    store = _build(root, TS_BASE)
    try:
        # Put the parser's names back, as before any pass ran.
        store._conn.execute(
            "UPDATE edges SET target_qualified = json_extract(extra, '$.inherited_endpoint'), "
            "extra = json_remove(extra, '$.inherited_endpoint') "
            "WHERE extra LIKE '%inherited_endpoint%'"
        )
        store.commit()
        transactions = []
        original = store._begin_immediate
        monkeypatch.setattr(
            store, "_begin_immediate", lambda: (transactions.append(1), original())[1],
        )
        assert store._resolve_inherited_endpoints() == 2
        assert len(transactions) == 1
        assert store._resolve_inherited_endpoints() == 0
    finally:
        store.close()


def test_a_class_used_only_through_an_inherited_method_stays_alive(tmp_path):
    store = _build(tmp_path, {
        "base.py": "class Base:\n    @staticmethod\n    def make():\n        return 1\n",
        "util.py": "from base import Base\n\n\nclass Util(Base):\n    pass\n",
        "app.py": "from util import Util\n\n\ndef run():\n    return Util.make()\n",
    })
    root = tmp_path.resolve()
    try:
        row = _call_from(store, "app.py::run", "make")
        dead = {node["name"] for node in find_dead_code(store)}
    finally:
        store.close()

    assert row["target"] == f"{root}/base.py::Base.make"
    assert "Util" not in dead
