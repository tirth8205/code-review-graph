"""Extreme edge cases for the Hermes Agent YAML install/uninstall (PR #778).

These stress the line-oriented YAML editing beyond the PR's own coverage:
odd indentation, header comments, duplicate top-level keys, flow-style
entries, column-zero comments splitting a section, CRLF input, unicode,
and repeated install/uninstall cycles. The contract under test: CRG may
add or remove exactly ``mcp_servers.code-review-graph`` and must either
preserve everything else or refuse to touch the file at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from code_review_graph import skills, uninstall

ENTRY = {"command": "code-review-graph", "args": ["serve"]}


@pytest.fixture
def hermes_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.fixture
def config(hermes_home: Path) -> Path:
    return hermes_home / "config.yaml"


def _install(config: Path) -> bool | None:
    return skills._merge_yaml_mcp_server(config, "mcp_servers", "code-review-graph", ENTRY)


def _uninstall(config: Path, hermes_home: Path) -> uninstall.UninstallReport:
    report = uninstall.UninstallReport()
    uninstall._remove_yaml_entry(config, "mcp_servers", hermes_home, report, dry_run=False)
    return report


class TestInstallEdges:
    def test_header_with_trailing_comment_is_still_a_block(self, config: Path) -> None:
        config.write_text(
            "mcp_servers:  # user comment on the header\n"
            "  other:\n"
            "    command: npx\n",
            encoding="utf-8",
        )
        assert _install(config) is True
        text = config.read_text(encoding="utf-8")
        assert "# user comment on the header" in text
        data = yaml.safe_load(text)
        assert set(data["mcp_servers"]) == {"other", "code-review-graph"}

    def test_comment_only_null_section(self, config: Path) -> None:
        config.write_text("mcp_servers:\n  # none yet\n", encoding="utf-8")
        assert _install(config) is True
        text = config.read_text(encoding="utf-8")
        assert "# none yet" in text
        data = yaml.safe_load(text)
        assert data["mcp_servers"]["code-review-graph"] == ENTRY

    def test_four_space_child_indent(self, config: Path) -> None:
        config.write_text(
            "mcp_servers:\n"
            "    other:\n"
            "        command: npx\n"
            "after: 1\n",
            encoding="utf-8",
        )
        assert _install(config) is True
        data = yaml.safe_load(config.read_text(encoding="utf-8"))
        assert data["mcp_servers"]["other"] == {"command": "npx"}
        assert data["mcp_servers"]["code-review-graph"] == ENTRY
        assert data["after"] == 1

    def test_file_without_trailing_newline(self, config: Path) -> None:
        config.write_text("mcp_servers:\n  other:\n    command: npx", encoding="utf-8")
        assert _install(config) is True
        data = yaml.safe_load(config.read_text(encoding="utf-8"))
        assert set(data["mcp_servers"]) == {"other", "code-review-graph"}

    def test_duplicate_top_level_sections_refused_unchanged(self, config: Path) -> None:
        # PyYAML resolves duplicate keys last-wins; a text edit into the
        # first block would silently vanish. The validator must refuse.
        original = (
            "mcp_servers:\n"
            "  first: {command: a}\n"
            "mcp_servers:\n"
            "  second: {command: b}\n"
        )
        config.write_text(original, encoding="utf-8")
        assert _install(config) is None
        assert config.read_text(encoding="utf-8") == original

    def test_crlf_file_stays_semantically_intact(self, config: Path) -> None:
        crlf = (
            "# top\r\nmodel:\r\n  default: x\r\n\r\n"
            "mcp_servers:\r\n  other:\r\n    command: npx\r\n\r\ntheme: dark\r\n"
        )
        config.write_bytes(crlf.encode("utf-8"))
        assert _install(config) is True
        data = yaml.safe_load(config.read_text(encoding="utf-8"))
        assert data["model"] == {"default": "x"}
        assert data["theme"] == "dark"
        assert data["mcp_servers"]["other"] == {"command": "npx"}
        assert data["mcp_servers"]["code-review-graph"] == ENTRY
        assert "# top" in config.read_text(encoding="utf-8")

    def test_unicode_comments_and_values_survive(self, config: Path) -> None:
        config.write_text(
            "# café ☕配置\nmodel:\n  default: 模型\nmcp_servers:\n"
            "  other:\n    command: npx\n",
            encoding="utf-8",
        )
        assert _install(config) is True
        text = config.read_text(encoding="utf-8")
        assert "# café ☕配置" in text
        data = yaml.safe_load(text)
        assert data["model"]["default"] == "模型"

    def test_append_to_comment_only_file(self, config: Path) -> None:
        config.write_text("# just a comment\n", encoding="utf-8")
        assert _install(config) is True
        text = config.read_text(encoding="utf-8")
        assert text.startswith("# just a comment\n")
        data = yaml.safe_load(text)
        assert data["mcp_servers"]["code-review-graph"] == ENTRY

    def test_tab_indented_file_refused(self, config: Path) -> None:
        # Tabs are illegal YAML indentation; the parse fails and the
        # installer must refuse rather than guess.
        broken = "mcp_servers:\n\tother:\n\t\tcommand: npx\n"
        config.write_text(broken, encoding="utf-8")
        assert _install(config) is None
        assert config.read_text(encoding="utf-8") == broken

    def test_section_last_line_comment_keeps_separator(self, config: Path) -> None:
        config.write_text(
            "mcp_servers:\n"
            "  other:\n"
            "    command: npx\n"
            "  # trailing note\n"
            "\n"
            "theme: dark\n",
            encoding="utf-8",
        )
        assert _install(config) is True
        text = config.read_text(encoding="utf-8")
        assert "  # trailing note\n" in text
        data = yaml.safe_load(text)
        assert data["theme"] == "dark"
        assert set(data["mcp_servers"]) == {"other", "code-review-graph"}


class TestUninstallEdges:
    def test_similarly_named_sibling_survives(
        self, config: Path, hermes_home: Path
    ) -> None:
        config.write_text(
            "mcp_servers:\n"
            "  code-review-graph-extra:\n"
            "    command: keepme\n"
            "  code-review-graph:\n"
            "    command: code-review-graph\n"
            "    args:\n"
            "    - serve\n",
            encoding="utf-8",
        )
        report = _uninstall(config, hermes_home)
        assert not report.skipped_paths
        data = yaml.safe_load(config.read_text(encoding="utf-8"))
        assert set(data["mcp_servers"]) == {"code-review-graph-extra"}

    def test_flow_style_entry_removed(self, config: Path, hermes_home: Path) -> None:
        config.write_text(
            "mcp_servers:\n"
            "  code-review-graph: {command: code-review-graph, args: [serve]}\n"
            "  other: {command: npx}\n",
            encoding="utf-8",
        )
        report = _uninstall(config, hermes_home)
        assert not report.skipped_paths
        data = yaml.safe_load(config.read_text(encoding="utf-8"))
        assert set(data["mcp_servers"]) == {"other"}

    def test_entry_mid_section_removes_only_entry(
        self, config: Path, hermes_home: Path
    ) -> None:
        original = (
            "mcp_servers:\n"
            "  before:\n"
            "    command: a\n"
            "  code-review-graph:\n"
            "    command: code-review-graph\n"
            "    # comment inside our entry, removed with it\n"
            "    args:\n"
            "    - serve\n"
            "  after:\n"
            "    command: b\n"
        )
        config.write_text(original, encoding="utf-8")
        report = _uninstall(config, hermes_home)
        assert not report.skipped_paths
        text = config.read_text(encoding="utf-8")
        assert "comment inside our entry" not in text
        data = yaml.safe_load(text)
        assert set(data["mcp_servers"]) == {"before", "after"}
        assert data["mcp_servers"]["before"] == {"command": "a"}
        assert data["mcp_servers"]["after"] == {"command": "b"}

    def test_column_zero_comment_hiding_entry_refuses_safely(
        self, config: Path, hermes_home: Path
    ) -> None:
        # A column-0 comment ends the scanned section early, so the entry
        # below it cannot be located. The only acceptable outcome is an
        # untouched file plus a skip report -- never a partial edit.
        original = (
            "mcp_servers:\n"
            "  other:\n"
            "    command: npx\n"
            "# column-zero comment splits the section\n"
            "  code-review-graph:\n"
            "    command: code-review-graph\n"
        )
        config.write_text(original, encoding="utf-8")
        report = _uninstall(config, hermes_home)
        assert config.read_text(encoding="utf-8") == original
        assert report.skipped_paths

    def test_duplicate_sections_both_holding_entry_refuse(
        self, config: Path, hermes_home: Path
    ) -> None:
        original = (
            "mcp_servers:\n"
            "  code-review-graph:\n"
            "    command: a\n"
            "mcp_servers:\n"
            "  code-review-graph:\n"
            "    command: b\n"
        )
        config.write_text(original, encoding="utf-8")
        report = _uninstall(config, hermes_home)
        assert config.read_text(encoding="utf-8") == original
        assert report.skipped_paths

    def test_entry_rewritten_with_space_before_colon_refuses(
        self, config: Path, hermes_home: Path
    ) -> None:
        # Valid YAML the text scanner cannot anchor on: refuse, do not guess.
        original = (
            "mcp_servers:\n"
            "  code-review-graph : {command: code-review-graph}\n"
            "  other: {command: npx}\n"
        )
        config.write_text(original, encoding="utf-8")
        report = _uninstall(config, hermes_home)
        assert config.read_text(encoding="utf-8") == original
        assert report.skipped_paths

    def test_alias_into_entry_refuses(self, config: Path, hermes_home: Path) -> None:
        # Another server aliases a node inside our entry; removing the
        # anchor would orphan the alias. The reparse must catch it.
        original = (
            "mcp_servers:\n"
            "  code-review-graph:\n"
            "    command: &crg code-review-graph\n"
            "  other:\n"
            "    command: *crg\n"
        )
        config.write_text(original, encoding="utf-8")
        report = _uninstall(config, hermes_home)
        assert config.read_text(encoding="utf-8") == original
        assert report.skipped_paths


class TestRoundTrips:
    def test_three_install_uninstall_cycles_restore_bytes(
        self, config: Path, hermes_home: Path
    ) -> None:
        original = (
            "# Hermes Agent configuration\n"
            "model:\n"
            "  default: claude-opus-5   # trailing comment\n"
            "\n"
            "mcp_servers:\n"
            "  browsermcp:\n"
            "    command: npx\n"
            "\n"
            "tool_output:\n"
            "  max_bytes: 50000\n"
        )
        config.write_text(original, encoding="utf-8")
        for _ in range(3):
            assert _install(config) is True
            assert _install(config) is False  # idempotent
            report = _uninstall(config, hermes_home)
            assert not report.skipped_paths
            assert config.read_text(encoding="utf-8") == original

    def test_crlf_round_trip_is_semantically_lossless(
        self, config: Path, hermes_home: Path
    ) -> None:
        crlf = (
            "# top\r\nmodel:\r\n  default: x\r\n\r\n"
            "mcp_servers:\r\n  other:\r\n    command: npx\r\n\r\ntheme: dark\r\n"
        )
        config.write_bytes(crlf.encode("utf-8"))
        before = yaml.safe_load(crlf)
        assert _install(config) is True
        report = _uninstall(config, hermes_home)
        assert not report.skipped_paths
        after_text = config.read_text(encoding="utf-8")
        assert yaml.safe_load(after_text) == before
        assert "# top" in after_text
