"""Tests for the incremental graph update module."""

import subprocess
from unittest.mock import MagicMock, patch  # noqa: F401 – patch used in tests

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import (
    _is_binary,
    _load_ignore_patterns,
    _parse_single_file,
    _should_ignore,
    _single_hop_dependents,
    ensure_repo_gitignore_excludes_crg,
    find_dependents,
    find_project_root,
    find_repo_root,
    full_build,
    get_all_tracked_files,
    get_changed_files,
    get_db_path,
    get_staged_and_unstaged,
    incremental_update,
)


class TestFindRepoRoot:
    def test_finds_git_dir(self, tmp_path):
        (tmp_path / ".git").mkdir()
        assert find_repo_root(tmp_path) == tmp_path

    def test_finds_parent_git_dir(self, tmp_path):
        (tmp_path / ".git").mkdir()
        sub = tmp_path / "a" / "b"
        sub.mkdir(parents=True)
        assert find_repo_root(sub) == tmp_path

    def test_returns_none_without_git(self, tmp_path):
        sub = tmp_path / "no_git"
        sub.mkdir()
        assert find_repo_root(sub) is None


class TestFindProjectRoot:
    def test_returns_git_root(self, tmp_path):
        (tmp_path / ".git").mkdir()
        assert find_project_root(tmp_path) == tmp_path

    def test_falls_back_to_start(self, tmp_path):
        sub = tmp_path / "no_git"
        sub.mkdir()
        assert find_project_root(sub) == sub


class TestGetDbPath:
    def test_creates_directory_and_db_path(self, tmp_path):
        db_path = get_db_path(tmp_path)
        assert db_path == tmp_path / ".code-review-graph" / "graph.db"
        assert (tmp_path / ".code-review-graph").is_dir()

    def test_creates_gitignore(self, tmp_path):
        get_db_path(tmp_path)
        gi = tmp_path / ".code-review-graph" / ".gitignore"
        assert gi.exists()
        assert "*\n" in gi.read_text()

    def test_migrates_legacy_db(self, tmp_path):
        legacy = tmp_path / ".code-review-graph.db"
        legacy.write_text("legacy data")
        db_path = get_db_path(tmp_path)
        assert db_path.exists()
        assert not legacy.exists()
        assert db_path.read_text() == "legacy data"

    def test_cleans_legacy_side_files(self, tmp_path):
        legacy = tmp_path / ".code-review-graph.db"
        legacy.write_text("data")
        for suffix in ("-wal", "-shm", "-journal"):
            (tmp_path / f".code-review-graph.db{suffix}").write_text("side")
        get_db_path(tmp_path)
        for suffix in ("-wal", "-shm", "-journal"):
            assert not (tmp_path / f".code-review-graph.db{suffix}").exists()


class TestEnsureRepoGitignoreExcludesCrg:
    def test_creates_gitignore_when_missing(self, tmp_path):
        state = ensure_repo_gitignore_excludes_crg(tmp_path)
        assert state == "created"

        gitignore = tmp_path / ".gitignore"
        assert gitignore.exists()
        assert gitignore.read_text() == (
            "# Added by code-review-graph\n"
            ".code-review-graph/\n"
        )

    def test_appends_rule_when_missing(self, tmp_path):
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("node_modules/\n")

        state = ensure_repo_gitignore_excludes_crg(tmp_path)
        assert state == "updated"
        assert gitignore.read_text() == (
            "node_modules/\n"
            "# Added by code-review-graph\n"
            ".code-review-graph/\n"
        )

    def test_idempotent_when_present(self, tmp_path):
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text(".code-review-graph/\n")

        state = ensure_repo_gitignore_excludes_crg(tmp_path)
        assert state == "already-present"
        assert gitignore.read_text() == ".code-review-graph/\n"

    def test_treats_wildcard_ignore_as_present(self, tmp_path):
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text(".code-review-graph/**\n")

        state = ensure_repo_gitignore_excludes_crg(tmp_path)
        assert state == "already-present"


