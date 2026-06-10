#!/usr/bin/env python3
"""Check whether this Python can reach PyPI (same path pip/pipx use for hatchling, etc.).

If TLS to pypi.org fails (e.g. Errno 9 in some IDE terminals), a user-wide
install from a git checkout may still work via uv (different downloader):

  uv tool install /path/to/code-review-graph --force

Run: python3 scripts/diagnose_pypi_connectivity.py
"""
from __future__ import annotations

import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request


def main() -> int:
    ok_tls = _try_tls_pypi()
    ok_url = _try_urllib()
    if ok_tls and ok_url:
        print("PyPI check: OK (this Python can use HTTPS to pypi.org).")
        return 0
    print("PyPI check: FAILED (pip/pipx may be unable to download build deps like hatchling).")
    print("Workaround: from the repo root, with https://github.com/astral-sh/uv installed:")
    print('  uv tool install . --force')
    print("Or run pipx from macOS Terminal.app (outside the IDE) if the failure is terminal-specific.")
    return 1


def _try_tls_pypi() -> bool:
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection(("pypi.org", 443), timeout=15) as sock:
            with ctx.wrap_socket(sock, server_hostname="pypi.org") as tsock:
                return bool(tsock.version())
    except OSError as e:
        print(f"  TLS pypi.org:443 -> {e!r}", file=sys.stderr)
        return False


def _try_urllib() -> bool:
    try:
        req = urllib.request.Request(
            "https://pypi.org/simple/hatchling/",
            headers={"User-Agent": "code-review-graph-diagnostic/1.0"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read(256)
        return True
    except (urllib.error.URLError, OSError) as e:
        print(f"  urllib hatchling index -> {e!r}", file=sys.stderr)
        return False


if __name__ == "__main__":
    raise SystemExit(main())
