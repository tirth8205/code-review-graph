"""End-to-end tests for the ``serve --http`` Host/Origin guard.

These exercise the real FastMCP ASGI application with the real middleware, and
assert the kwargs the server entry point passes are actually accepted by the
installed FastMCP. A mock of ``mcp.run`` cannot catch a keyword the pinned
FastMCP does not accept, so the signature contract is asserted explicitly.
"""

from __future__ import annotations

import inspect

import pytest
from fastmcp import FastMCP
from starlette.testclient import TestClient

from code_review_graph.http_origin_guard import (
    LoopbackOriginGuard,
    build_http_middleware,
    is_loopback_host,
    split_host_port,
)

HOST = "127.0.0.1"
PORT = 5555
BASE_URL = f"http://{HOST}:{PORT}"
MCP_PATH = "/mcp/"
# streamable-http requires both content types; without them FastMCP answers 406
# before dispatching, which is still proof the request passed the guard.
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.fixture(scope="module")
def client() -> TestClient:
    """A test client over the real FastMCP app wrapped in the real guard."""
    mcp: FastMCP = FastMCP("guard-test")

    @mcp.tool
    def ping() -> str:  # pragma: no cover - registered so the app has a tool
        return "pong"

    app = mcp.http_app(middleware=build_http_middleware(HOST, PORT))
    with TestClient(app, base_url=BASE_URL) as test_client:
        yield test_client


def _post(client: TestClient, **kwargs) -> int:
    return client.post(
        MCP_PATH, headers={**MCP_HEADERS, **kwargs.pop("headers", {})}, json={}, **kwargs
    ).status_code


class TestGuardEndToEnd:
    """Foreign Origin is rejected; same-Origin and no-Origin clients still work."""

    def test_foreign_origin_is_rejected(self, client: TestClient) -> None:
        assert _post(client, headers={"Origin": "http://evil.example"}) == 403

    def test_foreign_origin_over_https_is_rejected(self, client: TestClient) -> None:
        assert _post(client, headers={"Origin": "https://evil.example"}) == 403

    def test_same_origin_is_allowed(self, client: TestClient) -> None:
        assert _post(client, headers={"Origin": BASE_URL}) != 403

    def test_localhost_origin_is_allowed(self, client: TestClient) -> None:
        assert _post(client, headers={"Origin": f"http://localhost:{PORT}"}) != 403

    def test_no_origin_client_is_allowed(self, client: TestClient) -> None:
        """Ordinary (non-browser) MCP clients send no Origin at all."""
        assert _post(client) != 403

    def test_rebound_host_is_rejected(self, client: TestClient) -> None:
        """DNS rebinding arrives on loopback but carries the attacker's Host."""
        assert _post(client, headers={"Host": "evil.example"}) == 403

    def test_rebound_host_with_matching_port_is_rejected(self, client: TestClient) -> None:
        assert _post(client, headers={"Host": f"evil.example:{PORT}"}) == 403

    def test_origin_on_wrong_port_is_rejected(self, client: TestClient) -> None:
        assert _post(client, headers={"Origin": f"http://127.0.0.1:{PORT + 1}"}) == 403

    def test_origin_with_implicit_wrong_port_is_rejected(
        self,
        client: TestClient,
    ) -> None:
        assert _post(client, headers={"Origin": "http://127.0.0.1"}) == 403
        assert _post(client, headers={"Origin": "https://localhost"}) == 403

    def test_non_http_origin_scheme_is_rejected(self, client: TestClient) -> None:
        assert _post(client, headers={"Origin": "file://"}) == 403

    def test_other_ipv4_loopback_bind_is_guarded(self) -> None:
        """Every address in 127.0.0.0/8 is loopback, not only 127.0.0.1."""
        mcp: FastMCP = FastMCP("alternate-loopback-guard-test")
        host = "127.0.0.2"
        app = mcp.http_app(middleware=build_http_middleware(host, PORT))
        with TestClient(app, base_url=f"http://{host}:{PORT}") as test_client:
            assert (
                _post(
                    test_client,
                    headers={"Origin": "http://evil.example"},
                )
                == 403
            )


class TestFastMcpSignatureContract:
    """The kwargs the entry point passes must exist on the installed FastMCP.

    This is the regression guard for the failure a mocked ``mcp.run`` hides: a
    keyword the pinned FastMCP rejects raises ``TypeError`` at startup.
    """

    def test_run_http_async_accepts_the_kwargs_we_pass(self) -> None:
        signature = inspect.signature(FastMCP.run_http_async)
        # ``run`` forwards **transport_kwargs straight through to this method.
        signature.bind_partial(
            None,
            transport="streamable-http",
            host=HOST,
            port=PORT,
            middleware=build_http_middleware(HOST, PORT),
        )

    def test_middleware_is_a_supported_parameter(self) -> None:
        assert "middleware" in inspect.signature(FastMCP.run_http_async).parameters


class TestGuardDisabledForNonLoopbackBinds:
    """Binding off-loopback is an explicit exposure; the guard steps aside."""

    def test_guard_is_disabled_when_bound_to_all_interfaces(self) -> None:
        guard = LoopbackOriginGuard(lambda *_: None, host="0.0.0.0", port=PORT)
        assert guard.enabled is False

    def test_guard_is_enabled_for_loopback(self) -> None:
        for host in ("127.0.0.1", "localhost", "::1"):
            assert LoopbackOriginGuard(lambda *_: None, host=host, port=PORT).enabled


class TestHelpers:
    def test_split_host_port_handles_ipv6_and_bare_hosts(self) -> None:
        assert split_host_port("127.0.0.1:5555") == ("127.0.0.1", "5555")
        assert split_host_port("localhost") == ("localhost", None)
        assert split_host_port("[::1]:5555") == ("[::1]", "5555")
        assert split_host_port("[::1]") == ("[::1]", None)

    def test_split_host_port_rejects_trailing_data_after_ipv6(self) -> None:
        assert split_host_port("[::1]evil") == ("", None)
        assert split_host_port("[::1]:5555evil") == ("", None)

    def test_is_loopback_host(self) -> None:
        assert is_loopback_host("127.0.0.1")
        assert is_loopback_host("127.0.0.2")
        assert is_loopback_host("LocalHost")
        assert not is_loopback_host("0.0.0.0")
        assert not is_loopback_host("evil.example")
