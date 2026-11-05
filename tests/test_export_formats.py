"""Conformance checks for everything the tool writes out.

Covers all six ``visualize --format`` values (html, json, graphml, cypher,
obsidian, svg) plus ``wiki``. Every artefact is produced from a real built
graph (a real git repo parsed by the real pipeline) and validated with a
real consumer rather than a substring match:

* GraphML  -> ``xml.etree`` plus ``networkx.read_graphml`` plus the official
  GraphML 1.0 XSD vendored in ``tests/fixtures/graphml_xsd/``.
* Cypher   -> a real openCypher lexer and statement parser in this file
  (``_lex_cypher`` / ``_parse_cypher``) that decodes string literals back to
  Python and round-trips them against the graph payload.
* Obsidian -> a vault walker that resolves every ``[[wikilink]]`` to a file
  and parses every YAML frontmatter block with ``yaml.safe_load``.
* SVG      -> ``xml.etree``.
* HTML     -> ``html.parser``, ``json.loads`` on the embedded payload, and a
  SHA-384 check of the vendored D3 build against the pinned SRI hash.
* Wiki     -> a Markdown table and link walker.

Hostile node names are planted in the corpus so the escaping is exercised
where injection lives, and every export is produced twice and compared byte
for byte.

These are slow (a real build plus a full post-processing pass). Run them
with:

    uv run --python 3.13 --with xmlschema --with matplotlib \
        python -m pytest tests/test_export_formats.py -m exports -q

Tests marked ``xfail(strict=True)`` record a live bug in the exporter. When
one starts passing, the bug is fixed and the marker should be removed.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

import pytest
import yaml

from code_review_graph.exports import (
    _cypher_props,
    export_graphml,
    export_json,
    export_neo4j_cypher,
    export_obsidian_vault,
)
from code_review_graph.graph import GraphStore, _sanitize_name
from code_review_graph.parser import EdgeInfo, NodeInfo
from code_review_graph.visualization import (
    D3_CDN_URL,
    D3_LOCAL_FILENAME,
    D3_SRI_HASH,
    export_graph_data,
    generate_html,
)
from code_review_graph.wiki import _generate_community_page, generate_wiki

pytestmark = pytest.mark.exports


@pytest.fixture(autouse=True, scope="module")
def _opt_in_only(request):
    """Keep these out of the ordinary suite; they need an explicit -m exports.

    A real build plus a full post-processing pass plus fourteen exports is
    far too slow for the default run, and two of the consumers
    (``xmlschema``, ``matplotlib``) are not default dependencies. Skipping
    is reported with the exact command rather than silently passing.
    """
    if "exports" not in (request.config.getoption("markexpr") or ""):
        pytest.skip(
            "opt-in export conformance checks. Run: uv run --python 3.13 "
            "--with xmlschema --with matplotlib python -m pytest "
            "tests/test_export_formats.py -m exports -q"
        )


GRAPHML_NS = "http://graphml.graphdrawing.org/xmlns"
XSD_DIR = Path(__file__).parent / "fixtures" / "graphml_xsd"

_RUN_HINT = (
    "uv run --python 3.13 --with xmlschema --with matplotlib "
    "python -m pytest tests/test_export_formats.py -m exports -q"
)


def _require_xmlschema():
    """Fail loudly rather than skip: a skipped check is an unchecked format."""
    try:
        import xmlschema
    except ImportError:  # pragma: no cover - configuration failure
        pytest.fail(
            "GraphML schema validation needs xmlschema and skipping would "
            f"leave the format unchecked. Run: {_RUN_HINT}"
        )
    return xmlschema

# ---------------------------------------------------------------------------
# Hostile corpus
# ---------------------------------------------------------------------------

# Node names a repository can genuinely contain and that each exporter has to
# survive. Keys are stable ids used by the canary so a test cannot claim to
# have checked a case it never saw.
HOSTILE_NAMES: dict[str, str] = {
    # Closes the inline <script> that carries the HTML payload.
    "script_close": "</script><img src=x onerror=alert(1)>",
    # Template-expression syntax for JS template literals and Jinja/Vue.
    "template_expr": "${alert(1)} and {{7*7}}",
    # Breaks a single-quoted Cypher literal, plus a backslash and a newline.
    "cypher_quote": "it's a \\ backslash\nand a newline",
    # Markdown link with a javascript: target.
    "md_link": "[click me](javascript:alert(1))",
    # ASCII control characters that _sanitize_name is meant to strip.
    "control_chars": "ctrl\x00\x07\x1b\x1fname",
    # Breaks an XML attribute value and opens a sibling element.
    "xml_attr": '"><evil a="b"',
    # Breaks a GitHub-flavoured Markdown table row.
    "md_pipe": "pipe|cell|break",
    # Differs from cypher_quote's tail only by newline-vs-space: XML attribute
    # value normalisation collapses one onto the other.
    "newline_twin": "collide\nme",
    "space_twin": "collide me",
    # U+007F (DEL) is an ordinary character in XML 1.0 -- the Char production
    # admits all of [#x20-#xD7FF] -- and _sanitize_name keeps it, so it is
    # part of a node's identity. These two differ only by it: an exporter
    # that strips it turns them into one node, which is the newline collision
    # above in a smaller form.
    "del_twin": "del\x7ftwin",
    "del_shadow": "deltwin",
}

# A file path containing ": ", which is legal on POSIX and breaks unquoted
# YAML. Kept separate from the names so it exercises the frontmatter writer.
COLON_FILE_PATH = "odd: dir/module.py"

# Node kinds must stay valid Cypher labels and GraphML data, so plant the
# hostile content in names only.
_HOSTILE_KIND = "Function"


@dataclass
class Corpus:
    """One real built graph plus two independent runs of every exporter."""

    repo: Path
    store: GraphStore
    payload: dict
    run_a: Path
    run_b: Path
    planted: dict[str, str] = field(default_factory=dict)

    def qn(self, key: str) -> str:
        return self.planted[key]

    @property
    def names(self) -> set[str]:
        return {n["name"] for n in self.payload["nodes"]}

    @property
    def qns(self) -> set[str]:
        return {n["qualified_name"] for n in self.payload["nodes"]}


# ---------------------------------------------------------------------------
# Canary
# ---------------------------------------------------------------------------

# Every check records what it actually compared. ``_record`` refuses a
# zero-sized comparison, so a check that silently degraded into a no-op fails
# at the point it happens, and ``test_zz_canary_*`` fails if a whole format
# never reported in.
_CANARY: dict[str, dict[str, int]] = {}

_EXPECTED_CANARY_FORMATS = {
    "json", "graphml", "cypher", "obsidian", "svg", "html", "wiki", "cli",
}


def _record(fmt: str, what: str, count: int) -> int:
    """Record that *count* things were compared for *fmt*, refusing zero."""
    assert count > 0, (
        f"canary: {fmt}/{what} compared nothing -- the check ran but had no "
        "material to check, which is worse than no check at all"
    )
    _CANARY.setdefault(fmt, {})[what] = count
    return count


def _require_planted(found: set[str], fmt: str, normalise=None) -> int:
    """Fail unless every hostile name the consumer decoded is present.

    *found* is what the format's own consumer read back out of the artefact,
    not a substring scan, so an exporter that dropped or mangled a name is
    caught rather than papered over.
    """
    normalise = normalise or (lambda s: s)
    missing = [
        key for key, raw in HOSTILE_NAMES.items()
        if normalise(_sanitize_name(raw)) not in found
    ]
    assert not missing, (
        f"{fmt}: planted names absent from the artefact: {missing}. The check "
        "would have passed vacuously."
    )
    return _record(fmt, "planted_names_present", len(HOSTILE_NAMES))


def _require_planted_text(haystack: str, fmt: str, normalise=None) -> int:
    """Same guarantee for artefacts that carry names as literal text.

    *normalise* is applied to each expected name when the format provably
    transforms it (each caller says which transformation and why); the
    transformation itself is then pinned by its own test.
    """
    normalise = normalise or (lambda s: s)
    return _require_planted(
        {
            normalise(name) for name in _sanitised_hostile()
            if normalise(name) in haystack
        },
        fmt,
        normalise=normalise,
    )


def _sanitised_hostile() -> list[str]:
    return [_sanitize_name(raw) for raw in HOSTILE_NAMES.values()]


# ---------------------------------------------------------------------------
# Corpus construction
# ---------------------------------------------------------------------------

_SOURCES = {
    "src/auth.py": (
        "class AuthService:\n"
        "    def login(self, user, password):\n"
        "        return validate(user, password)\n"
        "\n"
        "\n"
        "def validate(user, password):\n"
        "    return bool(user and password)\n"
    ),
    "src/api.py": (
        "from .auth import AuthService\n"
        "\n"
        "\n"
        "def handle(request):\n"
        "    service = AuthService()\n"
        "    return service.login(request.user, request.password)\n"
    ),
    "src/session.py": (
        "from .auth import validate\n"
        "\n"
        "\n"
        "class SessionStore:\n"
        "    def open(self, user, password):\n"
        "        return validate(user, password)\n"
    ),
    "web/util.js": (
        "export function shout(text) { return text.toUpperCase(); }\n"
        "export function greet(name) { return shout('hi ' + name); }\n"
    ),
    "web/app.ts": (
        "import { greet } from './util';\n"
        "export function render(name: string): string { return greet(name); }\n"
    ),
    "tests/test_auth.py": (
        "from src.auth import validate\n"
        "\n"
        "\n"
        "def test_validate():\n"
        "    assert validate('a', 'b')\n"
    ),
}


def _init_repo(root: Path) -> None:
    for rel, body in _SOURCES.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": str(root / ".gitconfig-none"),
        "GIT_CONFIG_SYSTEM": str(root / ".gitconfig-none"),
    }
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, env=env)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, env=env)
    subprocess.run(
        [
            "git", "-c", "user.email=exports@test.invalid",
            "-c", "user.name=exports", "commit", "-qm", "corpus",
        ],
        cwd=root, check=True, env=env,
    )


def _plant_hostile_nodes(store: GraphStore, repo: Path) -> dict[str, str]:
    """Add the hostile-named nodes, wired to real nodes so edges resolve."""
    host_file = str((repo / "src" / "auth.py").resolve())
    planted: dict[str, str] = {}
    line = 100
    for key, raw in HOSTILE_NAMES.items():
        store.upsert_node(NodeInfo(
            kind=_HOSTILE_KIND, name=raw, file_path=host_file,
            line_start=line, line_end=line + 1, language="python",
            parent_name=None, params="user, password", return_type="bool",
            modifiers=None, is_test=False, extra={},
        ))
        planted[key] = f"{host_file}::{_sanitize_name(raw)}"
        line += 2

    # A node whose *file path* breaks unquoted YAML frontmatter.
    store.upsert_node(NodeInfo(
        kind=_HOSTILE_KIND, name="colon_path_function",
        file_path=COLON_FILE_PATH, line_start=1, line_end=2,
        language="python", parent_name=None, params=None, return_type=None,
        modifiers=None, is_test=False, extra={},
    ))
    planted["colon_path"] = f"{COLON_FILE_PATH}::colon_path_function"
    store.commit()

    # Edges in both directions so the hostile nodes appear on real pages.
    validate_qn = f"{host_file}::validate"
    for key, qn in planted.items():
        store.upsert_edge(EdgeInfo(
            kind="CALLS", source=validate_qn, target=qn,
            file_path=host_file, line=6, extra={},
        ))
        store.upsert_edge(EdgeInfo(
            kind="CALLS", source=qn, target=validate_qn,
            file_path=host_file, line=7, extra={},
        ))
        assert key
    store.commit()
    return planted


def _export_all(store: GraphStore, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    export_json(store, out / "graph.json")
    export_graphml(store, out / "graph.graphml")
    export_neo4j_cypher(store, out / "graph.cypher")
    export_obsidian_vault(store, out / "obsidian")
    generate_html(store, out / "graph.html")
    generate_wiki(store, out / "wiki", force=True)


@pytest.fixture(scope="module")
def corpus(tmp_path_factory) -> Corpus:
    """Build one real graph and export every format from it, twice."""
    base = tmp_path_factory.mktemp("export-corpus")
    monkey = pytest.MonkeyPatch()
    try:
        # Same isolation contract as tests/conftest.py, held for the module.
        monkey.setenv("HOME", str(base / "home"))
        monkey.setenv("CRG_HOME", str(base / "crg-home"))
        monkey.setenv("HERMES_HOME", str(base / "hermes-home"))
        (base / "home").mkdir()

        from code_review_graph.incremental import full_build
        from code_review_graph.postprocessing import run_post_processing

        repo = base / "repo"
        repo.mkdir()
        _init_repo(repo)

        store = GraphStore(base / "graph.db")
        full_build(repo, store)
        planted = _plant_hostile_nodes(store, repo)
        run_post_processing(store)

        payload = export_graph_data(store)
        assert len(payload["nodes"]) > len(HOSTILE_NAMES), "corpus is too thin"
        assert payload["edges"], "corpus has no edges"
        assert payload["communities"], "corpus produced no communities"

        run_a = base / "run-a"
        run_b = base / "run-b"
        _export_all(store, run_a)
        _export_all(store, run_b)
        yield Corpus(
            repo=repo, store=store, payload=payload,
            run_a=run_a, run_b=run_b, planted=planted,
        )
    finally:
        monkey.undo()


# ---------------------------------------------------------------------------
# Corpus sanity -- the canary for the fixture itself
# ---------------------------------------------------------------------------

def test_corpus_is_a_real_graph_carrying_every_hostile_name(corpus: Corpus):
    """Nothing below can pass vacuously if this fails."""
    names = corpus.names
    for key, raw in HOSTILE_NAMES.items():
        assert _sanitize_name(raw) in names, f"{key} missing from the graph"
    kinds = {n["kind"] for n in corpus.payload["nodes"]}
    assert {"File", "Function", "Class"} <= kinds, kinds
    languages = {n["language"] for n in corpus.payload["nodes"]}
    assert {"python", "javascript"} <= languages, languages
    hostile_edges = [
        e for e in corpus.payload["edges"]
        if e["target"] == corpus.qn("script_close")
    ]
    assert hostile_edges, "hostile nodes are not connected to the real graph"
    _record("corpus", "nodes", len(corpus.payload["nodes"]))
    _record("corpus", "edges", len(corpus.payload["edges"]))
    _record("corpus", "communities", len(corpus.payload["communities"]))


def test_sanitisation_strips_control_characters_from_every_name(corpus: Corpus):
    """The documented prompt-injection defence, checked on the real payload."""
    offenders = [
        n["qualified_name"] for n in corpus.payload["nodes"]
        if any(ord(c) < 0x20 and c not in "\t\n" for c in n["name"])
    ]
    assert not offenders, offenders
    raw = HOSTILE_NAMES["control_chars"]
    assert _sanitize_name(raw) == "ctrlname"
    assert "ctrlname" in corpus.names
    _record("corpus", "names_sanitised", len(corpus.names))


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------

def test_json_parses_and_round_trips_every_planted_name(corpus: Corpus):
    raw = (corpus.run_a / "graph.json").read_text(encoding="utf-8")
    data = json.loads(raw)
    assert data["nodes"] and data["edges"]
    names = {n["name"] for n in data["nodes"]}
    for key, hostile in HOSTILE_NAMES.items():
        assert _sanitize_name(hostile) in names, key
    assert len(data["nodes"]) == len(corpus.payload["nodes"])
    assert len(data["edges"]) == len(corpus.payload["edges"])
    assert raw.strip().startswith("{")
    _require_planted(names, "json")
    _record("json", "nodes_round_tripped", len(data["nodes"]))


def test_json_is_utf8_with_no_control_characters_in_names(corpus: Corpus):
    blob = (corpus.run_a / "graph.json").read_bytes()
    blob.decode("utf-8")  # raises on anything that is not valid UTF-8
    data = json.loads(blob)
    bad = [
        n["qualified_name"] for n in data["nodes"]
        if any(ord(c) < 0x20 and c not in "\t\n" for c in n["name"])
    ]
    assert not bad, bad
    _record("json", "names_checked", len(data["nodes"]))


# ---------------------------------------------------------------------------
# GraphML
# ---------------------------------------------------------------------------

def _graphml_text(corpus: Corpus) -> str:
    return (corpus.run_a / "graph.graphml").read_text(encoding="utf-8")


def _with_official_namespace(text: str) -> str:
    """Rewrite the declared namespace to the official one.

    A no-op on a correct document, and the guard that keeps it correct: a
    regression to a private namespace would be caught by
    test_graphml_declares_the_official_graphml_namespace rather than
    silently repaired here.
    """
    root = ET.fromstring(text)
    declared = root.tag.split("}")[0].lstrip("{")
    return text.replace(declared, GRAPHML_NS)


def _graphml_node_data(node: ET.Element) -> dict[str, str]:
    """Read one <node>'s <data> children into a plain dict."""
    return {
        d.get("key") or "": (d.text or "")
        for d in node.findall(f"{{{GRAPHML_NS}}}data")
    }


