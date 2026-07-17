"""Concurrency regression tests for local embedding initialization (#610)."""

from __future__ import annotations

import builtins
import sys
import threading
from types import ModuleType
from typing import Any, Callable

import pytest

from code_review_graph import embeddings
from code_review_graph import main as crg_main


@pytest.fixture(autouse=True)
def _isolate_model_cache():
    """Keep the process-wide model cache deterministic across tests."""
    original = dict(embeddings._MODEL_CACHE)
    embeddings._MODEL_CACHE.clear()
    yield
    embeddings._MODEL_CACHE.clear()
    embeddings._MODEL_CACHE.update(original)


def _fake_sentence_transformers(
    constructor: Callable[..., Any],
) -> ModuleType:
    module = ModuleType("sentence_transformers")
    module.SentenceTransformer = constructor
    return module


def _run_in_thread(
    target: Callable[[], Any],
    results: list[Any],
    errors: list[BaseException],
) -> threading.Thread:
    def run() -> None:
        try:
            results.append(target())
        except BaseException as exc:  # noqa: BLE001 - captured for test assertion
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    return thread


def test_availability_import_and_model_load_do_not_overlap(monkeypatch):
    """All first-use dependency imports share one process-wide lock."""
    original_import = builtins.__import__
    first_import_entered = threading.Event()
    release_first_import = threading.Event()
    overlapping_import = threading.Event()
    state_lock = threading.Lock()
    active_imports = 0
    import_calls = 0
    model = object()
    fake_module = _fake_sentence_transformers(lambda *_args, **_kwargs: model)

    def tracked_import(name, globals=None, locals=None, fromlist=(), level=0):
        nonlocal active_imports, import_calls
        if name != "sentence_transformers":
            return original_import(name, globals, locals, fromlist, level)

        with state_lock:
            import_calls += 1
            active_imports += 1
            if active_imports > 1:
                overlapping_import.set()
            is_first = import_calls == 1
        if is_first:
            first_import_entered.set()
            release_first_import.wait(timeout=2)
        with state_lock:
            active_imports -= 1
        return fake_module

    monkeypatch.setattr(builtins, "__import__", tracked_import)
    results: list[Any] = []
    errors: list[BaseException] = []
    provider = embeddings.LocalEmbeddingProvider("test-model")

    availability_thread = _run_in_thread(
        embeddings._check_available, results, errors,
    )
    assert first_import_entered.wait(timeout=1)
    model_thread = _run_in_thread(provider._get_model, results, errors)

    overlap_seen = overlapping_import.wait(timeout=0.5)
    release_first_import.set()
    availability_thread.join(timeout=2)
    model_thread.join(timeout=2)

    assert not availability_thread.is_alive()
    assert not model_thread.is_alive()
    assert errors == []
    assert overlap_seen is False
    assert True in results
    assert model in results


def test_concurrent_first_model_calls_wait_construct_once_and_share(monkeypatch):
    """The losing caller waits and receives the first caller's model."""
    first_constructor_entered = threading.Event()
    release_constructor = threading.Event()
    duplicate_constructor = threading.Event()
    state_lock = threading.Lock()
    constructor_calls = 0
    constructed_models: list[object] = []

    def construct(_name: str, **_kwargs):
        nonlocal constructor_calls
        with state_lock:
            constructor_calls += 1
            call_number = constructor_calls
        if call_number == 1:
            first_constructor_entered.set()
        else:
            duplicate_constructor.set()
        release_constructor.wait(timeout=2)
        model = object()
        constructed_models.append(model)
        return model

    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        _fake_sentence_transformers(construct),
    )
    first = embeddings.LocalEmbeddingProvider("test-model")
    second = embeddings.LocalEmbeddingProvider("test-model")
    results: list[Any] = []
    errors: list[BaseException] = []

    first_thread = _run_in_thread(first._get_model, results, errors)
    assert first_constructor_entered.wait(timeout=1)
    second_thread = _run_in_thread(second._get_model, results, errors)

    duplicate_seen = duplicate_constructor.wait(timeout=0.5)
    release_constructor.set()
    first_thread.join(timeout=2)
    second_thread.join(timeout=2)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert errors == []
    assert duplicate_seen is False
    assert constructor_calls == 1
    assert len(constructed_models) == 1
    assert results == [constructed_models[0], constructed_models[0]]
    assert embeddings._MODEL_CACHE["test-model"] is constructed_models[0]


