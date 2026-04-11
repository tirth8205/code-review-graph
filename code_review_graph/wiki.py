"""Wiki generation from community structure.

Generates markdown pages for each detected community and an index page,
providing a navigable documentation wiki for the codebase architecture.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

from .communities import get_communities
from .flows import get_flows
from .graph import GraphStore, _sanitize_name

logger = logging.getLogger(__name__)


def _slugify(name: str) -> str:
    """Convert a community name to a safe filename slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:80] or "unnamed"


def _generate_community_page(store: GraphStore, community: dict[str, Any]) -> str:
    """Build markdown content for a single community.

    Includes: heading, overview (size, cohesion, language), members table
    (top 50), execution flows through the community, and dependencies.

    Args:
        store: The graph store.
        community: Community dict from get_communities().

    Returns:
        Markdown string for the community page.
    """
    name = community["name"]
    size = community["size"]
    cohesion = community.get("cohesion", 0.0)
    lang = community.get("dominant_language", "")
    description = community.get("description", "")

    lines: list[str] = []
    lines.append(f"# {name}")
    lines.append("")

    # Overview section
    lines.append("## Overview")
    lines.append("")
    if description:
        lines.append(f"{description}")
        lines.append("")
    lines.append(f"- **Size**: {size} nodes")
    lines.append(f"- **Cohesion**: {cohesion:.4f}")
    if lang:
        lines.append(f"- **Dominant Language**: {lang}")
    lines.append("")

    # Members table (top 50)
    member_qns = community.get("members", [])
    lines.append("## Members")
    lines.append("")
    if member_qns:
        lines.append("| Name | Kind | File | Lines |")
        lines.append("|------|------|------|-------|")

        # Fetch node details for members (limit to 50)
        member_count = 0
        for qn in member_qns[:50]:
            node = store.get_node(qn)
            if node and node.kind != "File":
                node_name = _sanitize_name(node.name)
                lines.append(
                    f"| {node_name} | {node.kind} | {node.file_path} "
                    f"| {node.line_start}-{node.line_end} |"
                )
                member_count += 1

        if not member_count:
            # Remove the table headers if no members were added
            lines.pop()  # header separator
            lines.pop()  # header
            lines.append("No non-file members found.")

        if len(member_qns) > 50:
            lines.append("")
            lines.append(f"*... and {len(member_qns) - 50} more members.*")
    else:
        lines.append("No members found.")
    lines.append("")

    # Execution flows through community
    lines.append("## Execution Flows")
    lines.append("")
    member_set = set(member_qns)
    try:
        all_flows = get_flows(store, sort_by="criticality", limit=200)
        community_flows: list[dict] = []
        for flow in all_flows:
            # Check if this flow passes through any community member
            flow_qns = store.get_flow_qualified_names(flow["id"])
            if flow_qns & member_set:
                community_flows.append(flow)

        if community_flows:
            for flow in community_flows[:10]:
                flow_name = _sanitize_name(flow.get("name", "unnamed"))
                criticality = flow.get("criticality", 0.0)
                depth = flow.get("depth", 0)
                lines.append(
                    f"- **{flow_name}** (criticality: {criticality:.2f}, depth: {depth})"
                )
            if len(community_flows) > 10:
                lines.append(f"- *... and {len(community_flows) - 10} more flows.*")
        else:
            lines.append("No execution flows pass through this community.")
    except sqlite3.OperationalError as exc:
        logger.debug("wiki: flows table unavailable: %s", exc)
        lines.append("Execution flow data not available.")
    lines.append("")

    # Dependencies (cross-community edges)
    lines.append("## Dependencies")
    lines.append("")
    try:
        outgoing_targets: Counter[str] = Counter()
        incoming_sources: Counter[str] = Counter()
        if member_qns:
            qns = list(member_qns)

            # Outgoing: source is a member
            for t in store.get_outgoing_targets(qns):
                if t not in member_set:
                    outgoing_targets[t] += 1

            # Incoming: target is a member
            for s in store.get_incoming_sources(qns):
                if s not in member_set:
                    incoming_sources[s] += 1

        if outgoing_targets:
            lines.append("### Outgoing")
            lines.append("")
            for target, count in outgoing_targets.most_common(15):
                lines.append(f"- `{_sanitize_name(target)}` ({count} edge(s))")
            lines.append("")

        if incoming_sources:
            lines.append("### Incoming")
            lines.append("")
            for source, count in incoming_sources.most_common(15):
                lines.append(f"- `{_sanitize_name(source)}` ({count} edge(s))")
            lines.append("")

        if not outgoing_targets and not incoming_sources:
            lines.append("No cross-community dependencies detected.")
            lines.append("")
    except sqlite3.OperationalError as exc:
        logger.debug("wiki: dependency edges unavailable: %s", exc)
        lines.append("Dependency data not available.")
        lines.append("")

    return "\n".join(lines)


