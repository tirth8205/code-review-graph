"""Tests for multi-repo registry and connection pool."""

import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from code_review_graph.registry import ConnectionPool, Registry, resolve_repo
from code_review_graph.tools.registry_tools import _echoed, _select_repos


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

    def test_cross_repo_search_merges_by_local_rank(self, tmp_path):
        """Cross-repo results use local rank instead of incomparable raw scores."""
        from code_review_graph.tools import cross_repo_search_func

        android_repo = tmp_path / "android"
        ios_repo = tmp_path / "ios"
        android_repo.mkdir()
        ios_repo.mkdir()
        android_db = tmp_path / "android.db"
        ios_db = tmp_path / "ios.db"
        android_db.touch()
        ios_db.touch()

        android_results = [
            {"name": "Splash", "score": 0.032},
            {"name": "SplashWelcomeScreen", "score": 0.016},
        ]
        ios_results = [
            {"name": "SplashViewController", "score": 3.0},
            {"name": "SplashScreen", "score": 2.0},
        ]

        with (
            patch("code_review_graph.registry.Registry") as mock_registry_cls,
            patch(
                "code_review_graph.tools.registry_tools.get_db_path",
                side_effect=[android_db, ios_db],
            ),
            patch("code_review_graph.tools.registry_tools.GraphStore") as mock_store_cls,
            patch(
                "code_review_graph.tools.registry_tools.hybrid_search",
                side_effect=[android_results, ios_results],
            ) as mock_search,
        ):
            mock_registry_cls.return_value.list_repos.return_value = [
                {"path": str(android_repo), "alias": "android"},
                {"path": str(ios_repo), "alias": "ios"},
            ]
            mock_store_cls.side_effect = [MagicMock(), MagicMock()]

            result = cross_repo_search_func(query="splash", limit=2)

        assert result["status"] == "ok"
        assert [item["repo"] for item in result["results"]] == [
            "android",
            "ios",
            "android",
            "ios",
        ]
        assert [item["score"] for item in result["results"]] == [0.032, 3.0, 0.016, 2.0]
        assert [item["repo_path"] for item in result["results"]] == [
            str(android_repo),
            str(ios_repo),
            str(android_repo),
            str(ios_repo),
        ]
        assert result["summary"] == "Found 4 result(s) across 2 repo(s) for 'splash'"
        assert [call.kwargs["limit"] for call in mock_search.call_args_list] == [2, 2]
        assert "unknown" not in result

    def _three_repo_registry(self, tmp_path):
        """Three registered repos, one of them addressed only by folder name."""
        entries = []
        for folder, alias in (("android", "android"), ("ios", "ios"), ("web", None)):
            repo = tmp_path / folder
            repo.mkdir()
            db = tmp_path / f"{folder}.db"
            db.touch()
            entry = {"path": str(repo)}
            if alias:
                entry["alias"] = alias
            entries.append((entry, db))
        return entries

    def test_cross_repo_search_repos_limits_the_registry_fanout(self, tmp_path):
        """``repos`` searches only the named repos, by alias or folder name."""
        from code_review_graph.tools import cross_repo_search_func

        entries = self._three_repo_registry(tmp_path)
        ios_results = [{"name": "SplashViewController", "score": 3.0}]
        web_results = [{"name": "SplashBanner", "score": 0.5}]

        with (
            patch("code_review_graph.registry.Registry") as mock_registry_cls,
            patch(
                "code_review_graph.tools.registry_tools.get_db_path",
                side_effect=[entries[1][1], entries[2][1]],
            ),
            patch("code_review_graph.tools.registry_tools.GraphStore") as mock_store_cls,
            patch(
                "code_review_graph.tools.registry_tools.hybrid_search",
                side_effect=[ios_results, web_results],
            ) as mock_search,
        ):
            mock_registry_cls.return_value.list_repos.return_value = [
                entry for entry, _ in entries
            ]
            mock_store_cls.side_effect = [MagicMock(), MagicMock()]

            result = cross_repo_search_func(query="splash", repos=["web", "ios"])

        # Only the two named repos are opened at all: android never reaches search.
        assert mock_search.call_count == 2
        assert result["repos_searched"] == ["ios", "web"]
        assert [item["repo"] for item in result["results"]] == ["ios", "web"]
        assert result["unknown"] == []

    def test_cross_repo_search_reports_names_that_match_no_repo(self, tmp_path):
        """Unknown names are reported, not silently dropped."""
        from code_review_graph.tools import cross_repo_search_func

        entries = self._three_repo_registry(tmp_path)

        with (
            patch("code_review_graph.registry.Registry") as mock_registry_cls,
            patch(
                "code_review_graph.tools.registry_tools.get_db_path",
                side_effect=[entries[0][1]],
            ),
            patch("code_review_graph.tools.registry_tools.GraphStore") as mock_store_cls,
            patch(
                "code_review_graph.tools.registry_tools.hybrid_search",
                side_effect=[[{"name": "Splash", "score": 0.03}]],
            ),
        ):
            mock_registry_cls.return_value.list_repos.return_value = [
                entry for entry, _ in entries
            ]
            mock_store_cls.side_effect = [MagicMock()]

            result = cross_repo_search_func(
                query="splash", repos=["android", "desktop"]
            )

        assert result["status"] == "ok"
        assert result["repos_searched"] == ["android"]
        assert result["unknown"] == ["desktop"]

    def test_cross_repo_search_repos_matching_nothing_searches_nothing(self, tmp_path):
        """A selection that matches no entry returns empty, never the whole registry."""
        from code_review_graph.tools import cross_repo_search_func

        entries = self._three_repo_registry(tmp_path)

        with (
            patch("code_review_graph.registry.Registry") as mock_registry_cls,
            patch(
                "code_review_graph.tools.registry_tools.hybrid_search"
            ) as mock_search,
        ):
            mock_registry_cls.return_value.list_repos.return_value = [
                entry for entry, _ in entries
            ]

            result = cross_repo_search_func(query="splash", repos=["desktop"])

        assert result["status"] == "ok"
        assert result["results"] == []
        assert result["repos_searched"] == []
        assert result["unknown"] == ["desktop"]
        mock_search.assert_not_called()

    def test_cross_repo_search_repos_cannot_reorder_the_merge(self, tmp_path):
        """Selection order does not change the registry-order tie-breaker."""
        from code_review_graph.tools import cross_repo_search_func

        entries = self._three_repo_registry(tmp_path)
        android_results = [{"name": "Splash", "score": 0.03}]
        ios_results = [{"name": "SplashViewController", "score": 3.0}]

        with (
            patch("code_review_graph.registry.Registry") as mock_registry_cls,
            patch(
                "code_review_graph.tools.registry_tools.get_db_path",
                side_effect=[entries[0][1], entries[1][1]],
            ),
            patch("code_review_graph.tools.registry_tools.GraphStore") as mock_store_cls,
            patch(
                "code_review_graph.tools.registry_tools.hybrid_search",
                side_effect=[android_results, ios_results],
            ),
        ):
            mock_registry_cls.return_value.list_repos.return_value = [
                entry for entry, _ in entries
            ]
            mock_store_cls.side_effect = [MagicMock(), MagicMock()]

            result = cross_repo_search_func(query="splash", repos=["ios", "android"])

        assert [item["repo"] for item in result["results"]] == ["android", "ios"]

    def test_cross_repo_search_tool_forwards_repo_selection(self):
        """The MCP tool passes ``repos`` through, and defaults it to None."""
        from code_review_graph import main as crg_main
        from code_review_graph.main import cross_repo_search_tool

        with patch.object(
            crg_main, "cross_repo_search_func", return_value={"status": "ok"}
        ) as mock_func:
            cross_repo_search_tool(query="splash", repos=["android"])
            cross_repo_search_tool(query="splash")

        forwarded = [call.kwargs["repos"] for call in mock_func.call_args_list]
        assert forwarded == [["android"], None]


