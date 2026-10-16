"""Windows source checkouts keep the pinned renderer usable offline."""
import base64
import hashlib
from unittest.mock import Mock

from code_review_graph import visualization


def test_crlf_checkout_normalization_keeps_original_sri(tmp_path, monkeypatch):
    asset = visualization.resources.files("code_review_graph") / "assets" / "d3.v7.min.js"
    original = asset.read_bytes().replace(b"\r\n", b"\n")
    leaf = Mock()
    leaf.__truediv__ = Mock(return_value=leaf)
    leaf.read_bytes.return_value = original.replace(b"\n", b"\r\n")
    monkeypatch.setattr(visualization.resources, "files", lambda _: leaf)
    output = visualization._write_d3_asset(tmp_path)
    assert output is not None
    data = output.read_bytes()
    digest = base64.b64encode(hashlib.sha384(data).digest()).decode()
    assert "sha384-" + digest == visualization.D3_SRI_HASH
    assert data == original


def test_normalization_does_not_accept_tampered_asset(tmp_path, monkeypatch):
    leaf = Mock()
    leaf.__truediv__ = Mock(return_value=leaf)
    leaf.read_bytes.return_value = b"tampered\r\n"
    monkeypatch.setattr(visualization.resources, "files", lambda _: leaf)
    assert visualization._write_d3_asset(tmp_path) is None
