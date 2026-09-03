"""Call-registration HTTP endpoints (Express/Koa/Fastify, Go net/http, gin).

Mirrors test_spring_webflux_endpoints.py: a route registered by a call,
``app.get('/x', handler)`` or ``http.HandleFunc('/x', handler)``, becomes
an ``Endpoint`` node linked to its handler by a ``HANDLES`` edge, so the
existing ``handlers_of`` / ``endpoints_for`` queries work uniformly.
"""
from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.parser import CodeParser
from code_review_graph.tools.query import query_graph

JS_SOURCE = b"""
const express = require('express');
const app = express();
const router = express.Router();

function listUsers(req, res) { res.send(users); }
function ping(req, res) { res.send('ok'); }

app.get('/users', listUsers);
router.post('/ping', ping);

// not routes: no handler argument, or a non-route receiver
app.get('/health');
const cache = new Map();
cache.get('key');
"""

GO_SOURCE = b"""
package main

import (
\t"net/http"
)

func runCmd(w http.ResponseWriter, r *http.Request) {}
func status(w http.ResponseWriter, r *http.Request) {}

func main() {
\thttp.HandleFunc("/run", runCmd)
\trouter.GET("/status", status)
}
"""


def test_express_routes_link_endpoints_to_handlers(tmp_path: Path) -> None:
    path = tmp_path / "app.js"
    nodes, edges = CodeParser().parse_bytes(path, JS_SOURCE)
    endpoints = [n for n in nodes if n.kind == "Endpoint"]
    handles = [e for e in edges if e.kind == "HANDLES"]

    assert {(n.extra["http_method"], n.extra["route"]) for n in endpoints} == {
        ("GET", "/users"), ("POST", "/ping"),
    }
    assert {e.source for e in handles} == {
        f"{path.as_posix()}::listUsers", f"{path.as_posix()}::ping",
    }
    # app.get('/health') has no handler; cache.get('key') is not a route.
    assert all(n.extra["route"] != "/health" for n in endpoints)


def test_go_call_registration_routes(tmp_path: Path) -> None:
    path = tmp_path / "server.go"
    nodes, edges = CodeParser().parse_bytes(path, GO_SOURCE)
    endpoints = {(n.extra["http_method"], n.extra["route"]) for n in nodes if n.kind == "Endpoint"}
    sources = {e.source for e in edges if e.kind == "HANDLES"}
    assert endpoints == {("ANY", "/run"), ("GET", "/status")}
    assert sources == {f"{path.as_posix()}::runCmd", f"{path.as_posix()}::status"}


def test_endpoint_queries_use_addressable_nodes(tmp_path: Path) -> None:
    path = tmp_path / "app.js"
    nodes, edges = CodeParser().parse_bytes(path, JS_SOURCE)
    graph_dir = tmp_path / ".code-review-graph"
    graph_dir.mkdir()
    with GraphStore(graph_dir / "graph.db") as store:
        store.store_file_nodes_edges(str(path), nodes, edges, "hash")

    handler_qn = f"{path.as_posix()}::listUsers"
    endpoints = query_graph("endpoints_for", handler_qn, repo_root=str(tmp_path))
    assert any(
        r["qualified_name"].endswith("GET /users")
        for r in endpoints["results"]
    )
    endpoint_qn = next(
        n for n in nodes if n.kind == "Endpoint" and n.extra["route"] == "/users"
    )
    endpoint_qn = f"{path.as_posix()}::{endpoint_qn.name}"
    handlers = query_graph("handlers_of", endpoint_qn, repo_root=str(tmp_path))
    assert [r["qualified_name"] for r in handlers["results"]] == [handler_qn]


def test_non_route_member_calls_are_ignored(tmp_path: Path) -> None:
    source = b"""
const m = new Map();
const v = m.get('k');
promise.then(handler);
obj.post('data', handler);
"""
    nodes, _ = CodeParser().parse_bytes(tmp_path / "x.js", source)
    # obj.post('data', handler) has a non-slash route, so it is not an endpoint.
    assert [n for n in nodes if n.kind == "Endpoint"] == []


