"""Translation files stay out of the YAML path index, and search filters first.

A translation catalogue repeats the same keys once per language. On sidekiq,
``web/locales/*.yml`` gave 3,147 of the graph's 3,336 ``YamlPath`` nodes, and those
rows crowded real symbols out of search, where the ``kind`` filter ran only after
the fetch limit had already cut the hits. Introduced by PR #1150.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import code_review_graph.embeddings as embeddings_module
from code_review_graph.graph import GraphStore, node_to_dict
from code_review_graph.incremental import full_build, get_db_path, incremental_update
from code_review_graph.parser import CodeParser, NodeInfo
from code_review_graph.search import (
    _embedding_search,
    _fts_search,
    _keyword_search,
    hybrid_search,
    rebuild_fts_index,
)

_TRANSLATION = b"en:\n  errors:\n    missing: Not found\n    denied: Denied\n  title: Home\n"
_CONFIG = b"database:\n  host: localhost\n  pool: 5\n"


def _yaml_paths(nodes: list[NodeInfo]) -> list[str]:
    return [node.name for node in nodes if node.kind == "YamlPath"]


@pytest.mark.parametrize(
    "relative",
    [
        "config/locales/en.yml",
        "web/locales/zh-CN.yml",
        "web/locales/pt-BR.yml",
        "config/locales/devise.en.yml",
        "config/locales/en.devise.yml",
        "config/locales/fil.yml",
        "config/locales/sr-Latn.yml",
        "config/locales/es-419.yml",
        "lib/active_support/locale/en.yml",
        "src/locale/fr.yaml",
        "i18n/de.yml",
        "l10n/de.yml",
        "translations/messages.fr.yaml",
        "locales/en/common.yml",
        "locale/pt_BR/deep/app.yml",
        "_locales/es.yml",
        "Config/Locales/EN.yml",
    ],
)
def test_translation_files_get_a_file_node_only(tmp_path: Path, relative: str) -> None:
    nodes, edges = CodeParser(tmp_path).parse_bytes(tmp_path / relative, _TRANSLATION)

    assert [node.kind for node in nodes] == ["File"]
    assert edges == []
    assert nodes[0].language == "yaml"
    assert nodes[0].line_end == _TRANSLATION.count(b"\n") + 1
    assert nodes[0].extra["yaml_locale_file"] is True
    assert nodes[0].extra["values_indexed"] is False


@pytest.mark.parametrize(
    "relative", ["config/locales/en/application.yml", "locales/de/application-web.yaml"],
)
def test_a_catalogue_named_like_spring_config_is_still_a_translation(
    tmp_path: Path, relative: str,
) -> None:
    nodes, edges = CodeParser(tmp_path).parse_bytes(tmp_path / relative, _TRANSLATION)

    assert [node.kind for node in nodes] == ["File"]
    assert edges == []
    assert nodes[0].extra["yaml_locale_file"] is True


def test_spring_config_outside_a_translation_folder_keeps_its_properties(
    tmp_path: Path,
) -> None:
    nodes, _ = CodeParser(tmp_path).parse_bytes(
        tmp_path / "src/main/resources/application.yml", _CONFIG,
    )

    assert "ConfigProperty" in {node.kind for node in nodes}


def test_a_reader_can_see_why_a_translation_file_has_no_paths(tmp_path: Path) -> None:
    path = tmp_path / "locales" / "en.yml"
    nodes, _ = CodeParser(tmp_path).parse_bytes(path, _TRANSLATION)

    with GraphStore(tmp_path / "graph.db") as store:
        store.store_file_nodes_edges(str(path), nodes, [])
        stored = store.get_nodes_by_file(str(path))

    assert node_to_dict(stored[0])["yaml"]["locale_file"] is True


@pytest.mark.parametrize(
    "relative",
    [
        # The name alone proves nothing: these are also words and job names.
        "config/en.yml",
        "test/fixtures/en.yml",
        ".github/workflows/it.yml",
        "k8s/no.yml",
        "vars/os.yml",
        # Nor does the folder alone.
        "locales/deploy.yml",
        "locales/config.yaml",
        "i18n/settings.yml",
        "config/locales/i18n-tasks.yml",
        # These folders hold programming-language settings as often as not.
        "lang/ts.yml",
        "languages/sh.yml",
        # Named like a folder, or a language folder with no translation folder.
        "config/locales.yml",
        "en/app.yml",
        # Looks like a code, but is not one.
        "locales/api.yml",
        "locales/ui.yml",
    ],
)
def test_real_config_keeps_its_yaml_paths(tmp_path: Path, relative: str) -> None:
    nodes, _ = CodeParser(tmp_path).parse_bytes(tmp_path / relative, _TRANSLATION)

    assert _yaml_paths(nodes) != []
    assert "yaml_locale_file" not in nodes[0].extra


@pytest.mark.parametrize("relative", ["en.yml", "config/en.yml"])
def test_folders_above_the_checkout_do_not_count(tmp_path: Path, relative: str) -> None:
    root = tmp_path / "locales" / "en"
    nodes, _ = CodeParser(root).parse_bytes(root / relative, _TRANSLATION)

    assert _yaml_paths(nodes) != []


def test_without_a_repo_root_nothing_is_called_a_translation(tmp_path: Path) -> None:
    path = tmp_path / "locales" / "en.yml"

    nodes, _ = CodeParser().parse_bytes(path, _TRANSLATION)

    assert _yaml_paths(nodes) != []


def test_a_relative_path_is_read_from_the_repo_root() -> None:
    """The same rule as ``is_test_file``: a relative path has no folders above it."""
    nodes, _ = CodeParser().parse_bytes(Path("config/locales/en.yml"), _TRANSLATION)

    assert [node.kind for node in nodes] == ["File"]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, timeout=30,
    )


def _commit_repo(repo: Path) -> None:
    _git(repo, "add", "-A")
    _git(
        repo, "-c", "user.email=t@example.com", "-c", "user.name=T",
        "-c", "commit.gpgsign=false", "commit", "-q", "-m", "change",
    )


def _write(repo: Path, relative: str, content: bytes) -> None:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def test_builds_keep_config_and_drop_translations(tmp_path: Path) -> None:
    repo = tmp_path.resolve() / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _write(repo, "config/locales/en.yml", _TRANSLATION)
    _write(repo, "config/locales/fr.yml", _TRANSLATION)
    _write(repo, "config/database.yml", _CONFIG)
    _commit_repo(repo)

    with GraphStore(get_db_path(repo)) as store:
        full_build(repo, store)
        locale = str(repo / "config/locales/en.yml")
        config = str(repo / "config/database.yml")
        assert [n.kind for n in store.get_nodes_by_file(locale)] == ["File"]
        assert "$.database.host" in _yaml_paths(store.get_nodes_by_file(config))

    # An edit must not bring the keys back, and config changes still register.
    _write(repo, "config/locales/en.yml", _TRANSLATION + b"  more: words\n")
    _write(repo, "config/database.yml", _CONFIG + b"  port: 5432\n")
    _commit_repo(repo)

    with GraphStore(get_db_path(repo)) as store:
        incremental_update(repo, store)
        assert [n.kind for n in store.get_nodes_by_file(locale)] == ["File"]
        assert "$.database.port" in _yaml_paths(store.get_nodes_by_file(config))


def test_full_build_clears_translation_paths_from_an_older_graph(tmp_path: Path) -> None:
    repo = tmp_path.resolve() / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _write(repo, "config/locales/en.yml", _TRANSLATION)
    _commit_repo(repo)
    locale = repo / "config/locales/en.yml"

    with GraphStore(get_db_path(repo)) as store:
        full_build(repo, store)
        # What an earlier release wrote for this same, unchanged file.
        old_nodes, old_edges = CodeParser().parse_bytes(locale, _TRANSLATION)
        assert _yaml_paths(old_nodes) != []
        store.store_file_nodes_edges(str(locale), old_nodes, old_edges)
        assert _yaml_paths(store.get_nodes_by_file(str(locale))) != []

        full_build(repo, store)

        assert [n.kind for n in store.get_nodes_by_file(str(locale))] == ["File"]


def _node(store: GraphStore, name: str, kind: str) -> None:
    store.upsert_node(NodeInfo(
        kind=kind,
        name=name,
        file_path="/repo/thing.py" if kind != "YamlPath" else "/repo/thing.yml",
        line_start=1,
        line_end=1,
        language="python" if kind != "YamlPath" else "yaml",
    ))


@pytest.fixture
def store(tmp_path: Path):
    graph = GraphStore(tmp_path / "graph.db")
    yield graph
    graph.close()


def test_kind_filter_runs_before_the_fetch_limit(store: GraphStore) -> None:
    """Sixty better-scoring rows of another kind must not hide the only match."""
    for index in range(70):
        _node(store, f"retry_{index}", "YamlPath")
    _node(store, "retry_the_job_after_a_very_long_and_wordy_delay", "Function")
    store.commit()
    rebuild_fts_index(store)

    unfiltered = hybrid_search(store, "retry", limit=5)
    assert {hit["kind"] for hit in unfiltered} == {"YamlPath"}

    results = hybrid_search(store, "retry", kind="Function", limit=5)

    assert [hit["kind"] for hit in results] == ["Function"]


def test_precise_search_widens_when_only_another_kind_matched(store: GraphStore) -> None:
    """The first reading of a query has not answered if none of it is that kind."""
    _node(store, "retry_handler", "YamlPath")
    _node(store, "retry", "Function")
    store.commit()
    rebuild_fts_index(store)

    results = hybrid_search(store, "retry handler", kind="Function", limit=5)

    assert [hit["name"] for hit in results] == ["retry"]


def test_fts_search_filters_by_kind(store: GraphStore) -> None:
    _node(store, "retry", "YamlPath")
    _node(store, "retry_job", "Function")
    store.commit()
    rebuild_fts_index(store)
    function_id = store._conn.execute(
        "SELECT id FROM nodes WHERE kind = 'Function'",
    ).fetchone()[0]

    assert len(_fts_search(store._conn, "retry", limit=5)) == 2
    assert [row[0] for row in _fts_search(store._conn, "retry", limit=5, kind="Function")] == [
        function_id,
    ]


def test_keyword_search_filters_before_its_limit(store: GraphStore) -> None:
    for index in range(10):
        _node(store, f"retry_{index}", "YamlPath")
    _node(store, "retry_job", "Function")
    store.commit()
    function_id = store._conn.execute(
        "SELECT id FROM nodes WHERE kind = 'Function'",
    ).fetchone()[0]

    hits = _keyword_search(store._conn, "retry", limit=3, kind="Function")

    assert [row[0] for row in hits] == [function_id]


def test_embedding_search_filters_before_its_limit(
    store: GraphStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    for index in range(10):
        _node(store, f"retry_{index}", "YamlPath")
    _node(store, "retry_job", "Function")
    store.commit()
    ranked = [
        (row[0], 1.0 - position / 100)
        for position, row in enumerate(store._conn.execute(
            "SELECT qualified_name FROM nodes ORDER BY kind = 'Function', id",
        ))
    ]

    class FakeEmbeddingStore:
        available = True

        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def count(self) -> int:
            return len(ranked)

        def search(self, _query: str, limit: int = 20) -> list[tuple[str, float]]:
            return ranked[:limit]

        def close(self) -> None:
            pass

    monkeypatch.setattr(embeddings_module, "EmbeddingStore", FakeEmbeddingStore)

    assert len(_embedding_search(store, "retry", limit=3)) == 3
    hits = _embedding_search(store, "retry", limit=3, kind="Function")

    assert [store.get_node_by_id(row[0]).name for row in hits] == ["retry_job"]