class TestCrossRepoSearchEchoBounds:
    """The caller-supplied ``repos`` echo is bounded like every other list."""

    ENTRIES = [{"path": "/src/android", "alias": "droid"}]

    def _run(self, names):
        from code_review_graph.tools import cross_repo_search_func

        with patch("code_review_graph.registry.Registry") as mock_registry_cls:
            mock_registry_cls.return_value.list_repos.return_value = self.ENTRIES
            return cross_repo_search_func(query="splash", repos=names)

    def test_unknown_list_is_capped_and_reports_the_real_total(self):
        result = self._run(["nope%d" % i for i in range(500)])

        assert len(result["unknown"]) == 20
        assert result["unknown_total"] == 500
        assert result["unknown_truncated"] is True

    def test_summary_carries_a_count_not_the_caller_s_names(self):
        result = self._run(["z" * 500] * 100)

        assert "z" * 500 not in result["summary"]
        assert "100 name(s)" in result["summary"]

    def test_echoed_names_are_sanitised_like_every_other_name(self):
        result = self._run(["ev" + chr(0) + "il" + chr(7)])

        assert result["unknown"] == ["evil"]

    def test_a_dropped_name_reaches_the_summary_of_a_partial_match(self, tmp_path):
        """The partial branch says so too, not only the all-unknown branch."""
        from code_review_graph.tools import cross_repo_search_func

        repo = tmp_path / "android"
        repo.mkdir()
        db = tmp_path / "android.db"
        db.touch()

        with (
            patch("code_review_graph.registry.Registry") as mock_registry_cls,
            patch(
                "code_review_graph.tools.registry_tools.get_db_path",
                side_effect=[db],
            ),
            patch("code_review_graph.tools.registry_tools.GraphStore") as mock_store_cls,
            patch(
                "code_review_graph.tools.registry_tools.hybrid_search",
                side_effect=[[{"name": "Splash", "score": 0.03}]],
            ),
        ):
            mock_registry_cls.return_value.list_repos.return_value = [
                {"path": str(repo), "alias": "droid"},
            ]
            mock_store_cls.side_effect = [MagicMock()]

            result = cross_repo_search_func(
                query="splash", repos=["droid", "nope"]
            )

        assert result["unknown"] == ["nope"]
        assert "1 name(s) matched no repository" in result["summary"]


