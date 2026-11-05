"""Real-browser tests for the generated visualization page.

``tests/test_visualization.py`` asserts on the *text* of the HTML that
``visualization.generate_html`` writes. That catches template regressions but
not runtime ones: a page whose markup contains ``d3.select("#graph-svg")`` can
still render nothing because D3 failed to load, a handler threw, or a selector
matched the wrong element. These tests drive the real page in headless
Chromium via Playwright and assert on what the browser actually produced.

Why the page is served over ``http://127.0.0.1`` instead of opened as a
``file://`` URL: the generated page loads its vendored D3 with an ``integrity``
attribute (issue #475). Chromium treats every ``file://`` document as an opaque
origin, so the same-origin ``<script src="d3.v7.min.js" integrity="...">`` is
*blocked* ("the resource requires the request to be CORS enabled to check the
integrity") and the page falls back to the d3js.org CDN — i.e. a ``file://``
open needs the network. ``code-review-graph visualize --serve`` publishes the
same directory over HTTP, where the integrity check passes against the
same-origin copy and nothing leaves the machine. The local ``http.server``
below mirrors that supported path, which is also the only way these tests can
be offline and deterministic. ``test_file_url_open_needs_the_cdn`` pins the
``file://`` limitation so a future fix is noticed.

Every page in this module runs behind a ``page.route`` guard that aborts any
request leaving the test's own origin, so no test can silently depend on the
network.

Run them with::

    pip install -e ".[dev,browser-test]"
    python -m playwright install chromium
    pytest -m browser
"""

from __future__ import annotations

import functools
import json
import threading
from contextlib import contextmanager
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.parser import EdgeInfo, NodeInfo
from code_review_graph.visualization import generate_html

pytest.importorskip(
    "playwright",
    reason='Playwright is not installed; install the "browser-test" extra',
)

from playwright.sync_api import Error as PlaywrightError  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

pytestmark = pytest.mark.browser

# ---------------------------------------------------------------------------
# Fixture graph: 3 files x (1 class + 8 functions) = 30 nodes, 3 communities
# ---------------------------------------------------------------------------

FILE_COUNT = 3
FUNCS_PER_FILE = 8
# One File + one Class + FUNCS_PER_FILE Functions per file.
NODES_PER_FILE = 2 + FUNCS_PER_FILE
TOTAL_NODES = FILE_COUNT * NODES_PER_FILE
# Each file CONTAINS its class and its functions; plus one CALLS edge per file.
CONTAINS_PER_FILE = NODES_PER_FILE - 1
TOTAL_CONTAINS = FILE_COUNT * CONTAINS_PER_FILE
TOTAL_CALLS = FILE_COUNT
TOTAL_EDGES = TOTAL_CONTAINS + TOTAL_CALLS

COMMUNITY_NAMES = ("Auth Core", "Data Layer", "Utilities")
FLOW_NAME = "login_flow"

# A symbol that appears exactly once in the graph, used by the search test.
UNIQUE_SYMBOL = "mod1_func_3"

CALLS_STROKE = "#3fb950"

VIEWPORT = {"width": 1400, "height": 900}
# Rectangle of the canvas left clear by the fixed panels at VIEWPORT size:
# #legend (top-left), #controls / #search-results (top-right),
# #filter-panel (bottom-left) and #stats-bar (bottom edge).
CLEAR_BOX = {"x0": 380, "x1": 1020, "y0": 180, "y1": 740}

# Generous: a cold headless Chromium on a loaded CI runner is slow, and the
# assertions never depend on *how long* layout took, only that it finished.
LAYOUT_TIMEOUT_MS = 60_000


def _node(kind: str, name: str, file_path: str, line: int, **kw) -> NodeInfo:
    return NodeInfo(
        kind=kind,
        name=name,
        file_path=file_path,
        line_start=line,
        line_end=line + 5,
        language="python",
        parent_name=kw.get("parent_name"),
        params=kw.get("params"),
        return_type=kw.get("return_type"),
        modifiers=None,
        is_test=False,
        extra={},
    )


