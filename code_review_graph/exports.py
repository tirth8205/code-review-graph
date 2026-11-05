"""Additional export formats: JSON, GraphML, Neo4j Cypher, Obsidian, SVG."""

from __future__ import annotations

import html
import json
import logging
import os
import re
import tempfile
from pathlib import Path

from .graph import GraphStore, _sanitize_name
from .visualization import export_graph_data

logger = logging.getLogger(__name__)


class MissingOptionalDependencyError(ImportError):
    """An export needs a package that a default install does not ship.

    Subclasses ``ImportError`` so existing callers keep working, but is
    specific enough that a CLI can catch it and print the one-line install
    hint instead of a traceback — without swallowing an ImportError raised
    by a genuinely broken install of a required package.
    """


# -------------------------------------------------------------------
# JSON export
# -------------------------------------------------------------------

def export_json(store: GraphStore, output_path: Path) -> Path:
    """Export the complete local graph payload as UTF-8 JSON atomically.

    The payload can contain absolute local paths and code-structure metadata.
    Callers are responsible for deciding whether it is safe to publish.
    """
    data = export_graph_data(store)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output_path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    logger.info("JSON exported to %s", output_path)
    return output_path


# -------------------------------------------------------------------
# GraphML export (for Gephi, yEd, Cytoscape)
# -------------------------------------------------------------------

#: The GraphML 1.0 namespace. Every GraphML reader keys off this exact URI.
GRAPHML_NS = "http://graphml.graphdrawing.org/xmlns"
#: Canonical location of the schema, for the ``xsi:schemaLocation`` pair.
GRAPHML_SCHEMA = "http://graphml.graphdrawing.org/xmlns/1.0/graphml.xsd"

#: Characters XML 1.0 forbids outright — not even as a character reference.
#:
#: This is exactly the complement of the XML 1.0 ``Char`` production below
#: U+0020: everything from U+0000 to U+001F except tab, newline and carriage
#: return. U+007F (DEL) is deliberately *not* here. ``Char`` admits the whole
#: of ``[#x20-#xD7FF]``, so DEL is a legal XML 1.0 character, and
#: ``_sanitize_name`` keeps it in a node name. Dropping it would rewrite the
#: identity this export carries: two names differing only by a DEL would come
#: back from the file as one, which is the newline-collision bug this export
#: already avoids for ``node/@id``, in a smaller form.
_XML_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _xml_text(value: object) -> str:
    """Escape a value for XML element content.

    Drops the control characters XML 1.0 cannot represent at all, then
    escapes the markup characters. Element content (unlike an attribute
    value) is not whitespace-normalised, so a newline survives verbatim.
    """
    return html.escape(_XML_ILLEGAL.sub("", str(value)), quote=False)