class TestEchoedNames:
    """Direct tests for the bound-and-sanitise helper behind the name echoes."""

    def test_echoed_caps_the_list_and_reports_the_real_total(self):
        shown, total, truncated = _echoed(["n%d" % i for i in range(100)])

        assert len(shown) == 20
        assert total == 100
        assert truncated is True

    def test_echoed_leaves_a_short_list_whole(self):
        shown, total, truncated = _echoed(["a", "b"])

        assert shown == ["a", "b"]
        assert total == 2
        assert truncated is False

    def test_echoed_strips_control_characters_and_caps_length(self):
        shown, _, _ = _echoed([chr(0) + "ev" + chr(7) + "il", "z" * 500])

        assert shown[0] == "evil"
        assert len(shown[1]) == 256


class TestSelectRepos:
    """Direct tests for the registry selection helper behind ``repos``."""

    ENTRIES = [
        {"path": "/src/android", "alias": "droid"},
        {"path": "/src/ios"},
        {"path": "/src/web", "alias": "frontend"},
    ]

    def test_select_repos_matches_alias_then_folder_name(self):
        selected, unknown, ambiguous = _select_repos(self.ENTRIES, ["droid", "ios"])

        assert [entry["path"] for entry in selected] == ["/src/android", "/src/ios"]
        assert unknown == []
        assert ambiguous == []

    def test_select_repos_keeps_registry_order(self):
        selected, _, _ = _select_repos(self.ENTRIES, ["frontend", "droid"])

        assert [entry["path"] for entry in selected] == ["/src/android", "/src/web"]

    def test_select_repos_reports_unknown_names(self):
        selected, unknown, _ = _select_repos(
            self.ENTRIES, ["droid", "desktop", "tv"]
        )

        assert [entry["path"] for entry in selected] == ["/src/android"]
        assert unknown == ["desktop", "tv"]

    def test_select_repos_deduplicates_alias_and_folder_of_one_repo(self):
        """A repo named twice, once by alias and once by folder, is searched once."""
        selected, unknown, _ = _select_repos(self.ENTRIES, ["droid", "android"])

        assert [entry["path"] for entry in selected] == ["/src/android"]
        assert unknown == []

    def test_select_repos_selects_every_repo_sharing_a_folder_name(self):
        """Sibling checkouts with one folder name are all searched, and flagged."""
        entries = [
            {"path": "/work/acme/api", "alias": "acme"},
            {"path": "/work/beta/api", "alias": "beta"},
        ]

        selected, unknown, ambiguous = _select_repos(entries, ["api"])

        assert [entry["path"] for entry in selected] == [
            "/work/acme/api",
            "/work/beta/api",
        ]
        assert unknown == []
        assert ambiguous == ["api"]

    def test_select_repos_prefers_an_explicit_alias_over_a_folder_name(self):
        """An alias beats another entry's incidental folder name of the same text."""
        entries = [
            {"path": "/a/web"},
            {"path": "/b/frontend", "alias": "web"},
        ]

        selected, unknown, ambiguous = _select_repos(entries, ["web"])

        assert [entry["path"] for entry in selected] == ["/b/frontend"]
        assert unknown == []
        assert ambiguous == []


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


