"""DeepSeek Harness: the MCP row in ``$DSH_HOME/cordis.patch.yml`` (from PR #1101).

The layer is DSH's own text, full of comments and ``!!js`` values, so install
and uninstall add or cut out one ``insert`` operation and leave every other
byte alone.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from code_review_graph import dsh, skills, uninstall


@pytest.fixture
def dsh_home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "dsh-home"
    home.mkdir()
    monkeypatch.setenv("DSH_HOME", str(home))
    monkeypatch.setattr(
        skills, "_detect_serve_command", lambda: ("uvx", ["code-review-graph", "serve"])
    )
    return home


@pytest.fixture
def repo(tmp_path) -> Path:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    return root


USER_LAYER = """\
# my own settings
- id: model
  config:
    temperature: 0.2   # keep this comment

- insert:
    - id: mcp-github
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: github
        transport: stdio
        command: npx
        args: ['-y', '@modelcontextprotocol/server-github']
        env:
          GITHUB_TOKEN: !!js process.env.GITHUB_TOKEN
# trailing note
"""

EXPECTED_ROW = {
    "id": "mcp-code-review-graph",
    "name": "@deepseek-ai/dsh-mcp-client",
    "config": {
        "serverName": "code-review-graph",
        "transport": "stdio",
        "command": "uvx",
        "args": ["code-review-graph", "serve"],
    },
}


def _install(repo: Path) -> list[str]:
    return skills.install_platform_configs(repo, target="dsh")


def _rows(path: Path) -> list:
    document = dsh.load(path.read_text(encoding="utf-8"))
    return [row for op in document if "insert" in op for row in op["insert"]]


def test_home_follows_dsh_home_and_defaults_to_dot_dsh(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "custom"))
    assert skills._dsh_config_path() == tmp_path / "custom" / "cordis.patch.yml"
    monkeypatch.setenv("DSH_HOME", "   ")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert skills._dsh_config_path() == tmp_path / ".dsh" / "cordis.patch.yml"


def test_detected_by_its_home_not_by_a_dsh_binary(tmp_path, monkeypatch) -> None:
    # Debian ships an unrelated ``dsh`` (distributed shell) on PATH.
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "absent"))
    assert not skills.PLATFORMS["dsh"]["detect"]()
    (tmp_path / "absent").mkdir()
    assert skills.PLATFORMS["dsh"]["detect"]()


def test_fresh_install_writes_one_insert_dsh_reads(dsh_home, repo) -> None:
    assert _install(repo) == ["DeepSeek Harness"]
    path = dsh_home / "cordis.patch.yml"
    assert dsh.load(path.read_text(encoding="utf-8")) == [{"insert": [EXPECTED_ROW]}]
    # The row pins no repository: the layer applies to every DSH workspace.
    assert "cwd" not in EXPECTED_ROW["config"]


def test_reinstall_is_byte_identical(dsh_home, repo) -> None:
    _install(repo)
    path = dsh_home / "cordis.patch.yml"
    first = path.read_bytes()
    assert _install(repo) == ["DeepSeek Harness"]
    assert path.read_bytes() == first


def test_install_keeps_comments_tags_and_other_operations(dsh_home, repo) -> None:
    path = dsh_home / "cordis.patch.yml"
    path.write_text(USER_LAYER, encoding="utf-8")

    _install(repo)

    text = path.read_text(encoding="utf-8")
    assert text.startswith(USER_LAYER), "install rewrote the user's own text"
    rows = _rows(path)
    assert rows[-1] == EXPECTED_ROW
    assert isinstance(rows[0]["config"]["env"]["GITHUB_TOKEN"], dsh.Tagged)


def test_install_then_uninstall_restores_the_file_exactly(dsh_home, repo) -> None:
    path = dsh_home / "cordis.patch.yml"
    path.write_text(USER_LAYER, encoding="utf-8")
    _install(repo)

    report = uninstall.run(repo=repo, platforms=["dsh"])

    assert report.errors == []
    assert path.read_text(encoding="utf-8") == USER_LAYER
    assert str(path) in " ".join(report.edited_paths)


def test_uninstall_of_a_layer_install_created_leaves_an_empty_list(dsh_home, repo) -> None:
    _install(repo)
    path = dsh_home / "cordis.patch.yml"

    uninstall.run(repo=repo, platforms=["dsh"])

    # DSH refuses a layer that is not a list, so an emptied file says [].
    assert path.read_text(encoding="utf-8") == "[]\n"
    assert dsh.load(path.read_text(encoding="utf-8")) == []


def test_empty_list_and_comment_only_layers_are_filled_in(dsh_home, repo) -> None:
    path = dsh_home / "cordis.patch.yml"
    path.write_text("# notes\n[]\n", encoding="utf-8")
    _install(repo)
    assert path.read_text(encoding="utf-8").startswith("# notes\n- insert:\n")
    assert _rows(path) == [EXPECTED_ROW]

    path.write_text("# only a comment\n", encoding="utf-8")
    _install(repo)
    assert _rows(path) == [EXPECTED_ROW]
    uninstall.run(repo=repo, platforms=["dsh"])
    assert path.read_text(encoding="utf-8") == "# only a comment\n[]\n"


def test_an_indented_list_gets_an_operation_at_its_own_indent(dsh_home, repo) -> None:
    path = dsh_home / "cordis.patch.yml"
    path.write_text("  - id: model\n    config: {temperature: 1}\n", encoding="utf-8")
    _install(repo)
    assert _rows(path) == [EXPECTED_ROW]
    uninstall.run(repo=repo, platforms=["dsh"])
    assert path.read_text(encoding="utf-8") == "  - id: model\n    config: {temperature: 1}\n"


@pytest.mark.parametrize(
    "content",
    [
        "- insert: [\n",  # not YAML
        "theme: dark\n",  # a mapping, not DSH's list of operations
        "[{id: model}]\n",  # a non-empty flow list cannot take a block item
        "a: 1\n---\nb: 2\n",  # two documents
        "[] # comment on the same line\n",
    ],
)
def test_a_layer_that_cannot_be_edited_safely_is_left_alone(
    dsh_home, repo, content, capsys
) -> None:
    path = dsh_home / "cordis.patch.yml"
    path.write_text(content, encoding="utf-8")

    assert _install(repo) == []

    assert path.read_text(encoding="utf-8") == content
    assert "skipping to avoid data loss" in capsys.readouterr().out


def test_a_layer_that_is_not_utf8_is_left_alone(dsh_home, repo, capsys) -> None:
    path = dsh_home / "cordis.patch.yml"
    path.write_bytes(b"- id: model\n  config: {name: caf\xe9}\n")

    assert _install(repo) == []

    assert path.read_bytes() == b"- id: model\n  config: {name: caf\xe9}\n"
    assert "not UTF-8" in capsys.readouterr().out


def test_a_stale_row_from_another_interpreter_is_replaced_in_place(dsh_home, repo) -> None:
    path = dsh_home / "cordis.patch.yml"
    stale = (
        "- id: model\n"
        "  config: {temperature: 1}\n"
        "- insert:\n"
        "    - id: mcp-code-review-graph\n"
        "      name: '@deepseek-ai/dsh-mcp-client'\n"
        "      config:\n"
        "        serverName: code-review-graph\n"
        "        transport: stdio\n"
        "        command: /opt/old/bin/python3.9\n"
        "        args: [-m, code_review_graph, serve]\n"
        "# after\n"
    )
    path.write_text(stale, encoding="utf-8")

    _install(repo)

    text = path.read_text(encoding="utf-8")
    assert "/opt/old/bin/python3.9" not in text
    assert text.startswith("- id: model\n  config: {temperature: 1}\n- insert:\n")
    assert text.endswith("# after\n")
    assert _rows(path) == [EXPECTED_ROW]


@pytest.mark.parametrize(
    "row",
    [
        # A cwd the user chose with DSH's own expression syntax.
        "    - id: mcp-code-review-graph\n"
        "      name: '@deepseek-ai/dsh-mcp-client'\n"
        "      config:\n"
        "        serverName: code-review-graph\n"
        "        transport: stdio\n"
        "        command: uvx\n"
        "        args: [code-review-graph, serve]\n"
        "        cwd: !!js process.cwd()\n",
        # The same server name under a row id of the user's choosing.
        "    - id: my-graph\n"
        "      name: '@deepseek-ai/dsh-mcp-client'\n"
        "      config:\n"
        "        serverName: code-review-graph\n"
        "        transport: streamable-http\n"
        "        url: http://127.0.0.1:8000/mcp\n",
    ],
)
def test_a_hand_written_row_is_not_claimed(dsh_home, repo, row, capsys) -> None:
    path = dsh_home / "cordis.patch.yml"
    content = "- insert:\n" + row
    path.write_text(content, encoding="utf-8")

    assert _install(repo) == []

    assert path.read_text(encoding="utf-8") == content
    assert "hand-written" in capsys.readouterr().out


def test_uninstall_reports_a_row_merged_into_someone_elses_insert(dsh_home, repo) -> None:
    path = dsh_home / "cordis.patch.yml"
    content = (
        "- insert:\n"
        "    - id: mcp-github\n"
        "      name: '@deepseek-ai/dsh-mcp-client'\n"
        "      config: {serverName: github, transport: stdio, command: gh-mcp}\n"
        "    - id: mcp-code-review-graph\n"
        "      name: '@deepseek-ai/dsh-mcp-client'\n"
        "      config:\n"
        "        serverName: code-review-graph\n"
        "        transport: stdio\n"
        "        command: uvx\n"
        "        args: [code-review-graph, serve]\n"
    )
    path.write_text(content, encoding="utf-8")

    report = uninstall.run(repo=repo, platforms=["dsh"])

    assert path.read_text(encoding="utf-8") == content
    assert any("remove it by hand" in item for item in report.skipped_paths)


def test_dry_run_writes_nothing(dsh_home, repo) -> None:
    path = dsh_home / "cordis.patch.yml"
    path.write_text(USER_LAYER, encoding="utf-8")

    assert skills.install_platform_configs(repo, target="dsh", dry_run=True) == [
        "DeepSeek Harness"
    ]
    assert path.read_text(encoding="utf-8") == USER_LAYER

    _install(repo)
    installed = path.read_bytes()
    report = uninstall.run(repo=repo, platforms=["dsh"], dry_run=True)
    assert path.read_bytes() == installed
    assert str(path) in " ".join(report.edited_paths)


def test_dsh_home_outside_home_is_still_cleaned_up(tmp_path, monkeypatch, repo) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    elsewhere = tmp_path / "elsewhere" / "dsh"
    elsewhere.mkdir(parents=True)
    monkeypatch.setenv("DSH_HOME", str(elsewhere))
    _install(repo)
    path = elsewhere / "cordis.patch.yml"
    assert _rows(path)

    report = uninstall.run(repo=repo, platforms=["dsh"])

    assert report.errors == []
    assert dsh.load(path.read_text(encoding="utf-8")) == []


def test_unknown_tags_are_kept_opaque_never_run() -> None:
    document = dsh.load("- id: x\n  config:\n    a: !!python/object:os.system echo\n")
    assert document[0]["config"]["a"] == dsh.Tagged(
        "tag:yaml.org,2002:python/object:os.system", "echo"
    )
