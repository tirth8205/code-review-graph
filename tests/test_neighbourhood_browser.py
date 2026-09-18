"""Real-browser tests for the seeded neighbourhood page.

``tests/test_neighbourhood.py`` asserts on the payload and on the text of the
generated HTML.  That does not prove the page *behaves*: a payload carrying
hop labels is worthless if the renderer draws every ring at once, if clicking
a frontier node reveals nothing, or if adding the neighbourhood layer broke
the search box.  These tests drive the real page in headless Chromium.

The page is served over ``http://127.0.0.1`` rather than opened as
``file://`` for the reason documented at the top of
``tests/test_visualization_browser.py``: the vendored D3 carries an
``integrity`` attribute, and a ``file://`` document is an opaque origin, so
the same-origin script would be blocked and the page would reach for the CDN.

Run them with::

    pip install -e ".[dev,browser-test]"
    python -m playwright install chromium
    pytest -m browser
"""

from __future__ import annotations

import functools
import threading
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
# Fixture graph: one seed, a ring of 3, a ring of 6, and a far ring of 20 that
# a depth-2 neighbourhood must never ship.
# ---------------------------------------------------------------------------

SEED = "src/seed.py::entry"
RING1 = [f"src/ring1.py::a{i}" for i in range(1, 4)]
RING2 = [f"src/ring2.py::b{i}" for i in range(1, 7)]
RING3 = [f"src/far.py::z{i}" for i in range(1, 21)]

LAYOUT_TIMEOUT_MS = 60_000
PATH_STROKE = "#f2cc60"
MANY_SEEDS = ["src/seed.py", "src/ring1.py", "src/ring2.py", "src/far.py"]


def _node(kind: str, name: str, file_path: str, line: int) -> NodeInfo:
    return NodeInfo(
        kind=kind,
        name=name,
        file_path=file_path,
        line_start=line,
        line_end=line + 4,
        language="python",
        parent_name=None,
        params=None,
        return_type=None,
        modifiers=None,
        is_test=False,
        extra={},
    )


def _calls(source: str, target: str) -> EdgeInfo:
    return EdgeInfo(
        kind="CALLS",
        source=source,
        target=target,
        file_path=source.split("::")[0],
        line=1,
        extra={},
    )