def _graphml_qualified_names(text: str) -> list[str]:
    """Every node's qualified name, as the XML parser reads it back."""
    root = ET.fromstring(text)
    return [
        _graphml_node_data(n).get("qualified_name", "")
        for n in root.iter(f"{{{GRAPHML_NS}}}node")
    ]


def test_graphml_is_well_formed_xml_covering_the_whole_graph(corpus: Corpus):
    root = ET.fromstring(_graphml_text(corpus))
    ns = root.tag.split("}")[0].lstrip("{")
    graph = root.find(f"{{{ns}}}graph")
    assert graph is not None
    nodes = graph.findall(f"{{{ns}}}node")
    edges = graph.findall(f"{{{ns}}}edge")
    assert len(nodes) == len(corpus.payload["nodes"])
    assert len(edges) == len(corpus.payload["edges"])
    assert graph.get("edgedefault") == "directed"
    keys = {k.get("id") for k in root.findall(f"{{{ns}}}key")}
    used = {
        d.get("key") for d in root.iter(f"{{{ns}}}data")
    }
    assert used <= keys, f"undeclared <data key=>: {used - keys}"
    _record("graphml", "elements", len(nodes) + len(edges))


def test_graphml_declares_the_official_graphml_namespace(corpus: Corpus):
    root = ET.fromstring(_graphml_text(corpus))
    assert root.tag == f"{{{GRAPHML_NS}}}graphml"


