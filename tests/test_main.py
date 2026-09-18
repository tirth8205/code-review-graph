"""Tests for the MCP server entry point.

Focused on the ``_resolve_repo_root`` helper that threads the
``serve --repo <X>`` CLI flag into every tool wrapper, and on the
set of tools that must be registered as async coroutines so the MCP
stdio event loop stays responsive during long-running operations.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import textwrap
import threading
import time

import pytest

import code_review_graph.incremental as incremental_module
import code_review_graph.tools.docs as docs_module
from code_review_graph import main as crg_main
from code_review_graph.http_origin_guard import LoopbackOriginGuard


@pytest.fixture(autouse=True)
def _isolate_crg_tools_env(monkeypatch):
    """Always strip CRG_TOOLS so that any test invoking ``crg_main.main``
    does not accidentally permanently shrink the global tool registry
    when the suite runs under a developer environment that exports
    ``CRG_TOOLS``.  Without this the snapshot/restore in
    ``TestApplyToolFilter._restore_tools`` only sees the already-filtered
    set and cannot restore the dropped tools."""
    monkeypatch.delenv("CRG_TOOLS", raising=False)


class TestResolveRepoRoot:
    """Precedence rules for _resolve_repo_root (see #222 follow-up)."""

    @pytest.fixture(autouse=True)
    def _reset_default(self):
        """Save and restore the module-level default before/after each test."""
        original = crg_main._default_repo_root
        yield
        crg_main._default_repo_root = original

    def test_none_when_neither_is_set(self):
        crg_main._default_repo_root = None
        assert crg_main._resolve_repo_root(None) is None

    def test_empty_string_treated_as_unset(self):
        """Empty string from an MCP client should not shadow the --repo flag."""
        crg_main._default_repo_root = "/tmp/flag-repo"
        assert crg_main._resolve_repo_root("") == "/tmp/flag-repo"

    def test_flag_used_when_client_omits_repo_root(self):
        crg_main._default_repo_root = "/tmp/flag-repo"
        assert crg_main._resolve_repo_root(None) == "/tmp/flag-repo"

    def test_client_arg_wins_over_flag(self):
        crg_main._default_repo_root = "/tmp/flag-repo"
        assert crg_main._resolve_repo_root("/explicit") == "/explicit"

    def test_client_arg_used_when_no_flag(self):
        crg_main._default_repo_root = None
        assert crg_main._resolve_repo_root("/explicit") == "/explicit"


def test_docs_wrapper_falls_back_to_packaged_docs_with_resolved_repo(
    tmp_path, monkeypatch,
):
    """The server's resolved repo must not hide wheel-packaged docs."""
    package_dir = tmp_path / "site-packages" / "code_review_graph"
    tools_dir = package_dir / "tools"
    docs_dir = package_dir / "docs"
    tools_dir.mkdir(parents=True)
    docs_dir.mkdir()
    (docs_dir / "LLM-OPTIMIZED-REFERENCE.md").write_text(
        '<section name="usage">packaged docs</section>\n',
        encoding="utf-8",
    )

    repo_root = tmp_path / "repo"
    (repo_root / ".code-review-graph").mkdir(parents=True)
    monkeypatch.setattr(docs_module, "__file__", str(tools_dir / "docs.py"))
    monkeypatch.setattr(crg_main, "_default_repo_root", str(repo_root))
    tool = getattr(crg_main.get_docs_section_tool, "fn", None)
    get_docs = tool or crg_main.get_docs_section_tool

    result = get_docs(section_name="usage")

    assert result["status"] == "ok"
    assert result["content"] == "packaged docs"


class TestServeMainTransport:
    """``main()`` wires FastMCP to stdio or Streamable HTTP."""

    def test_stdio_calls_mcp_run_stdio(self, monkeypatch):
        calls: list[dict] = []

        def fake_run(**kwargs):
            assert incremental_module._MCP_STDIO_ACTIVE is True
            assert incremental_module._select_executor_kind() == "thread"
            calls.append(kwargs)

        monkeypatch.delenv("CRG_PARSE_EXECUTOR", raising=False)
        monkeypatch.setattr(
            incremental_module, "_MCP_STDIO_ACTIVE", False, raising=False,
        )
        monkeypatch.setattr(crg_main.mcp, "run", fake_run)
        crg_main.main(repo_root=None)
        assert calls == [{"transport": "stdio", "show_banner": False}]
        assert incremental_module._MCP_STDIO_ACTIVE is False

    def test_http_calls_mcp_run_with_host_port(self, monkeypatch):
        calls: list[dict] = []

        def fake_run(**kwargs):
            assert incremental_module._MCP_STDIO_ACTIVE is False
            calls.append(kwargs)

        monkeypatch.setattr(
            incremental_module, "_MCP_STDIO_ACTIVE", False, raising=False,
        )
        monkeypatch.setattr(crg_main.mcp, "run", fake_run)
        crg_main.main(
            repo_root="/tmp/r",
            transport="streamable-http",
            host="127.0.0.1",
            port=5555,
        )
        assert len(calls) == 1
        call = calls[0]
        assert call["transport"] == "streamable-http"
        assert call["host"] == "127.0.0.1"
        assert call["port"] == 5555
        # The loopback HTTP endpoint must be wrapped in the Host/Origin guard so it
        # cannot be driven cross-origin (e.g. via DNS rebinding). Behaviour is
        # covered end-to-end in tests/test_http_origin_guard.py; this only asserts
        # the entry point wires it up.
        assert [middleware.cls for middleware in call["middleware"]] == [
            LoopbackOriginGuard
        ]

    def test_streamable_http_without_host_port_raises(self):
        with pytest.raises(ValueError, match="requires host and port"):
            crg_main.main(transport="streamable-http", host=None, port=5555)
        with pytest.raises(ValueError, match="requires host and port"):
            crg_main.main(transport="streamable-http", host="127.0.0.1", port=None)


class TestLongRunningToolsAreAsync:
    """Long-running MCP tools must be registered as coroutines so the
    asyncio event loop stays responsive while the work runs in a
    background thread via ``asyncio.to_thread``. Without this, Windows
    MCP clients hang on ``build_or_update_graph_tool`` and
    ``embed_graph_tool`` — see #46, #136.
    """

    #: Every tool that can reach Git discovery, a graph traversal, FTS,
    #: an embedding provider or the filesystem, i.e. everything that can
    #: hold the stdio loop for seconds. Only ``get_docs_section_tool``
    #: (one bundled Markdown read) and ``list_repos_tool`` (one small
    #: JSON read) are allowed to stay synchronous; ``test_no_new_sync_tool``
    #: below fails if that list ever grows.
    HEAVY_TOOLS = {
        "build_or_update_graph_tool",
        "run_postprocess_tool",
        "embed_graph_tool",
        "detect_changes_tool",
        "generate_wiki_tool",
        "get_minimal_context_tool",
        "get_impact_radius_tool",
        "query_graph_tool",
        "get_review_context_tool",
        "semantic_search_nodes_tool",
        "list_graph_stats_tool",
        "find_large_functions_tool",
        "list_flows_tool",
        "get_flow_tool",
        "get_affected_flows_tool",
        "list_communities_tool",
        "get_community_tool",
        "get_architecture_overview_tool",
        "refactor_tool",
        "apply_refactor_tool",
        "get_wiki_page_tool",
        "get_hub_nodes_tool",
        "get_bridge_nodes_tool",
        "get_knowledge_gaps_tool",
        "get_surprising_connections_tool",
        "get_suggested_questions_tool",
        "traverse_graph_tool",
        "cross_repo_search_tool",
    }

    #: The only tools allowed to run inline on the event loop.
    SYNC_TOOLS = {"get_docs_section_tool", "list_repos_tool"}

    HEAVY_TOOL_IMPLS = {
        "build_or_update_graph_tool": "build_or_update_graph",
        "run_postprocess_tool": "run_postprocess",
        "embed_graph_tool": "embed_graph",
        "detect_changes_tool": "detect_changes_func",
        "generate_wiki_tool": "generate_wiki_func",
    }

    def test_heavy_tools_are_coroutines(self):
        """Regression guard for #46/#136: the 5 long-running MCP tools must
        stay ``async def`` so FastMCP can offload their blocking work via
        ``asyncio.to_thread`` and keep the stdio event loop responsive.

        The original implementation of this test went through
        ``crg_main.mcp.get_tools()``, which does not exist in the FastMCP
        2.14+ API pinned in pyproject.toml (``list_tools()`` replaces it and
        returns MCP protocol ``Tool`` objects, which do not expose the
        underlying Python function at all).  The sibling test
        ``test_heavy_tool_source_uses_to_thread`` already resolves each
        tool by ``getattr(crg_main, name)``; we do the same here so this
        guard is independent of any FastMCP internal surface.  See #239.
        """
        missing: list[str] = []
        not_async: list[str] = []

        for tool_name in self.HEAVY_TOOLS:
            fn = getattr(crg_main, tool_name, None)
            if fn is None:
                missing.append(tool_name)
                continue
            # The @mcp.tool() decorator wraps the function; FunctionTool
            # stores the underlying callable on ``.fn`` on current FastMCP
            # 2.x but we fall back to the wrapper itself for resilience.
            underlying = getattr(fn, "fn", None) or fn
            if not asyncio.iscoroutinefunction(underlying):
                not_async.append(tool_name)

        assert not missing, f"heavy tool(s) not registered at all: {missing}"
        assert not not_async, (
            f"these tools must be async but were registered as sync, "
            f"which will hang the stdio event loop on Windows: {not_async}"
        )

    def test_heavy_tool_source_uses_to_thread(self):
        """Defense in depth: every heavy tool must hand its blocking work
        to ``_offload``, and ``_offload`` must really run it off the loop.

        The offload used to be copy-pasted into each wrapper, so this
        checked each wrapper for a literal ``asyncio.to_thread``. There is
        one implementation now (#262), so the check is in two halves:
        every wrapper delegates, and the thing they delegate to threads."""
        offload_source = inspect.getsource(crg_main._run_off_loop)
        assert "anyio.to_thread.run_sync" in offload_source, (
            "_offload must thread its work through anyio.to_thread.run_sync; "
            "it is the single place every heavy tool relies on to stay off "
            "the stdio event loop. anyio and not asyncio.to_thread: see "
            "test_the_offload_uses_the_same_limiter_fastmcp_does. "
            "See #46, #136, #262."
        )
        for tool_name in self.HEAVY_TOOLS:
            fn = getattr(crg_main, tool_name, None)
            assert fn is not None, f"{tool_name} not found on module"
            # The @mcp.tool() decorator wraps the original function; walk
            # through the wrapper to find the underlying source.
            underlying = getattr(fn, "fn", None) or fn
            source = inspect.getsource(underlying)
            assert "_offload(" in source, (
                f"{tool_name} must hand its blocking work to _offload; "
                f"otherwise it runs inline and MCP clients see a hang. "
                f"See #46, #136, #262."
            )

    def test_no_new_sync_tool(self):
        """A newly added tool is async unless it is deliberately trivial.

        The failure this guards is silent: a tool added as a plain ``def``
        works in every test that calls it directly and only misbehaves as
        an unbounded, inline MCP call. Adding one fails here instead."""
        registered = {
            name for name in dir(crg_main)
            if name.endswith("_tool") and not name.startswith("_")
        }
        sync = {
            name for name in registered
            if not asyncio.iscoroutinefunction(
                getattr(getattr(crg_main, name), "fn", None) or getattr(crg_main, name)
            )
        }
        assert sync == self.SYNC_TOOLS, (
            "tools running inline on the event loop changed; either offload "
            f"the new one via _offload or justify it in SYNC_TOOLS: {sorted(sync)}"
        )
        assert registered == self.HEAVY_TOOLS | self.SYNC_TOOLS, (
            "the registered tool set changed; update HEAVY_TOOLS/SYNC_TOOLS: "
            f"{sorted(registered ^ (self.HEAVY_TOOLS | self.SYNC_TOOLS))}"
        )

    @pytest.mark.parametrize("tool_name,impl_name", HEAVY_TOOL_IMPLS.items())
    @pytest.mark.asyncio
    async def test_provenance_sqlite_read_runs_off_event_loop(
        self, tool_name, impl_name, monkeypatch,
    ):
        event_loop_thread = threading.get_ident()
        provenance_threads = []

        def fake_impl(*args, **kwargs):
            return {"status": "ok", "impl": impl_name}

        def fake_with_provenance(result, repo_root=None):
            provenance_threads.append(threading.get_ident())
            return {**result, "_graph": {"updated_at": "worker"}}

        monkeypatch.delenv("CRG_TOOL_TIMEOUT", raising=False)
        monkeypatch.setattr(crg_main, impl_name, fake_impl)
        monkeypatch.setattr(
            crg_main, "with_provenance", fake_with_provenance, raising=False,
        )
        tool = getattr(crg_main, tool_name)
        underlying = getattr(tool, "fn", None) or tool
        result = await underlying()

        assert result["impl"] == impl_name
        assert result["_graph"]["updated_at"] == "worker"
        assert provenance_threads
        assert all(tid != event_loop_thread for tid in provenance_threads)

    @pytest.mark.asyncio
    async def test_detect_changes_timeout_uses_error_response_shape(
        self, monkeypatch
    ):
        async def fake_wait_for(coro, timeout):
            coro.close()
            raise asyncio.TimeoutError

        monkeypatch.setenv("CRG_TOOL_TIMEOUT", "1")
        monkeypatch.setattr(crg_main.asyncio, "wait_for", fake_wait_for)

        tool = getattr(crg_main.detect_changes_tool, "fn", None)
        underlying = tool or crg_main.detect_changes_tool

        result = await underlying()

        assert result["status"] == "error"
        assert "timed out after 1s" in result["error"]
        assert result["summary"] == result["error"]

    @pytest.mark.parametrize("tool_name,impl_name", [
        ("get_impact_radius_tool", "get_impact_radius"),
        ("get_review_context_tool", "get_review_context"),
        ("get_minimal_context_tool", "get_minimal_context"),
        ("semantic_search_nodes_tool", "semantic_search_nodes"),
        ("query_graph_tool", "query_graph"),
        ("get_affected_flows_tool", "get_affected_flows_func"),
    ])
    @pytest.mark.asyncio
    async def test_offloaded_tool_runs_its_body_off_the_event_loop(
        self, tool_name, impl_name, monkeypatch,
    ):
        """The work itself, not just the wrapper, must leave the loop thread."""
        loop_thread = threading.get_ident()
        body_threads: list[int] = []

        def fake_impl(*args, **kwargs):
            body_threads.append(threading.get_ident())
            return {"status": "ok"}

        monkeypatch.delenv("CRG_TOOL_TIMEOUT", raising=False)
        monkeypatch.setattr(crg_main, impl_name, fake_impl)
        monkeypatch.setattr(
            crg_main, "with_provenance", lambda result, repo_root=None: result,
        )
        tool = getattr(crg_main, tool_name)
        underlying = getattr(tool, "fn", None) or tool
        kwargs = {}
        if tool_name == "query_graph_tool":
            kwargs = {"pattern": "callers_of", "target": "x"}
        elif tool_name == "semantic_search_nodes_tool":
            kwargs = {"query": "x"}

        result = await underlying(**kwargs)

        assert result == {"status": "ok"}
        assert body_threads and all(tid != loop_thread for tid in body_threads)

    @pytest.mark.asyncio
    async def test_a_slow_tool_leaves_the_event_loop_answerable(self, monkeypatch):
        """The whole point of #262, asserted against a running event loop.

        A tool body that blocks for seconds must not stop the loop from
        making progress: that is the difference between a slow answer and a
        client-side MCP -32001 on every other request in flight. A test that
        only checks ``iscoroutinefunction`` would pass on a wrapper that
        awaited its blocking work inline.
        """
        started = threading.Event()
        release = threading.Event()

        def blocking_impl(*args, **kwargs):
            started.set()
            release.wait(30)
            return {"status": "ok"}

        monkeypatch.delenv("CRG_TOOL_TIMEOUT", raising=False)
        monkeypatch.setattr(crg_main, "get_impact_radius", blocking_impl)
        monkeypatch.setattr(
            crg_main, "with_provenance", lambda result, repo_root=None: result,
        )
        tool = crg_main.get_impact_radius_tool
        underlying = getattr(tool, "fn", None) or tool

        slow = asyncio.create_task(underlying())
        try:
            assert await asyncio.to_thread(started.wait, 10)
            # The loop is free: this sleep resolves on schedule while the
            # tool is still outstanding.
            began = time.monotonic()
            await asyncio.sleep(0.05)
            elapsed = time.monotonic() - began
            assert elapsed < 2.0, (
                f"the event loop was blocked for {elapsed:.2f}s while a tool "
                "body ran; it must be offloaded via _offload"
            )
            assert not slow.done()
        finally:
            release.set()
        assert (await slow)["status"] == "ok"

    @pytest.mark.parametrize("tool_name", [
        "get_impact_radius_tool",
        "get_review_context_tool",
        "get_affected_flows_tool",
        "get_minimal_context_tool",
        "detect_changes_tool",
    ])
    @pytest.mark.asyncio
    async def test_tool_timeout_returns_a_named_error_not_an_exception(
        self, tool_name, monkeypatch,
    ):
        """Every offloaded tool answers on timeout, and names itself.

        An MCP client that gets a structured error can say what happened; one
        that gets nothing reports -32001 and the user has no idea which tool
        or which budget was involved (#262).
        """
        async def fake_wait_for(coro, timeout):
            coro.close()
            raise asyncio.TimeoutError

        monkeypatch.setenv("CRG_TOOL_TIMEOUT", "7")
        monkeypatch.setattr(crg_main.asyncio, "wait_for", fake_wait_for)
        monkeypatch.setattr(
            crg_main, "with_provenance", lambda result, repo_root=None: result,
        )
        tool = getattr(crg_main, tool_name)
        underlying = getattr(tool, "fn", None) or tool

        result = await underlying()

        assert result["status"] == "error"
        assert result["error"] == result["summary"]
        assert f"{tool_name} timed out after 7s" in result["error"]
        assert "CRG_TOOL_TIMEOUT" in result["error"]

    @pytest.mark.asyncio
    async def test_detect_changes_keeps_its_own_timeout_advice(self, monkeypatch):
        """Folding seven copies into one helper must not lose the specifics."""
        async def fake_wait_for(coro, timeout):
            coro.close()
            raise asyncio.TimeoutError

        monkeypatch.setenv("CRG_TOOL_TIMEOUT", "1")
        monkeypatch.setattr(crg_main.asyncio, "wait_for", fake_wait_for)

        underlying = (
            getattr(crg_main.detect_changes_tool, "fn", None)
            or crg_main.detect_changes_tool
        )
        result = await underlying()

        assert "CRG_MAX_CHANGED_FUNCS" in result["error"]
        assert "CRG_MAX_TRANSITIVE_FRONTIER" in result["error"]

    @pytest.mark.asyncio
    async def test_unset_tool_timeout_leaves_the_call_unbounded(self, monkeypatch):
        """Historical default: no ceiling unless one was asked for."""
        waited: list[float] = []

        async def fake_wait_for(coro, timeout):
            waited.append(timeout)
            return await coro

        monkeypatch.delenv("CRG_TOOL_TIMEOUT", raising=False)
        monkeypatch.setattr(crg_main.asyncio, "wait_for", fake_wait_for)
        monkeypatch.setattr(
            crg_main, "get_impact_radius", lambda **kw: {"status": "ok"},
        )
        monkeypatch.setattr(
            crg_main, "with_provenance", lambda result, repo_root=None: result,
        )
        underlying = (
            getattr(crg_main.get_impact_radius_tool, "fn", None)
            or crg_main.get_impact_radius_tool
        )

        assert (await underlying())["status"] == "ok"
        assert waited == [], "no timeout must be applied when the budget is 0"

        monkeypatch.setenv("CRG_TOOL_TIMEOUT", "0")
        assert (await underlying())["status"] == "ok"
        assert waited == []

    #: Offloaded, but never bounded by ``CRG_TOOL_TIMEOUT``. Each either
    #: writes (graph.db, embeddings, the wiki tree, the source tree itself)
    #: or legitimately runs for minutes, and ``asyncio.wait_for`` cancels the
    #: await rather than the worker thread -- so a "timeout" here reports
    #: failure to the client while the write goes on regardless.
    UNBOUNDED_TOOLS = {
        "build_or_update_graph_tool": "build_or_update_graph",
        "run_postprocess_tool": "run_postprocess",
        "embed_graph_tool": "embed_graph",
        "generate_wiki_tool": "generate_wiki_func",
        "apply_refactor_tool": "apply_refactor_func",
    }

    @pytest.mark.parametrize("tool_name,impl_name", UNBOUNDED_TOOLS.items())
    @pytest.mark.asyncio
    async def test_writing_tools_are_never_cut_short_by_the_tool_timeout(
        self, tool_name, impl_name, monkeypatch,
    ):
        """These were unbounded before the shared helper existed, and stay so.

        ``CRG_TOOL_TIMEOUT`` is exactly what #262 tells a user to set to keep
        review calls responsive. If that also aborted their build, the
        documented remedy would be a new bug -- and the abandoned worker would
        keep writing graph.db while the client was told the call failed, so a
        retry would run a second update against the same database.
        """
        waited: list[float] = []

        async def fake_wait_for(coro, timeout):
            waited.append(timeout)
            return await coro

        monkeypatch.setenv("CRG_TOOL_TIMEOUT", "1")
        monkeypatch.setattr(crg_main.asyncio, "wait_for", fake_wait_for)
        monkeypatch.setattr(
            crg_main, impl_name, lambda *a, **kw: {"status": "ok"},
        )
        monkeypatch.setattr(
            crg_main, "with_provenance", lambda result, repo_root=None: result,
        )
        tool = getattr(crg_main, tool_name)
        underlying = getattr(tool, "fn", None) or tool
        kwargs = {"refactor_id": "x"} if tool_name == "apply_refactor_tool" else {}

        assert (await underlying(**kwargs))["status"] == "ok"
        assert waited == [], (
            f"{tool_name} must not be bounded by CRG_TOOL_TIMEOUT: a timeout "
            "cannot stop its worker, only stop waiting for it"
        )

    @pytest.mark.parametrize("bad", ["", "   ", "abc", "2.5", "30s", "-1"])
    @pytest.mark.asyncio
    async def test_an_unusable_tool_timeout_does_not_break_every_tool(
        self, bad, monkeypatch,
    ):
        """One bad env var must not take the whole server down.

        ``int(os.environ.get(...))`` here used to raise before any work ran.
        The README calls this "Timeout in seconds", so "2.5" is a plausible
        thing to write, and an MCP config with ``"env": {"...": ""}`` produces
        the empty string for free. #912's env_int warns and falls back.
        """
        monkeypatch.setenv("CRG_TOOL_TIMEOUT", bad)
        monkeypatch.setattr(
            crg_main, "get_impact_radius", lambda **kw: {"status": "ok"},
        )
        monkeypatch.setattr(
            crg_main, "with_provenance", lambda result, repo_root=None: result,
        )
        underlying = (
            getattr(crg_main.get_impact_radius_tool, "fn", None)
            or crg_main.get_impact_radius_tool
        )

        assert (await underlying())["status"] == "ok"

    def test_the_offload_uses_the_same_limiter_fastmcp_does(self):
        """``asyncio.to_thread``'s executor is smaller than anyio's limiter.

        A plain ``def`` tool body is dispatched by FastMCP through anyio's
        default thread limiter (40 slots). Rewriting these tools as
        ``async def`` and threading them with ``asyncio.to_thread`` moves them
        onto its default executor, capped at ``min(32, cpu_count + 4)`` -- 8
        slots on a 4-core Windows box. Queueing sooner than staging did would
        be the opposite of the point of #262.
        """
        # The docstring explains the contrast, so read the code, not the prose.
        tree = ast.parse(textwrap.dedent(inspect.getsource(crg_main._run_off_loop)))
        func = tree.body[0]
        statements = [
            node for node in func.body
            if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))
        ]
        body = "\n".join(ast.unparse(node) for node in statements)
        assert "anyio.to_thread.run_sync" in body
        assert "asyncio.to_thread" not in body
        # abandon_on_cancel: without it, cancelling the await blocks until the
        # thread finishes anyway and the timeout could never fire.
        assert "abandon_on_cancel=True" in body

    def test_the_shared_timeout_hint_names_no_argument_as_universal(self):
        """Several bounded tools accept none of changed_files / max_depth /
        max_results. Advice naming a parameter the caller cannot pass is
        worse than no advice, so the shared message stays hedged."""
        assert "whichever of" in crg_main._TIMEOUT_HINT
        assert "CRG_TOOL_TIMEOUT" in crg_main._TIMEOUT_HINT

    def test_regression_guard_does_not_depend_on_fastmcp_internals(self):
        """Regression guard for #239 bug 3: ensure the async guards above
        resolve heavy tools by module attribute lookup, NOT through a
        FastMCP internal API that may drift between releases.

        The original ``test_heavy_tools_are_coroutines`` called an API on
        the mcp instance that does not exist in ``fastmcp>=2.14.0``.  It
        died with ``AttributeError`` at runtime on every platform,
        silently disabling the async-regression guard that was supposed
        to protect #46/#136 from regressing.  This test locks in the
        module-lookup approach so the guards keep working regardless of
        internal FastMCP surface changes.
        """
        import ast as _ast

        # Every heavy tool must be reachable by plain getattr on the
        # module — that's the only API surface the guards are allowed to
        # use.  No mcp internals.
        for tool_name in self.HEAVY_TOOLS:
            fn = getattr(crg_main, tool_name, None)
            assert fn is not None, (
                f"{tool_name} must be reachable via "
                f"getattr(crg_main, tool_name) so the async guards "
                f"do not depend on any FastMCP internal API"
            )

        # And the guards themselves must not reference renamed/removed
        # APIs on the mcp instance.  We check the parsed AST of the
        # function bodies (not the docstrings) so an explanatory comment
        # mentioning an old API name doesn't trip this guard.
        forbidden_mcp_attrs = {
            "get_tools", "_tools", "tool_manager", "_tool_manager",
        }
        for guard_fn in (
            self.test_heavy_tools_are_coroutines,
            self.test_heavy_tool_source_uses_to_thread,
        ):
            source = inspect.getsource(guard_fn).lstrip()
            tree = _ast.parse(source)
            for node in _ast.walk(tree):
                # We want chained attributes like ``crg_main.mcp.get_tools``.
                # That's an Attribute whose value is also an Attribute whose
                # attr == "mcp".
                if (
                    isinstance(node, _ast.Attribute)
                    and node.attr in forbidden_mcp_attrs
                    and isinstance(node.value, _ast.Attribute)
                    and node.value.attr == "mcp"
                ):
                    raise AssertionError(
                        f"{guard_fn.__name__} references mcp.{node.attr} — "
                        f"this attribute drifts across FastMCP releases "
                        f"and will silently break the guard.  Use "
                        f"getattr(crg_main, tool_name) instead."
                    )


