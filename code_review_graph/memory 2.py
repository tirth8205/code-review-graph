"""Memory/feedback loop -- persist Q&A results for graph enrichment."""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def save_result(
    question: str,
    answer: str,
    nodes: list[str] | None = None,
    result_type: str = "query",
    memory_dir: Path | None = None,
    repo_root: Path | None = None,
) -> Path:
    """Save a Q&A result as markdown for re-ingestion.

    Args:
        question: The question that was asked.
        answer: The answer/result.
        nodes: Related node qualified names.
        result_type: Type of result (query, review, debug).
        memory_dir: Directory to save to. Defaults to
            <repo>/.code-review-graph/memory/
        repo_root: Repository root for default memory_dir.

    Returns:
        Path to the saved file.
    """
    if memory_dir is None:
        if repo_root is None:
            raise ValueError(
                "Either memory_dir or repo_root required"
            )
        memory_dir = (
            repo_root / ".code-review-graph" / "memory"
        )

    memory_dir.mkdir(parents=True, exist_ok=True)

    # Generate filename from question
    slug = re.sub(r"[^\w\s-]", "", question.lower())
    slug = re.sub(r"[\s_]+", "-", slug).strip("-")[:60]
    timestamp = int(time.time())
    filename = f"{slug}-{timestamp}.md"

    # Build markdown with YAML frontmatter
    lines = [
        "---",
        f"type: {result_type}",
        f"timestamp: {timestamp}",
    ]
    if nodes:
        lines.append("nodes:")
        for n in nodes[:20]:
            lines.append(f"  - {n}")
    lines.extend([
        "---",
        "",
        f"# {question}",
        "",
        answer,
    ])

    path = memory_dir / filename
    path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("Saved result to %s", path)
    return path


def list_memories(
    memory_dir: Path | None = None,
    repo_root: Path | None = None,
) -> list[dict[str, Any]]:
    """List all saved memory files.

    Returns list of dicts with: path, question, type, timestamp.
    """
    if memory_dir is None:
        if repo_root is None:
            return []
        memory_dir = (
            repo_root / ".code-review-graph" / "memory"
        )

    if not memory_dir.exists():
        return []

    results = []
    for f in sorted(memory_dir.glob("*.md")):
        try:
            text = f.read_text(encoding="utf-8")
            # Parse frontmatter
            meta: dict[str, Any] = {"path": str(f)}
            if text.startswith("---"):
                parts = text.split("---", 2)
                if len(parts) >= 3:
                    fm_lines = parts[1].strip().split("\n")
                    for line in fm_lines:
                        if ": " in line and not line.startswith(" "):
                            k, v = line.split(": ", 1)
                            meta[k.strip()] = v.strip()
            # Extract question from first heading
            for line in text.split("\n"):
                if line.startswith("# "):
                    meta["question"] = line[2:].strip()
                    break
            results.append(meta)
        except OSError:
            continue

    return results


def clear_memories(
    memory_dir: Path | None = None,
    repo_root: Path | None = None,
) -> int:
    """Delete all memory files. Returns count deleted."""
    if memory_dir is None:
        if repo_root is None:
            return 0
        memory_dir = (
            repo_root / ".code-review-graph" / "memory"
        )

    if not memory_dir.exists():
        return 0

    count = 0
    for f in memory_dir.glob("*.md"):
        f.unlink()
        count += 1

    logger.info("Cleared %d memory files", count)
    return count