def test_graphml_is_readable_by_networkx(corpus: Corpus):
    import networkx as nx

    graph = nx.read_graphml(str(corpus.run_a / "graph.graphml"))
    assert graph.number_of_nodes() == len(corpus.payload["nodes"])


def test_graphml_body_carries_its_data_keys_into_networkx(
    corpus: Corpus, tmp_path
):
    """Not just parseable: the payload survives a real GraphML reader."""
    import networkx as nx

    fixed = tmp_path / "ns-fixed.graphml"
    fixed.write_text(
        _with_official_namespace(_graphml_text(corpus)), encoding="utf-8"
    )
    graph = nx.read_graphml(str(fixed))
    assert graph.number_of_nodes() == len(corpus.payload["nodes"])
    assert graph.number_of_edges() > 0
    kinds = {d.get("kind") for _, d in graph.nodes(data=True)}
    assert "Function" in kinds and "File" in kinds
    qns = {d.get("qualified_name") for _, d in graph.nodes(data=True)}
    assert corpus.qn("newline_twin") in qns
    _record("graphml", "networkx_nodes", graph.number_of_nodes())


def test_graphml_schema_location_is_a_namespace_location_pair(corpus: Corpus):
    text = _graphml_text(corpus)
    match = re.search(r'xsi:schemaLocation="([^"]*)"', text)
    assert match, "no xsi:schemaLocation declared"
    tokens = match.group(1).split()
    assert len(tokens) == 2 and len(tokens) % 2 == 0, tokens
    assert tokens[0] == GRAPHML_NS


def test_graphml_validates_against_the_official_xsd(corpus: Corpus):
    xmlschema = _require_xmlschema()
    schema = xmlschema.XMLSchema(str(XSD_DIR / "graphml.xsd"))
    schema.validate(_with_official_namespace(_graphml_text(corpus)))


def test_graphml_xsd_fixture_really_rejects_a_bad_document():
    """Teeth for the XSD check: the vendored schema is not a rubber stamp."""
    xmlschema = _require_xmlschema()
    schema = xmlschema.XMLSchema(str(XSD_DIR / "graphml.xsd"))
    good = (
        f'<graphml xmlns="{GRAPHML_NS}">'
        '<graph id="g" edgedefault="directed"><node id="n0"/></graph>'
        "</graphml>"
    )
    schema.validate(good)
    bad = (
        f'<graphml xmlns="{GRAPHML_NS}">'
        '<graph id="g" edgedefault="sideways"><node id="n0"/></graph>'
        "</graphml>"
    )
    with pytest.raises(Exception):
        schema.validate(bad)
    _record("graphml", "xsd_fixture_selfcheck", 2)


def test_graphml_ids_are_unique_and_edges_reference_declared_nodes(
    corpus: Corpus,
):
    root = ET.fromstring(_with_official_namespace(_graphml_text(corpus)))
    graph = root.find(f"{{{GRAPHML_NS}}}graph")
    ids = [n.get("id") for n in graph.findall(f"{{{GRAPHML_NS}}}node")]
    declared = set(ids)
    dangling = [
        (e.get("source"), e.get("target"))
        for e in graph.findall(f"{{{GRAPHML_NS}}}edge")
        if e.get("source") not in declared or e.get("target") not in declared
    ]
    assert not dangling, f"edges point at undeclared nodes: {dangling[:3]}"
    _record("graphml", "edges_resolved", len(ids))


def test_graphml_keeps_newline_bearing_names_distinct(corpus: Corpus):
    """A newline in a name must not merge two nodes into one.

    GraphML ids are NMTOKENs, so the qualified name cannot be the id; it
    travels in a <data> element, whose content - unlike an attribute value -
    is not whitespace-normalised, and the newline survives.
    """
    root = ET.fromstring(_graphml_text(corpus))
    graph = root.find(f"{{{GRAPHML_NS}}}graph")
    nodes = graph.findall(f"{{{GRAPHML_NS}}}node")
    ids = [n.get("id") for n in nodes]
    assert len(ids) == len(set(ids)), "duplicate node ids after XML parsing"
    qns = [_graphml_node_data(n).get("qualified_name") for n in nodes]
    assert len(qns) == len(set(qns)), "two names collapsed onto one node"
    assert corpus.qn("newline_twin") in set(qns)
    assert corpus.qn("space_twin") in set(qns)


def test_graphml_newline_and_space_twins_stay_two_nodes(corpus: Corpus):
    """Teeth for the check above: the collision it guards is reachable.

    'collide\\nme' and 'collide me' differ only by newline-versus-space, so
    they are exactly the pair XML attribute-value normalisation would fold
    together. Both must appear, once each, and carry the raw newline.
    """
    text = _graphml_text(corpus)
    qns = _graphml_qualified_names(text)
    newline_twin = corpus.qn("newline_twin")
    space_twin = corpus.qn("space_twin")
    assert "\n" in newline_twin, "the fixture no longer plants a newline"
    assert newline_twin.replace("\n", " ") == space_twin, "twins drifted apart"
    assert qns.count(newline_twin) == 1, qns.count(newline_twin)
    assert qns.count(space_twin) == 1, qns.count(space_twin)
    # The newline reaches the file as element content, never as an attribute
    # value, which is what makes it survive the round trip.
    assert f'id="{newline_twin}"' not in text
    _record("graphml", "twins_kept_apart", 2)


def test_graphml_keeps_del_bearing_names_distinct(corpus: Corpus):
    """U+007F is a legal XML 1.0 character and part of a node's identity.

    XML 1.0's Char production admits the whole of [#x20-#xD7FF], so DEL
    needs no escaping and no removal, and ``_sanitize_name`` leaves it in a
    node name. Dropping it on the way out rewrites the identity the export
    carries: 'del\\x7ftwin' and 'deltwin' come back as one name.
    """
    text = _graphml_text(corpus)
    qns = _graphml_qualified_names(text)
    twin = corpus.qn("del_twin")
    shadow = corpus.qn("del_shadow")
    assert "\x7f" in twin, "the fixture no longer plants a DEL"
    assert twin.replace("\x7f", "") == shadow, "twins drifted apart"
    assert qns.count(twin) == 1, f"DEL was stripped from the identity: {twin!r}"
    assert qns.count(shadow) == 1, qns.count(shadow)
    assert len(qns) == len(set(qns)), "two names collapsed onto one node"
    _record("graphml", "del_twins_kept_apart", 2)


def test_graphml_del_survives_a_real_graphml_reader(corpus: Corpus, tmp_path):
    """Teeth for the check above: a reader, not just the raw text.

    Writing the DEL out is only half of it -- an XML parser has to hand it
    back. It does: unlike a carriage return, DEL is not touched by
    line-end normalisation, and unlike a newline in an attribute value it is
    not whitespace-normalised in element content.
    """
    import networkx as nx

    fixed = tmp_path / "ns-fixed-del.graphml"
    fixed.write_text(
        _with_official_namespace(_graphml_text(corpus)), encoding="utf-8"
    )
    graph = nx.read_graphml(str(fixed))
    qns = {d.get("qualified_name") for _, d in graph.nodes(data=True)}
    assert corpus.qn("del_twin") in qns
    assert corpus.qn("del_shadow") in qns
    _record("graphml", "del_round_tripped", 2)


def test_graphml_escapes_hostile_names(corpus: Corpus):
    text = _graphml_text(corpus)
    root = ET.fromstring(text)
    # The XML parser is the consumer: names are read back out of the parsed
    # tree, so escaping that merely hid a name would fail here. No
    # transformation is allowed -- the identity round-trips byte for byte.
    parsed_qns = set(_graphml_qualified_names(text))
    parsed_names = {
        qn.split("::", 1)[-1] for qn in parsed_qns if "::" in qn
    } | parsed_qns
    _require_planted(parsed_names, "graphml")
    tags = {el.tag.split("}")[-1] for el in root.iter()}
    assert tags <= {"graphml", "key", "graph", "node", "edge", "data"}, tags
    assert "evil" not in tags and "img" not in tags and "script" not in tags
    # The markup characters survive as text, not as markup.
    assert corpus.qn("script_close") in parsed_qns
    assert corpus.qn("xml_attr") in parsed_qns
    assert "\x00" not in text and "\x1b" not in text
    _record("graphml", "hostile_ids_checked", len(HOSTILE_NAMES))


# ---------------------------------------------------------------------------
# Cypher: a real lexer and statement parser
# ---------------------------------------------------------------------------

