"""Recovery for non-empty graphs produced by partial diff-only builds.

Issue #803, PR #881. The edge-case tests at the end of the module drive the
recovery through real Git repositories and the real seeding path a released
diff-only build used.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from code_review_graph import cli
from code_review_graph.build_state import BUILD_IN_PROGRESS, BUILD_STATE_KEY
from code_review_graph.errors import GraphRootMismatchError
from code_review_graph.forget import forget_files
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, get_db_path, incremental_update
from code_review_graph.parser import CodeParser, NodeInfo
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


# ---------------------------------------------------------------------------
# Edge cases (#881 hardening)
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=T", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _git_repo(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    for relative, body in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root.resolve()


def _python_files(count: int, folder: str = "pkg") -> dict[str, str]:
    return {
        f"{folder}/mod{index}.py": f"def value{index}() -> int:\n    return {index}\n"
        for index in range(count)
    }


def _stored_names(repo: Path) -> set[str]:
    with GraphStore(get_db_path(repo)) as store:
        return {Path(path).name for path in store.get_file_marker_paths()}


def test_released_diff_only_build_is_recovered_then_stays_incremental(
    tmp_path: Path, monkeypatch,
) -> None:
    """The real poisoning path: a diff-only update into an empty graph.

    A released version stored only the changed file and still wrote the Git
    anchor, so the automatic base resolves and staging updated incrementally
    forever. The repository path holds a space and a non-ASCII character, and
    one file name is non-ASCII, so ``git ls-files -z`` paths must match the
    stored spellings. Later updates must not flip back into rebuilds.
    """
    monkeypatch.setenv("CRG_SERIAL_PARSE", "1")
    files = _python_files(10)
    files["pkg/café.py"] = "def cafe() -> int:\n    return 1\n"
    repo = _git_repo(tmp_path / "my repo ü", files)
    with GraphStore(get_db_path(repo)) as store:
        seeded = incremental_update(repo, store, changed_files=["pkg/mod0.py"])
        assert seeded["files_updated"] == 1
        assert store.get_metadata("git_head_sha") == _git(repo, "rev-parse", "HEAD")

    first = build_or_update_graph(repo_root=str(repo), postprocess="none")
    assert first["build_type"] == "full"
    assert first["recovery_reason"] == "partial_graph"
    assert first["files_parsed"] == 11
    assert "café.py" in _stored_names(repo)
    assert len(_stored_names(repo)) == 11

    for _ in range(2):
        again = build_or_update_graph(repo_root=str(repo), postprocess="none")
        assert again["build_type"] == "incremental"
        assert "recovery_reason" not in again
    assert len(_stored_names(repo)) == 11


def test_forget_records_marker_and_update_does_not_resurrect_files(
    tmp_path: Path, monkeypatch,
) -> None:
    """Forgetting most of a repository is intentional, not a poisoned graph."""
    monkeypatch.setenv("CRG_SERIAL_PARSE", "1")
    repo = _git_repo(tmp_path / "repo", _python_files(8))
    build_or_update_graph(repo_root=str(repo), full_rebuild=True, postprocess="none")
    with GraphStore(get_db_path(repo)) as store:
        targets = [
            path for path in store.get_file_marker_paths()
            if not path.endswith("mod0.py")
        ]
        assert len(targets) == 7
        forget_files(store, repo, targets)
        assert store.get_metadata("intentionally_incomplete") == "1"

    result = build_or_update_graph(repo_root=str(repo), postprocess="none")

    assert result["build_type"] == "incremental"
    assert "recovery_reason" not in result
    assert _stored_names(repo) == {"mod0.py"}


def test_forget_dry_run_does_not_record_the_marker(tmp_path: Path, capsys) -> None:
    repo = _git_repo(tmp_path / "repo", _python_files(8))
    build_or_update_graph(repo_root=str(repo), full_rebuild=True, postprocess="none")
    argv = ["code-review-graph", "forget", "pkg", "--repo", str(repo), "--dry-run"]
    with patch.object(sys, "argv", argv):
        cli.main()
    assert "No changes made" in capsys.readouterr().out
    with GraphStore(get_db_path(repo)) as store:
        assert store.get_metadata("intentionally_incomplete") == "0"
        assert len(store.get_file_marker_paths()) == 8


def test_full_rebuild_clears_forget_marker_and_reenables_recovery(
    tmp_path: Path, monkeypatch,
) -> None:
    from code_review_graph.tools.build import _should_rebuild_partial_graph

    monkeypatch.setenv("CRG_SERIAL_PARSE", "1")
    repo = _git_repo(tmp_path / "repo", _python_files(8))
    build_or_update_graph(repo_root=str(repo), full_rebuild=True, postprocess="none")
    with GraphStore(get_db_path(repo)) as store:
        forget_files(store, repo, store.get_file_marker_paths()[1:])
        assert store.get_metadata("intentionally_incomplete") == "1"

    rebuilt = build_or_update_graph(
        repo_root=str(repo), full_rebuild=True, postprocess="none",
    )
    assert rebuilt["files_parsed"] == 8
    with GraphStore(get_db_path(repo)) as store:
        assert store.get_metadata("intentionally_incomplete") == "0"
        # Lose most files again without ``forget``: recovery applies once more.
        for path in store.get_file_marker_paths()[1:]:
            store.remove_file_data(path)
        store.set_metadata("last_build_type", "incremental")
        store.commit()
        assert _should_rebuild_partial_graph(repo, store) is True


@pytest.mark.parametrize(
    ("represented", "expected"),
    [(2, True), (3, False)],
    ids=["exactly-25-percent-rebuilds", "above-25-percent-kept"],
)
def test_coverage_threshold_boundary(
    tmp_path: Path, represented: int, expected: bool,
) -> None:
    from code_review_graph.tools.build import _should_rebuild_partial_graph

    _make_repo(tmp_path)
    _seed_partial_graph(tmp_path, represented_count=represented)
    with GraphStore(get_db_path(tmp_path)) as store:
        assert _should_rebuild_partial_graph(tmp_path, store) is expected


def test_repository_below_minimum_size_is_left_alone(tmp_path: Path) -> None:
    from code_review_graph.tools.build import _should_rebuild_partial_graph

    _make_repo(tmp_path, source_count=7)
    _seed_partial_graph(tmp_path, represented_count=1)
    with GraphStore(get_db_path(tmp_path)) as store:
        assert _should_rebuild_partial_graph(tmp_path, store) is False


def test_well_covered_graph_skips_the_parseable_inventory(
    tmp_path: Path, monkeypatch,
) -> None:
    """At least half of the tracked files represented: no full directory scan."""
    from code_review_graph.tools import build as build_module

    monkeypatch.setenv("CRG_SERIAL_PARSE", "1")
    repo = _git_repo(tmp_path / "repo", _python_files(12))
    build_or_update_graph(repo_root=str(repo), full_rebuild=True, postprocess="none")

    def _no_inventory(*_args, **_kwargs):
        raise AssertionError("collect_all_files must not run for a well-covered graph")

    monkeypatch.setattr(build_module, "collect_all_files", _no_inventory)
    with GraphStore(get_db_path(repo)) as store:
        store.set_metadata("last_build_type", "incremental")
        store.commit()
        assert build_module._should_rebuild_partial_graph(repo, store) is False


@pytest.mark.parametrize("partial", [True, False], ids=["partial", "complete"])
def test_git_inventory_unavailable_falls_back_to_directory_walk(
    tmp_path: Path, monkeypatch, partial: bool,
) -> None:
    """``git ls-files`` timing out returns no tracked files, not an error."""
    from code_review_graph.tools import build as build_module

    monkeypatch.setenv("CRG_SERIAL_PARSE", "1")
    repo = _git_repo(tmp_path / "repo", _python_files(10))
    build_or_update_graph(repo_root=str(repo), full_rebuild=True, postprocess="none")
    monkeypatch.setattr(build_module, "get_all_tracked_files", lambda *_a, **_k: [])
    monkeypatch.setattr(
        "code_review_graph.incremental.get_all_tracked_files", lambda *_a, **_k: [],
    )
    with GraphStore(get_db_path(repo)) as store:
        if partial:
            for path in store.get_file_marker_paths()[1:]:
                store.remove_file_data(path)
        store.set_metadata("last_build_type", "incremental")
        store.commit()
        assert build_module._should_rebuild_partial_graph(repo, store) is partial


def test_backslash_stored_paths_count_as_represented(tmp_path: Path) -> None:
    """A graph written with Windows separators is not mistaken for a partial one."""
    from code_review_graph.tools.build import _should_rebuild_partial_graph

    _make_repo(tmp_path)
    with GraphStore(get_db_path(tmp_path)) as store:
        for index in range(8):
            stored = str(tmp_path / f"module{index}.py").replace("/", "\\")
            store.store_file_nodes_edges(
                stored,
                [NodeInfo(
                    kind="File", name=stored, file_path=stored,
                    line_start=1, line_end=2, language="python",
                )],
                [],
                f"hash-{index}",
            )
        store.set_metadata("last_build_type", "incremental")
        store.commit()
        assert _should_rebuild_partial_graph(tmp_path, store) is False


def test_symlinked_root_spelling_does_not_trigger_recovery(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setenv("CRG_SERIAL_PARSE", "1")
    repo = _git_repo(tmp_path / "real", _python_files(8))
    link = tmp_path / "link"
    link.symlink_to(repo, target_is_directory=True)
    build_or_update_graph(repo_root=str(link), full_rebuild=True, postprocess="none")
    with GraphStore(get_db_path(repo)) as store:
        store.set_metadata("last_build_type", "incremental")
        store.commit()

    for spelling in (link, repo):
        result = build_or_update_graph(repo_root=str(spelling), postprocess="none")
        assert result["build_type"] == "incremental"
        assert "recovery_reason" not in result


def test_foreign_graph_in_shared_data_dir_is_refused_not_rebuilt(
    tmp_path: Path, monkeypatch,
) -> None:
    """Maintainer's reproduction on #881: recovery must not overwrite repo A.

    A shared ``CRG_DATA_DIR`` holds repository A's one-file graph. Updating
    repository B (eight files) looks like a severely partial graph, but the
    root guard has to refuse before any rebuild replaces A's data.
    """
    monkeypatch.setenv("CRG_SERIAL_PARSE", "1")
    repo_a = _git_repo(tmp_path / "A", {"only.py": "def only() -> int:\n    return 1\n"})
    repo_b = _git_repo(tmp_path / "B", _python_files(8))
    shared = tmp_path / "shared"
    shared.mkdir()
    monkeypatch.setenv("CRG_DATA_DIR", str(shared))
    assert get_db_path(repo_a) == get_db_path(repo_b)
    with GraphStore(get_db_path(repo_a)) as store:
        full_build(repo_a, store)
        store.set_metadata("last_build_type", "incremental")
        store.commit()

    with pytest.raises(GraphRootMismatchError):
        build_or_update_graph(repo_root=str(repo_b), base="HEAD~1", postprocess="none")

    assert _stored_names(repo_a) == {"only.py"}


def test_interrupted_build_repair_takes_precedence_over_partial_recovery(
    tmp_path: Path, monkeypatch,
) -> None:
    _make_repo(tmp_path)
    _seed_partial_graph(tmp_path)
    with GraphStore(get_db_path(tmp_path)) as store:
        store.set_metadata(BUILD_STATE_KEY, BUILD_IN_PROGRESS)
    monkeypatch.setenv("CRG_SERIAL_PARSE", "1")

    result = build_or_update_graph(repo_root=str(tmp_path), postprocess="none")

    assert result["build_type"] == "full"
    assert result["repaired_interrupted_build"] is True
    assert "recovery_reason" not in result
    assert result["files_parsed"] == 8


@pytest.mark.parametrize(
    ("represented", "expected"),
    [(8, False), (1, True)],
    ids=["complete-legacy-full-graph", "partial-legacy-full-graph"],
)
def test_graph_from_release_without_marker(
    tmp_path: Path, represented: int, expected: bool,
) -> None:
    """Releases before #881 wrote ``last_build_type`` but no marker at all."""
    from code_review_graph.tools.build import _should_rebuild_partial_graph

    _make_repo(tmp_path)
    _seed_partial_graph(tmp_path, represented_count=represented)
    with GraphStore(get_db_path(tmp_path)) as store:
        store.set_metadata("last_build_type", "full")
        store.commit()
        assert store.get_metadata("intentionally_incomplete") is None
        assert _should_rebuild_partial_graph(tmp_path, store) is expected