class TestIgnorePatterns:
    def test_default_patterns_loaded(self, tmp_path):
        patterns = _load_ignore_patterns(tmp_path)
        assert "node_modules/**" in patterns
        assert ".git/**" in patterns
        assert "__pycache__/**" in patterns

    def test_custom_ignore_file(self, tmp_path):
        ignore = tmp_path / ".code-review-graphignore"
        ignore.write_text("custom/**\n# comment\n\nvendor/**\n")
        patterns = _load_ignore_patterns(tmp_path)
        assert "custom/**" in patterns
        assert "vendor/**" in patterns
        # Comments and blanks should be skipped
        assert "# comment" not in patterns
        assert "" not in patterns

    def test_should_ignore_matches(self):
        patterns = ["node_modules/**", "*.pyc", ".git/**"]
        assert _should_ignore("node_modules/foo/bar.js", patterns)
        assert _should_ignore("test.pyc", patterns)
        assert _should_ignore(".git/HEAD", patterns)
        assert not _should_ignore("src/main.py", patterns)

    def test_should_ignore_nested_dependency_dirs(self):
        """Nested node_modules / vendor / .gradle should be ignored (#91)."""
        patterns = [
            "node_modules/**", "vendor/**", ".gradle/**", ".venv/**",
        ]
        # Monorepo: nested node_modules
        assert _should_ignore("packages/app/node_modules/react/index.js", patterns)
        assert _should_ignore("apps/web/node_modules/lodash/index.js", patterns)
        # PHP/Laravel: vendor at any depth
        assert _should_ignore("backend/vendor/autoload.php", patterns)
        # Gradle at any depth
        assert _should_ignore("android/app/.gradle/cache/metadata.bin", patterns)
        # Negative: similarly-named dirs that aren't a match
        assert not _should_ignore("src/node_modules_helper/foo.py", patterns)
        assert not _should_ignore("src/venv_tools/bar.py", patterns)

    def test_should_ignore_framework_defaults(self):
        """Default patterns should cover Laravel, Gradle, Flutter, and caches."""
        from code_review_graph.incremental import DEFAULT_IGNORE_PATTERNS

        patterns = DEFAULT_IGNORE_PATTERNS
        # Laravel/PHP
        assert _should_ignore("vendor/autoload.php", patterns)
        assert _should_ignore("bootstrap/cache/packages.php", patterns)
        # Gradle/Java
        assert _should_ignore(".gradle/caches/jars.bin", patterns)
        assert _should_ignore("build/libs/app.jar", patterns)
        # Flutter/Dart
        assert _should_ignore(".dart_tool/package_config.json", patterns)
        # Coverage/cache
        assert _should_ignore("coverage/lcov.info", patterns)
        assert _should_ignore(".cache/webpack/index.pack", patterns)


class TestDataDir:
    """Tests for get_data_dir / CRG_DATA_DIR / CRG_REPO_ROOT (#155)."""

    def test_default_uses_repo_subdir(self, tmp_path, monkeypatch):
        """Without CRG_DATA_DIR, graphs live at <repo>/.code-review-graph."""
        monkeypatch.delenv("CRG_DATA_DIR", raising=False)
        from code_review_graph.incremental import get_data_dir
        result = get_data_dir(tmp_path)
        assert result == tmp_path / ".code-review-graph"
        assert result.is_dir()
        # Auto-generated gitignore must exist
        assert (result / ".gitignore").is_file()
        content = (result / ".gitignore").read_text(encoding="utf-8")
        assert content.strip().endswith("*")

    def test_env_override_replaces_repo_subdir(self, tmp_path, monkeypatch):
        """CRG_DATA_DIR replaces the default <repo>/.code-review-graph."""
        external = tmp_path / "external-graphs"
        repo = tmp_path / "project"
        repo.mkdir()
        monkeypatch.setenv("CRG_DATA_DIR", str(external))
        from code_review_graph.incremental import get_data_dir
        result = get_data_dir(repo)
        assert result == external.resolve()
        assert result.is_dir()
        # The repo itself should NOT have a .code-review-graph dir now
        assert not (repo / ".code-review-graph").exists()

    def test_get_db_path_uses_data_dir(self, tmp_path, monkeypatch):
        """get_db_path should honor CRG_DATA_DIR too."""
        external = tmp_path / "external"
        repo = tmp_path / "project"
        repo.mkdir()
        monkeypatch.setenv("CRG_DATA_DIR", str(external))
        from code_review_graph.incremental import get_db_path
        db_path = get_db_path(repo)
        assert db_path == external.resolve() / "graph.db"
        assert db_path.parent.is_dir()

    def test_find_project_root_env_override(self, tmp_path, monkeypatch):
        """CRG_REPO_ROOT should override normal git-root resolution."""
        from pathlib import Path as PathType
        external_repo = tmp_path / "elsewhere"
        external_repo.mkdir()
        monkeypatch.setenv("CRG_REPO_ROOT", str(external_repo))
        from code_review_graph.incremental import find_project_root
        result = find_project_root(PathType.cwd())
        assert result == external_repo.resolve()

    def test_find_project_root_env_override_missing_dir_falls_through(
        self, tmp_path, monkeypatch,
    ):
        """CRG_REPO_ROOT pointing at a non-existent path falls back to
        the usual resolution rather than crashing."""
        monkeypatch.setenv(
            "CRG_REPO_ROOT", str(tmp_path / "does-not-exist-123"),
        )
        from code_review_graph.incremental import find_project_root
        result = find_project_root(tmp_path)
        # Should NOT equal the bogus env value
        assert result != tmp_path / "does-not-exist-123"