def export_graphml(store: GraphStore, output_path: Path) -> Path:
    """Export the graph as GraphML XML for Gephi/yEd/Cytoscape.

    The document declares the official GraphML namespace and validates
    against the GraphML 1.0 schema. That schema types ``node/@id`` as an
    ``NMTOKEN``, which cannot hold the ``/`` in a qualified name, and XML
    attribute-value normalisation would fold a newline inside a name onto a
    space and silently merge two distinct nodes. So the identity travels in
    a ``<data key="qualified_name">`` element and the ids are synthetic.

    Returns the path to the written file.
    """
    data = export_graph_data(store)
    nodes = data["nodes"]
    edges = data["edges"]

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<graphml xmlns="{GRAPHML_NS}"',
        '  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"',
        f'  xsi:schemaLocation="{GRAPHML_NS} {GRAPHML_SCHEMA}">',
        '  <key id="qualified_name" for="node" '
        'attr.name="qualified_name" attr.type="string"/>',
        '  <key id="name" for="node" attr.name="name" '
        'attr.type="string"/>',
        '  <key id="kind" for="node" attr.name="kind" '
        'attr.type="string"/>',
        '  <key id="file" for="node" attr.name="file" '
        'attr.type="string"/>',
        '  <key id="language" for="node" attr.name="language" '
        'attr.type="string"/>',
        '  <key id="community" for="node" attr.name="community" '
        'attr.type="int"/>',
        '  <key id="edge_kind" for="edge" attr.name="kind" '
        'attr.type="string"/>',
        '  <graph id="code-review-graph" edgedefault="directed">',
    ]

    # Qualified name -> NMTOKEN id, so edges can reference declared nodes.
    node_ids: dict[str, str] = {}
    for index, n in enumerate(nodes):
        node_ids.setdefault(n["qualified_name"], f"n{index}")

    for index, n in enumerate(nodes):
        lines.append(f'    <node id="n{index}">')
        lines.append('      <data key="qualified_name">'
                     f'{_xml_text(n["qualified_name"])}</data>')
        lines.append('      <data key="name">'
                     f'{_xml_text(n.get("name", ""))}</data>')
        lines.append('      <data key="kind">'
                     f'{_xml_text(n.get("kind", ""))}</data>')
        lines.append('      <data key="file">'
                     f'{_xml_text(n.get("file_path", ""))}</data>')
        lines.append('      <data key="language">'
                     f'{_xml_text(n.get("language", "") or "")}</data>')
        cid = n.get("community_id")
        if cid is not None:
            lines.append(f'      <data key="community">{int(cid)}</data>')
        lines.append('    </node>')

    written_edges = 0
    for e in edges:
        src = node_ids.get(e["source"])
        tgt = node_ids.get(e["target"])
        if src is None or tgt is None:
            # A dangling endpoint would break the schema's keyref on
            # edge/@source and edge/@target, so drop the edge instead.
            logger.debug(
                "GraphML: dropping edge with an unknown endpoint (%r -> %r)",
                e["source"], e["target"],
            )
            continue
        lines.append(
            f'    <edge id="e{written_edges}" source="{src}" '
            f'target="{tgt}">'
        )
        lines.append('      <data key="edge_kind">'
                     f'{_xml_text(e.get("kind", ""))}</data>')
        lines.append('    </edge>')
        written_edges += 1

    lines.append('  </graph>')
    lines.append('</graphml>')

    output_path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("GraphML exported to %s (%d nodes, %d edges)",
                output_path, len(nodes), written_edges)
    return output_path


# -------------------------------------------------------------------
# Neo4j Cypher export
# -------------------------------------------------------------------

def export_neo4j_cypher(store: GraphStore, output_path: Path) -> Path:
    """Export the graph as Neo4j Cypher CREATE statements.

    Returns the path to the written file.
    """
    data = export_graph_data(store)
    nodes = data["nodes"]
    edges = data["edges"]

    lines = [
        "// Generated by code-review-graph",
        "// Import: paste into Neo4j Browser or run via cypher-shell",
        "",
    ]

    # Create nodes
    for n in nodes:
        kind = n.get("kind", "Node")
        props = {
            "qualified_name": n["qualified_name"],
            "name": n.get("name", ""),
            "file_path": n.get("file_path", ""),
            "language": n.get("language", "") or "",
        }
        cid = n.get("community_id")
        if cid is not None:
            props["community_id"] = cid
        props_str = _cypher_props(props)
        lines.append(f"CREATE (:{kind} {props_str});")

    lines.append("")

    # Create edges via MATCH
    for e in edges:
        kind = e.get("kind", "RELATES_TO")
        src_qn = _cypher_escape(e["source"])
        tgt_qn = _cypher_escape(e["target"])
        lines.append(
            f"MATCH (a {{qualified_name: '{src_qn}'}}), "
            f"(b {{qualified_name: '{tgt_qn}'}}) "
            f"CREATE (a)-[:{kind}]->(b);"
        )

    output_path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("Neo4j Cypher exported to %s (%d nodes, %d edges)",
                output_path, len(nodes), len(edges))
    return output_path


def _cypher_escape(s: str) -> str:
    """Escape a string for Cypher single-quoted literals."""
    return s.replace("\\", "\\\\").replace("'", "\\'")


def _cypher_props(d: dict) -> str:
    """Format a dict as Cypher property map.

    ``bool`` is tested before ``int`` because it is a subclass of ``int``:
    the other order makes the boolean branch unreachable and emits Python's
    ``True``/``False`` instead of Cypher's ``true``/``false``.
    """
    parts = []
    for k, v in d.items():
        if isinstance(v, str):
            parts.append(f"{k}: '{_cypher_escape(v)}'")
        elif isinstance(v, bool):
            parts.append(f"{k}: {'true' if v else 'false'}")
        elif isinstance(v, (int, float)):
            parts.append(f"{k}: {v}")
    return "{" + ", ".join(parts) + "}"


# -------------------------------------------------------------------
# Obsidian vault export
# -------------------------------------------------------------------

