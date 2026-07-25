"""Host/Origin validation for the opt-in ``serve --http`` MCP endpoint.

``code-review-graph serve --http`` starts a FastMCP streamable-http server bound
to loopback (127.0.0.1:5555 by default). A loopback bind is not by itself an
access control for a browser: a page the user visits can point a hostname it
controls at 127.0.0.1 (DNS rebinding) and then drive the MCP tools, which read
the user's source tree.

The defense is to check the two headers the browser controls but cannot forge
away:

* ``Host`` — a rebound request arrives with the attacker's hostname, not
  ``127.0.0.1``/``localhost``, so an allow-list on ``Host`` rejects it.
* ``Origin`` — cross-site requests carry the initiating site's origin. Ordinary
  MCP clients are not browsers and send no ``Origin`` at all, so requiring the
  origin (when present) to be the loopback endpoint costs them nothing.

The guard is a **pure ASGI** middleware rather than a
``starlette.middleware.base.BaseHTTPMiddleware`` subclass: streamable-http keeps
long-lived streaming/SSE responses open, and ``BaseHTTPMiddleware`` buffers
through an anyio task pair that interferes with them.

It is applied only when the server is bound to a loopback address — the default.
Binding elsewhere (``--host 0.0.0.0``) is an explicit decision to expose the
endpoint, where the operator's own hostnames are legitimate, so the guard steps
aside rather than second-guessing that choice.
"""

from __future__ import annotations

from starlette.middleware import Middleware
from starlette.types import ASGIApp, Receive, Scope, Send

#: Host names that mean "this machine" for a loopback-bound server.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})

_ALLOWED_ORIGIN_SCHEMES = frozenset({"http", "https"})


def is_loopback_host(host: str) -> bool:
    """Return ``True`` when ``host`` is a loopback bind address."""
    return host.strip().lower() in LOOPBACK_HOSTS


def split_host_port(value: str) -> tuple[str, str | None]:
    """Split a ``Host``/authority value into a lowercased host and optional port.

    Handles bracketed IPv6 literals (``[::1]:5555``) as well as the usual
    ``127.0.0.1:5555`` and bare ``localhost`` forms.
    """
    value = value.strip()
    if value.startswith("["):
        host, _, rest = value.partition("]")
        port = rest[1:] if rest.startswith(":") else ""
        return f"{host}]".lower(), port or None
    host, separator, port = value.rpartition(":")
    if separator and port.isdigit():
        return host.lower(), port
    return value.lower(), None


class LoopbackOriginGuard:
    """Reject cross-origin and rebound-``Host`` requests to a loopback server.

    Args:
        app: The wrapped ASGI application.
        host: The address the server is bound to.
        port: The port the server is bound to.
    """

    def __init__(self, app: ASGIApp, *, host: str, port: int) -> None:
        self.app = app
        self.enabled = is_loopback_host(host)
        self.port = str(port)
        self.allowed_hosts = LOOPBACK_HOSTS

    def _authority_allowed(self, value: str | None) -> bool:
        if not value:
            return False
        host, port = split_host_port(value)
        if port is not None and port != self.port:
            return False
        return host in self.allowed_hosts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self.enabled or scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {key.decode("latin-1").lower(): value.decode("latin-1")
                   for key, value in scope["headers"]}

        # DNS rebinding: the browser resolves an attacker-controlled name to
        # 127.0.0.1 but still sends that name in Host.
        if not self._authority_allowed(headers.get("host")):
            await self._forbid(send, "Forbidden: unrecognized Host header")
            return

        # Cross-site browser requests carry Origin; non-browser MCP clients omit
        # it, so an absent Origin is not treated as suspicious.
        origin = headers.get("origin")
        if origin is not None:
            scheme, separator, authority = origin.partition("://")
            if (not separator
                    or scheme.lower() not in _ALLOWED_ORIGIN_SCHEMES
                    or not self._authority_allowed(authority)):
                await self._forbid(send, "Forbidden: cross-origin request")
                return

        await self.app(scope, receive, send)

    @staticmethod
    async def _forbid(send: Send, message: str) -> None:
        body = message.encode()
        await send({
            "type": "http.response.start",
            "status": 403,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})


def build_http_middleware(host: str, port: int) -> list[Middleware]:
    """Return the ASGI middleware stack for the ``serve --http`` transport.

    Shared by the server entry point and the tests so both exercise the same
    configuration.
    """
    return [Middleware(LoopbackOriginGuard, host=host, port=port)]
