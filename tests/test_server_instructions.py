"""The MCP server instructions only name tools and patterns that exist.

With deferred tool loading an agent sees the server instructions and the tool
names, not the tool descriptions, so the instructions are its routing table. A
renamed tool or query pattern would leave that table pointing at something the
server no longer has, with nothing else failing.

Introduced by PR #1096. The later tests pin the instructions on the wire (the
real stdio handshake), under the ``serve --tools`` / ``CRG_TOOLS`` allow-list,
the argument names the routing lines spell, and the re-export claim the text
makes about graph answers.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from code_review_graph import main
from code_review_graph.tools.query import _QUERY_PATTERNS

try:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
except ImportError:  # pragma: no cover - ``mcp`` is a hard runtime dependency
    ClientSession = None  # type: ignore[assignment,misc]
    StdioServerParameters = None  # type: ignore[assignment,misc]
    stdio_client = None  # type: ignore[assignment]

# Claude Code truncates server instructions past this many characters.
CLAUDE_CODE_INSTRUCTIONS_LIMIT = 2048


def test_server_is_built_with_the_routing_instructions() -> None:
    assert main.mcp.instructions == main.SERVER_INSTRUCTIONS


def test_instructions_fit_before_truncation() -> None:
    assert len(main.SERVER_INSTRUCTIONS) <= CLAUDE_CODE_INSTRUCTIONS_LIMIT


def test_every_tool_named_is_registered() -> None:
    named = set(re.findall(r"\b\w+_tool\b", main.SERVER_INSTRUCTIONS))
    assert named, "the instructions no longer name any tool"
    registered = {tool.name for tool in asyncio.run(main.mcp.list_tools())}
    assert named <= registered, f"named but not registered: {sorted(named - registered)}"


def test_every_query_pattern_named_exists() -> None:
    named = set(re.findall(r"pattern=(\w+)", main.SERVER_INSTRUCTIONS))
    named |= set(re.findall(r"pattern=\w+ / (\w+)", main.SERVER_INSTRUCTIONS))
    assert named, "the instructions no longer name any query pattern"
    assert named <= set(_QUERY_PATTERNS), (
        f"named but not a query pattern: {sorted(named - set(_QUERY_PATTERNS))}"
    )


def test_plain_text_search_is_left_to_grep() -> None:
    # The graph indexes structure, not text. Without this line the routing table
    # reads as "use the graph for everything", and an agent looking for a log
    # message or an env var name gets an empty graph answer instead of a grep hit.
    assert "Grep" in main.SERVER_INSTRUCTIONS
    assert "does not index text" in main.SERVER_INSTRUCTIONS


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

REPO_SOURCE_ROOT = Path(__file__).resolve().parents[1]
HANDSHAKE_TIMEOUT = 120.0
_TOOL_NAME = re.compile(r"\b\w+_tool\b")

# The allow-list README.md shows for ``serve --tools`` (plus the review context
# tool its MCP client config example adds).
README_ALLOW_LIST = (
    "query_graph_tool,semantic_search_nodes_tool,detect_changes_tool,get_review_context_tool"
)

requires_mcp_client = pytest.mark.skipif(
    ClientSession is None, reason="mcp client library not installed"
)


def _server_env(home: Path, crg_tools: str | None) -> dict[str, str]:
    """Child environment: isolated user state, the checkout under test importable."""
    env = os.environ.copy()
    for key in (
        "HOME", "USERPROFILE", "APPDATA", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
        "XDG_CACHE_HOME", "CRG_HOME",
    ):
        env[key] = str(home)
    for key in ("CRG_TOOLS", "CRG_DATA_DIR", "CRG_REPO_ROOT"):
        env.pop(key, None)
    if crg_tools is not None:
        env["CRG_TOOLS"] = crg_tools
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = os.pathsep.join(
        value for value in (str(REPO_SOURCE_ROOT), env.get("PYTHONPATH")) if value
    )
    return env


async def _handshake(
    tmp_path: Path, *, tools_arg: str | None = None, crg_tools: str | None = None
) -> tuple[str, set[str]]:
    """Start ``serve`` over stdio as a client config would; return what the client sees."""
    repo = tmp_path / "repo with spaces"
    (repo / ".code-review-graph").mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    args = ["-m", "code_review_graph", "serve", "--repo", str(repo)]
    if tools_arg is not None:
        args += ["--tools", tools_arg]
    params = StdioServerParameters(
        command=sys.executable, args=args, env=_server_env(home, crg_tools), cwd=str(repo),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(
            read, write, read_timeout_seconds=timedelta(seconds=HANDSHAKE_TIMEOUT),
        ) as session:
            init = await asyncio.wait_for(session.initialize(), timeout=HANDSHAKE_TIMEOUT)
            listed = await asyncio.wait_for(session.list_tools(), timeout=HANDSHAKE_TIMEOUT)
    return init.instructions or "", {tool.name for tool in listed.tools}


@pytest.mark.e2e
@requires_mcp_client
async def test_initialize_handshake_delivers_the_routing_instructions(
    tmp_path: Path,
) -> None:
    # The attribute on the server object is not what an agent reads: the client
    # gets the text in the ``initialize`` result. Drive the real stdio server.
    instructions, listed = await _handshake(tmp_path)
    assert instructions == main.SERVER_INSTRUCTIONS
    named = set(_TOOL_NAME.findall(instructions))
    assert named and named <= listed, f"named but not served: {sorted(named - listed)}"


@pytest.mark.e2e
@requires_mcp_client
@pytest.mark.parametrize("via", ["--tools", "CRG_TOOLS"])
async def test_tool_allow_list_keeps_the_routing_lines_for_allowed_tools(
    tmp_path: Path, via: str,
) -> None:
    # ``serve --tools`` / ``CRG_TOOLS`` (README "Limiting tools") removes tools
    # after the server is built. The handshake must still succeed, list exactly
    # the allowed tools, and keep the routing lines that point at them. (The
    # text may still name tools the allow-list removed, as the prompts and
    # next_tool_suggestions already do; that is a separate follow-up.)
    if via == "--tools":
        instructions, listed = await _handshake(tmp_path, tools_arg=README_ALLOW_LIST)
    else:
        instructions, listed = await _handshake(tmp_path, crg_tools=README_ALLOW_LIST)
    assert listed == set(README_ALLOW_LIST.split(","))
    for line in (
        "query_graph_tool pattern=callers_of",
        "query_graph_tool pattern=importers_of",
        "query_graph_tool pattern=tests_for",
        "detect_changes_tool, get_review_context_tool",
    ):
        assert line in instructions, line
    named = set(_TOOL_NAME.findall(instructions))
    assert {"query_graph_tool", "detect_changes_tool", "get_review_context_tool"} <= (
        named & listed
    )


def test_every_argument_named_belongs_to_a_tool_on_its_line() -> None:
    # "query_graph_tool pattern=callers_of" only works if query_graph_tool still
    # takes an argument called ``pattern``; a renamed parameter would turn every
    # call the routing table suggests into a validation error.
    schemas = {
        tool.name: set((tool.parameters or {}).get("properties", {}))
        for tool in asyncio.run(main.mcp.list_tools())
    }
    checked = 0
    for line in main.SERVER_INSTRUCTIONS.splitlines():
        arguments = set(re.findall(r"\b(\w+)=", line))
        if not arguments:
            continue
        tools = _TOOL_NAME.findall(line)
        assert tools, f"argument without a tool: {line!r}"
        accepted = set().union(*(schemas.get(name, set()) for name in tools))
        assert arguments <= accepted, (
            f"{line!r}: {sorted(arguments - accepted)} is not a parameter of {tools}"
        )
        checked += 1
    assert checked, "no routing line names an argument any more"


def test_every_alternative_after_pattern_is_a_query_pattern() -> None:
    # The PR's own check reads at most two alternatives (``pattern=a / b``); a
    # third (``pattern=a / b / c``) or a comma-separated list would go unchecked.
    named: set[str] = set()
    for line in main.SERVER_INSTRUCTIONS.splitlines():
        if "pattern=" not in line:
            continue
        tail = line.split("pattern=", 1)[1]
        named |= {
            token for token in re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", tail)
            if not token.endswith("_tool")
        }
    assert {"callers_of", "references_to", "importers_of", "tests_for"} <= named
    assert named <= set(_QUERY_PATTERNS), (
        f"named but not a query pattern: {sorted(named - set(_QUERY_PATTERNS))}"
    )


_BARREL_FILES = {
    "src/lib.ts": (
        "export function computeTotal(xs: number[]): number {\n"
        "  return xs.reduce((a, b) => a + b, 0);\n}\n"
    ),
    "src/index.ts": "export * from './lib';\n",
    "src/consumer.ts": (
        "import { computeTotal } from './index';\n\n"
        "export function checkout(xs: number[]): number {\n"
        "  return computeTotal(xs) * 2;\n}\n"
    ),
}


@pytest.fixture
def barrel_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A TypeScript package whose consumer reaches ``lib.ts`` only through a barrel."""
    from code_review_graph.tools.build import build_or_update_graph

    monkeypatch.delenv("CRG_DATA_DIR", raising=False)
    monkeypatch.delenv("CRG_REPO_ROOT", raising=False)
    repo = tmp_path / "barrel repo"
    (repo / ".git").mkdir(parents=True)
    for rel, text in _BARREL_FILES.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    with patch(
        "code_review_graph.incremental.get_all_tracked_files",
        return_value=sorted(_BARREL_FILES),
    ):
        result = build_or_update_graph(
            full_rebuild=True, repo_root=str(repo), postprocess="none",
        )
    assert result["status"] == "ok"
    return repo