def export_obsidian_vault(
    store: GraphStore, output_dir: Path
) -> Path:
    """Export the graph as an Obsidian vault with wikilinks.

    Creates:
    - One .md per node with YAML frontmatter and [[wikilinks]]
    - _COMMUNITY_*.md overview notes per community
    - _INDEX.md with links to all nodes

    Returns the output directory path.
    """
    data = export_graph_data(store)
    nodes = data["nodes"]
    edges = data["edges"]
    communities = data.get("communities", [])

    output_dir.mkdir(parents=True, exist_ok=True)

    # Build adjacency for wikilinks
    neighbors: dict[str, list[dict]] = {}
    for e in edges:
        src = e["source"]
        tgt = e["target"]
        kind = e.get("kind", "RELATES_TO")
        neighbors.setdefault(src, []).append(
            {"target": tgt, "kind": kind}
        )
        neighbors.setdefault(tgt, []).append(
            {"target": src, "kind": kind}
        )

    # Node name -> slug mapping
    slugs: dict[str, str] = {}
    for n in nodes:
        slug = _obsidian_slug(n.get("name", n["qualified_name"]))
        # Handle collisions
        base_slug = slug
        counter = 1
        while slug in slugs.values():
            slug = f"{base_slug}-{counter}"
            counter += 1
        slugs[n["qualified_name"]] = slug

    # Write node pages
    for n in nodes:
        qn = n["qualified_name"]
        slug = slugs[qn]
        name = n.get("name", qn)

        frontmatter = {
            "kind": n.get("kind", ""),
            "file": n.get("file_path", ""),
            "language": n.get("language", "") or "",
            "community": n.get("community_id"),
            "tags": [n.get("kind", "").lower()],
        }

        lines = ["---"]
        for k, v in frontmatter.items():
            if isinstance(v, list):
                lines.append(f"{k}:")
                for item in v:
                    lines.append(f"  - {_yaml_scalar(item)}")
            elif v is not None:
                lines.append(f"{k}: {_yaml_scalar(v)}")
        lines.append("---")
        lines.append(f"# {_sanitize_name(name)}")
        lines.append("")
        lines.append(f"**Kind:** {n.get('kind', '')}")
        lines.append(f"**File:** `{n.get('file_path', '')}`")
        lines.append("")

        # Wikilinks to neighbors
        nbrs = neighbors.get(qn, [])
        if nbrs:
            lines.append("## Connections")
            lines.append("")
            seen = set()
            for nb in nbrs:
                tgt_slug = slugs.get(nb["target"])
                if tgt_slug and tgt_slug not in seen:
                    seen.add(tgt_slug)
                    tgt_name = tgt_slug.replace("-", " ").title()
                    lines.append(
                        f"- {nb['kind']}: "
                        f"[[{tgt_slug}|{tgt_name}]]"
                    )

        page_path = output_dir / f"{slug}.md"
        page_path.write_text("\n".join(lines), encoding="utf-8")

    # Write community overview pages
    community_map: dict[int, list[str]] = {}
    for n in nodes:
        cid = n.get("community_id")
        if cid is not None:
            community_map.setdefault(cid, []).append(
                n["qualified_name"]
            )

    for c in communities:
        cid = c.get("id")
        cname = c.get("name", f"community-{cid}")
        members = community_map.get(cid, [])

        lines = [f"# Community: {_sanitize_name(cname)}", ""]
        lines.append(f"**Size:** {c.get('size', len(members))}")
        lines.append(f"**Cohesion:** {c.get('cohesion', 0):.2f}")
        lang = c.get("dominant_language", "")
        if lang:
            lines.append(f"**Language:** {lang}")
        lines.append("")
        lines.append("## Members")
        lines.append("")
        for qn in members[:50]:
            slug = slugs.get(qn)
            if slug:
                lines.append(f"- [[{slug}]]")

        page_path = output_dir / f"_COMMUNITY_{cid}.md"
        page_path.write_text("\n".join(lines), encoding="utf-8")

    # Write index
    index_lines = ["# Code Graph Index", ""]
    index_lines.append(f"**Nodes:** {len(nodes)}")
    index_lines.append(f"**Edges:** {len(edges)}")
    index_lines.append(
        f"**Communities:** {len(communities)}"
    )
    index_lines.append("")
    index_lines.append("## All Nodes")
    index_lines.append("")
    for n in sorted(nodes, key=lambda x: x.get("name", "")):
        slug = slugs.get(n["qualified_name"])
        if slug:
            index_lines.append(
                f"- [[{slug}]] ({n.get('kind', '')})"
            )

    (output_dir / "_INDEX.md").write_text(
        "\n".join(index_lines), encoding="utf-8"
    )

    logger.info(
        "Obsidian vault exported to %s (%d pages)",
        output_dir, len(nodes)
    )
    return output_dir


