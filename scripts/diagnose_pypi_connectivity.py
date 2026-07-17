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
import time
import socket
import ssl
import sys
import urllib.error
import urllib.request

# Constants pulled out so they're easy to tweak/reuse across the two checks.
PYPI_HOST = "pypi.org"
PYPI_PORT = 443
CONNECT_TIMEOUT_SECONDS = 15
DOWNLOAD_TIMEOUT_SECONDS = 30
SIMPLE_INDEX_URL = "https://pypi.org/simple/hatchling/"
USER_AGENT = "code-review-graph-diagnostic/1.0"


def main() -> int:
    """Run both connectivity checks and report a pass/fail summary.

    Returns 0 if both checks succeed, 1 otherwise (suitable as a process
    exit code via `raise SystemExit(main())`).
    """
    tls_ok = check_raw_tls_handshake()
    http_ok = check_https_download()

    if tls_ok and http_ok:
        print("PyPI check: OK (this Python can use HTTPS to pypi.org).")
        return 0

    print("PyPI check: FAILED (pip/pipx may be unable to download build deps like hatchling).")
    print("Workaround: from the repo root, with https://github.com/astral-sh/uv installed:")
    print('  uv tool install . --force')
    print("Or run pipx from macOS Terminal.app (outside the IDE) if the failure is terminal-specific.")
    return 1


def check_raw_tls_handshake() -> bool:
    """Open a raw TCP socket to pypi.org:443 and perform a TLS handshake.

    This isolates low-level connectivity/TLS problems (e.g. broken system
    certs, network-level blocks) from anything urllib-specific, since it
    doesn't go through urllib's request machinery at all.

    Returns True if the handshake succeeds, False on any OSError.
    """
    try:
        ssl_context = ssl.create_default_context()
        with socket.create_connection(
            (PYPI_HOST, PYPI_PORT), timeout=CONNECT_TIMEOUT_SECONDS
        ) as raw_socket:
            with ssl_context.wrap_socket(
                raw_socket, server_hostname=PYPI_HOST
            ) as tls_socket:
                # tls_socket.version() returns a truthy string (e.g. "TLSv1.3")
                # once the handshake has completed successfully.
                return bool(tls_socket.version())
    except OSError as error:
        print(f"  TLS {PYPI_HOST}:{PYPI_PORT} -> {error!r}", file=sys.stderr)
        return False


def check_https_download() -> bool:
    """Fetch a small chunk of PyPI's simple index for hatchling via urllib.

    This mirrors the actual path pip/pipx take when resolving build
    dependencies, so a failure here is a strong signal that installs will
    fail too (even if the raw TLS check above succeeded).

    Returns True if the request succeeds, False on any URL/OS error.
    """
    try:
        request = urllib.request.Request(
            SIMPLE_INDEX_URL,
            headers={"User-Agent": USER_AGENT},
        )
        with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
            response.read(256)  # Just enough to confirm data is flowing.
        return True
    except (urllib.error.URLError, OSError) as error:
        print(f"  urllib hatchling index -> {error!r}", file=sys.stderr)
        return False


if __name__ == "__main__":
    raise SystemExit(main())