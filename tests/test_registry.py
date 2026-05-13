"""Tests for multi-repo registry and connection pool."""

import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from code_review_graph.registry import ConnectionPool, Registry, resolve_repo


class TestRegistry:
    def setup_method(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.registry_path = Path(self.tmp_dir) / "registry.json"
        self.registry = Registry(path=self.registry_path)

        # Create fake repos
        self.repo1 = Path(self.tmp_dir) / "repo1"
        self.repo1.mkdir()
        (self.repo1 / ".git").mkdir()

        self.repo2 = Path(self.tmp_dir) / "repo2"
        self.repo2.mkdir()
        (self.repo2 / ".code-review-graph").mkdir()

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_register_and_list(self):
        """Register repos and list them back."""
        self.registry.register(str(self.repo1), alias="r1")
        self.registry.register(str(self.repo2), alias="r2")

        repos = self.registry.list_repos()
        assert len(repos) == 2
        paths = [r["path"] for r in repos]
        assert str(self.repo1.resolve()) in paths
        assert str(self.repo2.resolve()) in paths

    def test_register_duplicate_path(self):
        """Registering the same path twice updates alias."""
        self.registry.register(str(self.repo1), alias="first")
        self.registry.register(str(self.repo1), alias="second")

        repos = self.registry.list_repos()
        assert len(repos) == 1
        assert repos[0]["alias"] == "second"

    def test_register_invalid_path(self):
        """Registering a non-existent path raises ValueError."""
        import pytest
        with pytest.raises(ValueError, match="not a directory"):
            self.registry.register("/nonexistent/path/repo")

    def test_register_not_a_repo(self):
        """Registering a dir without .git or .code-review-graph raises ValueError."""
        import pytest
        bare_dir = Path(self.tmp_dir) / "bare"
        bare_dir.mkdir()
        with pytest.raises(ValueError, match="does not look like a repository"):
            self.registry.register(str(bare_dir))

    def test_unregister_by_path(self):
        """Unregister a repo by path."""
        self.registry.register(str(self.repo1), alias="r1")
        assert len(self.registry.list_repos()) == 1

        result = self.registry.unregister(str(self.repo1))
        assert result is True
        assert len(self.registry.list_repos()) == 0

    def test_unregister_by_alias(self):
        """Unregister a repo by alias."""
        self.registry.register(str(self.repo1), alias="myalias")
        assert len(self.registry.list_repos()) == 1

        result = self.registry.unregister("myalias")
        assert result is True
        assert len(self.registry.list_repos()) == 0

    def test_unregister_not_found(self):
        """Unregistering a non-registered repo returns False."""
        result = self.registry.unregister("nonexistent")
        assert result is False

    def test_find_by_alias(self):
        """find_by_alias returns correct entry."""
        self.registry.register(str(self.repo1), alias="myrepo")
        entry = self.registry.find_by_alias("myrepo")
        assert entry is not None
        assert entry["alias"] == "myrepo"
        assert entry["path"] == str(self.repo1.resolve())

    def test_find_by_alias_not_found(self):
        """find_by_alias returns None for unknown alias."""
        entry = self.registry.find_by_alias("nope")
        assert entry is None

    def test_find_by_path(self):
        """find_by_path returns correct entry."""
        self.registry.register(str(self.repo1), alias="r1")
        entry = self.registry.find_by_path(str(self.repo1))
        assert entry is not None
        assert entry["path"] == str(self.repo1.resolve())

    def test_persistence(self):
        """Registry persists to disk and reloads correctly."""
        self.registry.register(str(self.repo1), alias="persistent")

        # Create a new registry from the same file
        registry2 = Registry(path=self.registry_path)
        repos = registry2.list_repos()
        assert len(repos) == 1
        assert repos[0]["alias"] == "persistent"

    def test_resolve_by_alias(self):
        """resolve_repo resolves alias to path."""
        self.registry.register(str(self.repo1), alias="r1")
        result = resolve_repo(self.registry, "r1")
        assert result == str(self.repo1.resolve())

    def test_resolve_by_direct_path(self):
        """resolve_repo resolves direct path."""
        result = resolve_repo(self.registry, str(self.repo1))
        assert result == str(self.repo1.resolve())

    def test_resolve_by_cwd(self):
        """resolve_repo falls back to cwd when repo is None."""
        result = resolve_repo(self.registry, None, cwd=str(self.repo1))
        assert result == str(self.repo1.resolve())

    def test_resolve_returns_none(self):
        """resolve_repo returns None when nothing matches."""
        result = resolve_repo(self.registry, None)
        assert result is None


class TestConnectionPool:
    def setup_method(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.pool = ConnectionPool(max_size=3)

    def teardown_method(self):
        self.pool.close_all()
        import shutil
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _make_db(self, name: str) -> str:
        """Create a temporary SQLite database file."""
        db_path = str(Path(self.tmp_dir) / f"{name}.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE IF NOT EXISTS test (id INTEGER)")
        conn.close()
        return db_path

    def test_get_creates_connection(self):
        """get() creates a new connection."""
        db_path = self._make_db("test1")
        conn = self.pool.get(db_path)
        assert conn is not None
        assert self.pool.size == 1

    def test_get_reuses_connection(self):
        """get() returns the same connection for the same path."""
        db_path = self._make_db("test1")
        conn1 = self.pool.get(db_path)
        conn2 = self.pool.get(db_path)
        assert conn1 is conn2
        assert self.pool.size == 1

    def test_eviction_on_full(self):
        """Pool evicts LRU connection when full."""
        db1 = self._make_db("db1")
        db2 = self._make_db("db2")
        db3 = self._make_db("db3")
        db4 = self._make_db("db4")

        self.pool.get(db1)
        self.pool.get(db2)
        self.pool.get(db3)
        assert self.pool.size == 3

        # Adding 4th should evict db1 (LRU)
        self.pool.get(db4)
        assert self.pool.size == 3

    def test_close_all(self):
        """close_all() clears all connections."""
        db1 = self._make_db("db1")
        db2 = self._make_db("db2")

        self.pool.get(db1)
        self.pool.get(db2)
        assert self.pool.size == 2

        self.pool.close_all()
        assert self.pool.size == 0

    def test_lru_ordering(self):
        """Recently used connections are kept over stale ones."""
        db1 = self._make_db("db1")
        db2 = self._make_db("db2")
        db3 = self._make_db("db3")
        db4 = self._make_db("db4")

        conn1 = self.pool.get(db1)
        self.pool.get(db2)
        self.pool.get(db3)

        # Access db1 again to make it recently used
        self.pool.get(db1)

        # Now add db4 — db2 should be evicted (LRU), not db1
        self.pool.get(db4)
        assert self.pool.size == 3

        # db1 should still be in pool
        conn1_again = self.pool.get(db1)
        assert conn1_again is conn1


class TestCrossRepoSearch:
    def test_cross_repo_search_no_repos(self):
        """cross_repo_search with empty registry returns empty results."""
        from code_review_graph.tools import cross_repo_search_func

        tmp_dir = tempfile.mkdtemp()

        with patch("code_review_graph.registry.Registry") as mock_registry_cls:
            mock_instance = MagicMock()
            mock_instance.list_repos.return_value = []
            mock_registry_cls.return_value = mock_instance

            result = cross_repo_search_func(query="test")
            assert result["status"] == "ok"
            assert result["results"] == []

        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)


class TestSetDataDir:
    """Tests for set_data_dir and get_data_dir_for_repo methods."""

    def setup_method(self):
        """Set up isolated test registry."""
        self.tmp_dir = tempfile.mkdtemp()
        self.registry_path = Path(self.tmp_dir) / "registry.json"
        self.registry = Registry(path=self.registry_path)

    def teardown_method(self):
        """Clean up temporary directory."""
        import shutil
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_set_data_dir_new_repo(self):
        """set_data_dir should create new registry entry if repo not registered."""
        repo = Path(self.tmp_dir) / "project"
        repo.mkdir()
        data_dir = Path(self.tmp_dir) / "data"

        entry = self.registry.set_data_dir(str(repo), str(data_dir))

        assert entry["path"] == str(repo.resolve())
        assert entry["data_dir"] == str(data_dir.resolve())

        # Verify it can be retrieved
        retrieved = self.registry.get_data_dir_for_repo(str(repo))
        assert retrieved == str(data_dir.resolve())

        # Verify entry is in list
        repos = self.registry.list_repos()
        assert len(repos) == 1
        assert repos[0]["path"] == str(repo.resolve())

    def test_set_data_dir_existing_repo(self):
        """set_data_dir should update data_dir for already registered repo."""
        repo = Path(self.tmp_dir) / "project"
        repo.mkdir()
        data_dir1 = Path(self.tmp_dir) / "data1"
        data_dir2 = Path(self.tmp_dir) / "data2"

        # Initial registration
        entry1 = self.registry.set_data_dir(str(repo), str(data_dir1))
        assert entry1["data_dir"] == str(data_dir1.resolve())

        # Update with new data_dir
        entry2 = self.registry.set_data_dir(str(repo), str(data_dir2))
        assert entry2["data_dir"] == str(data_dir2.resolve())

        # Verify only one entry exists
        repos = self.registry.list_repos()
        assert len(repos) == 1

    def test_get_data_dir_for_repo_unknown(self):
        """get_data_dir_for_repo should return None for unknown repo."""
        unknown_repo = Path(self.tmp_dir) / "unknown"

        result = self.registry.get_data_dir_for_repo(str(unknown_repo))
        assert result is None

    def test_set_data_dir_with_alias(self):
        """register() with data_dir should store both."""
        repo = Path(self.tmp_dir) / "project"
        repo.mkdir()
        (repo / ".git").mkdir()
        data_dir = Path(self.tmp_dir) / "data"
        alias = "my-project"

        entry = self.registry.register(str(repo), alias=alias, data_dir=str(data_dir))

        assert entry["path"] == str(repo.resolve())
        assert entry["alias"] == alias
        assert entry["data_dir"] == str(data_dir.resolve())

    def test_backward_compatibility(self):
        """Old registry entries without data_dir should work."""
        repo = Path(self.tmp_dir) / "project"
        repo.mkdir()

        # Create entry without data_dir (old format)
        self.registry._repos.append({
            "path": str(repo.resolve()),
            "alias": "old-project"
        })
        self.registry._save()

        # Should not crash
        result = self.registry.get_data_dir_for_repo(str(repo))
        assert result is None

        # Should be able to add data_dir
        data_dir = Path(self.tmp_dir) / "data"
        entry = self.registry.set_data_dir(str(repo), str(data_dir))
        assert entry["data_dir"] == str(data_dir.resolve())
