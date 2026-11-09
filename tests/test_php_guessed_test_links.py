"""PHP tests whose calls the graph cannot pin are named as guesses (part of #850).

Laravel tests reach a class through the service container:
``$this->service = app(Service::class)`` or ``app(Service::class)->run()``.
Nothing in the file says which class the call runs on, so the graph cannot
record the test as a test of ``Service::run``. Two things went wrong:

* A call on an expression, such as ``$this->service->run()``, was bound to a
  method of the same name in the test file: when the test was named after the
  method it tests, the test was recorded as calling itself.
* When several imported classes define the called method, the call stays
  unresolved and ``tests_for`` gave nothing, though the test file imports the
  class.

``tests_for`` now names such a test with ``inferred_by: "ambiguous_call"``, so
the answer says the link is a guess. A guess is never coverage: the test-gap
report and the risk score do not count it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from code_review_graph.changes import analyze_changes
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build
from code_review_graph.parser import EdgeInfo, NodeInfo
from code_review_graph.postprocessing import run_post_processing
from code_review_graph.tools.query import query_graph

_COMPOSER = '{"autoload": {"psr-4": {"App\\\\": "app/"}}}\n'

_COMPLETE = (
    "<?php\n"
    "namespace App\\Actions;\n"
    "\n"
    "class CompleteSupplierCount\n"
    "{\n"
    "    public function execute(int $count): int\n"
    "    {\n"
    "        return $count + 1;\n"
    "    }\n"
    "}\n"
)

_ARCHIVE = (
    "<?php\n"
    "namespace App\\Actions;\n"
    "\n"
    "class ArchiveSupplier\n"
    "{\n"
    "    public function execute(int $id): bool\n"
    "    {\n"
    "        return $id > 0;\n"
    "    }\n"
    "}\n"
)

# The shape from the issue, plus a test named after the method it tests.
_TEST = (
    "<?php\n"
    "namespace Tests\\Feature;\n"
    "\n"
    "use App\\Actions\\ArchiveSupplier;\n"
    "use App\\Actions\\CompleteSupplierCount;\n"
    "use PHPUnit\\Framework\\Attributes\\Test;\n"
    "use Tests\\TestCase;\n"
    "\n"
    "class CompleteSupplierCountTest extends TestCase\n"
    "{\n"
    "    private CompleteSupplierCount $service;\n"
    "\n"
    "    public function setUp(): void\n"
    "    {\n"
    "        parent::setUp();\n"
    "        $this->service = app(CompleteSupplierCount::class);\n"
    "    }\n"
    "\n"
    "    public function test_it_completes(): void\n"
    "    {\n"
    "        $completion = app(CompleteSupplierCount::class);\n"
    "        $completion->execute(1);\n"
    "    }\n"
    "\n"
    "    public function test_it_archives(): void\n"
    "    {\n"
    "        app(ArchiveSupplier::class)->execute(2);\n"
    "    }\n"
    "\n"
    "    #[Test]\n"
    "    public function execute(): void\n"
    "    {\n"
    "        $this->assertSame(2, $this->service->execute(1));\n"
    "    }\n"
    "}\n"
)


def _build(tmp_path: Path, files: dict[str, str]) -> GraphStore:
    for rel, source in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    graph_dir = tmp_path / ".code-review-graph"
    graph_dir.mkdir(exist_ok=True)
    store = GraphStore(graph_dir / "graph.db")
    full_build(tmp_path, store)
    run_post_processing(store)
    return store


def _laravel(tmp_path: Path) -> GraphStore:
    return _build(tmp_path, {
        "composer.json": _COMPOSER,
        "app/Actions/CompleteSupplierCount.php": _COMPLETE,
        "app/Actions/ArchiveSupplier.php": _ARCHIVE,
        "tests/Feature/CompleteSupplierCountTest.php": _TEST,
    })


def _qn(tmp_path: Path, rel: str, name: str) -> str:
    return f"{tmp_path}/{rel}::{name}"


def _rows(result: dict) -> dict[str, object]:
    return {row["name"]: row.get("inferred_by") for row in result["results"]}


def test_a_call_on_an_expression_does_not_bind_to_a_method_in_the_test_file(
    tmp_path: Path,
) -> None:
    store = _laravel(tmp_path)
    test_file = "tests/Feature/CompleteSupplierCountTest.php"
    helper = _qn(tmp_path, test_file, "CompleteSupplierCountTest.execute")
    rows = store._conn.execute(
        "SELECT source_qualified, line FROM edges "
        "WHERE kind = 'CALLS' AND target_qualified = ?",
        (helper,),
    ).fetchall()
    store.close()

    # ``$this->service->execute(1)`` inside ``execute`` and
    # ``app(ArchiveSupplier::class)->execute(2)`` both used to land here.
    assert [(row["source_qualified"], row["line"]) for row in rows] == []


def test_tests_for_names_a_test_its_call_could_mean_as_a_guess(
    tmp_path: Path,
) -> None:
    _laravel(tmp_path).close()
    target = _qn(
        tmp_path, "app/Actions/CompleteSupplierCount.php",
        "CompleteSupplierCount.execute",
    )

    result = query_graph("tests_for", target, repo_root=str(tmp_path))

    assert result["status"] == "ok"
    # ``test_it_archives`` imports CompleteSupplierCount too and calls an
    # ``execute``: the graph cannot tell which, so it is named, as a guess.
    assert _rows(result) == {
        "test_it_completes": "ambiguous_call",
        "test_it_archives": "ambiguous_call",
        "execute": "ambiguous_call",
    }
    assert all(row["indirect"] is False for row in result["results"])
    assert "3 of them guesses" in result["summary"]


def test_tests_for_a_class_names_the_guesses_for_its_methods(
    tmp_path: Path,
) -> None:
    _laravel(tmp_path).close()

    by_class = query_graph(
        "tests_for",
        _qn(tmp_path, "app/Actions/ArchiveSupplier.php", "ArchiveSupplier"),
        repo_root=str(tmp_path),
    )
    by_file = query_graph(
        "tests_for", "app/Actions/ArchiveSupplier.php", repo_root=str(tmp_path),
    )

    expected = {
        "test_it_completes": "ambiguous_call",
        "test_it_archives": "ambiguous_call",
        "execute": "ambiguous_call",
    }
    assert _rows(by_class) == expected
    assert _rows(by_file) == expected


def test_the_guess_label_survives_minimal_detail(tmp_path: Path) -> None:
    _laravel(tmp_path).close()
    target = _qn(
        tmp_path, "app/Actions/CompleteSupplierCount.php",
        "CompleteSupplierCount.execute",
    )

    result = query_graph(
        "tests_for", target, repo_root=str(tmp_path), detail_level="minimal",
    )

    assert {row["inferred_by"] for row in result["results"]} == {"ambiguous_call"}


def test_a_pinned_call_carries_no_guess_label(tmp_path: Path) -> None:
    _build(tmp_path, {
        "composer.json": _COMPOSER,
        "app/Actions/CompleteSupplierCount.php": _COMPLETE,
        "tests/Feature/CompleteTest.php": (
            "<?php\n"
            "namespace Tests\\Feature;\n"
            "\n"
            "use App\\Actions\\CompleteSupplierCount;\n"
            "use Tests\\TestCase;\n"
            "\n"
            "class CompleteTest extends TestCase\n"
            "{\n"
            "    public function test_it_completes(): void\n"
            "    {\n"
            "        app(CompleteSupplierCount::class)->execute(1);\n"
            "    }\n"
            "}\n"
        ),
    }).close()
    target = _qn(
        tmp_path, "app/Actions/CompleteSupplierCount.php",
        "CompleteSupplierCount.execute",
    )

    result = query_graph("tests_for", target, repo_root=str(tmp_path))

    # One imported class defines ``execute``, so the call is pinned.
    assert _rows(result) == {"test_it_completes": None}
    assert "guess" not in result["summary"]


def test_a_test_that_does_not_import_the_class_is_not_a_guess(
    tmp_path: Path,
) -> None:
    _build(tmp_path, {
        "composer.json": _COMPOSER,
        "app/Actions/CompleteSupplierCount.php": _COMPLETE,
        "app/Actions/ArchiveSupplier.php": _ARCHIVE,
        "app/Actions/Other.php": (
            "<?php\n"
            "namespace App\\Actions;\n"
            "\n"
            "class Other\n"
            "{\n"
            "    public function execute(): void {}\n"
            "}\n"
        ),
        "tests/Feature/ArchiveTest.php": (
            "<?php\n"
            "namespace Tests\\Feature;\n"
            "\n"
            "use App\\Actions\\ArchiveSupplier;\n"
            "use App\\Actions\\Other;\n"
            "use Tests\\TestCase;\n"
            "\n"
            "class ArchiveTest extends TestCase\n"
            "{\n"
            "    public function test_it_archives(): void\n"
            "    {\n"
            "        $job = app(ArchiveSupplier::class);\n"
            "        $job->execute(1);\n"
            "    }\n"
            "}\n"
        ),
    }).close()
    target = _qn(
        tmp_path, "app/Actions/CompleteSupplierCount.php",
        "CompleteSupplierCount.execute",
    )

    result = query_graph("tests_for", target, repo_root=str(tmp_path))

    assert result["results"] == []


def test_a_test_that_builds_one_candidate_is_no_guess_for_the_others(
    tmp_path: Path,
) -> None:
    job = (
        "<?php\n"
        "namespace App\\Jobs;\n"
        "\n"
        "class {name}\n"
        "{{\n"
        "    public function handle(): void {{}}\n"
        "}}\n"
    )
    _build(tmp_path, {
        "composer.json": _COMPOSER,
        "app/Jobs/UpdateBooks.php": job.format(name="UpdateBooks"),
        "app/Jobs/SyncBooks.php": job.format(name="SyncBooks"),
        # From monica: the test runs UpdateBooks and only checks that a
        # SyncBooks job was queued, so SyncBooks::handle never runs.
        "tests/Jobs/UpdateBooksTest.php": (
            "<?php\n"
            "namespace Tests\\Jobs;\n"
            "\n"
            "use App\\Jobs\\SyncBooks;\n"
            "use App\\Jobs\\UpdateBooks;\n"
            "use Tests\\TestCase;\n"
            "\n"
            "class UpdateBooksTest extends TestCase\n"
            "{\n"
            "    public function test_it_queues_a_sync(): void\n"
            "    {\n"
            "        (new UpdateBooks)->handle();\n"
            "        $this->assertPushed(SyncBooks::class);\n"
            "    }\n"
            "}\n"
        ),
    }).close()

    sync = query_graph(
        "tests_for", "app/Jobs/SyncBooks.php", repo_root=str(tmp_path),
    )
    update = query_graph(
        "tests_for", "app/Jobs/UpdateBooks.php", repo_root=str(tmp_path),
    )

    assert sync["results"] == []
    assert _rows(update) == {"test_it_queues_a_sync": None}


def test_building_one_class_does_not_hide_a_guess_for_another_call(
    tmp_path: Path,
) -> None:
    job = (
        "<?php\n"
        "namespace App\\Jobs;\n"
        "\n"
        "class {name}\n"
        "{{\n"
        "    public function handle(): void {{}}\n"
        "}}\n"
    )
    _build(tmp_path, {
        "composer.json": _COMPOSER,
        "app/Jobs/UpdateBooks.php": job.format(name="UpdateBooks"),
        "app/Jobs/SyncBooks.php": job.format(name="SyncBooks"),
        "tests/Jobs/BooksTest.php": (
            "<?php\n"
            "namespace Tests\\Jobs;\n"
            "\n"
            "use App\\Jobs\\SyncBooks;\n"
            "use App\\Jobs\\UpdateBooks;\n"
            "use Tests\\TestCase;\n"
            "\n"
            "class BooksTest extends TestCase\n"
            "{\n"
            "    public function test_it_runs_both(): void\n"
            "    {\n"
            "        (new UpdateBooks)->handle();\n"
            "        app(SyncBooks::class)->handle();\n"
            "    }\n"
            "}\n"
        ),
    }).close()

    sync = query_graph(
        "tests_for", "app/Jobs/SyncBooks.php", repo_root=str(tmp_path),
    )

    # ``new UpdateBooks`` settles only the call made on it; the second call
    # could still mean SyncBooks::handle.
    assert _rows(sync) == {"test_it_runs_both": "ambiguous_call"}


def test_a_class_built_on_its_own_line_still_settles_a_later_call(
    tmp_path: Path,
) -> None:
    job = (
        "<?php\n"
        "namespace App\\Jobs;\n"
        "\n"
        "class {name}\n"
        "{{\n"
        "    public function handle(): void {{}}\n"
        "}}\n"
    )
    _build(tmp_path, {
        "composer.json": _COMPOSER,
        "app/Jobs/DeleteLocalVCard.php": job.format(name="DeleteLocalVCard"),
        "app/Jobs/DeleteMultipleVCard.php": job.format(name="DeleteMultipleVCard"),
        # From monica: the job is built inside a batch, then run through a
        # chained call some lines later.
        "tests/Jobs/DeleteMultipleVCardTest.php": (
            "<?php\n"
            "namespace Tests\\Jobs;\n"
            "\n"
            "use App\\Jobs\\DeleteLocalVCard;\n"
            "use App\\Jobs\\DeleteMultipleVCard;\n"
            "use Tests\\TestCase;\n"
            "\n"
            "class DeleteMultipleVCardTest extends TestCase\n"
            "{\n"
            "    public function test_it_deletes_cards(): void\n"
            "    {\n"
            "        $batch = $this->fake->batch([\n"
            "            $job = new DeleteMultipleVCard(),\n"
            "        ]);\n"
            "        $job->withBatchId($batch->id)->handle();\n"
            "        $this->assertDispatched(DeleteLocalVCard::class);\n"
            "    }\n"
            "}\n"
        ),
    }).close()

    local = query_graph(
        "tests_for", "app/Jobs/DeleteLocalVCard.php", repo_root=str(tmp_path),
    )

    assert local["results"] == []


def test_guesses_are_not_coverage_and_the_gap_report_says_so(
    tmp_path: Path,
) -> None:
    store = _laravel(tmp_path)
    path = str(tmp_path / "app/Actions/CompleteSupplierCount.php")

    result = analyze_changes(store, [path], {path: [(6, 9)]})
    store.close()

    assert "execute" in {gap["name"] for gap in result["test_gaps"]}
    assert "PHP" in result["summary"]
    assert "guess" in result["summary"]


@pytest.mark.parametrize(("language", "expected"), [("php", 1), ("python", 0)])
def test_guesses_are_left_out_for_other_languages(
    tmp_path: Path, language: str, expected: int,
) -> None:
    store = GraphStore(tmp_path / "graph.db")
    for name, path, parent, is_test in (
        ("execute", "app/a.src", "A", False),
        ("execute", "app/b.src", "B", False),
        ("test_it", "tests/t.src", None, True),
    ):
        store.upsert_node(NodeInfo(
            kind="Test" if is_test else "Function", name=name, file_path=path,
            line_start=1, line_end=3, language=language, parent_name=parent,
            is_test=is_test, extra={},
        ), file_hash="h")
    candidates = ["app/a.src::A.execute", "app/b.src::B.execute"]
    store.upsert_edge(EdgeInfo(
        kind="TESTED_BY", source="execute", target="tests/t.src::test_it",
        file_path="tests/t.src", line=2,
        extra={"ambiguous_targets": candidates, "ambiguous_target_count": 2},
    ))
    store.commit()

    found = store.get_ambiguous_call_tests("app/a.src::A.execute")
    store.close()

    # Other languages decide such calls their own way; only PHP guesses.
    assert len(found) == expected