class TestIsBinary:
    def test_text_file_is_not_binary(self, tmp_path):
        f = tmp_path / "text.py"
        f.write_text("print('hello')\n")
        assert not _is_binary(f)

    def test_binary_file_is_binary(self, tmp_path):
        f = tmp_path / "binary.bin"
        f.write_bytes(b"header\x00binary data")
        assert _is_binary(f)

    def test_missing_file_is_binary(self, tmp_path):
        f = tmp_path / "missing.txt"
        assert _is_binary(f)


class TestGitOperations:
    @patch("code_review_graph.incremental.subprocess.run")
    def test_get_changed_files(self, mock_run, tmp_path):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="src/a.py\nsrc/b.py\n",
        )
        result = get_changed_files(tmp_path)
        assert result == ["src/a.py", "src/b.py"]
        mock_run.assert_called_once()
        call_args = mock_run.call_args
        assert "git" in call_args[0][0]
        assert call_args[1].get("timeout") == 30

    @patch("code_review_graph.incremental.subprocess.run")
    def test_get_changed_files_fallback(self, mock_run, tmp_path):
        # First call fails, second succeeds
        mock_run.side_effect = [
            MagicMock(returncode=1, stdout=""),
            MagicMock(returncode=0, stdout="staged.py\n"),
        ]
        result = get_changed_files(tmp_path)
        assert result == ["staged.py"]
        assert mock_run.call_count == 2

    @patch("code_review_graph.incremental.subprocess.run")
    def test_get_changed_files_timeout(self, mock_run, tmp_path):
        mock_run.side_effect = subprocess.TimeoutExpired("git", 30)
        result = get_changed_files(tmp_path)
        assert result == []

    @patch("code_review_graph.incremental.subprocess.run")
    def test_get_staged_and_unstaged(self, mock_run, tmp_path):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=" M src/a.py\n?? new.py\nR  old.py -> new_name.py\n",
        )
        result = get_staged_and_unstaged(tmp_path)
        assert "src/a.py" in result
        assert "new.py" in result
        assert "new_name.py" in result
        # old.py should NOT be in results (renamed away)
        assert "old.py" not in result

    @patch("code_review_graph.incremental.subprocess.run")
    def test_get_all_tracked_files(self, mock_run, tmp_path):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="a.py\nb.py\nc.go\n",
        )
        result = get_all_tracked_files(tmp_path)
        assert result == ["a.py", "b.py", "c.go"]

    @patch("code_review_graph.incremental.subprocess.run")
    def test_get_all_tracked_files_recurse_submodules_param(
        self, mock_run, tmp_path
    ):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="a.py\nsub/b.py\n",
        )
        result = get_all_tracked_files(tmp_path, recurse_submodules=True)
        assert result == ["a.py", "sub/b.py"]
        cmd = mock_run.call_args[0][0]
        assert "--recurse-submodules" in cmd

    @patch("code_review_graph.incremental.subprocess.run")
    def test_get_all_tracked_files_no_recurse_by_default(
        self, mock_run, tmp_path
    ):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="a.py\n",
        )
        result = get_all_tracked_files(tmp_path)
        assert result == ["a.py"]
        cmd = mock_run.call_args[0][0]
        assert "--recurse-submodules" not in cmd

    @patch("code_review_graph.incremental.subprocess.run")
    @patch("code_review_graph.incremental._RECURSE_SUBMODULES", True)
    def test_get_all_tracked_files_env_var_fallback(
        self, mock_run, tmp_path
    ):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="a.py\nsub/c.py\n",
        )
        # None -> falls back to env var (_RECURSE_SUBMODULES=True)
        result = get_all_tracked_files(tmp_path, recurse_submodules=None)
        assert result == ["a.py", "sub/c.py"]
        cmd = mock_run.call_args[0][0]
        assert "--recurse-submodules" in cmd

    @patch("code_review_graph.incremental.subprocess.run")
    @patch("code_review_graph.incremental._RECURSE_SUBMODULES", True)
    def test_get_all_tracked_files_param_overrides_env(
        self, mock_run, tmp_path
    ):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="a.py\n",
        )
        # Explicit False overrides env var
        result = get_all_tracked_files(tmp_path, recurse_submodules=False)
        assert result == ["a.py"]
        cmd = mock_run.call_args[0][0]
        assert "--recurse-submodules" not in cmd