def test_complete_graph_of_yaml_heavy_repo_is_not_rebuilt_on_alternate_updates(
    tmp_path: Path, monkeypatch,
) -> None:
    """Parseable files that yield no rows must not make a complete graph look partial.

    Generic YAML (Helm values, CI workflows, compose files) is in the
    parseable inventory but produces no nodes, so a fully built graph of three
    Python files and twenty YAML files represents three of 23 expected files.
    Every automatic update after the first rewrites ``last_build_type`` to
    ``incremental``, and the next one must not answer with a full rebuild.
    """
    monkeypatch.setenv("CRG_SERIAL_PARSE", "1")
    files = _python_files(3, folder="app")
    for index in range(20):
        files[f"charts/values{index}.yaml"] = f"image:\n  tag: \"1.{index}\"\n"
    repo = _git_repo(tmp_path / "infra", files)

    built = build_or_update_graph(
        repo_root=str(repo), full_rebuild=True, postprocess="none",
    )
    assert built["files_parsed"] == 23

    build_types = []
    for _ in range(3):
        result = build_or_update_graph(repo_root=str(repo), postprocess="none")
        build_types.append((result["build_type"], result.get("recovery_reason")))

    assert build_types == [("incremental", None)] * 3


def test_legacy_yaml_heavy_graph_is_rebuilt_at_most_once(
    tmp_path: Path, monkeypatch,
) -> None:
    """No clean build on record: one rebuild settles it, then updates stay incremental."""
    monkeypatch.setenv("CRG_SERIAL_PARSE", "1")
    files = _python_files(3, folder="app")
    for index in range(20):
        files[f"charts/values{index}.yaml"] = f"image:\n  tag: \"1.{index}\"\n"
    repo = _git_repo(tmp_path / "infra", files)
    build_or_update_graph(repo_root=str(repo), full_rebuild=True, postprocess="none")
    with GraphStore(get_db_path(repo)) as store:
        store._conn.execute(
            "DELETE FROM metadata WHERE key IN (?, ?)",
            ("verified_file_count", "intentionally_incomplete"),
        )
        store.set_metadata("last_build_type", "incremental")
        store.commit()

    build_types = [
        build_or_update_graph(repo_root=str(repo), postprocess="none")["build_type"]
        for _ in range(3)
    ]

    # Generic YAML now gives these files rows, so most graphs need no rebuild at all.
    assert build_types.count("full") <= 1
    assert build_types[1:] == ["incremental", "incremental"]


