"""Tests for the MCP server entry point.

Focused on the ``_resolve_repo_root`` helper that threads the
``serve --repo <X>`` CLI flag into every tool wrapper, and on the
set of tools that must be registered as async coroutines so the MCP
stdio event loop stays responsive during long-running operations.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from code_review_graph import main as crg_main


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


class TestLongRunningToolsAreAsync:
    """Long-running MCP tools must be registered as coroutines so the
    asyncio event loop stays responsive while the work runs in a
    background thread via ``asyncio.to_thread``. Without this, Windows
    MCP clients hang on ``build_or_update_graph_tool`` and
    ``embed_graph_tool`` — see #46, #136.
    """

    HEAVY_TOOLS = {
        "build_or_update_graph_tool",
        "run_postprocess_tool",
        "embed_graph_tool",
        "detect_changes_tool",
        "generate_wiki_tool",
    }

    @pytest.mark.asyncio
    async def test_heavy_tools_are_coroutines(self):
        tools = await crg_main.mcp.get_tools()
        registered: dict[str, bool] = {}
        for name, tool in tools.items():
            if name not in self.HEAVY_TOOLS:
                continue
            # FastMCP 2.x stores the underlying Python function on the
            # tool wrapper; attribute name has varied but is typically
            # ``fn`` on FunctionTool. Fall back to a few candidates.
            fn = (
                getattr(tool, "fn", None)
                or getattr(tool, "_func", None)
                or getattr(tool, "func", None)
                or tool
            )
            registered[name] = asyncio.iscoroutinefunction(fn)

        missing = self.HEAVY_TOOLS - registered.keys()
        assert not missing, f"heavy tool(s) not registered at all: {missing}"

        not_async = [name for name, is_async in registered.items() if not is_async]
        assert not not_async, (
            f"these tools must be async but were registered as sync, "
            f"which will hang the stdio event loop on Windows: {not_async}"
        )

    @pytest.mark.asyncio
    async def test_heavy_tool_source_uses_to_thread(self):
        """Defense in depth: the source of every heavy tool wrapper must
        literally call asyncio.to_thread so we don't accidentally turn
        a tool async without offloading the blocking work."""
        for tool_name in self.HEAVY_TOOLS:
            fn = getattr(crg_main, tool_name, None)
            assert fn is not None, f"{tool_name} not found on module"
            # The @mcp.tool() decorator wraps the original function; walk
            # through the wrapper to find the underlying source.
            underlying = getattr(fn, "fn", None) or fn
            source = inspect.getsource(underlying)
            assert "asyncio.to_thread" in source, (
                f"{tool_name} must call asyncio.to_thread to offload its "
                f"blocking work; otherwise Windows MCP clients will hang. "
                f"See #46, #136."
            )


class TestApplyToolFilter:
    """Tests for _apply_tool_filter (``serve --tools`` / ``CRG_TOOLS``).

    The filter removes MCP tools not present in the allow-list.
    This dramatically reduces per-turn token overhead in LLM-backed
    MCP clients by pruning unused tool descriptions.
    """

    @pytest.fixture(autouse=True)
    def _restore_tools(self):
        """Snapshot registered tools before test, restore after.

        _apply_tool_filter calls ``mcp.remove_tool()`` which is
        permanent.  We restore by re-adding from the saved snapshot.
        """
        original = dict(crg_main.mcp._tool_manager._tools)
        yield
        crg_main.mcp._tool_manager._tools.clear()
        crg_main.mcp._tool_manager._tools.update(original)

    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch):
        """Ensure CRG_TOOLS is not set from the outer environment."""
        monkeypatch.delenv("CRG_TOOLS", raising=False)

    @pytest.mark.asyncio
    async def test_no_filter_keeps_all_tools(self):
        """When neither --tools nor CRG_TOOLS is set, all tools remain."""
        before = set((await crg_main.mcp.get_tools()).keys())
        crg_main._apply_tool_filter(None)
        after = set((await crg_main.mcp.get_tools()).keys())
        assert before == after

    @pytest.mark.asyncio
    async def test_filter_via_argument(self):
        """The ``tools`` argument keeps only the listed tools."""
        keep = "query_graph_tool,semantic_search_nodes_tool"
        crg_main._apply_tool_filter(keep)
        remaining = set((await crg_main.mcp.get_tools()).keys())
        assert remaining == {"query_graph_tool", "semantic_search_nodes_tool"}

    @pytest.mark.asyncio
    async def test_filter_via_env_var(self, monkeypatch):
        """The ``CRG_TOOLS`` env var works as fallback."""
        monkeypatch.setenv("CRG_TOOLS", "query_graph_tool")
        crg_main._apply_tool_filter(None)
        remaining = set((await crg_main.mcp.get_tools()).keys())
        assert remaining == {"query_graph_tool"}

    @pytest.mark.asyncio
    async def test_argument_takes_precedence_over_env(self, monkeypatch):
        """CLI --tools wins over CRG_TOOLS env var."""
        monkeypatch.setenv("CRG_TOOLS", "list_repos_tool")
        crg_main._apply_tool_filter("query_graph_tool")
        remaining = set((await crg_main.mcp.get_tools()).keys())
        assert remaining == {"query_graph_tool"}

    @pytest.mark.asyncio
    async def test_empty_string_is_noop(self):
        """An empty string should not remove all tools."""
        before = set((await crg_main.mcp.get_tools()).keys())
        crg_main._apply_tool_filter("")
        after = set((await crg_main.mcp.get_tools()).keys())
        assert before == after

    @pytest.mark.asyncio
    async def test_whitespace_handling(self):
        """Spaces around tool names are stripped."""
        crg_main._apply_tool_filter(" query_graph_tool , semantic_search_nodes_tool ")
        remaining = set((await crg_main.mcp.get_tools()).keys())
        assert remaining == {"query_graph_tool", "semantic_search_nodes_tool"}

