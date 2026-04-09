"""Tests for the incremental graph update module."""

import subprocess
from unittest.mock import MagicMock, patch

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import (
    _is_binary,
    _load_ignore_patterns,
    _should_ignore,
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


class TestIgnorePatterns:
    def test_default_patterns_loaded(self, tmp_path):
        patterns = _load_ignore_patterns(tmp_path)
        assert "**/node_modules/**" in patterns
        assert ".git/**" in patterns
        assert "**/__pycache__/**" in patterns

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
        patterns = ["**/node_modules/**", "*.pyc", ".git/**"]
        assert _should_ignore("node_modules/foo/bar.js", patterns)
        assert _should_ignore("test.pyc", patterns)
        assert _should_ignore(".git/HEAD", patterns)
        assert not _should_ignore("src/main.py", patterns)

    def test_safe_anywhere_matches_nested_paths(self):
        """**/name/** patterns match nested dependency dirs (monorepos)."""
        patterns = ["**/node_modules/**", "**/vendor/**", "**/__pycache__/**"]
        # Nested node_modules (npm workspaces, Lerna, Turborepo)
        assert _should_ignore("packages/app/node_modules/react/index.js", patterns)
        # Nested vendor (PHP monorepo)
        assert _should_ignore("services/api/vendor/guzzlehttp/Client.php", patterns)
        # Nested __pycache__
        assert _should_ignore("src/utils/__pycache__/helpers.cpython-311.pyc", patterns)
        # Actual source code must not be affected
        assert not _should_ignore("packages/app/src/main.ts", patterns)
        assert not _should_ignore("src/vendors/custom.php", patterns)

    def test_root_relative_patterns_dont_match_nested(self):
        """name/** patterns should only match at root, not nested `name/` dirs."""
        patterns = ["packages/**", "bin/**", "build/**"]
        # Root-level matches
        assert _should_ignore("packages/nuget-cache/lib.dll", patterns)
        assert _should_ignore("bin/Debug/net8.0/app.dll", patterns)
        assert _should_ignore("build/output.txt", patterns)
        # Nested `packages/` in monorepo must NOT match
        assert not _should_ignore("apps/web/packages/src/main.ts", patterns)
        assert not _should_ignore("services/api/bin/helper.sh", patterns)
        assert not _should_ignore("docs/build/page.md", patterns)

    def test_multi_segment_root_prefix(self):
        """Multi-segment patterns like `bootstrap/cache/**` match only at root."""
        patterns = ["bootstrap/cache/**"]
        assert _should_ignore("bootstrap/cache/packages.php", patterns)
        assert not _should_ignore("src/bootstrap/cache/file.php", patterns)

    def test_should_ignore_framework_patterns(self):
        """Framework-specific dirs from DEFAULT_IGNORE_PATTERNS."""
        from code_review_graph.incremental import DEFAULT_IGNORE_PATTERNS

        patterns = DEFAULT_IGNORE_PATTERNS
        # Safe-anywhere: dependency dirs never used as source
        assert _should_ignore("vendor/laravel/framework/src/Collection.php", patterns)
        assert _should_ignore("services/api/vendor/pkg/file.go", patterns)  # nested
        assert _should_ignore(".dart_tool/package_config.json", patterns)
        assert _should_ignore(".gradle/caches/transforms-3/file.jar", patterns)
        assert _should_ignore("node_modules/react/index.js", patterns)
        assert _should_ignore("apps/api/node_modules/foo.js", patterns)  # nested
        # Root-only: Laravel framework dirs
        assert _should_ignore("storage/logs/laravel.log", patterns)
        assert _should_ignore("bootstrap/cache/packages.php", patterns)
        assert _should_ignore("public/build/assets/app.js", patterns)
        # Root-only: .NET
        assert _should_ignore("bin/Debug/net8.0/app.dll", patterns)
        assert _should_ignore("obj/Release/app.assets.cache", patterns)
        # Monorepo-safe: `packages/` is NOT in defaults (npm/Lerna/Turborepo
        # use it for source code; .NET users must add it to .code-review-graphignore)
        assert not _should_ignore("packages/app/src/main.ts", patterns)
        assert not _should_ignore("packages/Newtonsoft.Json.13.0.1/lib.dll", patterns)
        assert not _should_ignore("apps/web/src/main.tsx", patterns)
        assert not _should_ignore("app/Http/Controllers/UserController.php", patterns)


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
