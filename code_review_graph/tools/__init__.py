"""MCP tool definitions for the Code Review Graph server.

Exposes 27 tools:
1. build_or_update_graph  - full or incremental build
2. get_impact_radius      - blast radius from changed files
3. query_graph            - predefined graph queries
4. get_review_context     - focused subgraph + review prompt
5. semantic_search_nodes  - keyword + vector search across nodes
6. list_graph_stats       - aggregate statistics
7. embed_graph            - compute vector embeddings for semantic search
8. get_docs_section       - token-optimized documentation retrieval
9. find_large_functions   - find oversized functions/classes by line count
10. list_flows            - list execution flows sorted by criticality
11. get_flow              - get details of a single execution flow
12. get_affected_flows    - find flows affected by changed files
13. list_communities      - list detected code communities
14. get_community         - get details of a single community
15. get_architecture_overview - architecture overview from community structure
16. detect_changes        - risk-scored change impact analysis for code review
17. refactor_tool         - unified refactoring (rename preview, dead code, suggestions)
18. apply_refactor_tool   - apply a previously previewed refactoring
19. generate_wiki         - generate markdown wiki from community structure
20. get_wiki_page         - retrieve a specific wiki page
21. list_repos            - list registered repositories
22. cross_repo_search     - search across all registered repositories
23. get_hub_nodes         - find most connected nodes (architectural hotspots)
24. get_bridge_nodes      - find architectural chokepoints (betweenness centrality)
25. get_knowledge_gaps    - identify structural weaknesses
26. get_surprising_connections - find unexpected architectural coupling
27. get_suggested_questions - auto-generated review questions from graph analysis
28. traverse_graph        - BFS/DFS traversal from best-matching node
"""

from __future__ import annotations

# Re-export names that external code may patch via "code_review_graph.tools.*"
from ..changes import parse_git_diff_ranges as parse_git_diff_ranges
from ..incremental import (
    get_changed_files as get_changed_files,
)
from ..incremental import (
    get_staged_and_unstaged as get_staged_and_unstaged,
)

# -- _common ----------------------------------------------------------------
from ._common import (
    _BUILTIN_CALL_NAMES,
    _get_store,
    _validate_repo_root,
)

# -- analysis_tools ---------------------------------------------------------
from .analysis_tools import (
    get_bridge_nodes_func,
    get_hub_nodes_func,
    get_knowledge_gaps_func,
    get_suggested_questions_func,
    get_surprising_connections_func,
)

# -- build ------------------------------------------------------------------
from .build import build_or_update_graph, run_postprocess

# -- community_tools --------------------------------------------------------
from .community_tools import (
    get_architecture_overview_func,
    get_community_func,
    list_communities_func,
)

# -- context ----------------------------------------------------------------
from .context import get_minimal_context

# -- docs -------------------------------------------------------------------
from .docs import embed_graph, generate_wiki_func, get_docs_section, get_wiki_page_func

# -- flows_tools ------------------------------------------------------------
from .flows_tools import get_flow, list_flows

# -- query ------------------------------------------------------------------
from .query import (
    find_large_functions,
    get_impact_radius,
    list_graph_stats,
    query_graph,
    semantic_search_nodes,
    traverse_graph_func,
)

# -- refactor_tools ---------------------------------------------------------
from .refactor_tools import apply_refactor_func, refactor_func

# -- registry_tools ---------------------------------------------------------
from .registry_tools import cross_repo_search_func, list_repos_func

# -- review -----------------------------------------------------------------
from .review import (
    detect_changes_func,
    get_affected_flows_func,
    get_review_context,
)

__all__ = [
    # _common
    "_BUILTIN_CALL_NAMES",
    "_get_store",
    "_validate_repo_root",
    # build
    "build_or_update_graph",
    "run_postprocess",
    # context
    "get_minimal_context",
    # community_tools
    "get_architecture_overview_func",
    "get_community_func",
    "list_communities_func",
    # docs
    "embed_graph",
    "generate_wiki_func",
    "get_docs_section",
    "get_wiki_page_func",
    # flows_tools
    "get_flow",
    "list_flows",
    # query
    "find_large_functions",
    "get_impact_radius",
    "list_graph_stats",
    "query_graph",
    "semantic_search_nodes",
    "traverse_graph_func",
    # refactor_tools
    "apply_refactor_func",
    "refactor_func",
    # registry_tools
    "cross_repo_search_func",
    "list_repos_func",
    # review
    "detect_changes_func",
    "get_affected_flows_func",
    "get_review_context",
    # analysis_tools
    "get_bridge_nodes_func",
    "get_hub_nodes_func",
    "get_knowledge_gaps_func",
    "get_suggested_questions_func",
    "get_surprising_connections_func",
    # re-exported for backward compat (used in test patches)
    "get_changed_files",
    "get_staged_and_unstaged",
    "parse_git_diff_ranges",
]
