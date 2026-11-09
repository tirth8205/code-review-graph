"""Shared text-stream setup for the CLI entry points."""

import sys


def configure_utf8_stdio() -> None:
    """Allow Unicode paths and log text on streams using a legacy encoding."""
    for stream in (sys.stdout, sys.stderr):
        encoding = getattr(stream, "encoding", None)
        if not encoding or encoding.lower().replace("-", "") == "utf8":
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            pass