class CypherSyntaxError(Exception):
    """Raised when the generated Cypher would not lex or parse."""


# openCypher / Neo4j escape sequences inside a quoted string literal.
_CYPHER_ESCAPES = {
    "t": "\t", "b": "\b", "n": "\n", "r": "\r", "f": "\f",
    "'": "'", '"': '"', "\\": "\\",
}

_CYPHER_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


@dataclass(frozen=True)
class _Tok:
    kind: str   # "str" | "num" | "word" | "punct" | "ident"
    value: object
    pos: int


def _lex_cypher(text: str) -> list[_Tok]:
    """Tokenise Cypher, decoding string literals back to Python strings.

    Follows the openCypher lexical rules: ``//`` line comments, ``/* */``
    block comments, single- and double-quoted string literals with
    backslash escapes, and backtick-quoted identifiers. Raises
    ``CypherSyntaxError`` on anything a Cypher parser would reject.
    """
    toks: list[_Tok] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in " \t\r\n":
            i += 1
            continue
        if text.startswith("//", i):
            i = text.find("\n", i)
            if i == -1:
                break
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end == -1:
                raise CypherSyntaxError(f"unterminated block comment at {i}")
            i = end + 2
            continue
        if ch in "'\"":
            start = i
            i += 1
            buf: list[str] = []
            while True:
                if i >= n:
                    raise CypherSyntaxError(
                        f"unterminated string literal opened at offset {start}"
                    )
                c = text[i]
                if c == "\\":
                    if i + 1 >= n:
                        raise CypherSyntaxError("trailing backslash")
                    nxt = text[i + 1]
                    if nxt in _CYPHER_ESCAPES:
                        buf.append(_CYPHER_ESCAPES[nxt])
                        i += 2
                        continue
                    if nxt == "u":
                        buf.append(chr(int(text[i + 2:i + 6], 16)))
                        i += 6
                        continue
                    if nxt == "U":
                        buf.append(chr(int(text[i + 2:i + 10], 16)))
                        i += 10
                        continue
                    raise CypherSyntaxError(f"unknown escape \\{nxt} at {i}")
                if c == ch:
                    i += 1
                    break
                buf.append(c)
                i += 1
            toks.append(_Tok("str", "".join(buf), start))
            continue
        if ch == "`":
            end = text.find("`", i + 1)
            if end == -1:
                raise CypherSyntaxError(f"unterminated quoted identifier {i}")
            toks.append(_Tok("ident", text[i + 1:end], i))
            i = end + 1
            continue
        if ch.isdigit() or (ch == "-" and i + 1 < n and text[i + 1].isdigit()
                            and toks and toks[-1].value in ("{", ",", ":")):
            match = re.match(r"-?\d+(\.\d+)?([eE][-+]?\d+)?", text[i:])
            if not match:
                raise CypherSyntaxError(f"bad number at {i}")
            raw = match.group(0)
            toks.append(_Tok("num", float(raw) if "." in raw else int(raw), i))
            i += len(raw)
            continue
        if ch.isalpha() or ch == "_":
            match = re.match(r"[A-Za-z_][A-Za-z0-9_]*", text[i:])
            toks.append(_Tok("word", match.group(0), i))
            i += len(match.group(0))
            continue
        if ch in "(){}[],:;=<>-.$|!*+/%^":
            toks.append(_Tok("punct", ch, i))
            i += 1
            continue
        raise CypherSyntaxError(f"unexpected character {ch!r} at offset {i}")
    return toks


def _split_statements(toks: list[_Tok]) -> list[list[_Tok]]:
    out: list[list[_Tok]] = []
    cur: list[_Tok] = []
    for tok in toks:
        if tok.kind == "punct" and tok.value == ";":
            if cur:
                out.append(cur)
            cur = []
            continue
        cur.append(tok)
    if cur:
        raise CypherSyntaxError("trailing statement without a ';' terminator")
    return out


@dataclass
class _CypherNode:
    label: str | None
    props: dict


@dataclass
class _CypherRel:
    rel_type: str
    source_props: dict
    target_props: dict


def _parse_props(toks: list[_Tok], i: int) -> tuple[dict, int]:
    """Parse ``{ key: value, ... }`` starting at the opening brace."""
    assert toks[i].value == "{"
    i += 1
    props: dict = {}
    if toks[i].value == "}":
        return props, i + 1
    while True:
        key_tok = toks[i]
        if key_tok.kind not in ("word", "ident"):
            raise CypherSyntaxError(f"bad property key {key_tok}")
        if toks[i + 1].value != ":":
            raise CypherSyntaxError(f"missing ':' after {key_tok.value}")
        val = toks[i + 2]
        if val.kind in ("str", "num"):
            value: object = val.value
        elif val.kind == "word" and str(val.value).lower() in (
            "true", "false", "null"
        ):
            lowered = str(val.value).lower()
            value = None if lowered == "null" else lowered == "true"
        else:
            raise CypherSyntaxError(
                f"property {key_tok.value} has a non-literal value "
                f"{val.value!r}"
            )
        props[str(key_tok.value)] = value
        i += 3
        if toks[i].value == ",":
            i += 1
            continue
        if toks[i].value == "}":
            return props, i + 1
        raise CypherSyntaxError(f"expected ',' or '}}', got {toks[i].value!r}")


def _parse_cypher(text: str) -> tuple[list[_CypherNode], list[_CypherRel]]:
    """Parse the exporter's Cypher into nodes and relationships.

    Understands exactly the two statement shapes ``exports.py`` emits, and
    raises on anything else, so a malformed statement cannot slip past.
    """
    nodes: list[_CypherNode] = []
    rels: list[_CypherRel] = []
    for stmt in _split_statements(_lex_cypher(text)):
        head = str(stmt[0].value).upper()
        if head == "CREATE":
            # CREATE (:Label {props})
            if stmt[1].value != "(" or stmt[2].value != ":":
                raise CypherSyntaxError(f"bad CREATE at offset {stmt[0].pos}")
            label_tok = stmt[3]
            if label_tok.kind == "word" and not _CYPHER_IDENT.match(
                str(label_tok.value)
            ):
                raise CypherSyntaxError(f"bad label {label_tok.value!r}")
            props, i = _parse_props(stmt, 4)
            if i != len(stmt) - 1 or stmt[i].value != ")":
                raise CypherSyntaxError("CREATE has trailing tokens")
            nodes.append(_CypherNode(str(label_tok.value), props))
            continue
        if head == "MATCH":
            # MATCH (a {p}), (b {p}) CREATE (a)-[:TYPE]->(b)
            if stmt[1].value != "(" or stmt[2].kind != "word":
                raise CypherSyntaxError("bad MATCH pattern")
            src, i = _parse_props(stmt, 3)
            if stmt[i].value != ")" or stmt[i + 1].value != ",":
                raise CypherSyntaxError("bad MATCH separator")
            i += 2
            if stmt[i].value != "(" or stmt[i + 1].kind != "word":
                raise CypherSyntaxError("bad second MATCH pattern")
            tgt, i = _parse_props(stmt, i + 2)
            if stmt[i].value != ")":
                raise CypherSyntaxError("unclosed second MATCH pattern")
            i += 1
            if str(stmt[i].value).upper() != "CREATE":
                raise CypherSyntaxError("MATCH without CREATE")
            shape = "".join(str(t.value) for t in stmt[i + 1:])
            match = re.fullmatch(r"\(\w+\)-\[:(\w+)\]->\(\w+\)", shape)
            if not match:
                raise CypherSyntaxError(f"bad relationship pattern {shape!r}")
            rels.append(_CypherRel(match.group(1), src, tgt))
            continue
        raise CypherSyntaxError(f"unknown statement head {head!r}")
    return nodes, rels


def _cypher_text(corpus: Corpus) -> str:
    return (corpus.run_a / "graph.cypher").read_text(encoding="utf-8")


def test_cypher_lexer_rejects_what_neo4j_would_reject():
    """Teeth for the Cypher consumer: it is not a rubber stamp."""
    with pytest.raises(CypherSyntaxError):
        _lex_cypher("CREATE (:X {name: 'unterminated});")
    with pytest.raises(CypherSyntaxError):
        _parse_cypher("CREATE (:X {name: 'a'})")          # missing ';'
    with pytest.raises(CypherSyntaxError):
        _parse_cypher("CREATE (:X {name: 'a'}) extra;")
    with pytest.raises(CypherSyntaxError):
        _parse_cypher("MERGE (:X {name: 'a'});")
    with pytest.raises(CypherSyntaxError):
        _parse_cypher("CREATE (:X {name: oops});")
    assert _lex_cypher(r"'a\'b'")[0].value == "a'b"
    _record("cypher", "lexer_selfcheck", 6)


def test_cypher_parses_and_covers_the_whole_graph(corpus: Corpus):
    text = _cypher_text(corpus)
    nodes, rels = _parse_cypher(text)
    _require_planted({str(n.props["name"]) for n in nodes}, "cypher")
    assert len(nodes) == len(corpus.payload["nodes"])
    assert len(rels) == len(corpus.payload["edges"])
    _record("cypher", "statements_parsed", len(nodes) + len(rels))