class TestGraphBackedToolProvenanceCoverage:
    """Every single-repository graph tool must expose freshness metadata."""

    TOOL_CATEGORIES = {
        "build": {"build_or_update_graph_tool", "run_postprocess_tool"},
        "context_and_search": {
            "get_minimal_context_tool", "get_impact_radius_tool",
            "query_graph_tool", "get_review_context_tool",
            "semantic_search_nodes_tool", "find_large_functions_tool",
            "traverse_graph_tool",
        },
        "embeddings_and_stats": {"embed_graph_tool", "list_graph_stats_tool"},
        "flows_and_communities": {
            "list_flows_tool", "get_flow_tool", "get_affected_flows_tool",
            "list_communities_tool", "get_community_tool",
            "get_architecture_overview_tool",
        },
        "review_and_refactor": {
            "detect_changes_tool", "refactor_tool", "apply_refactor_tool",
        },
        "wiki_and_analysis": {
            "generate_wiki_tool", "get_wiki_page_tool", "get_hub_nodes_tool",
            "get_bridge_nodes_tool", "get_knowledge_gaps_tool",
            "get_surprising_connections_tool", "get_suggested_questions_tool",
        },
    }

    @pytest.mark.parametrize("category,tool_names", TOOL_CATEGORIES.items())
    def test_every_graph_backed_tool_category_attaches_provenance(
        self, category, tool_names,
    ):
        assert tool_names, f"{category} must name at least one tool"
        # ``_offload`` stamps provenance for every tool that routes through
        # it, so a wrapper attaches provenance either directly or by
        # delegating without opting out. See #262.
        assert "with_provenance" in inspect.getsource(crg_main._offload)
        for tool_name in tool_names:
            tool = getattr(crg_main, tool_name, None)
            assert tool is not None, f"{category}: missing {tool_name}"
            underlying = getattr(tool, "fn", None) or tool
            source = inspect.getsource(underlying)
            attaches = "with_provenance" in source or (
                "_offload(" in source and "provenance=False" not in source
            )
            assert attaches, (
                f"{category}: {tool_name} does not attach graph provenance"
            )

    @pytest.mark.parametrize("tool_name", [
        "get_docs_section_tool", "list_repos_tool", "cross_repo_search_tool",
    ])
    def test_non_single_repository_tools_do_not_claim_one_graph(self, tool_name):
        tool = getattr(crg_main, tool_name)
        underlying = getattr(tool, "fn", None) or tool
        source = inspect.getsource(underlying)
        assert "with_provenance" not in source
        # Offloading must not smuggle provenance in through the back door:
        # a tool that spans several repositories, or none, has no single
        # graph whose freshness it could report.
        if "_offload(" in source:
            assert "provenance=False" in source