def test_failed_model_construction_is_not_cached_and_retry_succeeds(monkeypatch):
    """A failed attempt publishes nothing and the same provider can retry."""
    attempts = 0
    recovered_model = object()

    def construct(_name: str, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("model load failed")
        return recovered_model

    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        _fake_sentence_transformers(construct),
    )
    provider = embeddings.LocalEmbeddingProvider("flaky-model")

    with pytest.raises(RuntimeError, match="model load failed"):
        provider._get_model()

    assert provider._model is None
    assert "flaky-model" not in embeddings._MODEL_CACHE
    assert provider._get_model() is recovered_model
    assert provider._model is recovered_model
    assert embeddings._MODEL_CACHE["flaky-model"] is recovered_model
    assert attempts == 2


def test_model_cache_remains_scoped_by_model_name(monkeypatch):
    """Serializing initialization must not mix distinct model identities."""
    constructed: dict[str, object] = {}

    def construct(name: str, **_kwargs):
        model = object()
        constructed[name] = model
        return model

    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        _fake_sentence_transformers(construct),
    )

    alpha = embeddings.LocalEmbeddingProvider("alpha")._get_model()
    beta = embeddings.LocalEmbeddingProvider("beta")._get_model()
    alpha_again = embeddings.LocalEmbeddingProvider("alpha")._get_model()

    assert alpha is constructed["alpha"]
    assert beta is constructed["beta"]
    assert alpha is not beta
    assert alpha_again is alpha
    assert set(embeddings._MODEL_CACHE) == {"alpha", "beta"}


def test_posix_server_start_does_not_prewarm_local_embeddings(monkeypatch, tmp_path):
    """Unused local embeddings impose no model import/load cost on POSIX."""
    events: list[str] = []
    monkeypatch.delenv("CRG_TOOLS", raising=False)
    monkeypatch.setattr(crg_main, "_default_repo_root", None)
    monkeypatch.setattr(crg_main.sys, "platform", "linux")
    monkeypatch.setattr(
        embeddings,
        "prewarm_local_embeddings",
        lambda: events.append("prewarm"),
    )
    monkeypatch.setattr(
        crg_main.mcp,
        "run",
        lambda **_kwargs: events.append("run"),
    )

    crg_main.main(repo_root=str(tmp_path))

    assert events == ["run"]


def test_windows_server_still_prewarms_before_mcp_run(monkeypatch, tmp_path):
    """Windows retains main-thread prewarm for its worker-thread deadlock."""
    events: list[str] = []
    policy = object()
    monkeypatch.delenv("CRG_TOOLS", raising=False)
    monkeypatch.setattr(crg_main, "_default_repo_root", None)
    monkeypatch.setattr(crg_main.sys, "platform", "win32")
    monkeypatch.setattr(
        crg_main.asyncio,
        "WindowsSelectorEventLoopPolicy",
        lambda: policy,
        raising=False,
    )
    monkeypatch.setattr(
        crg_main.asyncio,
        "set_event_loop_policy",
        lambda value: events.append("policy") if value is policy else None,
    )
    monkeypatch.setattr(
        embeddings,
        "prewarm_local_embeddings",
        lambda: events.append("prewarm"),
    )
    monkeypatch.setattr(
        crg_main.mcp,
        "run",
        lambda **_kwargs: events.append("run"),
    )

    crg_main.main(repo_root=str(tmp_path))

    assert events == ["policy", "prewarm", "run"]