JS_INLINE = b"""
const app = require('express')();
app.post('/run', (req, res) => { doThing(req.body.cmd); });
app.get('/read', function (req, res) { res.send(readFileSync(req.query.p)); });
"""

GO_INLINE = b"""
package main

import "net/http"

func main() {
\thttp.HandleFunc("/x", func(w http.ResponseWriter, r *http.Request) {})
}
"""


def test_inline_js_handlers_get_synthetic_nodes(tmp_path: Path) -> None:
    path = tmp_path / "inline.js"
    nodes, edges = CodeParser().parse_bytes(path, JS_INLINE)
    endpoints = {(n.extra["http_method"], n.extra["route"])
                 for n in nodes if n.kind == "Endpoint"}
    assert endpoints == {("POST", "/run"), ("GET", "/read")}
    synth = [n for n in nodes
             if n.kind == "Function" and n.extra.get("synthetic_route_handler")]
    assert len(synth) == 2
    # every HANDLES source resolves to a node in this file (the synthetic
    # handler), so the handler is addressable and analyzable.
    node_qns = {f"{path.as_posix()}::{n.name}" for n in nodes}
    handles = [e for e in edges if e.kind == "HANDLES"]
    assert handles and all(e.source in node_qns for e in handles)


def test_inline_go_handler_gets_synthetic_node(tmp_path: Path) -> None:
    path = tmp_path / "srv.go"
    nodes, _ = CodeParser().parse_bytes(path, GO_INLINE)
    endpoints = {(n.extra["http_method"], n.extra["route"])
                 for n in nodes if n.kind == "Endpoint"}
    assert endpoints == {("ANY", "/x")}
    synth = [n for n in nodes
             if n.kind == "Function" and n.extra.get("synthetic_route_handler")]
    assert len(synth) == 1


OBJECT_CONFIG = b"""
function getWidget(context, req, res) { return res.ok(); }
router.get({ path: '/api/widget', validate: false }, getWidget);
router.post({ path: '/api/create' }, async (context, req, res) => res.ok());
"""

DJANGO = b"""
from django.urls import path, re_path

def user_list(request): return None
def user_detail(request): return None

urlpatterns = [
    path('users/', user_list, name='users'),
    re_path(r'^users/(?P<pk>[0-9]+)/$', user_detail),
]
"""

AIOHTTP_FLASK = b"""
async def handle(request): return None
def flask_view(): return None

app.router.add_get('/x', handle)
app.add_url_rule('/y', 'yname', flask_view)
"""


def test_object_config_routes(tmp_path: Path) -> None:
    nodes, _ = CodeParser().parse_bytes(tmp_path / "routes.ts", OBJECT_CONFIG)
    endpoints = {(n.extra["http_method"], n.extra["route"])
                 for n in nodes if n.kind == "Endpoint"}
    assert endpoints == {("GET", "/api/widget"), ("POST", "/api/create")}


def test_django_urlconf_routes(tmp_path: Path) -> None:
    path_obj = tmp_path / "urls.py"
    nodes, edges = CodeParser().parse_bytes(path_obj, DJANGO)
    routes = {n.extra["route"] for n in nodes if n.kind == "Endpoint"}
    assert routes == {"users/", "^users/(?P<pk>[0-9]+)/$"}
    sources = {e.source for e in edges if e.kind == "HANDLES"}
    assert sources == {
        f"{path_obj.as_posix()}::user_list",
        f"{path_obj.as_posix()}::user_detail",
    }


def test_python_add_routes(tmp_path: Path) -> None:
    nodes, _ = CodeParser().parse_bytes(tmp_path / "app.py", AIOHTTP_FLASK)
    endpoints = {(n.extra["http_method"], n.extra["route"])
                 for n in nodes if n.kind == "Endpoint"}
    assert ("GET", "/x") in endpoints
    assert ("ANY", "/y") in endpoints