def test_rebuild_with_file_errors_is_not_trusted_as_complete(
    tmp_path: Path, monkeypatch,
) -> None:
    """The failed files are missing, so the next update must try again."""
    monkeypatch.setenv("CRG_SERIAL_PARSE", "1")
    repo = _git_repo(tmp_path / "repo", _python_files(8))
    keep = (repo / "pkg" / "mod0.py").resolve()
    real_read_bytes = Path.read_bytes

    def flaky_read_bytes(self):
        if self.suffix == ".py" and self.resolve() != keep:
            raise PermissionError(13, "Permission denied", str(self))
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", flaky_read_bytes)
    failed = build_or_update_graph(
        repo_root=str(repo), full_rebuild=True, postprocess="none",
    )
    monkeypatch.setattr(Path, "read_bytes", real_read_bytes)
    assert failed["status"] == "partial"
    assert len(_stored_names(repo)) == 1

    recovered = build_or_update_graph(repo_root=str(repo), postprocess="none")

    assert recovered["build_type"] == "full"
    assert recovered["recovery_reason"] == "partial_graph"
    assert len(_stored_names(repo)) == 8


def test_untracked_files_do_not_count_as_coverage_of_tracked_files(tmp_path: Path) -> None:
    from code_review_graph.tools.build import _should_rebuild_partial_graph

    repo = _git_repo(tmp_path / "repo", _python_files(8))
    with GraphStore(get_db_path(repo)) as store:
        parser = CodeParser(repo)
        (repo / "scratch").mkdir()
        untracked = [repo / "scratch" / f"note{index}.py" for index in range(3)]
        for path in [repo / "pkg" / "mod0.py", *untracked]:
            if not path.exists():
                path.write_text("def note() -> int:\n    return 1\n", encoding="utf-8")
            nodes, edges = parser.parse_file(path)
            store.store_file_nodes_edges(str(path), nodes, edges, f"seed-{path.name}")
        store.set_metadata("last_build_type", "incremental")
        store.commit()
        assert _should_rebuild_partial_graph(repo, store) is True