def _build_store(db_path: Path) -> GraphStore:
    """Build a deterministic 30-node graph with communities and one flow."""
    store = GraphStore(db_path)
    for i in range(FILE_COUNT):
        file_path = f"src/mod{i}.py"
        store.upsert_node(_node("File", f"mod{i}.py", file_path, 1))
        class_qn = f"{file_path}::Service{i}"
        store.upsert_node(_node("Class", f"Service{i}", file_path, 5))
        store.upsert_edge(EdgeInfo(
            kind="CONTAINS", source=file_path, target=class_qn,
            file_path=file_path, line=5, extra={},
        ))
        for j in range(FUNCS_PER_FILE):
            fn_name = f"mod{i}_func_{j}"
            store.upsert_node(_node(
                "Function", fn_name, file_path, 20 + j * 10,
                params="payload", return_type="bool",
            ))
            store.upsert_edge(EdgeInfo(
                kind="CONTAINS", source=file_path, target=f"{file_path}::{fn_name}",
                file_path=file_path, line=20 + j * 10, extra={},
            ))

    # One cross-file CALLS edge per file, so every community pair is linked.
    for i in range(FILE_COUNT):
        nxt = (i + 1) % FILE_COUNT
        store.upsert_edge(EdgeInfo(
            kind="CALLS",
            source=f"src/mod{i}.py::mod{i}_func_0",
            target=f"src/mod{nxt}.py::mod{nxt}_func_1",
            file_path=f"src/mod{i}.py",
            line=25,
            extra={},
        ))

    # One community per file. Parameterised SQL only (CLAUDE.md invariant).
    for i, community_name in enumerate(COMMUNITY_NAMES):
        store._conn.execute(
            "UPDATE nodes SET community_id = ? WHERE file_path = ?",
            (i, f"src/mod{i}.py"),
        )
        store._conn.execute(
            "INSERT INTO communities "
            "(id, name, level, cohesion, size, dominant_language, description) "
            "VALUES (?, ?, 0, 0.8, ?, 'python', ?)",
            (i, community_name, NODES_PER_FILE, f"{community_name} description"),
        )

    entry_row = store._conn.execute(
        "SELECT id FROM nodes WHERE qualified_name = ?",
        ("src/mod0.py::mod0_func_0",),
    ).fetchone()
    path_ids = [
        row["id"]
        for row in store._conn.execute(
            "SELECT id FROM nodes WHERE qualified_name IN (?, ?)",
            ("src/mod0.py::mod0_func_0", "src/mod1.py::mod1_func_1"),
        ).fetchall()
    ]
    store._conn.execute(
        "INSERT INTO flows "
        "(name, entry_point_id, depth, node_count, file_count, criticality, path_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (FLOW_NAME, entry_row["id"], 2, len(path_ids), 2, 0.9, json.dumps(path_ids)),
    )
    store.commit()
    return store


