"""Recovery for non-empty graphs produced by partial diff-only builds."""

from __future__ import annotations

from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import get_db_path
from code_review_graph.parser import CodeParser
from code_review_graph.tools.build import build_or_update_graph


def _make_repo(root: Path, source_count: int = 8) -> None:
    (root / ".git").mkdir()
    (root / ".code-review-graph").mkdir()
    for index in range(source_count):
        (root / f"module{index}.py").write_text(
            f"def value{index}() -> int:\n    return {index}\n",
            encoding="utf-8",
        )


def _seed_partial_graph(root: Path, represented_count: int = 1) -> None:
    with GraphStore(get_db_path(root)) as store:
        parser = CodeParser(root)
        for relative in [f"module{index}.py" for index in range(represented_count)]:
            source_path = root / relative
            nodes, edges = parser.parse_file(source_path)
            store.store_file_nodes_edges(
                str(source_path), nodes, edges,
                f"seed-{relative}",
            )
        store.set_metadata("last_build_type", "incremental")
        store.set_metadata("git_head_sha", "0" * 40)


def test_severely_partial_synced_graph_gets_full_rebuild(
    tmp_path: Path, monkeypatch,
) -> None:
    _make_repo(tmp_path)
    _seed_partial_graph(tmp_path)
    monkeypatch.setenv("CRG_SERIAL_PARSE", "1")
    monkeypatch.setattr(
        "code_review_graph.tools.build.resolve_incremental_base",
        lambda *_args, **_kwargs: "HEAD~1",
    )
    monkeypatch.setattr(
        "code_review_graph.incremental.get_changed_files",
        lambda *_args, **_kwargs: [],
    )

    result = build_or_update_graph(
        repo_root=str(tmp_path),
        full_rebuild=False,
        postprocess="none",
    )

    assert result["build_type"] == "full"
    assert result["files_parsed"] == 8
    assert result["recovery_reason"] == "partial_graph"
    with GraphStore(get_db_path(tmp_path)) as store:
        assert len(store.get_all_files()) == 8
        assert store.get_metadata("last_build_type") == "full"
        assert store.get_metadata("intentionally_incomplete") == "0"


def test_complete_incremental_graph_is_not_rebuilt(
    tmp_path: Path, monkeypatch,
) -> None:
    _make_repo(tmp_path)
    monkeypatch.setenv("CRG_SERIAL_PARSE", "1")
    first = build_or_update_graph(
        repo_root=str(tmp_path),
        full_rebuild=True,
        postprocess="none",
    )
    assert first["files_parsed"] == 8
    with GraphStore(get_db_path(tmp_path)) as store:
        store.set_metadata("last_build_type", "incremental")

    monkeypatch.setattr(
        "code_review_graph.tools.build.resolve_incremental_base",
        lambda *_args, **_kwargs: "HEAD~1",
    )
    monkeypatch.setattr(
        "code_review_graph.incremental.get_changed_files",
        lambda *_args, **_kwargs: [],
    )

    result = build_or_update_graph(
        repo_root=str(tmp_path),
        full_rebuild=False,
        postprocess="none",
    )

    assert result["build_type"] == "incremental"
    assert result["files_updated"] == 0
    assert "recovery_reason" not in result


def test_unknown_tracked_assets_do_not_look_like_a_poisoned_graph(
    tmp_path: Path, monkeypatch,
) -> None:
    _make_repo(tmp_path)
    for index in range(80):
        (tmp_path / f"asset{index}.txt").write_text(
            f"documentation or data {index}\n", encoding="utf-8",
        )
    _seed_partial_graph(tmp_path, represented_count=8)
    monkeypatch.setattr(
        "code_review_graph.tools.build.resolve_incremental_base",
        lambda *_args, **_kwargs: "HEAD~1",
    )
    monkeypatch.setattr(
        "code_review_graph.incremental.get_changed_files",
        lambda *_args, **_kwargs: [],
    )

    result = build_or_update_graph(
        repo_root=str(tmp_path),
        full_rebuild=False,
        postprocess="none",
    )

    assert result["build_type"] == "incremental"
    assert "recovery_reason" not in result


def test_intentional_forget_marker_disables_coverage_recovery(tmp_path: Path) -> None:
    _make_repo(tmp_path)
    _seed_partial_graph(tmp_path, represented_count=1)
    with GraphStore(get_db_path(tmp_path)) as store:
        store.set_metadata("intentionally_incomplete", "1")
        from code_review_graph.tools.build import _should_rebuild_partial_graph

        assert _should_rebuild_partial_graph(tmp_path, store) is False
