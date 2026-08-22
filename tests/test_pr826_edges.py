"""Edge-case tests for the UTF-8 stdio reconfiguration done at CLI startup.

Covers stream shapes the happy-path regression test does not: absent
streams (pythonw), streams without ``encoding`` or ``reconfigure``,
closed and detached wrappers, UTF-8 spelling variants, idempotency,
buffered-data preservation across the implicit flush, non-BMP output,
and a real subprocess honoring ``PYTHONIOENCODING=cp1252``.
"""

import io
import os
import subprocess
import sys

from code_review_graph import cli


def _legacy_stream(encoding: str = "cp1252", **kwargs):
    raw = io.BytesIO()
    return raw, io.TextIOWrapper(raw, encoding=encoding, **kwargs)


class _RecordingStream:
    """Minimal stream double that records reconfigure calls."""

    def __init__(self, encoding):
        self.encoding = encoding
        self.calls = []

    def write(self, text):
        return len(text)

    def reconfigure(self, **kwargs):
        self.calls.append(kwargs)


class _FailingReconfigureStream:
    def __init__(self, exc):
        self.encoding = "cp1252"
        self._exc = exc

    def write(self, text):
        return len(text)

    def reconfigure(self, **kwargs):
        raise self._exc


def test_none_streams_do_not_crash(monkeypatch):
    """pythonw-style None streams must be skipped and main() must survive."""
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(sys, "argv", ["code-review-graph"])
    cli._configure_utf8_stdio()
    cli.main()  # print() to a None stdout is a silent no-op


def test_stream_with_none_encoding_is_left_alone(monkeypatch):
    """StringIO reports encoding=None; banner must still print."""
    fake_out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", fake_out)
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    monkeypatch.setattr(sys, "argv", ["code-review-graph"])
    cli.main()
    assert "code-review-graph" in fake_out.getvalue()
    assert fake_out.encoding is None


def test_legacy_stream_without_reconfigure_is_untouched(monkeypatch):
    """No reconfigure method: keep the stream as-is without raising."""

    class _Plain:
        encoding = "cp1252"

        def write(self, text):
            return len(text)

    plain = _Plain()
    monkeypatch.setattr(sys, "stdout", plain)
    monkeypatch.setattr(sys, "stderr", plain)
    cli._configure_utf8_stdio()
    assert plain.encoding == "cp1252"


def test_reconfigure_failures_are_swallowed(monkeypatch):
    for exc in (
        io.UnsupportedOperation("boom"),
        OSError("boom"),
        ValueError("boom"),
    ):
        failing = _FailingReconfigureStream(exc)
        monkeypatch.setattr(sys, "stdout", failing)
        monkeypatch.setattr(sys, "stderr", failing)
        cli._configure_utf8_stdio()  # must not raise


def test_closed_stream_is_swallowed(monkeypatch):
    _, wrapper = _legacy_stream()
    wrapper.close()
    monkeypatch.setattr(sys, "stdout", wrapper)
    monkeypatch.setattr(sys, "stderr", wrapper)
    cli._configure_utf8_stdio()  # reconfigure raises ValueError internally
    assert wrapper.encoding == "cp1252"


def test_detached_stream_is_swallowed(monkeypatch):
    _, wrapper = _legacy_stream()
    wrapper.detach()
    monkeypatch.setattr(sys, "stdout", wrapper)
    monkeypatch.setattr(sys, "stderr", wrapper)
    cli._configure_utf8_stdio()  # must not raise


def test_utf8_spelling_variants_skip_reconfigure(monkeypatch):
    """Any common spelling of UTF-8 must not trigger a reconfigure."""
    for spelling in ("utf-8", "UTF-8", "utf8", "UTF8", "Utf-8"):
        probe = _RecordingStream(spelling)
        monkeypatch.setattr(sys, "stdout", probe)
        monkeypatch.setattr(sys, "stderr", probe)
        cli._configure_utf8_stdio()
        assert probe.calls == [], spelling


def test_double_configure_is_idempotent(monkeypatch):
    raw, wrapper = _legacy_stream()
    monkeypatch.setattr(sys, "stdout", wrapper)
    monkeypatch.setattr(sys, "stderr", wrapper)
    cli._configure_utf8_stdio()
    cli._configure_utf8_stdio()
    assert wrapper.encoding == "utf-8"
    wrapper.write("\U0001f680─│●")
    wrapper.flush()
    assert raw.getvalue().decode("utf-8") == "\U0001f680─│●"


def test_buffered_output_is_flushed_not_lost(monkeypatch):
    """Text written before the switch is flushed in the old encoding."""
    raw, wrapper = _legacy_stream()
    wrapper.write("caf\xe9")  # encodable in cp1252, still buffered
    monkeypatch.setattr(sys, "stdout", wrapper)
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    cli._configure_utf8_stdio()
    wrapper.write("●")
    wrapper.flush()
    assert raw.getvalue() == "caf\xe9".encode("cp1252") + "●".encode()


def test_ascii_stream_is_upgraded_and_banner_prints(monkeypatch):
    raw, wrapper = _legacy_stream(encoding="ascii")
    raw_err, wrapper_err = _legacy_stream(encoding="ascii")
    monkeypatch.setattr(sys, "stdout", wrapper)
    monkeypatch.setattr(sys, "stderr", wrapper_err)
    monkeypatch.setattr(sys, "argv", ["code-review-graph"])
    cli.main()
    wrapper.flush()
    out = raw.getvalue().decode("utf-8")
    assert "●──●──●" in out  # box art
    assert "Commands:" in out


def test_errors_replace_stream_round_trips_exactly(monkeypatch):
    """errors='replace' legacy stream must emit exact UTF-8 after switch."""
    raw, wrapper = _legacy_stream(errors="replace")
    monkeypatch.setattr(sys, "stdout", wrapper)
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    cli._configure_utf8_stdio()
    wrapper.write("◆\U0001f9ea")
    wrapper.flush()
    assert raw.getvalue().decode("utf-8") == "◆\U0001f9ea"


def test_stdin_is_never_touched(monkeypatch):
    raw_in = io.BytesIO(b"data")
    stdin = io.TextIOWrapper(raw_in, encoding="cp1252")
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    cli._configure_utf8_stdio()
    assert stdin.encoding == "cp1252"


def test_subprocess_with_pythonioencoding_cp1252_renders_banner():
    """Real process: PYTHONIOENCODING=cp1252 must not crash the banner."""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "cp1252"
    env["NO_COLOR"] = "1"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from code_review_graph.cli import main; "
            "sys.argv = ['code-review-graph']; main()",
        ],
        capture_output=True,
        env=env,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    out = result.stdout.decode("utf-8")
    assert "●──●──●" in out
    assert "Commands:" in out


def test_subprocess_version_flag_with_ascii_encoding():
    """--version path also survives a pure-ASCII stdio configuration."""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "ascii"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from code_review_graph.cli import main; "
            "sys.argv = ['code-review-graph', '--version']; main()",
        ],
        capture_output=True,
        env=env,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert b"code-review-graph" in result.stdout