class TestFullBuild:
    def test_full_build_parses_files(self, tmp_path):
        # Create a simple Python file
        py_file = tmp_path / "sample.py"
        py_file.write_text("def hello():\n    pass\n")
        (tmp_path / ".git").mkdir()

        db_path = tmp_path / "test.db"
        store = GraphStore(db_path)
        try:
            mock_target = "code_review_graph.incremental.get_all_tracked_files"
            with patch(mock_target, return_value=["sample.py"]):
                result = full_build(tmp_path, store)
            assert result["files_parsed"] == 1
            assert result["total_nodes"] > 0
            assert result["errors"] == []
            assert store.get_metadata("last_build_type") == "full"
        finally:
            store.close()


class TestIncrementalUpdate:
    def test_incremental_with_no_changes(self, tmp_path):
        db_path = tmp_path / "test.db"
        store = GraphStore(db_path)
        try:
            result = incremental_update(tmp_path, store, changed_files=[])
            assert result["files_updated"] == 0
        finally:
            store.close()

    def test_incremental_with_changed_file(self, tmp_path):
        py_file = tmp_path / "mod.py"
        py_file.write_text("def greet():\n    return 'hi'\n")

        db_path = tmp_path / "test.db"
        store = GraphStore(db_path)
        try:
            result = incremental_update(
                tmp_path, store, changed_files=["mod.py"]
            )
            assert result["files_updated"] >= 1
            assert result["total_nodes"] > 0
        finally:
            store.close()

    def test_incremental_deleted_file(self, tmp_path):
        db_path = tmp_path / "test.db"
        store = GraphStore(db_path)
        try:
            # Pre-populate with a file
            py_file = tmp_path / "old.py"
            py_file.write_text("x = 1\n")
            result = incremental_update(tmp_path, store, changed_files=["old.py"])
            assert result["total_nodes"] > 0

            # Now delete the file and run incremental
            py_file.unlink()
            incremental_update(tmp_path, store, changed_files=["old.py"])
            # File should have been removed from graph
            nodes = store.get_nodes_by_file(str(tmp_path / "old.py"))
            assert len(nodes) == 0
        finally:
            store.close()


class TestParallelParsing:
    def test_parse_single_file(self, tmp_path):
        py_file = tmp_path / "single.py"
        py_file.write_text("def foo():\n    pass\n")
        rel_path, nodes, edges, error, fhash = _parse_single_file(
            ("single.py", str(tmp_path))
        )
        assert rel_path == "single.py"
        assert error is None
        assert len(nodes) > 0
        assert fhash != ""

    def test_parse_single_file_missing(self, tmp_path):
        rel_path, nodes, edges, error, fhash = _parse_single_file(
            ("missing.py", str(tmp_path))
        )
        assert error is not None
        assert nodes == []
        assert edges == []

    def test_parallel_build_produces_same_results(self, tmp_path):
        """Serial and parallel builds produce identical node/edge counts."""
        (tmp_path / ".git").mkdir()
        # Create several Python files
        for i in range(10):
            (tmp_path / f"mod{i}.py").write_text(
                f"def func_{i}():\n    return {i}\n\n"
                f"class Cls{i}:\n    pass\n"
            )

        tracked = [f"mod{i}.py" for i in range(10)]
        mock_target = "code_review_graph.incremental.get_all_tracked_files"

        # Serial build
        db_serial = tmp_path / "serial.db"
        store_serial = GraphStore(db_serial)
        try:
            with patch(mock_target, return_value=tracked):
                with patch.dict("os.environ", {"CRG_SERIAL_PARSE": "1"}):
                    result_serial = full_build(tmp_path, store_serial)
            serial_nodes = result_serial["total_nodes"]
            serial_edges = result_serial["total_edges"]
            serial_files = result_serial["files_parsed"]
        finally:
            store_serial.close()

        # Parallel build
        db_parallel = tmp_path / "parallel.db"
        store_parallel = GraphStore(db_parallel)
        try:
            with patch(mock_target, return_value=tracked):
                with patch.dict("os.environ", {"CRG_SERIAL_PARSE": ""}):
                    result_parallel = full_build(tmp_path, store_parallel)
            parallel_nodes = result_parallel["total_nodes"]
            parallel_edges = result_parallel["total_edges"]
            parallel_files = result_parallel["files_parsed"]
        finally:
            store_parallel.close()

        assert serial_files == parallel_files
        assert serial_nodes == parallel_nodes
        assert serial_edges == parallel_edges