def generate_wiki(
    store: GraphStore,
    wiki_dir: str | Path,
    force: bool = False,
) -> dict[str, Any]:
    """Generate a markdown wiki from the community structure.

    For each community, generates a markdown page. Also generates an
    index.md with links to all community pages.

    Args:
        store: The graph store.
        wiki_dir: Directory to write wiki pages into.
        force: If True, regenerate all pages even if content unchanged.

    Returns:
        Dict with pages_generated, pages_updated, pages_unchanged counts.
    """
    wiki_path = Path(wiki_dir)
    wiki_path.mkdir(parents=True, exist_ok=True)

    communities = get_communities(store)

    pages_generated = 0
    pages_updated = 0
    pages_unchanged = 0

    page_entries: list[tuple[str, str, int]] = []  # (slug, name, size)

    # Track slugs we've already used in THIS run so two communities that
    # slugify to the same filename don't overwrite each other (#222 follow-up).
    # Previously "Data Processing" and "data processing" both became
    # "data-processing.md", causing silent data loss and inflated "updated"
    # counters (each collision was counted as an update while only one file
    # made it to disk).
    used_slugs: set[str] = set()

    for comm in communities:
        name = comm["name"]
        base_slug = _slugify(name)
        slug = base_slug
        suffix = 2
        while slug in used_slugs:
            slug = f"{base_slug}-{suffix}"
            suffix += 1
        used_slugs.add(slug)

        filename = f"{slug}.md"
        filepath = wiki_path / filename

        content = _generate_community_page(store, comm)

        if filepath.exists() and not force:
            existing = filepath.read_text(encoding="utf-8")
            if existing == content:
                pages_unchanged += 1
                page_entries.append((slug, name, comm["size"]))
                continue

        already_existed = filepath.exists()
        filepath.write_text(content, encoding="utf-8")
        if already_existed:
            pages_updated += 1
        else:
            pages_generated += 1
        page_entries.append((slug, name, comm["size"]))

    # Generate index.md
    index_lines: list[str] = []
    index_lines.append("# Code Wiki")
    index_lines.append("")
    index_lines.append(
        "Auto-generated documentation from the code knowledge graph community structure."
    )
    index_lines.append("")
    index_lines.append(f"**Total communities**: {len(communities)}")
    index_lines.append("")
    index_lines.append("## Communities")
    index_lines.append("")
    index_lines.append("| Community | Size | Link |")
    index_lines.append("|-----------|------|------|")
    for slug, name, size in sorted(page_entries, key=lambda x: x[1]):
        index_lines.append(f"| {name} | {size} | [{slug}.md]({slug}.md) |")
    index_lines.append("")

    index_content = "\n".join(index_lines)
    index_path = wiki_path / "index.md"

    if index_path.exists() and not force:
        existing_index = index_path.read_text(encoding="utf-8")
        if existing_index == index_content:
            pages_unchanged += 1
        else:
            index_path.write_text(index_content, encoding="utf-8")
            pages_updated += 1
    else:
        index_path.write_text(index_content, encoding="utf-8")
        pages_generated += 1

    return {
        "pages_generated": pages_generated,
        "pages_updated": pages_updated,
        "pages_unchanged": pages_unchanged,
    }


def get_wiki_page(wiki_dir: str | Path, page_name: str) -> str | None:
    """Retrieve a specific wiki page by community name.

    Args:
        wiki_dir: Directory containing wiki pages.
        page_name: Community name (will be slugified for filename lookup).

    Returns:
        Page content as a string, or None if the page does not exist.
    """
    wiki_path = Path(wiki_dir)
    slug = _slugify(page_name)
    filepath = wiki_path / f"{slug}.md"

    if filepath.is_file():
        return filepath.read_text(encoding="utf-8")

    # Fallback: try exact filename match — with path traversal protection
    exact_path = (wiki_path / page_name).resolve()
    if exact_path.is_file() and exact_path.is_relative_to(wiki_path.resolve()):
        return exact_path.read_text(encoding="utf-8")

    # Fallback: search for partial match
    if wiki_path.is_dir():
        for p in wiki_path.iterdir():
            if p.suffix == ".md" and slug in p.stem:
                return p.read_text(encoding="utf-8")

    return None