def test_cypher_string_literals_round_trip_every_hostile_name(corpus: Corpus):
    nodes, _ = _parse_cypher(_cypher_text(corpus))
    by_qn = {n.props["qualified_name"]: n for n in nodes}
    for key, raw in HOSTILE_NAMES.items():
        qn = corpus.qn(key)
        assert qn in by_qn, f"{key}: {qn!r} missing from the Cypher"
        assert by_qn[qn].props["name"] == _sanitize_name(raw), key
    assert by_qn[corpus.qn("cypher_quote")].props["name"].count("'") == 1
    assert "\\" in by_qn[corpus.qn("cypher_quote")].props["name"]
    _record("cypher", "names_round_tripped", len(HOSTILE_NAMES))


def test_cypher_labels_and_relationship_types_are_valid_identifiers(
    corpus: Corpus,
):
    nodes, rels = _parse_cypher(_cypher_text(corpus))
    for node in nodes:
        assert _CYPHER_IDENT.match(node.label or ""), node.label
    for rel in rels:
        assert _CYPHER_IDENT.match(rel.rel_type), rel.rel_type
    _record("cypher", "identifiers_checked", len(nodes) + len(rels))


def test_cypher_relationships_reference_created_nodes(corpus: Corpus):
    """An import that matched nothing would create zero relationships."""
    nodes, rels = _parse_cypher(_cypher_text(corpus))
    created = {n.props["qualified_name"] for n in nodes}
    dangling = [
        (r.source_props["qualified_name"], r.target_props["qualified_name"])
        for r in rels
        if r.source_props["qualified_name"] not in created
        or r.target_props["qualified_name"] not in created
    ]
    assert not dangling, f"MATCH would find nothing for {dangling[:3]}"
    _record("cypher", "relationships_resolved", len(rels))


def test_cypher_props_emits_cypher_boolean_literals():
    out = _cypher_props({"is_test": True, "is_stub": False})
    assert out == "{is_test: true, is_stub: false}", out


def test_cypher_boolean_branch_is_reachable_and_wins_over_int():
    """Teeth for the check above: bool must be tested before int.

    ``bool`` is a subclass of ``int``, so an ``isinstance(v, (int, float))``
    branch placed first makes the boolean branch dead code and emits
    Python's ``True``/``False``. Real integers must keep their own form.
    """
    assert _cypher_props({"flag": True}) == "{flag: true}"
    assert _cypher_props({"flag": False}) == "{flag: false}"
    assert _cypher_props({"n": 1, "zero": 0}) == "{n: 1, zero: 0}"
    nodes, _ = _parse_cypher(
        "CREATE (:X " + _cypher_props({"flag": True, "n": 3}) + ");"
    )
    assert nodes[0].props == {"flag": True, "n": 3}
    _record("cypher", "boolean_branch_pinned", 3)


@pytest.mark.xfail(
    strict=True,
    reason="BUG: exports._cypher_escape escapes only backslash and quote, so "
           "a newline or tab in a name is written raw into the literal and "
           "one statement spills across lines",
)
def test_cypher_escapes_control_whitespace_in_literals():
    from code_review_graph.exports import _cypher_escape

    assert _cypher_escape("a\nb") == "a\\nb"
    assert _cypher_escape("a\tb") == "a\\tb"


def test_cypher_newline_in_a_name_spills_a_statement_across_lines(
    corpus: Corpus,
):
    """Teeth for the xfail above: show the raw newline really spills.

    The generated file is laid out one statement per line and the header
    tells the reader to paste it into Neo4j Browser. A name carrying a
    newline breaks that layout even though the literal still round-trips.
    """
    lines = _cypher_text(corpus).splitlines()
    spilled = [
        ln for ln in lines
        if ln and not ln.startswith(("//", "CREATE", "MATCH"))
    ]
    assert spilled, "expected at least one statement to spill across lines"
    assert any("and a newline" in ln for ln in spilled), spilled[:3]
    _record("cypher", "spilled_lines_found", len(spilled))


# ---------------------------------------------------------------------------
# Obsidian vault
# ---------------------------------------------------------------------------

_WIKILINK = re.compile(r"\[\[([^\]|\n]+?)(?:\|[^\]\n]*)?\]\]")


def _vault(corpus: Corpus) -> Path:
    return corpus.run_a / "obsidian"


def test_obsidian_every_wikilink_resolves_to_a_file_that_exists(
    corpus: Corpus,
):
    vault = _vault(corpus)
    pages = sorted(vault.glob("*.md"))
    assert pages, "vault is empty"
    checked = 0
    broken: list[tuple[str, str]] = []
    for page in pages:
        for match in _WIKILINK.finditer(page.read_text(encoding="utf-8")):
            checked += 1
            if not (vault / f"{match.group(1)}.md").is_file():
                broken.append((page.name, match.group(1)))
    assert not broken, f"dangling wikilinks: {broken[:5]}"
    _record("obsidian", "wikilinks_resolved", checked)


def test_obsidian_wikilink_walker_would_catch_a_dangling_link(tmp_path):
    """Teeth for the walker: it detects a link that points nowhere."""
    (tmp_path / "a.md").write_text("[[b]] and [[missing|Gone]]\n")
    (tmp_path / "b.md").write_text("ok\n")
    found = [
        m.group(1) for m in _WIKILINK.finditer(
            (tmp_path / "a.md").read_text()
        )
    ]
    assert found == ["b", "missing"]
    assert not (tmp_path / "missing.md").exists()
    _record("obsidian", "walker_selfcheck", 2)


def test_obsidian_no_two_nodes_collide_onto_one_filename(corpus: Corpus):
    vault = _vault(corpus)
    node_pages = [p for p in vault.glob("*.md") if not p.name.startswith("_")]
    assert len(node_pages) == len(corpus.payload["nodes"]), (
        f"{len(corpus.payload['nodes'])} nodes produced "
        f"{len(node_pages)} pages -- names collided onto one file"
    )
    lowered = [p.name.lower() for p in node_pages]
    assert len(set(lowered)) == len(lowered), "case-insensitive collision"
    _record("obsidian", "pages_without_collision", len(node_pages))


def test_obsidian_collision_handling_survives_names_that_slugify_alike(
    corpus: Corpus, tmp_path
):
    """Deliberately alike names must still get one file each."""
    store = GraphStore(tmp_path / "collide.db")
    alike = ["Report", "report", "RE_PORT", "re port", "report!", "report-1"]
    for index, name in enumerate(alike):
        store.upsert_node(NodeInfo(
            kind="Function", name=name, file_path=f"m{index}.py",
            line_start=1, line_end=2, language="python", parent_name=None,
            params=None, return_type=None, modifiers=None, is_test=False,
            extra={},
        ))
    store.commit()
    vault = tmp_path / "vault"
    export_obsidian_vault(store, vault)
    pages = [p for p in vault.glob("*.md") if not p.name.startswith("_")]
    assert len(pages) == len(alike), [p.name for p in pages]
    _record("obsidian", "alike_names_kept_apart", len(alike))


def _frontmatter(text: str) -> str | None:
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---", 4)
    return None if end == -1 else text[4:end]


def test_obsidian_frontmatter_is_valid_yaml(corpus: Corpus):
    bad: list[tuple[str, str]] = []
    checked = 0
    for page in _vault(corpus).glob("*.md"):
        if page.name.startswith("_"):
            continue
        block = _frontmatter(page.read_text(encoding="utf-8"))
        assert block is not None, f"{page.name} has no frontmatter"
        checked += 1
        try:
            parsed = yaml.safe_load(block)
        except yaml.YAMLError as exc:
            bad.append((page.name, type(exc).__name__))
            continue
        assert isinstance(parsed, dict), page.name
    assert checked > 0
    assert not bad, bad


def test_obsidian_colon_path_round_trips_through_yaml(corpus: Corpus):
    """Teeth for the check above: name the page that used to fail, and why.

    ``odd: dir/module.py`` is a legal POSIX path and a bare YAML scalar
    cannot carry the ``": "``. The value must come back out of a real YAML
    parser byte for byte, not merely parse into something.
    """
    page = _vault(corpus) / "colon-path-function.md"
    assert page.is_file(), sorted(p.name for p in _vault(corpus).glob("*.md"))
    text = page.read_text(encoding="utf-8")
    assert f"file: {COLON_FILE_PATH}" not in text, "still a bare scalar"
    parsed = yaml.safe_load(_frontmatter(text) or "")
    assert parsed["file"] == COLON_FILE_PATH, parsed
    _record("obsidian", "colon_path_round_tripped", 1)


def test_obsidian_hostile_names_stay_inert(corpus: Corpus):
    vault = _vault(corpus)
    joined = "\n".join(
        p.read_text(encoding="utf-8") for p in sorted(vault.glob("*.md"))
    )
    _require_planted_text(joined, "obsidian")
    assert "\x00" not in joined and "\x1b" not in joined
    # Wikilink targets are slugs, never raw names, so nothing inside [[...]]
    # can carry markup or a pipe that changes the link's meaning.
    targets = {m.group(1) for m in _WIKILINK.finditer(joined)}
    assert targets, "no wikilinks at all"
    for target in targets:
        assert re.fullmatch(r"[a-z0-9-]+", target), target
    _record("obsidian", "link_targets_are_slugs", len(targets))


