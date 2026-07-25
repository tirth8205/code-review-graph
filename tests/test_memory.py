"""Tests for code_review_graph.memory module."""

from __future__ import annotations

from pathlib import Path

import pytest

from code_review_graph.memory import clear_memories, list_memories, save_result


def test_save_result_basic(tmp_path: Path) -> None:
    """Save Q&A result normally and verify file creation and contents."""
    question = "How to build the graph?"
    answer = "Run `code-review-graph build`."

    saved_path = save_result(
        question=question,
        answer=answer,
        memory_dir=tmp_path,
    )

    assert saved_path.exists()
    assert saved_path.suffix == ".md"
    assert "how-to-build-the-graph" in saved_path.name

    content = saved_path.read_text(encoding="utf-8")
    assert "type: query" in content
    assert f"# {question}" in content
    assert answer in content


def test_save_result_with_nodes(tmp_path: Path) -> None:
    """Passing nodes list should include them in the frontmatter."""
    nodes = ["code_review_graph.cli.main", "code_review_graph.memory.save_result"]

    saved_path = save_result(
        question="What functions handle memory?",
        answer="memory.py functions.",
        nodes=nodes,
        result_type="review",
        memory_dir=tmp_path,
    )

    content = saved_path.read_text(encoding="utf-8")
    assert "type: review" in content
    assert "nodes:" in content
    for node in nodes:
        assert f"  - {node}" in content


def test_save_result_no_dir_no_root() -> None:
    """Missing both memory_dir and repo_root must raise ValueError."""
    with pytest.raises(ValueError, match="Either memory_dir or repo_root required"):
        save_result(
            question="Test question",
            answer="Test answer",
            memory_dir=None,
            repo_root=None,
        )


def test_save_result_creates_dir(tmp_path: Path) -> None:
    """Non-existent memory_dir should be created automatically."""
    nested_dir = tmp_path / "custom" / "memory_dir"
    assert not nested_dir.exists()

    saved_path = save_result(
        question="Creates dir test?",
        answer="Yes, created.",
        memory_dir=nested_dir,
    )

    assert nested_dir.exists()
    assert saved_path.exists()


def test_save_result_uses_repo_root_default(tmp_path: Path) -> None:
    """When repo_root is provided and memory_dir is None, default path is used."""
    saved_path = save_result(
        question="Default dir test?",
        answer="Saved to repo_root.",
        repo_root=tmp_path,
    )

    expected_dir = tmp_path / ".code-review-graph" / "memory"
    assert saved_path.parent == expected_dir
    assert saved_path.exists()


def test_list_memories_empty(tmp_path: Path) -> None:
    """Empty or non-existent directory should return empty list."""
    assert list_memories(memory_dir=tmp_path) == []

    non_existent = tmp_path / "does_not_exist"
    assert list_memories(memory_dir=non_existent) == []


def test_list_memories_returns_metadata(tmp_path: Path) -> None:
    """Read frontmatter metadata and question header from saved memory files."""
    save_result(
        question="Question 1",
        answer="Answer 1",
        result_type="query",
        memory_dir=tmp_path,
    )
    save_result(
        question="Question 2",
        answer="Answer 2",
        result_type="debug",
        memory_dir=tmp_path,
    )

    memories = list_memories(memory_dir=tmp_path)
    assert len(memories) == 2

    questions = {m.get("question") for m in memories}
    assert questions == {"Question 1", "Question 2"}

    types = {m.get("type") for m in memories}
    assert types == {"query", "debug"}

    for item in memories:
        assert "path" in item
        assert "timestamp" in item


def test_list_memories_no_root() -> None:
    """Returns empty list if memory_dir and repo_root are both None."""
    assert list_memories(memory_dir=None, repo_root=None) == []


def test_clear_memories_basic(tmp_path: Path) -> None:
    """Delete all memory markdown files and return count."""
    save_result(
        question="Q1",
        answer="A1",
        memory_dir=tmp_path,
    )
    save_result(
        question="Q2",
        answer="A2",
        memory_dir=tmp_path,
    )

    assert len(list(tmp_path.glob("*.md"))) == 2

    deleted_count = clear_memories(memory_dir=tmp_path)
    assert deleted_count == 2
    assert len(list(tmp_path.glob("*.md"))) == 0


def test_clear_memories_nonexistent_dir(tmp_path: Path) -> None:
    """Deleting non-existent dir or missing params should return 0."""
    non_existent = tmp_path / "missing"
    assert clear_memories(memory_dir=non_existent) == 0
    assert clear_memories(memory_dir=None, repo_root=None) == 0


def test_save_result_nodes_truncation_limit(tmp_path: Path) -> None:
    """Passing more than 20 nodes should truncate list to first 20 in frontmatter."""
    many_nodes = [f"node_{i}" for i in range(30)]

    saved_path = save_result(
        question="Many nodes test",
        answer="Checking 20 limit",
        nodes=many_nodes,
        memory_dir=tmp_path,
    )

    content = saved_path.read_text(encoding="utf-8")
    assert "  - node_0" in content
    assert "  - node_19" in content
    assert "  - node_20" not in content


def test_list_memories_malformed_or_no_frontmatter(tmp_path: Path) -> None:
    """Handling markdown files with missing frontmatter or no H1 heading."""
    no_fm_file = tmp_path / "simple.md"
    no_fm_file.write_text("Just plain text with no frontmatter.", encoding="utf-8")

    memories = list_memories(memory_dir=tmp_path)
    assert len(memories) == 1
    assert memories[0]["path"] == str(no_fm_file)
    assert "question" not in memories[0]


def test_clear_memories_preserves_non_md_files(tmp_path: Path) -> None:
    """clear_memories must only delete .md files and leave other files untouched."""
    (tmp_path / "memory1.md").write_text("md file", encoding="utf-8")
    config_file = tmp_path / "config.json"
    config_file.write_text("{}", encoding="utf-8")

    deleted = clear_memories(memory_dir=tmp_path)
    assert deleted == 1
    assert not (tmp_path / "memory1.md").exists()
    assert config_file.exists()

