"""Tests for the standalone PyPI connectivity diagnostic."""

import ssl
from contextlib import nullcontext

from scripts import diagnose_pypi_connectivity


class _FakeTLSConnection:
    def version(self) -> str:
        return "TLSv1.2"


class _FakeTLSContext:
    def __init__(self) -> None:
        self.minimum_version = ssl.TLSVersion.TLSv1

    def wrap_socket(self, _socket, *, server_hostname: str):
        assert server_hostname == "pypi.org"
        return nullcontext(_FakeTLSConnection())


def test_direct_tls_probe_requires_tls_1_2_or_newer(monkeypatch):
    context = _FakeTLSContext()
    monkeypatch.setattr(
        diagnose_pypi_connectivity.ssl,
        "create_default_context",
        lambda: context,
    )
    monkeypatch.setattr(
        diagnose_pypi_connectivity.socket,
        "create_connection",
        lambda *_args, **_kwargs: nullcontext(object()),
    )

    assert diagnose_pypi_connectivity._try_tls_pypi() is True
    assert context.minimum_version is ssl.TLSVersion.TLSv1_2