def test_graph_reaches_dependents_through_a_barrel_file(barrel_repo: Path) -> None:
    # The instructions promise "dependents reached through re-exports and barrel
    # files that never spell the name". consumer.ts never spells "lib", so a text
    # search for the module misses it; callers_of and the blast radius must not.
    from code_review_graph.tools.query import get_impact_radius, query_graph

    assert "lib" not in _BARREL_FILES["src/consumer.ts"]
    assert "pattern=callers_of" in main.SERVER_INSTRUCTIONS
    assert "get_impact_radius_tool" in main.SERVER_INSTRUCTIONS

    callers = query_graph(
        pattern="callers_of", target="computeTotal", repo_root=str(barrel_repo),
    )
    assert callers["status"] == "ok"
    assert {r["name"] for r in callers["results"]} == {"checkout"}

    impact = get_impact_radius(changed_files=["src/lib.ts"], repo_root=str(barrel_repo))
    assert impact["status"] == "ok"
    impacted = {Path(f).as_posix() for f in impact["impacted_files"]}
    assert any(f.endswith("src/consumer.ts") for f in impacted), impacted


@pytest.mark.xfail(
    strict=True,
    reason=(
        "`export * from './lib'` creates no IMPORTS_FROM edge, so importers_of a "
        "file reached only through a barrel is empty although the instructions "
        "route 'which files import file X' to it; a text search for './lib' finds "
        "the barrel. Graph limitation that predates #1096."
    ),
)
def test_importers_of_includes_a_barrel_that_re_exports_the_file(barrel_repo: Path) -> None:
    from code_review_graph.tools.query import query_graph

    assert "pattern=importers_of" in main.SERVER_INSTRUCTIONS
    importers = query_graph(
        pattern="importers_of", target="src/lib.ts", repo_root=str(barrel_repo),
    )
    assert importers["status"] == "ok"
    files = {Path(r["file"]).as_posix() for r in importers["results"]}
    assert any(f.endswith("src/index.ts") for f in files), files
