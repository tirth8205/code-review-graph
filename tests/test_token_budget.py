"""Per-tool token budgets for every registered MCP tool.

Why this file exists
--------------------
The project's core promise is that a graph tool call is cheap: CLAUDE.md
documents "5 tool calls, 800 tokens total" for a task. Issue #849 found
``get_affected_flows`` returning ~247k tokens inside that workflow, and
PR #853 capped that one tool. A sweep of the other 29 found the same class
of bug in ten more places -- ``list_communities`` returned 206k tokens with
*default* arguments, ``get_community`` 134k, ``get_architecture_overview``
625k in standard mode.

This module is the regression guard. It calls every registered tool against
one real, module-scoped fixture graph and asserts the serialized response
stays under a documented per-tool ceiling. Removing a cap, or adding an
unbounded field to a response, makes the matching case fail loudly.

How the numbers are produced
----------------------------
* Responses are serialized the way FastMCP 3.x serializes them:
  ``pydantic_core.to_json(value, fallback=str)`` -- compact JSON, no indent.
* Tokens are counted with tiktoken ``cl100k_base`` when tiktoken is
  importable. tiktoken is not a project dependency, so the documented
  fallback is ``len(serialized) / 4``. The two agree within ~5% on this
  payload shape (measured: 206,858 tiktoken vs 203,131 len/4 on the same
  812KB response), and every ceiling below carries far more headroom than
  that, so the assertions hold under either counter.

Reading the budget table
------------------------
``DEFAULT_BUDGET`` is what the tool costs with only its required arguments
-- the number an agent actually pays in a normal workflow.
``WORST_BUDGET`` is what it costs with every verbosity knob pushed past its
hard ceiling; it proves the ceiling exists and binds.

The fixture graph is deliberately small, so these ceilings are not the
absolute worst case on a large repository. What they pin down is the
*shape* of each response: a cap that gets removed lets the list grow with
the fixture's node count and blows the ceiling immediately.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
from pathlib import Path
from typing import Any

import pytest

from code_review_graph import main as crg_main
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build
from code_review_graph.tools import analysis_tools, community_tools, review
from code_review_graph.tools import refactor_tools as refactor_mod

try:  # pragma: no cover - exercised only when tiktoken is installed
    import tiktoken

    _ENCODING = tiktoken.get_encoding("cl100k_base")
except Exception:  # pragma: no cover - the documented default path
    _ENCODING = None


# ---------------------------------------------------------------------------
# Fixture repository
# ---------------------------------------------------------------------------

# Sized so the hard ceilings actually bind: 6 packages x 12 modules x 12
# functions = 864 functions across 78 files, giving communities of ~150
# members, >100 hub candidates and >25 affected flows. That is what lets
# ``test_hard_ceilings_bind`` assert exact truncated lengths rather than
# hoping a cap was applied.
_PACKAGES = 6
_MODULES_PER_PACKAGE = 12
_FUNCS_PER_MODULE = 12


def _write_fixture_repo(root: Path) -> list[str]:
    """Generate a deterministic multi-package Python repo. Returns rel paths."""
    (root / ".code-review-graph").mkdir(parents=True, exist_ok=True)
    rel_paths: list[str] = []

    for pkg in range(_PACKAGES):
        pkg_dir = root / f"pkg{pkg}"
        pkg_dir.mkdir(exist_ok=True)
        (pkg_dir / "__init__.py").write_text("", encoding="utf-8")
        rel_paths.append(f"pkg{pkg}/__init__.py")

        for mod in range(_MODULES_PER_PACKAGE):
            lines = [f'"""Module {pkg}.{mod}."""', ""]
            # Import from the neighbouring package so cross-community edges,
            # hub nodes and bridge nodes all have something to find.
            neighbour = (pkg + 1) % _PACKAGES
            lines.append(f"from pkg{neighbour}.mod0 import helper_{neighbour}_0_0")
            lines.append("")
            for fn in range(_FUNCS_PER_MODULE):
                name = f"helper_{pkg}_{mod}_{fn}"
                lines.append(f"def {name}(value):")
                lines.append(f'    """Helper {pkg}.{mod}.{fn}."""')
                # A body long enough for find_large_functions and for the
                # source-snippet budgets to have something to trim.
                for step in range(12):
                    lines.append(f"    value = value + {step}  # step {step}")
                if fn > 0:
                    lines.append(f"    value = helper_{pkg}_{mod}_{fn - 1}(value)")
                lines.append(f"    return helper_{neighbour}_0_0(value)")
                lines.append("")
            path = pkg_dir / f"mod{mod}.py"
            path.write_text("\n".join(lines), encoding="utf-8")
            rel_paths.append(f"pkg{pkg}/mod{mod}.py")

        # One test module per package so flows have entry points and
        # tests_for / test-gap analysis has real data.
        test_lines = [f"from pkg{pkg}.mod0 import *", ""]
        for fn in range(_FUNCS_PER_MODULE):
            test_lines.append(f"def test_helper_{pkg}_0_{fn}():")
            test_lines.append(f"    assert helper_{pkg}_0_{fn}(1) is not None")
            test_lines.append("")
        test_path = pkg_dir / f"test_pkg{pkg}.py"
        test_path.write_text("\n".join(test_lines), encoding="utf-8")
        rel_paths.append(f"pkg{pkg}/test_pkg{pkg}.py")

    return rel_paths


@pytest.fixture(scope="module")
def graph_repo(tmp_path_factory) -> dict[str, Any]:
    """Build the fixture graph exactly once for the whole module."""
    root = tmp_path_factory.mktemp("token-budget-repo")
    rel_paths = _write_fixture_repo(root)

    db_path = root / ".code-review-graph" / "graph.db"
    # Serial parsing keeps the build deterministic and avoids spawning a
    # ProcessPoolExecutor inside the test session.
    os.environ["CRG_SERIAL_PARSE"] = "1"
    with GraphStore(db_path) as store:
        full_build(root, store)

    # Populate communities and flows so the tools that read them have data.
    asyncio.run(crg_main.run_postprocess_tool(repo_root=str(root)))
    asyncio.run(crg_main.generate_wiki_tool(repo_root=str(root)))

    return {
        "root": str(root),
        "files": rel_paths,
        # The last module of the last package: imported by nothing, so a
        # change there is an ordinary change rather than a repo-wide one.
        "leaf_file": f"pkg{_PACKAGES - 1}/mod{_MODULES_PER_PACKAGE - 1}.py",
    }


# ---------------------------------------------------------------------------
# Token accounting
# ---------------------------------------------------------------------------


def _serialize(value: Any) -> str:
    """Serialize a tool result the way the FastMCP layer does."""
    try:
        import pydantic_core

        return pydantic_core.to_json(value, fallback=str).decode()
    except Exception:  # pragma: no cover - pydantic_core ships with fastmcp
        return json.dumps(value, default=str, separators=(",", ":"))


def count_tokens(value: Any) -> int:
    """Token cost of a serialized tool response.

    Uses tiktoken cl100k_base when available, otherwise the documented
    ``len / 4`` estimate. See the module docstring for why both are safe.
    """
    text = _serialize(value)
    if _ENCODING is not None:
        return len(_ENCODING.encode(text, disallowed_special=()))
    return len(text) // 4


# ---------------------------------------------------------------------------
# The budget table
# ---------------------------------------------------------------------------

# A sentinel that pushes any result cap past its hard ceiling.
HUGE = 10**6

# Tools whose result lists live in code_review_graph/tools/query.py. That
# module is owned elsewhere and its unbounded worst cases are reported, not
# fixed, by this change:
#   * get_impact_radius  -- changed_nodes and edges ignore max_results
#     (3.4M tokens on a whole-repo diff), and max_results is not even
#     exposed on the MCP tool signature.
#   * find_large_functions -- limit is neither validated nor capped
#     (737k tokens at limit=10**6).
#   * traverse_graph -- token_budget is neither validated nor capped
#     (385k tokens at token_budget=10**6).
#   * semantic_search_nodes -- limit is neither validated nor capped.
# Their *default* budgets are still asserted below; only the worst case is
# skipped, so a regression in normal use is still caught here.
QUERY_OWNED_UNBOUNDED = {
    "get_impact_radius_tool",
    "find_large_functions_tool",
    "traverse_graph_tool",
    "semantic_search_nodes_tool",
}

# No tool this change owns may exceed this even with every knob maxed out.
# Before the sweep, six tools blew past it by one to two orders of
# magnitude. It is deliberately generous: it is a catastrophe backstop, not
# the workflow budget. The workflow budget is DEFAULT_BUDGET.
ABSOLUTE_MAX_TOKENS = 50_000

# tool name -> (default kwargs, worst-case kwargs, DEFAULT_BUDGET, WORST_BUDGET)
#
# Budgets carry roughly 2x headroom over the measured fixture cost so that
# ordinary parser or fixture drift does not turn this into a flaky test,
# while removing a cap (which multiplies a list by 5-50x) still fails.
BUDGETS: dict[str, dict[str, Any]] = {
    "build_or_update_graph_tool": {
        "default": {"postprocess": "none"},
        "worst": {"postprocess": "none"},
        "default_max": 1_500,
        "worst_max": 1_500,
    },
    "run_postprocess_tool": {
        "default": {},
        "worst": {},
        "default_max": 1_500,
        "worst_max": 1_500,
    },
    "get_minimal_context_tool": {
        "default": {},
        "worst": {"task": "review the pull request", "changed_files": "ALL"},
        "default_max": 800,
        "worst_max": 800,
    },
    "get_impact_radius_tool": {
        "default": {"changed_files": "LEAF"},
        "worst": {"changed_files": "ALL", "max_depth": 5},
        # Higher than it should be: changed_nodes and edges ignore
        # max_results in query.py, so even a single-file default grows with
        # the graph. Reported, not fixed here.
        "default_max": 12_000,
        "worst_max": None,  # see QUERY_OWNED_UNBOUNDED
    },
    "query_graph_tool": {
        "default": {"pattern": "callers_of", "target": "helper_0_0_0"},
        "worst": {
            "pattern": "file_summary", "target": "pkg0/mod0.py",
            "max_results": HUGE,
        },
        "default_max": 4_000,
        # query.py caps this one via max_results; the ceiling is the caller's
        # own value, so a whole-file summary is the realistic worst case.
        "worst_max": 40_000,
    },
    "get_review_context_tool": {
        "default": {"changed_files": "LEAF"},
        "worst": {
            "changed_files": "ALL", "include_source": True,
            "max_lines_per_file": HUGE, "max_results": HUGE,
            "max_files": HUGE,
        },
        # Larger budgets by design: this is the "give me everything needed to
        # review" tool, and it inlines source. Bounded by max_results
        # (100 nodes / 150 edges), an 800-line shared source budget, and
        # max_lines_per_file capped at 500.
        "default_max": 20_000,
        "worst_max": 50_000,
    },
    "semantic_search_nodes_tool": {
        "default": {"query": "helper"},
        "worst": {"query": "helper", "limit": HUGE},
        "default_max": 8_000,
        "worst_max": None,  # see QUERY_OWNED_UNBOUNDED
    },
    "embed_graph_tool": {
        # sentence-transformers is not a test dependency, so this exercises
        # the structured "provider unavailable" error response.
        "default": {},
        "worst": {},
        "default_max": 800,
        "worst_max": 800,
    },
    "list_graph_stats_tool": {
        "default": {},
        "worst": {},
        "default_max": 1_500,
        "worst_max": 1_500,
    },
    "get_docs_section_tool": {
        "default": {"section_name": "usage"},
        "worst": {"section_name": "commands"},
        "default_max": 4_000,
        "worst_max": 4_000,
    },
    "find_large_functions_tool": {
        "default": {},
        "worst": {"min_lines": 1, "limit": HUGE},
        "default_max": 20_000,
        "worst_max": None,  # see QUERY_OWNED_UNBOUNDED
    },
    "list_flows_tool": {
        "default": {},
        "worst": {"limit": HUGE},
        "default_max": 12_000,
        "worst_max": 40_000,
    },
    "get_flow_tool": {
        "default": {"flow_id": "FLOW_ID"},
        "worst": {
            "flow_id": "FLOW_ID", "include_source": True,
            "max_steps": HUGE, "max_source_lines": HUGE,
        },
        "default_max": 8_000,
        "worst_max": 40_000,
    },
    "get_affected_flows_tool": {
        "default": {"changed_files": "LEAF"},
        # max_flows=0 is PR #853's documented "no caller limit" escape; it is
        # now still subject to the per-detail-level ceiling.
        "worst": {"changed_files": "ALL", "max_flows": 0},
        "default_max": 6_000,
        # Standard mode carries a full steps list per flow (~980 tokens each
        # on a real repo), so the ceiling is 25 flows rather than PR #853's 50.
        "worst_max": 40_000,
    },
    "list_communities_tool": {
        "default": {},
        "worst": {"max_results": HUGE, "max_members": HUGE},
        "default_max": 12_000,
        "worst_max": 40_000,
    },
    "get_community_tool": {
        "default": {"community_id": "COMMUNITY_ID"},
        "worst": {
            "community_id": "COMMUNITY_ID", "include_members": True,
            "max_members": HUGE,
        },
        "default_max": 3_000,
        "worst_max": 20_000,
    },
    "get_architecture_overview_tool": {
        "default": {},
        "worst": {
            "detail_level": "standard", "max_results": HUGE,
            "max_members": HUGE,
        },
        "default_max": 4_000,
        # Standard mode is the explicit "full per-edge detail" mode: 200
        # cross-community rows plus 25 members per community.
        "worst_max": 50_000,
    },
    "detect_changes_tool": {
        "default": {"changed_files": "LEAF"},
        "worst": {
            "changed_files": "ALL", "include_source": True, "max_depth": 5,
            "max_results": HUGE, "max_flows": HUGE,
        },
        "default_max": 12_000,
        "worst_max": 50_000,
    },
    "refactor_tool:dead_code": {
        "tool": "refactor_tool",
        "default": {"mode": "dead_code"},
        "worst": {"mode": "dead_code", "max_results": HUGE},
        "default_max": 12_000,
        "worst_max": 40_000,
    },
    "refactor_tool:suggest": {
        "tool": "refactor_tool",
        "default": {"mode": "suggest"},
        "worst": {"mode": "suggest", "max_results": HUGE},
        "default_max": 12_000,
        "worst_max": 40_000,
    },
    "refactor_tool:rename": {
        "tool": "refactor_tool",
        "default": {
            "mode": "rename", "old_name": "helper_0_0_0",
            "new_name": "renamed_helper",
        },
        "worst": {
            "mode": "rename", "old_name": "helper_0_0_0",
            "new_name": "renamed_helper", "max_results": HUGE,
        },
        "default_max": 8_000,
        "worst_max": 20_000,
    },
    "apply_refactor_tool": {
        "default": {"refactor_id": "REFACTOR_ID", "dry_run": True},
        "worst": {
            "refactor_id": "REFACTOR_ID", "dry_run": True,
            "max_diff_files": HUGE,
        },
        # A dry-run diff is a bulk artifact by nature -- one unified diff per
        # touched file. It is bounded by max_diff_files (25 by default),
        # never by trimming the individual diffs, so a reviewer always sees
        # a complete diff for each file shown.
        "default_max": 25_000,
        "worst_max": 40_000,
    },
    "generate_wiki_tool": {
        "default": {},
        "worst": {"force": True},
        "default_max": 1_000,
        "worst_max": 1_000,
    },
    "get_wiki_page_tool": {
        "default": {"community_name": "COMMUNITY_NAME"},
        "worst": {"community_name": "COMMUNITY_NAME", "max_chars": HUGE},
        "default_max": 8_000,
        "worst_max": 25_000,
    },
    "get_hub_nodes_tool": {
        "default": {},
        "worst": {"top_n": HUGE},
        "default_max": 4_000,
        "worst_max": 25_000,
    },
    "get_bridge_nodes_tool": {
        "default": {},
        "worst": {"top_n": HUGE},
        "default_max": 4_000,
        "worst_max": 25_000,
    },
    "get_knowledge_gaps_tool": {
        "default": {},
        "worst": {"max_per_category": HUGE},
        "default_max": 8_000,
        "worst_max": 20_000,
    },
    "get_surprising_connections_tool": {
        "default": {},
        "worst": {"top_n": HUGE},
        "default_max": 4_000,
        "worst_max": 30_000,
    },
    "get_suggested_questions_tool": {
        # Bounded by construction: generate_suggested_questions draws at most
        # 3 bridges + 3 hubs + 3 surprises + 2 thin communities + 2 untested
        # hotspots, so it takes no result cap.
        "default": {},
        "worst": {},
        "default_max": 2_000,
        "worst_max": 2_000,
    },
    "traverse_graph_tool": {
        "default": {"query": "helper_0_0_0"},
        "worst": {"query": "helper_0_0_0", "depth": 6, "token_budget": HUGE},
        "default_max": 8_000,
        "worst_max": None,  # see QUERY_OWNED_UNBOUNDED
    },
    "list_repos_tool": {
        "no_repo_root": True,
        "default": {},
        "worst": {},
        "default_max": 2_000,
        "worst_max": 2_000,
    },
    "cross_repo_search_tool": {
        "no_repo_root": True,
        "default": {"query": "helper"},
        "worst": {"query": "helper", "limit": HUGE, "max_results": HUGE},
        "default_max": 4_000,
        "worst_max": 30_000,
    },
}


def _pick_row(repo: dict[str, Any], sql: str, column: int) -> Any:
    """Read one id straight from the graph, at call time.

    ``run_postprocess_tool`` is itself under test and rebuilds the flows and
    communities tables with fresh ids, so ids captured once in the fixture
    go stale mid-module.
    """
    with GraphStore(Path(repo["root"]) / ".code-review-graph" / "graph.db") as store:
        rows = store._conn.execute(sql).fetchall()
    return rows[0][column] if rows else None


_FLOW_SQL = "SELECT id FROM flows ORDER BY node_count DESC LIMIT 1"
_COMMUNITY_SQL = "SELECT id, name FROM communities ORDER BY size DESC LIMIT 1"


def _resolve_kwargs(kwargs: dict[str, Any], repo: dict[str, Any]) -> dict[str, Any]:
    """Substitute the fixture-dependent placeholders in a budget entry."""
    resolved: dict[str, Any] = {}
    for key, value in kwargs.items():
        if value == "ALL":
            resolved[key] = repo["files"]
        elif value == "LEAF":
            # A module nothing else imports, so "default" arguments measure a
            # typical change rather than a whole-repo blast radius.
            resolved[key] = [repo["leaf_file"]]
        elif value == "FLOW_ID":
            resolved[key] = _pick_row(repo, _FLOW_SQL, 0)
        elif value == "COMMUNITY_ID":
            resolved[key] = _pick_row(repo, _COMMUNITY_SQL, 0)
        elif value == "COMMUNITY_NAME":
            resolved[key] = _pick_row(repo, _COMMUNITY_SQL, 1)
        elif value == "REFACTOR_ID":
            resolved[key] = repo["refactor_id"]
        else:
            resolved[key] = value
    return resolved


def _call(name: str, spec: dict[str, Any], kwargs: dict[str, Any],
          repo: dict[str, Any]) -> Any:
    """Invoke one registered tool with fixture-resolved arguments."""
    tool = getattr(crg_main, spec.get("tool", name))
    func = getattr(tool, "fn", tool)
    call_kwargs = _resolve_kwargs(kwargs, repo)
    if not spec.get("no_repo_root"):
        call_kwargs["repo_root"] = repo["root"]
    if inspect.iscoroutinefunction(func):
        return asyncio.run(func(**call_kwargs))
    return func(**call_kwargs)


@pytest.fixture(scope="module")
def repo(graph_repo) -> dict[str, Any]:
    """Fixture graph plus a live refactor_id for apply_refactor_tool."""
    preview = crg_main.refactor_tool(
        mode="rename", old_name="helper_0_0_0", new_name="renamed_helper",
        repo_root=graph_repo["root"],
    )
    return {**graph_repo, "refactor_id": preview.get("refactor_id", "missing")}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_budget_table_covers_every_registered_tool():
    """Every ``@mcp.tool()`` in main.py must have a budget entry.

    Without this, a new tool could ship unbounded and no case would notice.
    """
    registered = {
        name for name in dir(crg_main)
        if name.endswith("_tool") and callable(getattr(crg_main, name))
        and not name.startswith("_")
    }
    covered = {spec.get("tool", name) for name, spec in BUDGETS.items()}
    assert registered - covered == set(), (
        "MCP tools missing a token budget entry: "
        f"{sorted(registered - covered)}"
    )


@pytest.mark.parametrize("name", sorted(BUDGETS))
def test_default_args_stay_in_budget(name, repo):
    """A tool called the way an agent calls it must be cheap."""
    spec = BUDGETS[name]
    result = _call(name, spec, spec["default"], repo)
    tokens = count_tokens(result)
    assert tokens <= spec["default_max"], (
        f"{name} returned {tokens} tokens with default arguments, over its "
        f"{spec['default_max']} budget. A result cap was probably removed or "
        f"an unbounded field added."
    )


@pytest.mark.parametrize("name", sorted(BUDGETS))
def test_worst_case_args_stay_in_budget(name, repo):
    """Maxing every verbosity knob must still hit a hard ceiling."""
    spec = BUDGETS[name]
    if spec["worst_max"] is None:
        pytest.skip(
            f"{name} is bounded in code_review_graph/tools/query.py, which "
            "this change does not own; its unbounded worst case is reported "
            "rather than fixed (see QUERY_OWNED_UNBOUNDED)."
        )
    result = _call(name, spec, spec["worst"], repo)
    tokens = count_tokens(result)
    assert tokens <= spec["worst_max"], (
        f"{name} returned {tokens} tokens with maxed arguments, over its "
        f"{spec['worst_max']} ceiling. The hard cap was probably raised or "
        f"removed."
    )
    assert tokens <= ABSOLUTE_MAX_TOKENS, (
        f"{name} returned {tokens} tokens, over the {ABSOLUTE_MAX_TOKENS} "
        "absolute ceiling that no MCP tool response may cross."
    )


def test_caps_actually_bind_on_the_fixture_graph(repo):
    """Guard against a budget that passes only because the fixture is small.

    Each tool below must report ``truncated`` on this fixture at its own
    default arguments. If a cap is removed, the flag disappears and this
    fails even where the raw token count would still squeak under budget.
    """
    root = repo["root"]
    all_files = repo["files"]
    cases = {
        "list_communities": crg_main.list_communities_tool(
            repo_root=root, max_results=1,
        ),
        "list_flows": crg_main.list_flows_tool(repo_root=root, limit=1),
        "get_affected_flows": crg_main.get_affected_flows_tool(
            repo_root=root, changed_files=all_files, max_flows=1,
        ),
        "refactor_dead_code": crg_main.refactor_tool(
            repo_root=root, mode="dead_code", max_results=1,
        ),
        "get_hub_nodes": crg_main.get_hub_nodes_tool(repo_root=root, top_n=1),
        "get_bridge_nodes": crg_main.get_bridge_nodes_tool(
            repo_root=root, top_n=1,
        ),
        "get_surprising_connections": crg_main.get_surprising_connections_tool(
            repo_root=root, top_n=1,
        ),
        "get_architecture_overview": crg_main.get_architecture_overview_tool(
            repo_root=root, max_results=1,
        ),
    }
    for label, result in cases.items():
        assert result.get("truncated") is True, (
            f"{label} did not report truncation at a cap of 1 -- its result "
            "cap is no longer applied"
        )


# The ceiling constants are themselves part of the contract. Asserting them
# directly is what catches "someone raised the cap" -- a token budget alone
# can be masked by its own headroom, and a length assertion silently passes
# once the ceiling exceeds what the fixture can produce.
MAX_CEILINGS = {
    "community_tools._MAX_MEMBERS": (community_tools._MAX_MEMBERS, 25),
    "community_tools._MAX_COMMUNITIES": (community_tools._MAX_COMMUNITIES, 200),
    "community_tools._MAX_CROSS_EDGES": (community_tools._MAX_CROSS_EDGES, 200),
    "analysis_tools._MAX_HUB_NODES": (analysis_tools._MAX_HUB_NODES, 100),
    "analysis_tools._MAX_BRIDGE_NODES": (analysis_tools._MAX_BRIDGE_NODES, 100),
    "analysis_tools._MAX_SURPRISING": (analysis_tools._MAX_SURPRISING, 100),
    "analysis_tools._MAX_GAPS_PER_CATEGORY": (
        analysis_tools._MAX_GAPS_PER_CATEGORY, 50,
    ),
    "review._MAX_REVIEW_NODES": (review._MAX_REVIEW_NODES, 100),
    "review._MAX_REVIEW_EDGES": (review._MAX_REVIEW_EDGES, 150),
    "review._MAX_REVIEW_SOURCE_LINES": (review._MAX_REVIEW_SOURCE_LINES, 800),
    "review._MAX_LINES_PER_FILE": (review._MAX_LINES_PER_FILE, 500),
    "review._MAX_CHANGED_FUNCTIONS": (review._MAX_CHANGED_FUNCTIONS, 100),
    "review._MAX_AFFECTED_FLOWS_STANDARD": (
        review._MAX_AFFECTED_FLOWS_STANDARD, 25,
    ),
    "review._MAX_AFFECTED_FLOWS_MINIMAL": (
        review._MAX_AFFECTED_FLOWS_MINIMAL, 500,
    ),
    "review._MAX_AFFECTED_FLOW_STEPS": (review._MAX_AFFECTED_FLOW_STEPS, 400),
    "refactor_tools._MAX_REFACTOR_RESULTS": (
        refactor_mod._MAX_REFACTOR_RESULTS, 150,
    ),
}


@pytest.mark.parametrize("name", sorted(MAX_CEILINGS))
def test_ceiling_constants_are_not_raised(name):
    """Raising a hard ceiling is a budget change and must be deliberate.

    If a ceiling genuinely needs to grow, update the number here in the same
    commit and say why -- that is the review conversation this test forces.
    """
    actual, allowed = MAX_CEILINGS[name]
    assert actual <= allowed, (
        f"{name} was raised to {actual}, over the {allowed} this budget "
        "table was measured against"
    )


def test_hard_ceilings_bind(repo):
    """A caller asking for everything gets the ceiling, not everything.

    Complements the constant check above: this proves the ceilings are
    actually applied to the response, not merely declared.
    """
    root = repo["root"]
    all_files = repo["files"]

    communities = crg_main.list_communities_tool(
        repo_root=root, max_members=HUGE,
    )["communities"]
    oversized = [c for c in communities if c["size"] > 25]
    assert oversized, "fixture no longer has a community big enough to cap"
    for community in oversized:
        assert len(community["members"]) == community_tools._MAX_MEMBERS
        assert community["members_truncated"] is True

    hubs = crg_main.get_hub_nodes_tool(repo_root=root, top_n=HUGE)
    assert hubs["total"] > analysis_tools._MAX_HUB_NODES
    assert len(hubs["hub_nodes"]) == analysis_tools._MAX_HUB_NODES

    surprises = crg_main.get_surprising_connections_tool(
        repo_root=root, top_n=HUGE,
    )
    assert len(surprises["surprising_connections"]) == (
        min(surprises["total"], analysis_tools._MAX_SURPRISING)
    )

    flows = crg_main.get_affected_flows_tool(
        repo_root=root, changed_files=all_files, max_flows=HUGE,
    )
    assert flows["total"] > review._MAX_AFFECTED_FLOWS_STANDARD
    assert len(flows["affected_flows"]) == review._MAX_AFFECTED_FLOWS_STANDARD
    # The shared step budget must also hold, whatever the flow depth.
    emitted = sum(len(f.get("steps") or []) for f in flows["affected_flows"])
    assert emitted <= review._MAX_AFFECTED_FLOW_STEPS

    changes = asyncio.run(crg_main.detect_changes_tool(
        repo_root=root, changed_files=all_files, max_results=HUGE,
    ))
    assert changes["changed_functions_total"] > review._MAX_CHANGED_FUNCTIONS
    assert len(changes["changed_functions"]) == review._MAX_CHANGED_FUNCTIONS

    context = crg_main.get_review_context_tool(
        repo_root=root, changed_files=all_files, max_results=HUGE,
        max_files=HUGE, include_source=True, max_lines_per_file=HUGE,
    )["context"]
    assert len(context["graph"]["impacted_nodes"]) <= review._MAX_REVIEW_NODES
    assert len(context["graph"]["edges"]) <= review._MAX_REVIEW_EDGES
    emitted_lines = sum(
        len(snippet.splitlines())
        for snippet in context["source_snippets"].values()
    )
    # Each snippet can overshoot by the "..." separators it inserts, so allow
    # a small margin over the raw line budget.
    assert emitted_lines <= review._MAX_REVIEW_SOURCE_LINES * 1.5

    dead = crg_main.refactor_tool(
        repo_root=root, mode="dead_code", max_results=HUGE,
    )
    assert len(dead["dead_code"]) == min(
        dead["total"], refactor_mod._MAX_REFACTOR_RESULTS,
    )


class TestTruncationContract:
    """The contract PR #853 established, applied to the newly capped tools."""

    def test_list_communities_reports_untruncated_total(self, repo):
        result = crg_main.list_communities_tool(
            repo_root=repo["root"], max_results=1,
        )
        assert result["status"] == "ok"
        assert result["truncated"] is True
        assert result["total"] > len(result["communities"])
        assert f"showing {len(result['communities'])} of" in result["summary"]

    def test_get_community_keeps_true_size_when_members_cut(self, repo):
        result = crg_main.get_community_tool(
            repo_root=repo["root"],
            community_id=_pick_row(repo, _COMMUNITY_SQL, 0),
            include_members=True, max_members=1,
        )
        community = result["community"]
        assert community["members_truncated"] is True
        # ``size`` is the real member count, not the truncated list length.
        assert community["size"] > len(community["members"])

    def test_detect_changes_flows_carry_no_step_lists(self, repo):
        """#849's payload must not leak back in through detect_changes."""
        result = asyncio.run(crg_main.detect_changes_tool(
            repo_root=repo["root"], changed_files=repo["files"],
        ))
        for flow in result["affected_flows"]:
            assert "steps" not in flow, (
                "detect_changes embeds per-flow metadata only; full step "
                "lists are what made get_affected_flows return 247k tokens"
            )

    def test_affected_flows_zero_still_hits_the_ceiling(self, repo):
        """``max_flows=0`` means 'no caller limit', not 'no limit'."""
        result = crg_main.get_affected_flows_tool(
            repo_root=repo["root"], changed_files=repo["files"], max_flows=0,
        )
        assert len(result["affected_flows"]) <= 25
        assert result["total"] >= len(result["affected_flows"])

    def test_rename_preview_response_is_cut_but_apply_is_not(self, repo):
        """Truncating the response must not truncate the stored refactor."""
        preview = crg_main.refactor_tool(
            repo_root=repo["root"], mode="rename", old_name="helper_0_0_0",
            new_name="renamed_helper", max_results=1,
        )
        assert preview["truncated"] is True
        assert len(preview["edits"]) == 1
        assert preview["total"] > 1
        # The stored preview still holds every edit, so a dry run reports the
        # full set of files rather than the one shown edit.
        applied = crg_main.apply_refactor_tool(
            repo_root=repo["root"], refactor_id=preview["refactor_id"],
            dry_run=True,
        )
        assert applied["status"] == "ok"
        assert applied["edits_applied"] >= preview["total"]