class TestRegistryNonAscii:
    """#497: registry.json is serialized with json.dumps(..., indent=2), which
    defaults to ensure_ascii=True — a registered repo path containing non-ASCII
    characters gets written as literal \\uXXXX escapes instead of UTF-8.
    """

    def test_register_preserves_non_ascii_path(self, tmp_path):
        registry_path = tmp_path / "registry.json"
        registry = Registry(path=registry_path)

        repo = tmp_path / "基于STM32的项目"
        repo.mkdir()
        (repo / ".git").mkdir()
        registry.register(str(repo), alias="crg")

        raw = registry_path.read_text(encoding="utf-8")
        assert "基于STM32的项目" in raw
        assert "\\u" not in raw


class TestRegistryLocationIsolation:
    """The registry must never fall back to the real home directory in tests."""

    def test_default_path_follows_the_env_override(self, tmp_path, monkeypatch):
        from code_review_graph.registry import default_registry_path

        monkeypatch.setenv("CRG_HOME", str(tmp_path / "elsewhere"))
        assert default_registry_path() == tmp_path / "elsewhere" / "registry.json"

    def test_override_is_read_per_call_not_at_import(self, tmp_path, monkeypatch):
        """A module-level constant would freeze the value at first import.

        The autouse fixture sets CRG_HOME before any test runs, so an
        import-time constant would capture the wrong directory and every later
        override would be ignored.
        """
        from code_review_graph.registry import default_registry_path

        monkeypatch.setenv("CRG_HOME", str(tmp_path / "first"))
        first = default_registry_path()
        monkeypatch.setenv("CRG_HOME", str(tmp_path / "second"))
        assert default_registry_path() != first
        assert default_registry_path() == tmp_path / "second" / "registry.json"

    def test_blank_override_falls_back_to_home(self, monkeypatch):
        from code_review_graph.constants import crg_home

        monkeypatch.setenv("CRG_HOME", "   ")
        assert crg_home() == Path.home() / ".code-review-graph"

    def test_bare_registry_writes_under_the_override(self, tmp_path, monkeypatch):
        """Registry() with no path argument must land in the sandbox.

        This is the leak that put pytest tmp paths into a developer's real
        ~/.code-review-graph/registry.json.
        """
        # Point Path.home() at a fake home too, so the assertion that nothing
        # was written there needs no access to the developer's real one.
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

        sandbox = tmp_path / "sandbox"
        monkeypatch.setenv("CRG_HOME", str(sandbox))

        repo = tmp_path / "project"
        repo.mkdir()
        (repo / ".git").mkdir()

        Registry().register(str(repo), alias="leaky")

        sandboxed = sandbox / "registry.json"
        assert sandboxed.exists()
        assert "leaky" in sandboxed.read_text(encoding="utf-8")
        assert not (fake_home / ".code-review-graph").exists()

    def test_get_data_dir_uses_the_sandboxed_registry(self, tmp_path, monkeypatch):
        """incremental.get_data_dir() builds its own Registry() internally."""
        from code_review_graph.incremental import get_data_dir

        monkeypatch.setenv("CRG_HOME", str(tmp_path / "sandbox"))
        monkeypatch.delenv("CRG_DATA_DIR", raising=False)

        repo = tmp_path / "project"
        repo.mkdir()
        (repo / ".git").mkdir()
        external = tmp_path / "external"

        Registry().set_data_dir(str(repo), str(external))

        assert get_data_dir(repo) == external.resolve()
        assert (tmp_path / "sandbox" / "registry.json").exists()