class TestApplyToolFilter:
    """Tests for _apply_tool_filter (``serve --tools`` / ``CRG_TOOLS``).

    The filter removes MCP tools not present in the allow-list.
    This dramatically reduces per-turn token overhead in LLM-backed
    MCP clients by pruning unused tool descriptions.
    """

    @pytest.fixture(autouse=True)
    def _restore_tools(self):
        """Snapshot registered tools before test, restore after.

        ``_apply_tool_filter`` calls ``mcp.remove_tool()`` which is
        permanent.  We snapshot the list of Tool objects via the public
        ``list_tools()`` async API (FastMCP >=3) and re-register them
        after the test body runs.
        """
        import asyncio

        original = asyncio.run(crg_main.mcp.list_tools())
        yield
        current_names = {
            t.name for t in asyncio.run(crg_main.mcp.list_tools())
        }
        for tool in original:
            if tool.name not in current_names:
                crg_main.mcp.add_tool(tool)

    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch):
        """Ensure CRG_TOOLS is not set from the outer environment."""
        monkeypatch.delenv("CRG_TOOLS", raising=False)

    @staticmethod
    async def _tool_names() -> set[str]:
        return {t.name for t in await crg_main.mcp.list_tools()}

    @pytest.mark.asyncio
    async def test_no_filter_keeps_all_tools(self):
        """When neither --tools nor CRG_TOOLS is set, all tools remain."""
        before = await self._tool_names()
        crg_main._apply_tool_filter(None)
        after = await self._tool_names()
        assert before == after

    @pytest.mark.asyncio
    async def test_filter_via_argument(self):
        """The ``tools`` argument keeps only the listed tools."""
        keep = "query_graph_tool,semantic_search_nodes_tool"
        crg_main._apply_tool_filter(keep)
        remaining = await self._tool_names()
        assert remaining == {"query_graph_tool", "semantic_search_nodes_tool"}

    @pytest.mark.asyncio
    async def test_filter_via_env_var(self, monkeypatch):
        """The ``CRG_TOOLS`` env var works as fallback."""
        monkeypatch.setenv("CRG_TOOLS", "query_graph_tool")
        crg_main._apply_tool_filter(None)
        remaining = await self._tool_names()
        assert remaining == {"query_graph_tool"}

    @pytest.mark.asyncio
    async def test_argument_takes_precedence_over_env(self, monkeypatch):
        """CLI --tools wins over CRG_TOOLS env var."""
        monkeypatch.setenv("CRG_TOOLS", "list_repos_tool")
        crg_main._apply_tool_filter("query_graph_tool")
        remaining = await self._tool_names()
        assert remaining == {"query_graph_tool"}

    @pytest.mark.asyncio
    async def test_empty_string_is_noop(self):
        """An empty string should not remove all tools."""
        before = await self._tool_names()
        crg_main._apply_tool_filter("")
        after = await self._tool_names()
        assert before == after

    @pytest.mark.asyncio
    async def test_whitespace_handling(self):
        """Spaces around tool names are stripped."""
        crg_main._apply_tool_filter(" query_graph_tool , semantic_search_nodes_tool ")
        remaining = await self._tool_names()
        assert remaining == {"query_graph_tool", "semantic_search_nodes_tool"}