def test_obsidian_community_pages_reflect_the_graph(corpus: Corpus):
    vault = _vault(corpus)
    community_pages = sorted(vault.glob("_COMMUNITY_*.md"))
    assert len(community_pages) == len(corpus.payload["communities"])
    members_total = 0
    for page in community_pages:
        text = page.read_text(encoding="utf-8")
        assert text.startswith("# Community: ")
        members = _WIKILINK.findall(text)
        members_total += len(members)
        for slug in members:
            assert (vault / f"{slug}.md").is_file(), slug
    assert members_total > 0
    index = (vault / "_INDEX.md").read_text(encoding="utf-8")
    assert f"**Nodes:** {len(corpus.payload['nodes'])}" in index
    assert f"**Edges:** {len(corpus.payload['edges'])}" in index
    _record("obsidian", "community_members_linked", members_total)


# ---------------------------------------------------------------------------
# SVG
# ---------------------------------------------------------------------------

def _require_matplotlib():
    try:
        import matplotlib  # noqa: F401
    except ImportError:  # pragma: no cover - configuration failure
        pytest.fail(
            "SVG export needs matplotlib and skipping would leave the "
            f"format unchecked. Run: {_RUN_HINT}"
        )


@pytest.fixture(scope="module")
def svg_pair(corpus: Corpus, tmp_path_factory) -> tuple[Path, Path]:
    """Two independent SVG exports of the same graph."""
    _require_matplotlib()
    from code_review_graph.exports import export_svg

    out = tmp_path_factory.mktemp("svg")
    first, second = out / "a.svg", out / "b.svg"
    export_svg(corpus.store, first)
    export_svg(corpus.store, second)
    return first, second


def test_svg_parses_as_xml_and_is_an_svg_document(svg_pair):
    first, _ = svg_pair
    root = ET.parse(first).getroot()
    assert root.tag == "{http://www.w3.org/2000/svg}svg"
    paths = [e for e in root.iter() if e.tag.endswith("}path")]
    assert paths, "SVG has no drawn geometry"
    _record("svg", "path_elements", len(paths))


def test_svg_escapes_hostile_names(svg_pair, corpus: Corpus):
    first, _ = svg_pair
    text = first.read_text(encoding="utf-8")
    # matplotlib writes each label into an XML comment, escaped. Unescape via
    # the XML rules before looking for the planted names, so this checks the
    # decoded content rather than the raw bytes.
    decoded = (
        text.replace("&lt;", "<").replace("&gt;", ">")
        .replace("&quot;", '"').replace("&#39;", "'").replace("&amp;", "&")
    )
    # matplotlib renders a label up to its first newline, so that is the one
    # transformation the names undergo here; it is pinned by
    # test_svg_truncates_a_label_at_its_first_newline below.
    _require_planted_text(
        decoded, "svg", normalise=lambda s: s.split("\n", 1)[0]
    )
    root = ET.parse(first).getroot()
    tags = {e.tag.split("}")[-1] for e in root.iter()}
    assert "img" not in tags and "script" not in tags and "evil" not in tags
    assert "</script><img" not in text, "raw markup escaped into the document"
    assert "&lt;/script&gt;" in text, "the name is present, escaped"
    assert "\x00" not in text and "\x1b" not in text
    _record("svg", "hostile_names_escaped", len(HOSTILE_NAMES))


def test_svg_truncates_a_label_at_its_first_newline(svg_pair, corpus: Corpus):
    """Pin the one transformation the planted-name check allows for SVG.

    matplotlib renders only the first line of a label, so a name carrying a
    newline is drawn short. Cosmetic for a picture, but it is a real
    difference from the graph and the check above must not paper over it.
    """
    text = svg_pair[0].read_text(encoding="utf-8")
    assert "<!-- collide -->" in text, "expected the label to stop at \\n"
    assert "<!-- collide me -->" in text, "the space twin is drawn in full"
    _record("svg", "label_truncation_pinned", 2)


def test_svg_survives_a_name_that_looks_like_mathtext(corpus: Corpus, tmp_path):
    _require_matplotlib()
    from code_review_graph.exports import export_svg

    store = GraphStore(tmp_path / "math.db")
    for index, name in enumerate([r"$\qqq$", "plain_function"]):
        store.upsert_node(NodeInfo(
            kind="Function", name=name, file_path=f"m{index}.js",
            line_start=1, line_end=2, language="javascript",
            parent_name=None, params=None, return_type=None, modifiers=None,
            is_test=False, extra={},
        ))
    store.commit()
    export_svg(store, tmp_path / "math.svg")


def test_svg_draws_a_mathtext_shaped_name_literally(
    corpus: Corpus, tmp_path
):
    """Teeth for the check above: the label is drawn, not silently dropped.

    Surviving the export is not enough -- a name matplotlib refused to
    typeset must still reach the picture as ordinary text.
    """
    _require_matplotlib()
    from code_review_graph.exports import export_svg

    store = GraphStore(tmp_path / "math2.db")
    store.upsert_node(NodeInfo(
        kind="Function", name=r"$\qqq$", file_path="m.js", line_start=1,
        line_end=2, language="javascript", parent_name=None, params=None,
        return_type=None, modifiers=None, is_test=False, extra={},
    ))
    store.commit()
    out = tmp_path / "math2.svg"
    export_svg(store, out)
    text = out.read_text(encoding="utf-8")
    assert r"<!-- $\qqq$ -->" in text, "the label never reached the picture"
    ET.fromstring(text)
    _record("svg", "mathtext_label_drawn", 1)


def _normalise_svg(text: str, *, clip_ids: bool) -> str:
    """Strip the parts of a matplotlib SVG that are not graph content."""
    # Named exception 1: the render timestamp matplotlib stamps into metadata.
    out = re.sub(r"<dc:date>[^<]*</dc:date>", "<dc:date>X</dc:date>", text)
    if clip_ids:
        # Named exception 2: matplotlib's per-process random id salt, which
        # ends every generated clip-path and glyph id with 10 hex digits.
        out = re.sub(r"\bp[0-9a-f]{10}\b", "pCLIP", out)
        out = re.sub(r"_[0-9a-f]{10}\b", "_SALT", out)
    return out


@pytest.mark.xfail(
    strict=True,
    reason="BUG: export_svg sets neither matplotlib's svg.hashsalt nor a "
           "fixed SOURCE_DATE_EPOCH, so every run emits fresh random "
           "clip-path ids and two exports of one graph differ",
)
def test_svg_is_deterministic_apart_from_its_timestamp(svg_pair):
    first, second = svg_pair
    a = _normalise_svg(first.read_text(encoding="utf-8"), clip_ids=False)
    b = _normalise_svg(second.read_text(encoding="utf-8"), clip_ids=False)
    assert a == b


def test_svg_content_is_stable_once_timestamp_and_clip_salt_are_removed(
    svg_pair,
):
    """Teeth for the xfail above: the geometry itself is reproducible."""
    first, second = svg_pair
    a = _normalise_svg(first.read_text(encoding="utf-8"), clip_ids=True)
    b = _normalise_svg(second.read_text(encoding="utf-8"), clip_ids=True)
    assert a == b, "SVG differs by more than its timestamp and clip salt"
    assert a != first.read_text(encoding="utf-8"), "normalisation did nothing"
    _record("svg", "normalised_bytes_compared", len(a))


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