def _build_store(db_path: Path) -> GraphStore:
    store = GraphStore(db_path)
    for file_path in ("src/seed.py", "src/ring1.py", "src/ring2.py", "src/far.py"):
        store.upsert_node(_node("File", file_path.rsplit("/", 1)[-1], file_path, 1))
    for line, qualified_name in enumerate([SEED, *RING1, *RING2, *RING3]):
        file_path, name = qualified_name.split("::")
        store.upsert_node(_node("Function", name, file_path, 10 + line * 5))
        store.upsert_edge(EdgeInfo(
            kind="CONTAINS", source=file_path, target=qualified_name,
            file_path=file_path, line=10 + line * 5, extra={},
        ))
    for target in RING1:
        store.upsert_edge(_calls(SEED, target))
    for index, target in enumerate(RING2):
        store.upsert_edge(_calls(RING1[index // 2], target))
    for target in RING3:
        store.upsert_edge(_calls(RING2[0], target))
    store.commit()
    return store


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture(scope="module")
def neighbourhood_site(tmp_path_factory):
    """Serve a depth-2 neighbourhood page, a path page and a many-seed page."""
    root = tmp_path_factory.mktemp("nb-browser")
    store = _build_store(root / "graph.db")
    try:
        (root / "hood").mkdir()
        generate_html(
            store, root / "hood" / "graph.html", seed_symbols=[SEED], depth=2
        )
        (root / "path").mkdir()
        generate_html(
            store,
            root / "path" / "graph.html",
            path_from=SEED,
            path_to=RING2[5],
            depth=0,
        )
        # What --seed-changed produces on a review that touched every file.
        (root / "many").mkdir()
        generate_html(
            store,
            root / "many" / "graph.html",
            seed_files=MANY_SEEDS,
            depth=1,
        )
    finally:
        store.close()

    handler = functools.partial(_QuietHandler, directory=str(root))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield {
            name: f"{base}/{name}/graph.html"
            for name in ("hood", "path", "many")
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=30)


@pytest.fixture(scope="module")
def chromium():
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


_SETTLED_JS = """
() => document.getElementById("loading-overlay").classList.contains("hidden")
"""

_LABELS_JS = """
() => Array.from(
  document.querySelectorAll("#graph-svg g.nodes g.node-g")
).map(g => g.getAttribute("aria-label"))
"""

_PATH_EDGE_COUNT_JS = """
(stroke) => Array.from(
  document.querySelectorAll("#graph-svg g.links line")
).filter(l => l.getAttribute("stroke") === stroke).length
"""


def _open(chromium, url: str):
    page = chromium.new_page(viewport={"width": 1400, "height": 900})
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on(
        "console",
        lambda message: errors.append(message.text)
        if message.type == "error"
        else None,
    )
    origin = url.rsplit("/", 2)[0]
    page.route(
        "**/*",
        lambda route: route.continue_()
        if route.request.url.startswith(origin)
        else route.abort(),
    )
    page.goto(url)
    page.wait_for_function(_SETTLED_JS, timeout=LAYOUT_TIMEOUT_MS)
    return page, errors


def _labels(page) -> set[str]:
    return set(page.evaluate(_LABELS_JS))


def test_only_the_render_depth_ring_is_drawn_first(chromium, neighbourhood_site):
    page, errors = _open(chromium, neighbourhood_site["hood"])
    try:
        labels = _labels(page)
        assert "Function: entry" in labels
        assert "Function: a1" in labels, "hop 1 is drawn"
        assert "Function: b1" not in labels, "hop 2 waits for an expand"
        assert "Function: z1" not in labels, "hop 3 is not in the payload at all"
        assert errors == [], errors
    finally:
        page.close()


def test_clicking_a_frontier_node_reveals_its_ring(chromium, neighbourhood_site):
    page, errors = _open(chromium, neighbourhood_site["hood"])
    try:
        before = _labels(page)
        assert "Function: b1" not in before
        page.evaluate("() => { nbExpand('src/ring1.py::a1'); nbRedraw(); }")
        page.wait_for_function(_SETTLED_JS, timeout=LAYOUT_TIMEOUT_MS)
        after = _labels(page)
        assert "Function: b1" in after
        assert "Function: b2" in after
        assert "File: src/ring2.py" in after, "the revealed nodes keep their file"
        assert "Function: b3" not in after, "only a1's own ring was revealed"
        assert errors == [], errors
    finally:
        page.close()


def test_plus_one_hop_button_reveals_the_whole_ring(chromium, neighbourhood_site):
    page, errors = _open(chromium, neighbourhood_site["hood"])
    try:
        assert page.locator("#nb-bar").is_visible()
        page.locator("#nb-expand").click()
        page.wait_for_function(_SETTLED_JS, timeout=LAYOUT_TIMEOUT_MS)
        labels = _labels(page)
        for index in range(1, 7):
            assert f"Function: b{index}" in labels
        assert "Function: z1" not in labels
        assert page.locator("#nb-expand").is_disabled(), "depth 2 is the payload"
        assert errors == [], errors
    finally:
        page.close()


def test_reset_returns_to_the_render_depth(chromium, neighbourhood_site):
    page, errors = _open(chromium, neighbourhood_site["hood"])
    try:
        page.locator("#nb-expand").click()
        page.wait_for_function(_SETTLED_JS, timeout=LAYOUT_TIMEOUT_MS)
        assert "Function: b1" in _labels(page)
        page.locator("#nb-reset").click()
        page.wait_for_function(_SETTLED_JS, timeout=LAYOUT_TIMEOUT_MS)
        assert "Function: b1" not in _labels(page)
        assert errors == [], errors
    finally:
        page.close()


def test_path_query_draws_and_highlights_the_path(chromium, neighbourhood_site):
    page, errors = _open(chromium, neighbourhood_site["path"])
    try:
        labels = _labels(page)
        # entry -> a3 -> b6 is the only route through CALLS.
        for name in ("entry", "a3", "b6"):
            assert f"Function: {name}" in labels
        assert "Function: a1" not in labels, "off-path nodes are not shipped"
        highlighted = page.evaluate(_PATH_EDGE_COUNT_JS, PATH_STROKE)
        assert highlighted == 2, "both path edges are gold"
        assert errors == [], errors
    finally:
        page.close()


def test_existing_interaction_surface_still_works(chromium, neighbourhood_site):
    page, errors = _open(chromium, neighbourhood_site["hood"])
    try:
        # Search still narrows to a match.
        page.locator("#search").fill("a2")
        page.wait_for_timeout(100)
        assert page.locator("#search-results .sr-item").count() >= 1

        # The detail panel still opens.
        page.evaluate("() => showDetailPanel(nodeById.get('src/seed.py::entry'))")
        assert page.locator("#detail-panel").is_visible()
        # Close via the button, not Escape: the panel hides #legend while it is
        # open and only the close button puts it back (pre-existing behaviour).
        page.locator("#detail-panel .dp-close").click()

        # Edge-kind toggles still remove edges.
        before = page.locator("#graph-svg g.links line").count()
        page.locator('[data-edge-kind="CALLS"]').click()
        page.wait_for_timeout(100)
        assert page.locator("#graph-svg g.links line").count() < before

        # Node-kind filters still hide nodes.
        page.locator('#filter-panel input[data-kind="Function"]').uncheck()
        page.wait_for_timeout(200)
        assert "Function: entry" not in _labels(page)
        assert errors == [], errors
    finally:
        page.close()


def test_the_bar_truncates_a_many_seed_run(chromium, neighbourhood_site):
    """A --seed-changed page must not paint every seed path across the graph."""
    page, errors = _open(chromium, neighbourhood_site["many"])
    try:
        bar = page.locator("#nb-bar")
        assert bar.is_visible()
        shown = page.locator("#nb-bar .nb-seed").inner_text()
        assert shown.count(",") == 2, f"three seeds, not {shown}"
        assert "src/far.py" not in shown, "the fourth is behind the toggle"
        toggle = page.locator("#nb-seed-toggle")
        assert toggle.inner_text() == "+1 more"
        assert page.locator("#nb-seed-list").is_visible() is False
        assert errors == [], errors
    finally:
        page.close()


def test_the_full_seed_list_is_one_click_away(chromium, neighbourhood_site):
    page, errors = _open(chromium, neighbourhood_site["many"])
    try:
        toggle = page.locator("#nb-seed-toggle")
        assert toggle.get_attribute("aria-expanded") == "false"
        toggle.click()
        listing = page.locator("#nb-seed-list")
        assert listing.is_visible()
        assert listing.locator("li").count() == len(MANY_SEEDS)
        assert "src/far.py" in listing.inner_text()
        assert toggle.get_attribute("aria-expanded") == "true"
        toggle.click()
        assert listing.is_visible() is False
        assert errors == [], errors
    finally:
        page.close()


def test_a_single_seed_needs_no_disclosure(chromium, neighbourhood_site):
    page, errors = _open(chromium, neighbourhood_site["hood"])
    try:
        assert page.locator("#nb-bar").is_visible()
        assert page.locator("#nb-seed-toggle").count() == 0
        assert page.locator("#nb-seed-list").count() == 0
        assert errors == [], errors
    finally:
        page.close()
