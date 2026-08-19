"""Edge-case guards for the google-genai packaging switch (PR #856 / issue #534).

All tests are offline and deterministic: they inspect packaging metadata
(pyproject.toml, uv.lock) and force the ImportError path without needing
google-genai installed or absent.
"""

import builtins
import sys
from pathlib import Path

import pytest

try:
    import tomllib
except ImportError:  # pragma: no cover - Python 3.10
    import tomli as tomllib


ROOT = Path(__file__).parents[1]


def _pyproject() -> dict:
    with (ROOT / "pyproject.toml").open("rb") as f:
        return tomllib.load(f)


def _lock() -> dict:
    with (ROOT / "uv.lock").open("rb") as f:
        return tomllib.load(f)


def test_all_extra_covers_every_optional_group():
    """[all] must reference every optional group except dev, per README's
    "All optional dependencies" claim (issue #534)."""
    optional = _pyproject()["project"]["optional-dependencies"]
    referenced = {
        req.split("[", 1)[1].rstrip("]")
        for req in optional["all"]
        if req.startswith("code-review-graph[")
    }
    expected = set(optional) - {"all", "dev"}
    assert referenced == expected


def test_all_extra_contains_only_self_referential_extras():
    """Every entry in [all] must be a self-referential extra so pip/uv
    resolve it against this same distribution."""
    optional = _pyproject()["project"]["optional-dependencies"]
    for req in optional["all"]:
        assert req.startswith("code-review-graph["), req
        assert req.endswith("]"), req


def test_old_google_sdk_fully_evicted_from_lock():
    """The deprecated SDK and its exclusive transitive tree must be gone."""
    names = {pkg["name"] for pkg in _lock()["package"]}
    for stale in (
        "google-generativeai",
        "google-ai-generativelanguage",
        "google-api-python-client",
        "google-api-core",
        "grpcio",
        "grpcio-status",
        "proto-plus",
    ):
        assert stale not in names, f"{stale} should no longer be locked"
    assert "google-genai" in names


def test_lock_pins_google_genai_within_pyproject_bounds():
    lock_pkgs = {pkg["name"]: pkg for pkg in _lock()["package"]}
    version = lock_pkgs["google-genai"]["version"]
    major = int(version.split(".", 1)[0])
    assert 1 <= major < 3, version


def test_lock_registry_sources_are_pypi_only():
    """Supply-chain guard: every locked package must come from pypi.org."""
    for pkg in _lock()["package"]:
        source = pkg.get("source", {})
        registry = source.get("registry")
        if registry is not None:
            assert registry == "https://pypi.org/simple", (
                pkg["name"],
                registry,
            )


def test_lock_requires_dist_matches_pyproject():
    """The lock's recorded requires-dist must agree with pyproject on both
    the SDK swap and the [all] extra addition."""
    lock = _lock()
    crg = next(
        pkg
        for pkg in lock["package"]
        if pkg["name"] == "code-review-graph"
    )
    requires = crg["metadata"]["requires-dist"]
    google = [r for r in requires if r["name"] == "google-genai"]
    assert len(google) == 1
    assert google[0]["marker"] == "extra == 'google-embeddings'"
    assert google[0]["specifier"] == ">=1.0.0,<3"
    assert not any(r["name"] == "google-generativeai" for r in requires)
    all_extras = [
        r["extras"]
        for r in requires
        if r["name"] == "code-review-graph" and r.get("marker") == "extra == 'all'"
    ]
    assert ["google-embeddings"] in all_extras


def test_import_error_names_current_sdk_and_quotes_extra(monkeypatch):
    """When google-genai is missing, the guidance must name the current SDK
    and quote the extra so zsh-style shells do not glob the brackets."""
    from code_review_graph.embeddings import GoogleEmbeddingProvider

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "google" or name.startswith("google."):
            raise ImportError(f"blocked: {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "google", raising=False)
    monkeypatch.delitem(sys.modules, "google.genai", raising=False)
    monkeypatch.setattr(builtins, "__import__", blocked)

    with pytest.raises(ImportError) as excinfo:
        GoogleEmbeddingProvider(api_key="k")

    message = str(excinfo.value)
    assert "google-genai" in message
    assert "google-generativeai" not in message.replace("google-genai", "")
    assert '"code-review-graph[google-embeddings]"' in message


def test_no_stale_sdk_references_outside_lock():
    """No source, docs, or config file may still name the deprecated SDK."""
    stale_hits = []
    for pattern in ("*.py", "*.toml", "*.md", "*.yml", "*.yaml"):
        for path in ROOT.rglob(pattern):
            parts = path.relative_to(ROOT).parts
            if parts[0] in {".git", ".venv", "node_modules", ".claude"}:
                continue
            if " 2." in path.name or path.name == "test_pr856_edges.py":
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if "google-generativeai" in text or "google_generativeai" in text:
                stale_hits.append(str(path.relative_to(ROOT)))
    assert stale_hits == []