class TestMultiHopDependents:
    """Tests for N-hop dependent discovery."""

    def _make_chain_store(self, tmp_path):
        """Build A -> B -> C chain in the graph."""
        from code_review_graph.parser import EdgeInfo, NodeInfo

        db_path = tmp_path / "chain.db"
        store = GraphStore(db_path)
        for name, path in [("a", "/a.py"), ("b", "/b.py"), ("c", "/c.py")]:
            store.upsert_node(NodeInfo(
                kind="File", name=path, file_path=path,
                line_start=1, line_end=10, language="python",
            ))
            store.upsert_node(NodeInfo(
                kind="Function", name=f"func_{name}", file_path=path,
                line_start=2, line_end=8, language="python",
            ))
        # A imports B, B imports C
        store.upsert_edge(EdgeInfo(
            kind="IMPORTS_FROM", source="/a.py::func_a",
            target="/b.py::func_b", file_path="/a.py", line=1,
        ))
        store.upsert_edge(EdgeInfo(
            kind="IMPORTS_FROM", source="/b.py::func_b",
            target="/c.py::func_c", file_path="/b.py", line=1,
        ))
        store.commit()
        return store

    def test_single_hop_finds_direct_only(self, tmp_path):
        store = self._make_chain_store(tmp_path)
        try:
            deps = _single_hop_dependents(store, "/c.py")
            assert "/b.py" in deps
            assert "/a.py" not in deps
        finally:
            store.close()

    def test_one_hop_finds_b_not_a(self, tmp_path):
        store = self._make_chain_store(tmp_path)
        try:
            deps = find_dependents(store, "/c.py", max_hops=1)
            assert "/b.py" in deps
            assert "/a.py" not in deps
        finally:
            store.close()

    def test_two_hops_finds_b_and_a(self, tmp_path):
        store = self._make_chain_store(tmp_path)
        try:
            deps = find_dependents(store, "/c.py", max_hops=2)
            assert "/b.py" in deps
            assert "/a.py" in deps
        finally:
            store.close()

    def test_cap_triggers_on_many_files(self, tmp_path):
        """The 500-file cap prevents runaway expansion."""
        from code_review_graph.parser import EdgeInfo, NodeInfo

        db_path = tmp_path / "big.db"
        store = GraphStore(db_path)
        try:
            # Hub node that many files depend on
            store.upsert_node(NodeInfo(
                kind="File", name="/hub.py", file_path="/hub.py",
                line_start=1, line_end=10, language="python",
            ))
            store.upsert_node(NodeInfo(
                kind="Function", name="hub_func", file_path="/hub.py",
                line_start=2, line_end=8, language="python",
            ))
            for i in range(600):
                path = f"/dep{i}.py"
                store.upsert_node(NodeInfo(
                    kind="File", name=path, file_path=path,
                    line_start=1, line_end=10, language="python",
                ))
                store.upsert_node(NodeInfo(
                    kind="Function", name=f"func_{i}", file_path=path,
                    line_start=2, line_end=8, language="python",
                ))
                store.upsert_edge(EdgeInfo(
                    kind="IMPORTS_FROM", source=f"{path}::func_{i}",
                    target="/hub.py::hub_func", file_path=path, line=1,
                ))
            store.commit()

            # Even with high max_hops, cap should limit results
            deps = find_dependents(store, "/hub.py", max_hops=5)
            assert len(deps) <= 500
        finally:
            store.close()