def test_svn_working_copy_is_never_listed_on_update(
    tmp_path: Path, monkeypatch,
) -> None:
    from code_review_graph.tools import build as build_module

    _make_repo(tmp_path)
    _seed_partial_graph(tmp_path)
    monkeypatch.setattr(build_module, "detect_vcs", lambda _root: "svn")

    def _no_listing(*_args, **_kwargs):
        raise AssertionError("an update must not ask the SVN server for an inventory")

    monkeypatch.setattr(build_module, "get_all_tracked_files", _no_listing)
    monkeypatch.setattr(build_module, "collect_all_files", _no_listing)
    with GraphStore(get_db_path(tmp_path)) as store:
        assert build_module._should_rebuild_partial_graph(tmp_path, store) is False


def test_passed_check_is_remembered_so_the_inventory_runs_once(
    tmp_path: Path, monkeypatch,
) -> None:
    from code_review_graph.tools import build as build_module

    _make_repo(tmp_path)
    _seed_partial_graph(tmp_path, represented_count=4)
    calls = []
    real_collect = build_module.collect_all_files

    def counting_collect(*args, **kwargs):
        calls.append(args)
        return real_collect(*args, **kwargs)

    monkeypatch.setattr(build_module, "collect_all_files", counting_collect)
    with GraphStore(get_db_path(tmp_path)) as store:
        assert build_module._should_rebuild_partial_graph(tmp_path, store) is False
        assert build_module._should_rebuild_partial_graph(tmp_path, store) is False
    assert len(calls) == 1