class TestBoundValidation:
    """Result bounds are validated the way query.py validates max_results."""

    @pytest.mark.parametrize(
        ("tool", "kwargs"),
        [
            ("get_hub_nodes_tool", {"top_n": 0}),
            ("get_hub_nodes_tool", {"top_n": True}),
            ("get_bridge_nodes_tool", {"top_n": -1}),
            ("get_surprising_connections_tool", {"top_n": 0}),
            ("get_knowledge_gaps_tool", {"max_per_category": 0}),
            ("list_communities_tool", {"max_results": 0}),
            ("list_communities_tool", {"max_members": True}),
            ("get_community_tool", {"max_members": 0}),
            ("get_architecture_overview_tool", {"max_results": 0}),
            ("list_flows_tool", {"limit": 0}),
            ("get_flow_tool", {"max_steps": 0}),
            ("get_flow_tool", {"max_source_lines": -5}),
            ("get_review_context_tool", {"max_results": 0}),
            ("get_review_context_tool", {"max_files": True}),
            ("get_wiki_page_tool", {"community_name": "x", "max_chars": 0}),
            ("apply_refactor_tool", {"refactor_id": "x", "max_diff_files": 0}),
        ],
    )
    def test_rejects_non_positive_bounds(self, tool, kwargs, repo):
        func = getattr(crg_main, tool)
        with pytest.raises(ValueError, match="greater than or equal to 1"):
            func(repo_root=repo["root"], **kwargs)

    def test_refactor_rejects_non_positive_max_results(self, repo):
        with pytest.raises(ValueError, match="greater than or equal to 1"):
            crg_main.refactor_tool(
                repo_root=repo["root"], mode="dead_code", max_results=0,
            )

    def test_detect_changes_rejects_non_positive_bounds(self, repo):
        with pytest.raises(ValueError, match="greater than or equal to 1"):
            asyncio.run(crg_main.detect_changes_tool(
                repo_root=repo["root"], max_results=0,
            ))

    def test_cross_repo_search_rejects_non_positive_bounds(self):
        with pytest.raises(ValueError, match="greater than or equal to 1"):
            crg_main.cross_repo_search_tool(query="x", max_results=0)
