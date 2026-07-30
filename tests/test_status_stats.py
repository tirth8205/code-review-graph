"""Regression tests for issue #474 — ``status`` must report the live graph.

``get_stats()`` used to derive ``languages`` from every node row in the
database.  Virtual rows that are not tied to a real indexed file (for
example the Spring ``Event`` nodes emitted by the event resolver with the
synthetic file path ``"event"``) could therefore keep a language alive in
``code-review-graph status`` long after the last real file of that
language left the graph.  These tests pin the contract: the file count and
language list printed by ``status`` always match the files actually
indexed in the graph — after a full build and after an incremental update
that removes every file of one language.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

from code_review_graph import cli
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, incremental_update


def _write_spring_event_trio(root: Path) -> Path:
    """Create Java files that make the event resolver emit a virtual node."""
    pkg = root / "alpha"
    pkg.mkdir(parents=True)
    (pkg / "SharedEvent.java").write_text(
        "package alpha;\nclass SharedEvent {}\n", encoding="utf-8",
    )
    (pkg / "Publisher.java").write_text(
        "package alpha;\n"
        "class Publisher {\n"
        "  void publish() { events.publishEvent(new SharedEvent()); }\n"
        "}\n",
        encoding="utf-8",
    )
    (pkg / "Listener.java").write_text(
        "package alpha;\n"
        "import org.springframework.context.event.EventListener;\n"
        "class Listener {\n"
        "  @EventListener\n"
        "  void on(SharedEvent e) {}\n"
        "}\n",
        encoding="utf-8",
    )
    return pkg


def _build_mixed_repo(tmp_path: Path) -> GraphStore:
    _write_spring_event_trio(tmp_path)
    (tmp_path / "main.py").write_text(
        "def greet():\n    return 'hi'\n", encoding="utf-8",
    )
    db_dir = tmp_path / ".code-review-graph"
    db_dir.mkdir()
    store = GraphStore(db_dir / "graph.db")
    full_build(tmp_path, store)
    return store


def _live_file_inventory(store: GraphStore) -> tuple[int, list[str]]:
    """File count and language list straight from the File rows in SQLite."""
    files = store._conn.execute(
        "SELECT COUNT(*) FROM nodes WHERE kind = 'File'"
    ).fetchone()[0]
    languages = [
        row[0]
        for row in store._conn.execute(
            "SELECT DISTINCT language FROM nodes WHERE kind = 'File' "
            "AND language IS NOT NULL AND language != '' ORDER BY language"
        )
    ]
    return files, languages


class TestStatusMatchesLiveGraph:
    def test_stats_match_db_contents_after_build(self, tmp_path: Path) -> None:
        store = _build_mixed_repo(tmp_path)
        try:
            stats = store.get_stats()
            db_files, db_languages = _live_file_inventory(store)

            assert stats.files_count == db_files == 4
            assert sorted(stats.languages) == db_languages == ["java", "python"]
        finally:
            store.close()

    def test_update_removing_language_drops_it_from_stats(
        self, tmp_path: Path,
    ) -> None:
        """An update that removes every Java file must drop 'java'.

        The deletion is surfaced through stale-file reconciliation (empty
        ``changed_files``), the path a plain git diff does not cover — for
        example when files become ignored or the diff base is unavailable.
        On the buggy code the virtual Event node (file_path='event',
        language='java') survived and kept 'java' in the status output.
        """
        store = _build_mixed_repo(tmp_path)
        try:
            assert "java" in store.get_stats().languages

            pkg = tmp_path / "alpha"
            for java_file in pkg.glob("*.java"):
                java_file.unlink()
            pkg.rmdir()

            result = incremental_update(tmp_path, store, changed_files=[])
            assert result["stale_files_removed"] == 3

            stats = store.get_stats()
            db_files, db_languages = _live_file_inventory(store)

            assert stats.files_count == db_files == 1
            assert sorted(stats.languages) == db_languages == ["python"]
            assert "java" not in stats.languages

            # The stale virtual Event row must be gone from the graph too.
            stale = store._conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE kind = 'Event'"
            ).fetchone()[0]
            assert stale == 0
        finally:
            store.close()

    def test_stats_ignore_rows_not_backed_by_file_nodes(
        self, tmp_path: Path,
    ) -> None:
        """Historical/virtual rows without a File node must not leak."""
        store = _build_mixed_repo(tmp_path)
        try:
            # Simulate a leftover row from an old build: a node whose file
            # was removed from the graph without its row being cleaned up.
            store._conn.execute(
                "INSERT INTO nodes (kind, name, qualified_name, file_path,"
                " language, updated_at) VALUES ('Function', 'old_sub',"
                " 'legacy.pl::old_sub', '/gone/legacy.pl', 'perl', 0)"
            )
            store.commit()

            stats = store.get_stats()
            assert sorted(stats.languages) == ["java", "python"]
            assert "perl" not in stats.languages
        finally:
            store.close()


class TestStatusCli:
    def test_status_json_reports_live_files_and_sorted_languages(
        self, tmp_path: Path, capsys,
    ) -> None:
        store = _build_mixed_repo(tmp_path)
        store.close()

        argv = [
            "code-review-graph", "status", "--repo", str(tmp_path), "--json",
        ]
        with patch.object(sys, "argv", argv):
            cli.main()

        payload = json.loads(capsys.readouterr().out)
        assert payload["files"] == 4
        assert payload["languages"] == ["java", "python"]
