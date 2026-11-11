"""Let the stdio test clients run on both majors of the ``mcp`` SDK.

fastmcp 3 brings the ``mcp`` SDK 1.x and fastmcp 4 brings 2.x (#970). The
tests that drive a real server through ``ClientSession`` hit three changes:

- 1.x takes ``read_timeout_seconds`` as a ``timedelta``; 2.x takes seconds.
- 1.x names the error class ``McpError``; 2.x renamed it ``MCPError``.
- 1.x model fields carry the wire names (``isError``, ``inputSchema``); 2.x
  uses snake case (``is_error``, ``input_schema``). fastmcp 4 adds the old
  names back with a warning, but only once fastmcp is imported, so a test
  process that never imports it gets ``AttributeError``.

A test that imported the old name skipped itself as "mcp client library not
installed" on 2.x, so the end-to-end checks went quiet instead of failing.
"""

from __future__ import annotations

import re
from datetime import timedelta
from importlib.metadata import version
from typing import Any

MCP_SDK_MAJOR = int(version("mcp").split(".")[0])

try:
    from mcp.shared.exceptions import MCPError as McpError
except ImportError:
    from mcp.shared.exceptions import McpError  # type: ignore[no-redef]


def read_timeout(seconds: float) -> Any:
    """The ``read_timeout_seconds`` value the installed SDK accepts."""
    if MCP_SDK_MAJOR < 2:
        return timedelta(seconds=seconds)
    return seconds


def wire(model: Any, name: str) -> Any:
    """Read the field the MCP wire calls ``name`` (camel case) on either SDK."""
    if MCP_SDK_MAJOR < 2:
        return getattr(model, name)
    return getattr(model, re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower())


__all__ = ["MCP_SDK_MAJOR", "McpError", "read_timeout", "wire"]
