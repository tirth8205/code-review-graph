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

import ipaddress

from starlette.middleware import Middleware
from starlette.types import ASGIApp, Receive, Scope, Send

#: Common spellings retained for callers that import this public constant.
#: Numeric validation itself uses :mod:`ipaddress` so all of 127.0.0.0/8 is
#: protected, not only 127.0.0.1.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})

_ALLOWED_ORIGIN_SCHEMES = frozenset({"http", "https"})
_FORBIDDEN_AUTHORITY_CHARS = frozenset("/\\?#@")


def is_loopback_host(host: str) -> bool:
    """Return ``True`` when ``host`` is a loopback bind address."""
    value = host.strip().lower()
    if value == "localhost":
        return True
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _normalize_port(value: str) -> str | None:
    """Return a canonical valid TCP port, or ``None`` when invalid."""
    if not value.isdigit():
        return None
    port = int(value)
    if not 0 <= port <= 65535:
        return None
    return str(port)


def split_host_port(value: str) -> tuple[str, str | None]:
    """Split a ``Host``/authority value into a lowercased host and optional port.

    Handles bracketed IPv6 literals (``[::1]:5555``) as well as the usual
    ``127.0.0.1:5555`` and bare ``localhost`` forms.

    Invalid authorities return ``("", None)``. In particular, bracketed IPv6
    must end after ``]`` or continue with exactly ``:<numeric-port>``.
    """
    value = value.strip()
    if (
        not value
        or any(char.isspace() for char in value)
        or any(char in _FORBIDDEN_AUTHORITY_CHARS for char in value)
    ):
        return "", None

    if value.startswith("["):
        closing = value.find("]")
        if closing <= 1:
            return "", None
        literal = value[1:closing]
        rest = value[closing + 1 :]
        try:
            address = ipaddress.ip_address(literal)
        except ValueError:
            return "", None
        if address.version != 6:
            return "", None
        if not rest:
            port = None
        elif rest.startswith(":"):
            port = _normalize_port(rest[1:])
            if port is None:
                return "", None
        else:
            return "", None
        return f"[{address.compressed}]", port

    if "[" in value or "]" in value or value.count(":") > 1:
        return "", None

    host, separator, raw_port = value.rpartition(":")
    if separator:
        if not host:
            return "", None
        port = _normalize_port(raw_port)
        if port is None:
            return "", None
    else:
        host = value
        port = None

    try:
        host = ipaddress.ip_address(host).compressed
    except ValueError:
        host = host.lower()
    return host, port


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

    def _authority_allowed(
        self,
        value: str | None,
        *,
        implicit_port: str | None = None,
    ) -> bool:
        if not value:
            return False
        host, port = split_host_port(value)
        effective_port = port if port is not None else implicit_port
        if effective_port is not None and effective_port != self.port:
            return False
        return is_loopback_host(host)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self.enabled or scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope["headers"]
        }

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
            if (
                not separator
                or scheme.lower() not in _ALLOWED_ORIGIN_SCHEMES
                or not self._authority_allowed(
                    authority,
                    implicit_port="443" if scheme.lower() == "https" else "80",
                )
            ):
                await self._forbid(send, "Forbidden: cross-origin request")
                return

        await self.app(scope, receive, send)

    @staticmethod
    async def _forbid(send: Send, message: str) -> None:
        body = message.encode()
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def build_http_middleware(host: str, port: int) -> list[Middleware]:
    """Return the ASGI middleware stack for the ``serve --http`` transport.

    Shared by the server entry point and the tests so both exercise the same
    configuration.
    """
    return [Middleware(LoopbackOriginGuard, host=host, port=port)]