class _QuietHandler(SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler without the per-request stderr logging."""

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture(scope="module")
def graph_site(tmp_path_factory):
    """Generate the auto- and community-mode pages and serve them locally.

    Returns ``(base_url, {"auto": "<url>", "community": "<url>"})``. The
    server is bound to 127.0.0.1 on an ephemeral port, so the tests never
    touch the network and never collide with a port already in use.
    """
    root = tmp_path_factory.mktemp("viz-browser")
    store = _build_store(root / "graph.db")
    try:
        for mode in ("auto", "community"):
            mode_dir = root / mode
            mode_dir.mkdir()
            generate_html(store, mode_dir / "graph.html", mode=mode)
    finally:
        store.close()

    handler = functools.partial(_QuietHandler, directory=str(root))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield base_url, {m: f"{base_url}/{m}/graph.html" for m in ("auto", "community")}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=30)


@pytest.fixture(scope="module")
def chromium():
    """A headless Chromium, or a clean skip when it has not been installed."""
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except PlaywrightError as exc:  # pragma: no cover - environment guard
            pytest.skip(
                "Playwright's Chromium build is unavailable "
                f"(run `python -m playwright install chromium`): {exc}"
            )
        try:
            yield browser
        finally:
            browser.close()


class _Probe:
    """Wraps a page and records console errors, page errors and requests."""

    def __init__(self, page) -> None:
        self.page = page
        self.console_errors: list[str] = []
        self.page_errors: list[str] = []
        self.requests: list[str] = []
        self.blocked: list[str] = []
        page.on("console", self._on_console)
        page.on("pageerror", self._on_page_error)
        page.on("request", lambda request: self.requests.append(request.url))

    def _on_console(self, message) -> None:
        if message.type == "error":
            self.console_errors.append(message.text)

    def _on_page_error(self, error) -> None:
        self.page_errors.append(str(error))

    def assert_no_errors(self) -> None:
        assert self.console_errors == [], f"console errors: {self.console_errors}"
        assert self.page_errors == [], f"uncaught page errors: {self.page_errors}"

    # -- page helpers -------------------------------------------------

    @property
    def node_groups(self):
        return self.page.locator("#graph-svg g.nodes g.node-g")

    @property
    def edge_lines(self):
        return self.page.locator("#graph-svg g.links line")

    def wait_for_layout(self) -> None:
        """Block until the force layout has finished.

        The full-graph template exposes this as UI state: ``simulation.on("end")``
        adds ``.hidden`` to ``#loading-overlay``. The aggregated template has no
        overlay, so fall back to D3's own end condition (``alpha < alphaMin``) on
        the page-global ``simulation``. No sleeping either way.
        """
        self.page.wait_for_function(_LAYOUT_SETTLED_JS, timeout=LAYOUT_TIMEOUT_MS)

    def freeze_layout(self) -> None:
        """Pin every node to a grid in the panel-free area and reset the zoom.

        Real mouse clicks on a live force simulation are a race: the node moves
        between the bounding box being measured and the click landing, and the
        post-layout ``fitGraph()`` zoom transition moves it again. Pinning
        ``fx``/``fy`` and resetting the transform to the identity makes the
        geometry exact, keeps every node clear of the fixed panels, and lets the
        clicks below be genuine mouse events with Playwright's full actionability
        checks rather than synthesised DOM events.
        """
        self.page.evaluate(_FREEZE_LAYOUT_JS, CLEAR_BOX)

    def node_by_label(self, aria_label: str):
        return self.page.locator(
            f'#graph-svg g.nodes g.node-g[aria-label="{aria_label}"]'
        )

    def node_by_qualified_name(self, qualified_name: str):
        """Locate a node group by its bound datum (aggregated pages have no label)."""
        index = self.page.evaluate(_NODE_INDEX_JS, qualified_name)
        assert index >= 0, f"no rendered node for {qualified_name!r}"
        return self.node_groups.nth(index)

    def visible_shape_count(self, opacity: str) -> int:
        return self.page.evaluate(_SHAPE_OPACITY_COUNT_JS, opacity)

    def stroke_count(self, stroke: str) -> int:
        return self.page.evaluate(_EDGE_STROKE_COUNT_JS, stroke)


_LAYOUT_SETTLED_JS = """
() => {
  const overlay = document.getElementById("loading-overlay");
  if (overlay) return overlay.classList.contains("hidden");
  const sim = window.simulation;
  return !!(sim && sim.alpha() < sim.alphaMin());
}
"""

_FREEZE_LAYOUT_JS = """
(box) => {
  const d3 = window.d3;
  const all = (window.nodes || window.currentNodes || []).slice();
  all.sort((a, b) => (
    a.qualified_name < b.qualified_name ? -1 : a.qualified_name > b.qualified_name ? 1 : 0
  ));
  const cols = Math.max(1, Math.ceil(Math.sqrt(all.length)));
  const rows = Math.max(1, Math.ceil(all.length / cols));
  const dx = cols > 1 ? (box.x1 - box.x0) / (cols - 1) : 0;
  const dy = rows > 1 ? (box.y1 - box.y0) / (rows - 1) : 0;
  all.forEach((n, i) => {
    n.x = n.fx = box.x0 + (i % cols) * dx;
    n.y = n.fy = box.y0 + Math.floor(i / cols) * dy;
  });
  if (window.simulation) window.simulation.stop();
  window.svg.interrupt();
  window.svg.call(window.zoomBehavior.transform, d3.zoomIdentity);
  d3.select("#graph-svg").selectAll("g.node-g")
    .attr("transform", (d) => "translate(" + d.x + "," + d.y + ")");
  return all.length;
}
"""

_NODE_INDEX_JS = """
(qn) => Array.from(document.querySelectorAll("#graph-svg g.nodes g.node-g"))
  .findIndex((el) => el.__data__ && el.__data__.qualified_name === qn)
"""

_SHAPE_OPACITY_COUNT_JS = """
(opacity) => document.querySelectorAll(
  '#graph-svg g.nodes g.node-g .node-shape[opacity="' + opacity + '"]'
).length
"""

_EDGE_STROKE_COUNT_JS = """
(stroke) => Array.from(document.querySelectorAll("#graph-svg g.links line"))
  .filter((line) => line.getAttribute("stroke") === stroke).length
"""


def _block_foreign_requests(base_url: str):
    def handler(route) -> None:
        if route.request.url.startswith(base_url):
            route.continue_()
        else:
            route.abort()

    return handler


@contextmanager
def _open(chromium_browser, base_url: str, url: str):
    """Open *url* in a fresh page that can only talk to *base_url*."""
    page = chromium_browser.new_page(viewport=VIEWPORT)
    probe = _Probe(page)
    page.route("**/*", _block_foreign_requests(base_url))
    try:
        page.goto(url, wait_until="load", timeout=LAYOUT_TIMEOUT_MS)
        yield probe
    finally:
        page.close()


@pytest.fixture
def full_page(chromium, graph_site):
    base_url, urls = graph_site
    with _open(chromium, base_url, urls["auto"]) as probe:
        probe.wait_for_layout()
        yield probe


@pytest.fixture
def community_page(chromium, graph_site):
    base_url, urls = graph_site
    with _open(chromium, base_url, urls["community"]) as probe:
        probe.wait_for_layout()
        yield probe


# ---------------------------------------------------------------------------
# Full (auto -> full) page
# ---------------------------------------------------------------------------


def test_full_page_renders_every_node_and_edge(full_page):
    """The default page must actually paint the graph, not just ship markup."""
    assert full_page.node_groups.count() == TOTAL_NODES
    assert full_page.edge_lines.count() == TOTAL_EDGES
    assert full_page.stroke_count(CALLS_STROKE) == TOTAL_CALLS

    # The loading overlay is gone and the empty state never appeared.
    assert "hidden" in (full_page.page.locator("#loading-overlay").get_attribute("class") or "")
    assert not full_page.page.locator("#empty-state").is_visible()

    stats = full_page.page.locator("#stats-bar").inner_text()
    assert str(TOTAL_NODES) in stats
    assert str(TOTAL_EDGES) in stats

    # The detected flow reached the dropdown (placeholder option + one flow).
    flow_options = full_page.page.locator("#flow-select option")
    assert flow_options.count() == 2
    assert FLOW_NAME in flow_options.nth(1).inner_text()

    full_page.assert_no_errors()


def test_search_box_filters_to_a_known_symbol(full_page):
    """Typing a unique symbol dims everything else and lists one result."""
    # Before searching, the shapes carry no explicit opacity — nothing is dimmed.
    assert full_page.visible_shape_count("0.08") == 0

    full_page.page.fill("#search", UNIQUE_SYMBOL)

    results = full_page.page.locator("#search-results .sr-item")
    results.first.wait_for(state="visible", timeout=LAYOUT_TIMEOUT_MS)
    assert results.count() == 1
    assert UNIQUE_SYMBOL in results.first.inner_text()

    # Exactly the matching node keeps full opacity; the rest are dimmed to 0.08.
    assert full_page.visible_shape_count("1") == 1
    assert full_page.visible_shape_count("0.08") == TOTAL_NODES - 1

    full_page.page.fill("#search", "")
    assert full_page.visible_shape_count("1") == TOTAL_NODES
    assert full_page.visible_shape_count("0.08") == 0
    full_page.assert_no_errors()


def test_clicking_a_file_node_collapses_and_expands_its_children(full_page):
    """Clicking a File toggles the symbols it CONTAINS."""
    full_page.freeze_layout()
    target = full_page.node_by_label("File: src/mod0.py")
    assert target.count() == 1

    target.click()
    expected_after_collapse = TOTAL_NODES - CONTAINS_PER_FILE
    full_page.page.wait_for_function(
        "(n) => document.querySelectorAll('#graph-svg g.nodes g.node-g').length === n",
        arg=expected_after_collapse,
        timeout=LAYOUT_TIMEOUT_MS,
    )
    assert full_page.node_groups.count() == expected_after_collapse
    # Edges into the hidden children go with them; the CALLS edge out of
    # mod0_func_0 and the one into mod0_func_1 are hidden too.
    assert full_page.edge_lines.count() < TOTAL_EDGES

    full_page.freeze_layout()
    target.click()
    full_page.page.wait_for_function(
        "(n) => document.querySelectorAll('#graph-svg g.nodes g.node-g').length === n",
        arg=TOTAL_NODES,
        timeout=LAYOUT_TIMEOUT_MS,
    )
    assert full_page.node_groups.count() == TOTAL_NODES
    full_page.assert_no_errors()


def test_legend_edge_toggle_hides_that_edge_type(full_page):
    """Clicking an edge type in the legend removes those lines from the canvas."""
    calls_toggle = full_page.page.locator('.legend-item[data-edge-kind="CALLS"]')
    assert calls_toggle.get_attribute("aria-pressed") == "true"
    assert full_page.stroke_count(CALLS_STROKE) == TOTAL_CALLS

    calls_toggle.click()
    assert calls_toggle.get_attribute("aria-pressed") == "false"
    assert "dimmed" in (calls_toggle.get_attribute("class") or "")
    assert full_page.stroke_count(CALLS_STROKE) == 0
    assert full_page.edge_lines.count() == TOTAL_EDGES - TOTAL_CALLS

    calls_toggle.click()
    assert calls_toggle.get_attribute("aria-pressed") == "true"
    assert full_page.stroke_count(CALLS_STROKE) == TOTAL_CALLS
    assert full_page.edge_lines.count() == TOTAL_EDGES
    full_page.assert_no_errors()


# ---------------------------------------------------------------------------
# Aggregated (community) page
# ---------------------------------------------------------------------------


def test_community_page_renders_super_nodes_and_drills_in(community_page):
    """Community mode paints one node per community and drills in on double-click."""
    assert community_page.node_groups.count() == len(COMMUNITY_NAMES)
    # SVG <text> is not an HTMLElement, so read text_content(), not inner_text().
    labels = community_page.page.locator("#graph-svg g.labels text.node-label")
    assert {labels.nth(i).text_content() for i in range(labels.count())} == set(COMMUNITY_NAMES)
    # One CROSS_COMMUNITY edge per community pair.
    assert community_page.edge_lines.count() == len(COMMUNITY_NAMES)

    back_button = community_page.page.locator("#btn-back")
    assert not back_button.is_visible()

    community_page.freeze_layout()
    community_page.node_by_qualified_name("__community__0").dblclick()

    community_page.page.wait_for_function(
        "(n) => document.querySelectorAll('#graph-svg g.nodes g.node-g').length === n",
        arg=NODES_PER_FILE,
        timeout=LAYOUT_TIMEOUT_MS,
    )
    assert community_page.node_groups.count() == NODES_PER_FILE
    assert back_button.is_visible()
    assert COMMUNITY_NAMES[0] in community_page.page.locator("#filter-info").inner_text()

    back_button.click()
    community_page.page.wait_for_function(
        "(n) => document.querySelectorAll('#graph-svg g.nodes g.node-g').length === n",
        arg=len(COMMUNITY_NAMES),
        timeout=LAYOUT_TIMEOUT_MS,
    )
    assert not back_button.is_visible()
    community_page.assert_no_errors()


# ---------------------------------------------------------------------------
# Offline behaviour
# ---------------------------------------------------------------------------


def test_page_renders_offline_from_the_vendored_d3(chromium, graph_site):
    """Issue #475: a served page must never need d3js.org.

    The page fixture already aborts every request that leaves the test's own
    origin, so this asserts the consequence explicitly: D3 v7 is live, the
    graph painted, and the only subresource fetched is the same-origin
    ``d3.v7.min.js`` written next to the HTML.
    """
    base_url, urls = graph_site
    with _open(chromium, base_url, urls["auto"]) as probe:
        probe.wait_for_layout()

        assert probe.page.evaluate("() => typeof window.d3") == "object"
        assert probe.page.evaluate("() => window.d3.version").startswith("7.")
        assert probe.node_groups.count() == TOTAL_NODES

        foreign = [url for url in probe.requests if not url.startswith(base_url)]
        assert foreign == [], f"page reached outside its origin: {foreign}"
        assert any(url.endswith("/d3.v7.min.js") for url in probe.requests)
        probe.assert_no_errors()


def test_file_url_open_needs_the_cdn(chromium, graph_site, tmp_path):
    """Characterisation test: ``file://`` opens cannot use the vendored D3.

    Chromium gives every ``file://`` document an opaque origin, so the
    SRI-pinned ``<script src="d3.v7.min.js" integrity="...">`` cannot be
    validated and is blocked; the page then falls back to the d3js.org CDN.
    With the network blocked (as it is here) nothing renders. This is why
    ``visualize --serve`` — not a double-click on ``graph.html`` — is the
    offline-capable path, and why the rest of this module uses HTTP.

    If this test ever fails, the ``file://`` limitation has been fixed (for
    example by inlining D3 or dropping ``integrity`` from the local tag) and
    this test should be replaced by a positive one.
    """
    store = _build_store(tmp_path / "graph.db")
    try:
        output = tmp_path / "graph.html"
        generate_html(store, output, mode="full")
    finally:
        store.close()
    assert (tmp_path / "d3.v7.min.js").exists()

    page = chromium.new_page(viewport=VIEWPORT)
    probe = _Probe(page)
    page.route("**/*", _block_foreign_requests("file://"))
    try:
        page.goto(output.as_uri(), wait_until="load", timeout=LAYOUT_TIMEOUT_MS)
        assert page.evaluate("() => typeof window.d3") == "undefined"
        assert probe.node_groups.count() == 0
        # The overlay never clears because the layout never starts.
        assert "hidden" not in (
            page.locator("#loading-overlay").get_attribute("class") or ""
        )
        assert any("integrity" in msg.lower() for msg in probe.console_errors), (
            f"expected a Subresource Integrity console error, got {probe.console_errors}"
        )
    finally:
        page.close()