class _PageReader(HTMLParser):
    """Collect the element tree shape and every inline script body."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.inline: list[str] = []
        self._chunks: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        attrdict = dict(attrs)
        self.tags.append((tag, attrdict))
        if tag == "script" and "src" not in attrdict:
            self._chunks = []

    def handle_data(self, data):
        if self._chunks is not None:
            self._chunks.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self._chunks is not None:
            self.inline.append("".join(self._chunks))
            self._chunks = None


def _read_page(path: Path) -> _PageReader:
    reader = _PageReader()
    reader.feed(path.read_text(encoding="utf-8"))
    reader.close()
    return reader


def _embedded_graph(reader: _PageReader) -> dict:
    for body in reader.inline:
        match = re.search(r"var graphData = (.*?);\n", body, re.S)
        if match:
            # generate_html escapes "</" as "<\/" so the payload cannot close
            # the script element; undo exactly that to get back valid JSON.
            return json.loads(match.group(1).replace("<\\/", "</"))
    raise AssertionError("no embedded graphData found in the page")


@pytest.mark.parametrize("mode", ["full", "community", "file"])
def test_html_embedded_json_parses_for_every_render_mode(
    corpus: Corpus, tmp_path, mode
):
    page = tmp_path / f"graph-{mode}.html"
    generate_html(corpus.store, page, mode=mode)
    reader = _read_page(page)
    data = _embedded_graph(reader)
    assert data["nodes"], mode
    assert isinstance(data["nodes"], list)
    if mode == "full":
        assert len(data["nodes"]) == len(corpus.payload["nodes"])
        assert len(data["edges"]) == len(corpus.payload["edges"])
    _record("html", f"json_nodes_{mode}", len(data["nodes"]))


def test_html_ships_the_vendored_d3_with_its_integrity_hash(
    corpus: Corpus,
):
    page = corpus.run_a / "graph.html"
    reader = _read_page(page)
    external = [
        (a.get("src"), a.get("integrity"))
        for tag, a in reader.tags if tag == "script" and a.get("src")
    ]
    assert (D3_LOCAL_FILENAME, D3_SRI_HASH) in external, external
    asset = corpus.run_a / D3_LOCAL_FILENAME
    assert asset.is_file(), "the vendored D3 build was not written"
    digest = base64.b64encode(hashlib.sha384(asset.read_bytes()).digest())
    assert f"sha384-{digest.decode()}" == D3_SRI_HASH
    # The CDN fallback lives inside an inline document.write and must carry
    # the same pin.
    text = page.read_text(encoding="utf-8")
    assert D3_CDN_URL in text
    fallback = re.search(
        rf'src=\\?"{re.escape(D3_CDN_URL)}\\?" integrity=\\?"([^"\\]+)',
        text,
    )
    assert fallback and fallback.group(1) == D3_SRI_HASH, "CDN pin missing"
    _record("html", "d3_pins_verified", 2)


def test_html_hostile_names_cannot_escape_the_script_block(corpus: Corpus):
    page = corpus.run_a / "graph.html"
    text = page.read_text(encoding="utf-8")
    reader = _read_page(page)
    tag_names = {tag for tag, _ in reader.tags}
    assert "img" not in tag_names and "evil" not in tag_names, tag_names
    data = _embedded_graph(reader)
    names = {n["name"] for n in data["nodes"]}
    _require_planted(names, "html")
    assert "</script><img" not in text
    assert "<\\/script><img" in text, "the name is present, neutralised"
    assert "\x00" not in text and "\x1b" not in text
    _record("html", "hostile_names_neutralised", len(HOSTILE_NAMES))


def test_html_page_reader_would_notice_an_injected_element(tmp_path):
    """Teeth for the HTML consumer: it really sees injected markup."""
    bad = tmp_path / "bad.html"
    bad.write_text(
        "<html><body><script>var graphData = {\"nodes\": []};\n"
        "</script><img src=x></body></html>",
        encoding="utf-8",
    )
    reader = _read_page(bad)
    assert "img" in {tag for tag, _ in reader.tags}
    _record("html", "reader_selfcheck", 1)


# ---------------------------------------------------------------------------
# Wiki
# ---------------------------------------------------------------------------

_MD_LINK = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")


def _wiki(corpus: Corpus) -> Path:
    return corpus.run_a / "wiki"


def _table_lines(text: str, header: str) -> list[str]:
    """Return the raw body lines of the Markdown table under *header*.

    A GFM table runs to the next blank line, so the walker stops there
    rather than at the first line that fails to look like a row -- otherwise
    a corrupt row would end the scan and hide everything after it.
    """
    lines = text.splitlines()
    start = lines.index(header) + 2
    body: list[str] = []
    for line in lines[start:]:
        if not line.strip():
            break
        body.append(line)
    return body


def _cells(line: str) -> list[str] | None:
    """Split one Markdown table row, or None if the line is not a row."""
    stripped = line.strip()
    if not (stripped.startswith("|") and stripped.endswith("|")):
        return None
    return [c.strip() for c in stripped[1:-1].split("|")]


def _table_rows(text: str, header: str) -> list[list[str]]:
    """Return only the well-formed body rows under *header*."""
    return [
        cells for cells in (_cells(line) for line in _table_lines(text, header))
        if cells is not None
    ]


def test_wiki_index_covers_every_community_and_links_resolve(corpus: Corpus):
    from code_review_graph.communities import get_communities

    wiki = _wiki(corpus)
    index = (wiki / "index.md").read_text(encoding="utf-8")
    communities = get_communities(corpus.store)
    assert communities, "no communities to document"
    assert f"**Total communities**: {len(communities)}" in index
    rows = _table_rows(index, "| Community | Size | Link |")
    assert len(rows) == len(communities), (
        f"{len(communities)} communities produced {len(rows)} index rows"
    )
    listed = {row[0] for row in rows}
    assert listed == {c["name"] for c in communities}, listed
    for row in rows:
        link = _MD_LINK.search(row[2])
        assert link, row
        assert (wiki / link.group(2)).is_file(), link.group(2)
    _record("wiki", "index_rows_resolved", len(rows))


def test_wiki_pages_reflect_the_graph_rather_than_being_empty(corpus: Corpus):
    wiki = _wiki(corpus)
    pages = [p for p in wiki.glob("*.md") if p.name != "index.md"]
    assert pages, "no community pages generated"
    real_names = corpus.names
    documented = 0
    for page in pages:
        text = page.read_text(encoding="utf-8")
        assert text.startswith("# "), page.name
        for section in ("## Overview", "## Members", "## Execution Flows",
                        "## Dependencies"):
            assert section in text, f"{page.name} missing {section}"
        assert re.search(r"- \*\*Size\*\*: [1-9]\d* nodes", text), page.name
        rows = _table_rows(text, "| Name | Kind | File | Lines |")
        assert rows, f"{page.name} documents no members"
        for row in rows:
            if len(row) == 4 and row[0] in real_names:
                documented += 1
    assert documented > 0, "no table row matched a real graph node"
    _record("wiki", "member_rows_matched_graph", documented)


_MEMBER_HEADER = "| Name | Kind | File | Lines |"


def _member_table_lines(corpus: Corpus) -> list[tuple[str, str]]:
    """Every raw Members-table body line in the wiki, with its page name."""
    out: list[tuple[str, str]] = []
    for page in sorted(_wiki(corpus).glob("*.md")):
        if page.name == "index.md":
            continue
        text = page.read_text(encoding="utf-8")
        if _MEMBER_HEADER not in text:
            continue
        for line in _table_lines(text, _MEMBER_HEADER):
            out.append((page.name, line))
    return out


def test_wiki_member_tables_are_well_formed(corpus: Corpus):
    lines = _member_table_lines(corpus)
    assert lines, "no Members table to check"
    malformed = [
        (name, line[:60]) for name, line in lines
        if _cells(line) is None or len(_cells(line) or []) != 4
    ]
    assert not malformed, malformed


def test_wiki_pipe_and_newline_names_are_on_the_page_and_in_one_row(
    corpus: Corpus,
):
    """Teeth for the check above: the names that corrupted the table are
    still documented, each in exactly one four-cell row.

    A table can also be made well-formed by dropping the awkward rows, which
    would be a worse bug than the one being fixed, so the rows are located
    by content here rather than merely counted.
    """
    lines = _member_table_lines(corpus)
    assert lines, "no Members table to check"
    pipe_rows = [ln for _, ln in lines if "pipe" in ln]
    newline_rows = [ln for _, ln in lines if "and a newline" in ln]
    assert len(pipe_rows) == 1, pipe_rows
    assert len(newline_rows) == 1, newline_rows
    for row in pipe_rows + newline_rows:
        cells = _cells(row)
        assert cells is not None and len(cells) == 4, row
    # The pipe is still readable -- escaped, not deleted -- and the newline
    # no longer ends the row early.
    assert "pipe&#124;cell&#124;break" in pipe_rows[0], pipe_rows[0]
    assert "backslash and a newline" in newline_rows[0], newline_rows[0]
    _record("wiki", "awkward_rows_kept_intact", 2)


def test_wiki_lists_every_member_of_every_community(corpus: Corpus):
    from code_review_graph.communities import get_communities

    by_name = {c["name"]: c for c in get_communities(corpus.store)}
    checked = 0
    for page in sorted(_wiki(corpus).glob("*.md")):
        if page.name == "index.md":
            continue
        text = page.read_text(encoding="utf-8")
        community = by_name[text.splitlines()[0][2:]]
        expected = sum(
            1 for qn in community["members"][:50]
            if corpus.store.get_node(qn) is None
            or corpus.store.get_node(qn).kind != "File"
        )
        rows = _table_rows(text, _MEMBER_HEADER)
        checked += 1
        assert len(rows) >= expected, (
            f"{page.name}: {expected} non-file members, {len(rows)} rows"
        )
    assert checked


def test_wiki_documents_the_control_character_node(corpus: Corpus):
    """Teeth for the check above: the node that used to vanish is on a page.

    get_communities() runs every member qualified name through
    _sanitize_name before returning it, so the sanitised spelling is not a
    key store.get_node() understands. The node is still a member; the page
    has to find it anyway.
    """
    from code_review_graph.communities import get_communities

    sanitised_qn = corpus.qn("control_chars")
    assert sanitised_qn.endswith("::ctrlname")
    # The community reports it as a member ...
    members = {
        qn for c in get_communities(corpus.store) for qn in c["members"]
    }
    assert sanitised_qn in members
    # ... the store still cannot find it under that spelling ...
    assert corpus.store.get_node(sanitised_qn) is None
    # ... but the raw spelling, control characters and all, is there ...
    raw_qn = sanitised_qn.replace(
        "ctrlname", HOSTILE_NAMES["control_chars"]
    )
    assert corpus.store.get_node(raw_qn) is not None
    # ... and the wiki documents it, under its sanitised name.
    rows = [line for _, line in _member_table_lines(corpus)
            if "ctrlname" in line]
    assert len(rows) == 1, rows
    assert _cells(rows[0]) is not None and len(_cells(rows[0])) == 4
    joined = "\n".join(
        page.read_text(encoding="utf-8")
        for page in _wiki(corpus).glob("*.md")
    )
    assert "\x00" not in joined and "\x1b" not in joined
    _record("wiki", "control_character_member_documented", 1)


def test_wiki_community_name_is_escaped_in_the_page_heading(corpus: Corpus):
    page = _generate_community_page(
        corpus.store,
        {
            "name": "billing|core\nsplit",
            "size": 1,
            "cohesion": 0.5,
            "dominant_language": "python",
            "members": [],
        },
    )
    # Nothing may spill out of the heading into the page body: everything
    # before "## Overview" is the H1 and blank lines, nothing else. (The
    # blank line the generator writes after the heading is why this looks at
    # the prologue's lines rather than at the raw text.)
    prologue = page.split("\n## Overview")[0].splitlines()
    assert prologue[0].startswith("# ")
    assert [line for line in prologue[1:] if line.strip()] == [], prologue
    # The same name is written into the index table, one cell wide.
    index_row = f"| {prologue[0][2:]} | 1 | [x.md](x.md) |"
    assert len(index_row.strip().strip("|").split("|")) == 3, index_row


def test_wiki_community_name_survives_escaping_readably(corpus: Corpus):
    """Teeth for the check above: escaped, not truncated at the first '|'.

    A heading that simply dropped everything after the pipe would also pass
    the structural check, so pin what the reader actually sees.
    """
    page = _generate_community_page(
        corpus.store,
        {
            "name": "billing|core\nsplit",
            "size": 1,
            "cohesion": 0.5,
            "dominant_language": "python",
            "members": [],
        },
    )
    assert page.startswith("# billing&#124;core split\n"), page.splitlines()[0]
    _record("wiki", "community_name_escaped", 1)


def test_wiki_hostile_member_names_are_sanitised(corpus: Corpus):
    from code_review_graph.wiki import _md_cell

    pages = sorted(_wiki(corpus).glob("*.md"))
    joined = "\n".join(p.read_text(encoding="utf-8") for p in pages)
    # A Markdown table cell cannot carry a raw '|' or a newline, so those
    # two are escaped on the way in; that is the one transformation allowed
    # here, and it is pinned by
    # test_wiki_pipe_and_newline_names_are_on_the_page_and_in_one_row.
    present = {
        key for key, raw in HOSTILE_NAMES.items()
        if _md_cell(_sanitize_name(raw)) in joined
    }
    missing = set(HOSTILE_NAMES) - present
    assert not missing, f"missing from the wiki: {missing}"
    assert "\x00" not in joined and "\x1b" not in joined
    _record("wiki", "hostile_names_present", len(present))
    _record("wiki", "pages_scanned", len(pages))


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

_TEXT_ARTEFACTS = ["graph.json", "graph.graphml", "graph.cypher", "graph.html"]
_DIR_ARTEFACTS = ["obsidian", "wiki"]


@pytest.mark.parametrize("artefact", _TEXT_ARTEFACTS)
def test_text_export_is_byte_identical_on_a_second_run(
    corpus: Corpus, artefact
):
    a = (corpus.run_a / artefact).read_bytes()
    b = (corpus.run_b / artefact).read_bytes()
    assert a, f"{artefact} is empty"
    assert a == b, f"{artefact} differs between two runs of the same graph"
    _record("determinism", artefact, len(a))


@pytest.mark.parametrize("artefact", _DIR_ARTEFACTS)
def test_directory_export_is_byte_identical_on_a_second_run(
    corpus: Corpus, artefact
):
    def digest(root: Path) -> dict[str, str]:
        return {
            p.relative_to(root).as_posix():
                hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()
        }

    a = digest(corpus.run_a / artefact)
    b = digest(corpus.run_b / artefact)
    assert a, f"{artefact} produced no files"
    assert a == b, f"{artefact} differs between two runs of the same graph"
    _record("determinism", artefact, len(a))


def test_determinism_comparison_would_notice_a_single_changed_byte(
    corpus: Corpus, tmp_path
):
    """Teeth for the determinism checks: they compare real bytes."""
    original = (corpus.run_a / "graph.cypher").read_bytes()
    tampered = original.replace(b"CREATE", b"CREATe", 1)
    assert tampered != original
    (tmp_path / "t.cypher").write_bytes(tampered)
    assert (tmp_path / "t.cypher").read_bytes() != original
    _record("determinism", "selfcheck", 1)


# ---------------------------------------------------------------------------
# The CLI surface that produces all of this
# ---------------------------------------------------------------------------

def _seed_data_dir(corpus: Corpus, data_dir: Path) -> None:
    """Copy the built graph into *data_dir* for the CLI to read.

    The store runs in WAL mode, so the committed rows can still live in the
    -wal sidecar; checkpoint first or the CLI opens what looks like an empty
    graph and every export silently produces nothing.
    """
    import shutil

    corpus.store._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    data_dir.mkdir(parents=True, exist_ok=True)
    source = Path(corpus.store.db_path)
    shutil.copy(source, data_dir / "graph.db")
    for suffix in ("-wal", "-shm"):
        sidecar = source.with_name(source.name + suffix)
        if sidecar.exists():
            shutil.copy(sidecar, data_dir / f"graph.db{suffix}")


_CLI_FORMATS = {
    "json": "graph.json",
    "graphml": "graph.graphml",
    "cypher": "graph.cypher",
    "obsidian": "obsidian",
    "svg": "graph.svg",
    "html": "graph.html",
}


@pytest.mark.parametrize("fmt,expected", sorted(_CLI_FORMATS.items()))
def test_cli_visualize_writes_the_artefact_for_every_format(
    corpus: Corpus, tmp_path, fmt, expected
):
    if fmt == "svg":
        _require_matplotlib()
    data_dir = tmp_path / f"data-{fmt}"
    _seed_data_dir(corpus, data_dir)
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "CRG_HOME": str(tmp_path / "crg-home"),
    }
    (tmp_path / "home").mkdir(exist_ok=True)
    result = subprocess.run(
        [
            sys.executable, "-m", "code_review_graph", "visualize",
            "--repo", str(corpus.repo), "--data-dir", str(data_dir),
            "--format", fmt,
        ],
        capture_output=True, text=True, env=env, timeout=600,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    produced = data_dir / expected
    assert produced.exists(), f"{fmt}: {produced} was not written"
    size = sum(
        p.stat().st_size for p in produced.rglob("*") if p.is_file()
    ) if produced.is_dir() else produced.stat().st_size
    assert size > 0, f"{fmt}: artefact is empty"
    _record("cli", f"visualize_{fmt}", size)


def test_cli_wiki_writes_pages(corpus: Corpus, tmp_path):
    data_dir = tmp_path / "data-wiki"
    _seed_data_dir(corpus, data_dir)
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "CRG_HOME": str(tmp_path / "crg-home"),
    }
    (tmp_path / "home").mkdir(exist_ok=True)
    result = subprocess.run(
        [
            sys.executable, "-m", "code_review_graph", "wiki",
            "--repo", str(corpus.repo), "--data-dir", str(data_dir),
            "--force",
        ],
        capture_output=True, text=True, env=env, timeout=600,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    pages = sorted((data_dir / "wiki").glob("*.md"))
    assert len(pages) >= 2, [p.name for p in pages]
    _record("cli", "wiki_pages", len(pages))


# ---------------------------------------------------------------------------
# Canary
# ---------------------------------------------------------------------------

def test_zz_canary_every_format_was_really_compared():
    """Fail if a whole format never reported a real comparison.

    Runs last (name-ordered within the module). Every check above calls
    ``_record`` with the number of things it actually compared, and
    ``_record`` refuses zero, so this cannot pass on an empty artefact or a
    check that quietly became a no-op.
    """
    missing = sorted(_EXPECTED_CANARY_FORMATS - set(_CANARY))
    assert not missing, (
        f"no comparison was recorded for: {missing}. Either those checks did "
        "not run, or they ran without comparing anything."
    )
    for fmt, entries in sorted(_CANARY.items()):
        assert entries, fmt
        assert all(v > 0 for v in entries.values()), (fmt, entries)
    total = sum(sum(e.values()) for e in _CANARY.values())
    print(f"\nexport-conformance canary: {json.dumps(_CANARY, indent=2)}")
    assert total > 100, total