def _yaml_scalar(value: object) -> str:
    """Render a frontmatter value as a scalar any YAML parser will accept.

    A bare scalar cannot carry ``": "``, a leading ``#``, a quote or a
    newline, and a legal POSIX file path can contain all of them, so
    anything that is not a number is emitted double-quoted. JSON string
    syntax is a subset of YAML's double-quoted style, so ``json.dumps``
    produces exactly the escaping YAML expects. ``bool`` is tested before
    ``int`` because it is a subclass of ``int``.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    return json.dumps(str(value), ensure_ascii=False)


def _obsidian_slug(name: str) -> str:
    """Convert a name to an Obsidian-friendly filename slug."""
    slug = re.sub(r"[^\w\s-]", "", name.lower())
    slug = re.sub(r"[\s_]+", "-", slug).strip("-")
    return slug[:100] or "unnamed"


# -------------------------------------------------------------------
# SVG export (matplotlib-based)
# -------------------------------------------------------------------

def export_svg(store: GraphStore, output_path: Path) -> Path:
    """Export a static SVG graph visualization.

    Requires matplotlib (optional dependency).
    Returns the path to the written file.

    Raises:
        MissingOptionalDependencyError: If matplotlib is not installed.
            Callers on a user-facing surface should print the message and
            exit non-zero rather than let it become a traceback.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise MissingOptionalDependencyError(
            "SVG export requires matplotlib. "
            'Run: pip install "code-review-graph[eval]"'
        ) from exc

    import networkx as nx

    data = export_graph_data(store)
    nodes_data = data["nodes"]
    edges_data = data["edges"]

    nxg: nx.DiGraph = nx.DiGraph()  # type: ignore[type-arg]
    for n in nodes_data:
        nxg.add_node(
            n["qualified_name"],
            label=n.get("name", ""),
            kind=n.get("kind", ""),
        )
    for e in edges_data:
        if e["source"] in nxg and e["target"] in nxg:
            nxg.add_edge(e["source"], e["target"])

    if nxg.number_of_nodes() == 0:
        raise ValueError("Graph is empty, nothing to export")

    # Color by kind
    kind_colors = {
        "File": "#6c757d",
        "Class": "#0d6efd",
        "Function": "#198754",
        "Type": "#ffc107",
        "Test": "#dc3545",
    }
    colors = [
        kind_colors.get(
            nxg.nodes[n].get("kind", ""), "#adb5bd"
        )
        for n in nxg.nodes()
    ]

    # Node names are code identifiers, never TeX. With matplotlib's default
    # ``text.parse_math`` a name carrying two '$' (legal in JavaScript, PHP,
    # Perl, shell and in file paths) is parsed as mathtext and an unknown
    # symbol aborts the whole export, so every label is drawn literally.
    # ``parse_math`` is read when a Text artist is built *and* when it is
    # rendered, so the context has to cover savefig too.
    with matplotlib.rc_context({"text.parse_math": False}):
        fig, ax = plt.subplots(1, 1, figsize=(16, 12))
        pos = nx.spring_layout(
            nxg, k=2 / (nxg.number_of_nodes() ** 0.5),
            iterations=50, seed=42
        )

        # Limit labels to avoid clutter
        labels = {}
        if nxg.number_of_nodes() <= 100:
            labels = {
                n: nxg.nodes[n].get("label", n.split("::")[-1])
                for n in nxg.nodes()
            }

        nx.draw_networkx_nodes(
            nxg, pos, ax=ax, node_color=colors,
            node_size=30, alpha=0.8
        )
        nx.draw_networkx_edges(
            nxg, pos, ax=ax, alpha=0.2,
            arrows=True, arrowsize=5
        )
        if labels:
            nx.draw_networkx_labels(
                nxg, pos, labels=labels, ax=ax,
                font_size=6
            )

        ax.set_title("Code Review Graph", fontsize=14)
        ax.axis("off")

        fig.savefig(
            str(output_path), format="svg",
            bbox_inches="tight", dpi=150
        )
        plt.close(fig)

    logger.info("SVG exported to %s (%d nodes)",
                output_path, nxg.number_of_nodes())
    return output_path
