"""CLI entry point for code-review-graph.

Usage:
    code-review-graph install
    code-review-graph init
    code-review-graph build [--base BASE]
    code-review-graph update [--base BASE]
    code-review-graph watch
    code-review-graph status
    code-review-graph serve
    code-review-graph visualize
    code-review-graph wiki
    code-review-graph detect-changes [--base BASE] [--brief]
    code-review-graph register <path> [--alias name]
    code-review-graph unregister <path_or_alias>
    code-review-graph repos
    code-review-graph query <pattern> <target>
    code-review-graph impact [--files ...] [--depth N]
    code-review-graph search <query> [--kind KIND]
    code-review-graph flows [--sort COLUMN]
    code-review-graph flow [--id ID | --name NAME]
    code-review-graph communities [--sort COLUMN]
    code-review-graph community [--id ID | --name NAME]
    code-review-graph architecture
    code-review-graph large-functions [--min-lines N]
    code-review-graph refactor <mode>
"""

from __future__ import annotations

import sys

# Python version check — must come before any other imports
if sys.version_info < (3, 10):
    print("code-review-graph requires Python 3.10 or higher.")
    print(f"  You are running Python {sys.version}")
    print()
    print("Install Python 3.10+: https://www.python.org/downloads/")
    sys.exit(1)

import argparse
import json
import logging
import os
from importlib.metadata import version as pkg_version
from pathlib import Path


def _get_version() -> str:
    """Get the installed package version."""
    try:
        return pkg_version("code-review-graph")
    except Exception:
        return "dev"


def _supports_color() -> bool:
    """Check if the terminal likely supports ANSI colors."""
    if os.environ.get("NO_COLOR"):
        return False
    if not hasattr(sys.stdout, "isatty"):
        return False
    return sys.stdout.isatty()


def _print_banner() -> None:
    """Print the startup banner with graph art and available commands."""
    color = _supports_color()
    version = _get_version()

    # ANSI escape codes
    c = "\033[36m" if color else ""   # cyan — graph art
    y = "\033[33m" if color else ""   # yellow — center node
    b = "\033[1m" if color else ""    # bold
    d = "\033[2m" if color else ""    # dim
    g = "\033[32m" if color else ""   # green — commands
    r = "\033[0m" if color else ""    # reset

    print(f"""
{c}  ●──●──●{r}
{c}  │╲ │ ╱│{r}       {b}code-review-graph{r}  {d}v{version}{r}
{c}  ●──{y}◆{c}──●{r}
{c}  │╱ │ ╲│{r}       {d}Structural knowledge graph for{r}
{c}  ●──●──●{r}       {d}smarter code reviews{r}

  {b}Commands:{r}
    {g}install{r}     Set up MCP server for AI coding platforms
    {g}init{r}        Alias for install
    {g}build{r}       Full graph build {d}(parse all files){r}
    {g}update{r}      Incremental update {d}(changed files only){r}
    {g}watch{r}       Auto-update on file changes
    {g}status{r}      Show graph statistics
    {g}visualize{r}   Generate interactive HTML graph
    {g}wiki{r}        Generate markdown wiki from communities
    {g}detect-changes{r} Analyze change impact {d}(risk-scored review){r}
    {g}register{r}    Register a repository in the multi-repo registry
    {g}unregister{r}  Remove a repository from the registry
    {g}repos{r}       List registered repositories
    {g}postprocess{r} Run post-processing {d}(flows, communities, FTS){r}
    {g}eval{r}        Run evaluation benchmarks
    {g}serve{r}       Start MCP server

  {b}Graph queries:{r}
    {g}query{r}       Query relationships {d}(callers_of, callees_of, ...){r}
    {g}impact{r}      Analyze blast radius of changes
    {g}search{r}      Search code entities by name/keyword
    {g}flows{r}       List execution flows by criticality
    {g}flow{r}        Get details of a single execution flow
    {g}communities{r} List detected code communities
    {g}community{r}   Get details of a single community
    {g}architecture{r} Architecture overview from communities
    {g}large-functions{r} Find functions exceeding line threshold
    {g}refactor{r}    Rename preview, dead code, suggestions

  {d}Run{r} {b}code-review-graph <command> --help{r} {d}for details{r}
""")


def _handle_init(args: argparse.Namespace) -> None:
    """Set up MCP config for detected AI coding platforms."""
    from .incremental import ensure_repo_gitignore_excludes_crg, find_repo_root
    from .skills import install_platform_configs

    repo_root = Path(args.repo) if args.repo else find_repo_root()
    if not repo_root:
        repo_root = Path.cwd()

    dry_run = getattr(args, "dry_run", False)
    target = getattr(args, "platform", "all") or "all"
    if target == "claude-code":
        target = "claude"

    print("Installing MCP server config...")
    configured = install_platform_configs(repo_root, target=target, dry_run=dry_run)

    if not configured:
        print("No platforms detected.")
    else:
        print(f"\nConfigured {len(configured)} platform(s): {', '.join(configured)}")

    if dry_run:
        print("[dry-run] Would ensure .gitignore ignores .code-review-graph/.")
        print("\n[dry-run] No files were modified.")
        return

    gitignore_state = ensure_repo_gitignore_excludes_crg(repo_root)
    if gitignore_state == "created":
        print("Created .gitignore and added .code-review-graph/.")
    elif gitignore_state == "updated":
        print("Updated .gitignore with .code-review-graph/.")
    else:
        print(".gitignore already contains .code-review-graph/.")

    # Skills and hooks are installed by default so Claude actually uses the
    # graph tools proactively.  Use --no-skills / --no-hooks to opt out.
    skip_skills = getattr(args, "no_skills", False)
    skip_hooks = getattr(args, "no_hooks", False)
    # Legacy: --skills/--hooks/--all still accepted (no-op, everything is default)

    from .skills import (
        generate_skills,
        inject_claude_md,
        inject_platform_instructions,
        install_git_hook,
        install_hooks,
    )

    if not skip_skills:
        skills_dir = generate_skills(repo_root)
        print(f"Generated skills in {skills_dir}")
        if target in ("claude", "all"):
            inject_claude_md(repo_root)
        updated = inject_platform_instructions(repo_root, target=target)
        if updated:
            print(f"Injected graph instructions into: {', '.join(updated)}")

    if not skip_hooks and target in ("claude", "all"):
        install_hooks(repo_root)
        print(f"Installed hooks in {repo_root / '.claude' / 'settings.json'}")
        git_hook = install_git_hook(repo_root)
        if git_hook:
            print(f"Installed git pre-commit hook in {git_hook}")

    print()
    print("Next steps:")
    print("  1. code-review-graph build    # build the knowledge graph")
    print("  2. Restart your AI coding tool to pick up the new config")


def _run_post_processing(store: object) -> None:
    """Run post-build steps: signatures, FTS indexing, flow detection, communities.

    Mirrors the post-processing in tools/build.py. Each step is non-fatal;
    failures are logged and skipped so the build result is never lost.
    """
    import sqlite3

    # Compute signatures for nodes that don't have them
    try:
        rows = store.get_nodes_without_signature()  # type: ignore[attr-defined]
        for row in rows:
            node_id, name, kind, params, ret = row[0], row[1], row[2], row[3], row[4]
            if kind in ("Function", "Test"):
                sig = f"def {name}({params or ''})"
                if ret:
                    sig += f" -> {ret}"
            elif kind == "Class":
                sig = f"class {name}"
            else:
                sig = name
            store.update_node_signature(node_id, sig[:512])  # type: ignore[attr-defined]
        store.commit()  # type: ignore[attr-defined]
        sig_count = len(rows) if rows else 0
        if sig_count:
            print(f"Signatures computed: {sig_count} nodes")
    except (sqlite3.OperationalError, TypeError, KeyError, AttributeError) as e:
        store.rollback()
        logging.warning("Signature computation skipped: %s", e)

    # Rebuild FTS index
    try:
        from .search import rebuild_fts_index

        fts_count = rebuild_fts_index(store)
        print(f"FTS indexed: {fts_count} nodes")
    except (sqlite3.OperationalError, ImportError, AttributeError) as e:
        store.rollback()
        logging.warning("FTS index rebuild skipped: %s", e)

    # Trace execution flows
    try:
        from .flows import store_flows as _store_flows
        from .flows import trace_flows as _trace_flows

        flows = _trace_flows(store)
        count = _store_flows(store, flows)
        print(f"Flows detected: {count}")
    except (sqlite3.OperationalError, ImportError, AttributeError) as e:
        store.rollback()
        logging.warning("Flow detection skipped: %s", e)

    # Detect communities
    try:
        from .communities import detect_communities as _detect_communities
        from .communities import store_communities as _store_communities

        comms = _detect_communities(store)
        count = _store_communities(store, comms)
        print(f"Communities: {count}")
    except (sqlite3.OperationalError, ImportError, AttributeError) as e:
        store.rollback()
        logging.warning("Community detection skipped: %s", e)


def main() -> None:
    """Main CLI entry point."""
    ap = argparse.ArgumentParser(
        prog="code-review-graph",
        description="Persistent incremental knowledge graph for code reviews",
    )
    ap.add_argument(
        "-v", "--version", action="store_true", help="Show version and exit"
    )
    sub = ap.add_subparsers(dest="command")

    # install (primary) + init (alias)
    install_cmd = sub.add_parser(
        "install", help="Register MCP server with AI coding platforms"
    )
    install_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")
    install_cmd.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be done without writing files",
    )
    install_cmd.add_argument(
        "--no-skills", action="store_true",
        help="Skip generating Claude Code skill files",
    )
    install_cmd.add_argument(
        "--no-hooks", action="store_true",
        help="Skip installing Claude Code hooks",
    )
    # Legacy flags (kept for backwards compat, now no-ops since all is default)
    install_cmd.add_argument("--skills", action="store_true", help=argparse.SUPPRESS)
    install_cmd.add_argument("--hooks", action="store_true", help=argparse.SUPPRESS)
    install_cmd.add_argument("--all", action="store_true", dest="install_all",
                             help=argparse.SUPPRESS)
    install_cmd.add_argument(
        "--platform",
        choices=[
            "codex", "claude", "claude-code", "cursor", "windsurf", "zed",
            "continue", "opencode", "antigravity", "all",
        ],
        default="all",
        help="Target platform for MCP config (default: all detected)",
    )

    init_cmd = sub.add_parser(
        "init", help="Alias for install"
    )
    init_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")
    init_cmd.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be done without writing files",
    )
    init_cmd.add_argument(
        "--no-skills", action="store_true",
        help="Skip generating Claude Code skill files",
    )
    init_cmd.add_argument(
        "--no-hooks", action="store_true",
        help="Skip installing Claude Code hooks",
    )
    init_cmd.add_argument("--skills", action="store_true", help=argparse.SUPPRESS)
    init_cmd.add_argument("--hooks", action="store_true", help=argparse.SUPPRESS)
    init_cmd.add_argument("--all", action="store_true", dest="install_all",
                             help=argparse.SUPPRESS)
    init_cmd.add_argument(
        "--platform",
        choices=[
            "codex", "claude", "claude-code", "cursor", "windsurf", "zed",
            "continue", "opencode", "antigravity", "all",
        ],
        default="all",
        help="Target platform for MCP config (default: all detected)",
    )

    # build
    build_cmd = sub.add_parser("build", help="Full graph build (re-parse all files)")
    build_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")
    build_cmd.add_argument(
        "--skip-flows", action="store_true",
        help="Skip flow/community detection (signatures + FTS only)",
    )
    build_cmd.add_argument(
        "--skip-postprocess", action="store_true",
        help="Skip all post-processing (raw parse only)",
    )

    # update
    update_cmd = sub.add_parser("update", help="Incremental update (only changed files)")
    update_cmd.add_argument("--base", default="HEAD~1", help="Git diff base (default: HEAD~1)")
    update_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")
    update_cmd.add_argument(
        "--skip-flows", action="store_true",
        help="Skip flow/community detection (signatures + FTS only)",
    )
    update_cmd.add_argument(
        "--skip-postprocess", action="store_true",
        help="Skip all post-processing (raw parse only)",
    )

    # postprocess
    pp_cmd = sub.add_parser(
        "postprocess",
        help="Run post-processing on existing graph (flows, communities, FTS)",
    )
    pp_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")
    pp_cmd.add_argument("--no-flows", action="store_true", help="Skip flow detection")
    pp_cmd.add_argument("--no-communities", action="store_true", help="Skip community detection")
    pp_cmd.add_argument("--no-fts", action="store_true", help="Skip FTS rebuild")

    # watch
    watch_cmd = sub.add_parser("watch", help="Watch for changes and auto-update")
    watch_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")

    # status
    status_cmd = sub.add_parser("status", help="Show graph statistics")
    status_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")

    # visualize
    vis_cmd = sub.add_parser("visualize", help="Generate interactive HTML graph visualization")
    vis_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")
    vis_cmd.add_argument(
        "--mode",
        choices=["auto", "full", "community", "file"],
        default="auto",
        help="Rendering mode: auto (default), full, community, or file",
    )
    vis_cmd.add_argument(
        "--serve", action="store_true",
        help="Start a local HTTP server to view the visualization (localhost:8765)",
    )

    # wiki
    wiki_cmd = sub.add_parser("wiki", help="Generate markdown wiki from community structure")
    wiki_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")
    wiki_cmd.add_argument(
        "--force", action="store_true",
        help="Regenerate all pages even if content unchanged",
    )

    # register
    register_cmd = sub.add_parser(
        "register", help="Register a repository in the multi-repo registry"
    )
    register_cmd.add_argument("path", help="Path to the repository root")
    register_cmd.add_argument("--alias", default=None, help="Short alias for the repository")

    # unregister
    unregister_cmd = sub.add_parser(
        "unregister", help="Remove a repository from the multi-repo registry"
    )
    unregister_cmd.add_argument("path_or_alias", help="Repository path or alias to remove")

    # repos
    sub.add_parser("repos", help="List registered repositories")

    # eval
    eval_cmd = sub.add_parser("eval", help="Run evaluation benchmarks")
    eval_cmd.add_argument(
        "--benchmark", default=None,
        help="Comma-separated benchmarks to run (token_efficiency, impact_accuracy, "
             "flow_completeness, search_quality, build_performance)",
    )
    eval_cmd.add_argument("--repo", default=None, help="Comma-separated repo config names")
    eval_cmd.add_argument("--all", action="store_true", dest="run_all", help="Run all benchmarks")
    eval_cmd.add_argument("--report", action="store_true", help="Generate report from results")
    eval_cmd.add_argument("--output-dir", default=None, help="Output directory for results")

    # detect-changes
    detect_cmd = sub.add_parser("detect-changes", help="Analyze change impact")
    detect_cmd.add_argument(
        "--base", default="HEAD~1", help="Git diff base (default: HEAD~1)"
    )
    detect_cmd.add_argument(
        "--brief", action="store_true", help="Show brief summary only"
    )
    detect_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")

    # serve
    serve_cmd = sub.add_parser("serve", help="Start MCP server (stdio transport)")
    serve_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")

    # --- CLI-first commands (expose MCP tool functions directly) ---

    # query
    query_cmd = sub.add_parser(
        "query", help="Query graph relationships (callers_of, callees_of, imports_of, etc.)"
    )
    query_cmd.add_argument(
        "pattern",
        choices=[
            "callers_of", "callees_of", "imports_of", "importers_of",
            "children_of", "tests_for", "inheritors_of", "file_summary",
        ],
        help="Query pattern",
    )
    query_cmd.add_argument("target", help="Node name, qualified name, or file path")
    query_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")

    # impact
    impact_cmd = sub.add_parser(
        "impact", help="Analyze blast radius of changed files"
    )
    impact_cmd.add_argument(
        "--files", nargs="*", default=None,
        help="Changed files (auto-detected from git if omitted)",
    )
    impact_cmd.add_argument("--depth", type=int, default=2, help="BFS hops (default: 2)")
    impact_cmd.add_argument("--base", default="HEAD~1", help="Git diff base (default: HEAD~1)")
    impact_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")

    # search
    search_cmd = sub.add_parser("search", help="Search code entities by name/keyword")
    search_cmd.add_argument("query", help="Search string")
    search_cmd.add_argument(
        "--kind", default=None,
        choices=["File", "Class", "Function", "Type", "Test"],
        help="Filter by entity kind",
    )
    search_cmd.add_argument("--limit", type=int, default=20, help="Max results (default: 20)")
    search_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")

    # flows
    flows_cmd = sub.add_parser("flows", help="List execution flows by criticality")
    flows_cmd.add_argument(
        "--sort", default="criticality",
        choices=["criticality", "depth", "node_count", "file_count", "name"],
        help="Sort column (default: criticality)",
    )
    flows_cmd.add_argument("--limit", type=int, default=20, help="Max results (default: 20)")
    flows_cmd.add_argument(
        "--kind", default=None, help="Filter by entry point kind (e.g. Test, Function)"
    )
    flows_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")

    # flow (single)
    flow_cmd = sub.add_parser("flow", help="Get details of a single execution flow")
    flow_cmd.add_argument("--id", type=int, default=None, help="Flow ID")
    flow_cmd.add_argument("--name", default=None, help="Flow name (partial match)")
    flow_cmd.add_argument(
        "--source", action="store_true", help="Include source code snippets"
    )
    flow_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")

    # communities
    comms_cmd = sub.add_parser("communities", help="List detected code communities")
    comms_cmd.add_argument(
        "--sort", default="size", choices=["size", "cohesion", "name"],
        help="Sort column (default: size)",
    )
    comms_cmd.add_argument("--min-size", type=int, default=0, help="Min community size")
    comms_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")

    # community (single)
    comm_cmd = sub.add_parser("community", help="Get details of a single community")
    comm_cmd.add_argument("--id", type=int, default=None, help="Community ID")
    comm_cmd.add_argument("--name", default=None, help="Community name (partial match)")
    comm_cmd.add_argument(
        "--members", action="store_true", help="Include member node details"
    )
    comm_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")

    # architecture
    arch_cmd = sub.add_parser("architecture", help="Architecture overview from community structure")
    arch_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")

    # large-functions
    large_cmd = sub.add_parser(
        "large-functions", help="Find functions/classes exceeding line threshold"
    )
    large_cmd.add_argument("--min-lines", type=int, default=50, help="Min lines (default: 50)")
    large_cmd.add_argument(
        "--kind", default=None, choices=["Function", "Class", "File", "Test"],
        help="Filter by kind",
    )
    large_cmd.add_argument("--path", default=None, help="Filter by file path substring")
    large_cmd.add_argument("--limit", type=int, default=50, help="Max results (default: 50)")
    large_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")

    # refactor
    refactor_cmd = sub.add_parser(
        "refactor", help="Rename preview, dead code detection, suggestions"
    )
    refactor_cmd.add_argument(
        "mode", choices=["rename", "dead_code", "suggest"],
        help="Operation mode",
    )
    refactor_cmd.add_argument("--old-name", default=None, help="(rename) Current symbol name")
    refactor_cmd.add_argument("--new-name", default=None, help="(rename) New symbol name")
    refactor_cmd.add_argument(
        "--kind", default=None, choices=["Function", "Class"],
        help="(dead_code) Filter by kind",
    )
    refactor_cmd.add_argument("--path", default=None, help="(dead_code) Filter by file path")
    refactor_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")

    args = ap.parse_args()

    if args.version:
        print(f"code-review-graph {_get_version()}")
        return

    if not args.command:
        _print_banner()
        return

    if args.command == "serve":
        from .main import main as serve_main
        serve_main(repo_root=args.repo)
        return

    if args.command == "eval":
        from .eval.reporter import generate_full_report, generate_readme_tables
        from .eval.runner import run_eval

        if getattr(args, "report", False):
            output_dir = Path(
                getattr(args, "output_dir", None) or "evaluate/results"
            )
            report = generate_full_report(output_dir)
            report_path = Path("evaluate/reports/summary.md")
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(report, encoding="utf-8")
            print(f"Report written to {report_path}")

            tables = generate_readme_tables(output_dir)
            print("\n--- README Tables (copy-paste) ---\n")
            print(tables)
        else:
            repos = (
                [r.strip() for r in args.repo.split(",")]
                if getattr(args, "repo", None)
                else None
            )
            benchmarks = (
                [b.strip() for b in args.benchmark.split(",")]
                if getattr(args, "benchmark", None)
                else None
            )

            if not repos and not benchmarks and not getattr(args, "run_all", False):
                print("Specify --all, --repo, or --benchmark. See --help.")
                return

            results = run_eval(
                repos=repos,
                benchmarks=benchmarks,
                output_dir=getattr(args, "output_dir", None),
            )
            print(f"\nCompleted {len(results)} benchmark(s).")
            print("Run 'code-review-graph eval --report' to generate tables.")
        return

    if args.command in ("init", "install"):
        _handle_init(args)
        return

    if args.command in ("register", "unregister", "repos"):
        logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
        from .registry import Registry

        registry = Registry()
        if args.command == "register":
            try:
                entry = registry.register(args.path, alias=args.alias)
                alias_info = f" (alias: {entry['alias']})" if entry.get("alias") else ""
                print(f"Registered: {entry['path']}{alias_info}")
            except ValueError as exc:
                logging.error(str(exc))
                sys.exit(1)
        elif args.command == "unregister":
            if registry.unregister(args.path_or_alias):
                print(f"Unregistered: {args.path_or_alias}")
            else:
                print(f"Not found: {args.path_or_alias}")
                sys.exit(1)
        elif args.command == "repos":
            repos = registry.list_repos()
            if not repos:
                print("No repositories registered.")
                print("Use: code-review-graph register <path> [--alias name]")
            else:
                for entry in repos:
                    alias = entry.get("alias", "")
                    alias_str = f"  ({alias})" if alias else ""
                    print(f"  {entry['path']}{alias_str}")
        return

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    from .incremental import (
        find_project_root,
        find_repo_root,
        get_db_path,
        watch,
    )

    # Commands that delegate to tool functions which create their own GraphStore.
    # We skip opening a redundant store for these.
    DELEGATED_COMMANDS = {
        "query", "impact", "search", "flows", "flow",
        "communities", "community", "architecture",
        "large-functions", "refactor", "postprocess",
    }

    if args.command in ("update", "detect-changes", "impact"):
        # update, detect-changes, and impact require git for diffing
        repo_root = Path(args.repo) if args.repo else find_repo_root()
        if not repo_root:
            logging.error(
                "Not in a git repository. '%s' requires git for diffing.",
                args.command,
            )
            logging.error("Use 'build' for a full parse, or run 'git init' first.")
            sys.exit(1)
    else:
        repo_root = Path(args.repo) if args.repo else find_project_root()

    if args.command in DELEGATED_COMMANDS:
        # These commands handle their own store lifecycle
        if args.command == "postprocess":
            from .tools.build import run_postprocess
            result = run_postprocess(
                flows=not getattr(args, "no_flows", False),
                communities=not getattr(args, "no_communities", False),
                fts=not getattr(args, "no_fts", False),
                repo_root=str(repo_root),
            )
            parts = []
            if result.get("flows_detected"):
                parts.append(f"{result['flows_detected']} flows")
            if result.get("communities_detected"):
                parts.append(f"{result['communities_detected']} communities")
            if result.get("fts_indexed"):
                parts.append(f"{result['fts_indexed']} FTS entries")
            print(f"Post-processing: {', '.join(parts) or 'done'}")

        elif args.command == "query":
            from .tools import query_graph
            result = query_graph(
                pattern=args.pattern, target=args.target,
                repo_root=str(repo_root),
            )
            print(json.dumps(result, indent=2, default=str))

        elif args.command == "impact":
            from .tools import get_impact_radius
            result = get_impact_radius(
                changed_files=args.files, max_depth=args.depth,
                repo_root=str(repo_root), base=args.base,
            )
            print(json.dumps(result, indent=2, default=str))

        elif args.command == "search":
            from .tools import semantic_search_nodes
            result = semantic_search_nodes(
                query=args.query, kind=args.kind, limit=args.limit,
                repo_root=str(repo_root),
            )
            print(json.dumps(result, indent=2, default=str))

        elif args.command == "flows":
            from .tools import list_flows
            result = list_flows(
                repo_root=str(repo_root), sort_by=args.sort,
                limit=args.limit, kind=args.kind,
            )
            print(json.dumps(result, indent=2, default=str))

        elif args.command == "flow":
            if args.id is None and args.name is None:
                print("Error: provide --id or --name to select a flow.")
                sys.exit(1)
            from .tools import get_flow
            result = get_flow(
                flow_id=args.id, flow_name=args.name,
                include_source=args.source, repo_root=str(repo_root),
            )
            print(json.dumps(result, indent=2, default=str))

        elif args.command == "communities":
            from .tools import list_communities_func
            result = list_communities_func(
                repo_root=str(repo_root), sort_by=args.sort,
                min_size=args.min_size,
            )
            print(json.dumps(result, indent=2, default=str))

        elif args.command == "community":
            if args.id is None and args.name is None:
                print("Error: provide --id or --name to select a community.")
                sys.exit(1)
            from .tools import get_community_func
            result = get_community_func(
                community_name=args.name, community_id=args.id,
                include_members=args.members, repo_root=str(repo_root),
            )
            print(json.dumps(result, indent=2, default=str))

        elif args.command == "architecture":
            from .tools import get_architecture_overview_func
            result = get_architecture_overview_func(repo_root=str(repo_root))
            print(json.dumps(result, indent=2, default=str))

        elif args.command == "large-functions":
            from .tools import find_large_functions
            result = find_large_functions(
                min_lines=args.min_lines, kind=args.kind,
                file_path_pattern=args.path, limit=args.limit,
                repo_root=str(repo_root),
            )
            print(json.dumps(result, indent=2, default=str))

        elif args.command == "refactor":
            if args.mode == "rename" and (not args.old_name or not args.new_name):
                print("Error: refactor rename requires --old-name and --new-name.")
                sys.exit(1)
            from .tools import refactor_func
            result = refactor_func(
                mode=args.mode, old_name=args.old_name,
                new_name=args.new_name, kind=args.kind,
                file_pattern=args.path, repo_root=str(repo_root),
            )
            print(json.dumps(result, indent=2, default=str))

        return
        repo_root = Path(args.repo) if args.repo else find_repo_root()
        if not repo_root:
            logging.error(
                "Not in a git repository. '%s' requires git for diffing.",
                args.command,
            )
            logging.error("Use 'build' for a full parse, or run 'git init' first.")
            sys.exit(1)
    else:
        repo_root = Path(args.repo) if args.repo else find_project_root()

    db_path = get_db_path(repo_root)

    # For delegated commands, warn if the graph hasn't been built yet, then
    # delegate directly to the tool functions (they manage their own store).
    if args.command in DELEGATED_COMMANDS:
        if not db_path.exists():
            print(
                "WARNING: Graph not built yet. "
                "Run 'code-review-graph build' first."
            )
            print()
        _run_delegated_command(args, repo_root)
        return

    # For non-delegated commands that need the graph DB (everything except build),
    # warn if the DB is missing.
    if args.command != "build" and not db_path.exists():
        print(
            "WARNING: Graph not built yet. "
            "Run 'code-review-graph build' first."
        )
        print()

    from .graph import GraphStore
    from .incremental import (
        full_build,
        incremental_update,
        watch,
    )

    store = GraphStore(db_path)

    try:
        if args.command == "build":
            pp = "none" if getattr(args, "skip_postprocess", False) else (
                "minimal" if getattr(args, "skip_flows", False) else "full"
            )
            from .tools.build import build_or_update_graph
            result = build_or_update_graph(
                full_rebuild=True, repo_root=str(repo_root), postprocess=pp,
            )
            parsed = result.get("files_parsed", 0)
            nodes = result.get("total_nodes", 0)
            edges = result.get("total_edges", 0)
            print(
                f"Full build: {parsed} files, "
                f"{nodes} nodes, {edges} edges"
                f" (postprocess={pp})"
            )
            if result.get("errors"):
                print(f"Errors: {len(result['errors'])}")
            _run_post_processing(store)

        elif args.command == "update":
            pp = "none" if getattr(args, "skip_postprocess", False) else (
                "minimal" if getattr(args, "skip_flows", False) else "full"
            )
            from .tools.build import build_or_update_graph
            result = build_or_update_graph(
                full_rebuild=False, repo_root=str(repo_root),
                base=args.base, postprocess=pp,
            )
            updated = result.get("files_updated", 0)
            nodes = result.get("total_nodes", 0)
            edges = result.get("total_edges", 0)
            print(
                f"Incremental: {updated} files updated, "
                f"{nodes} nodes, {edges} edges"
                f" (postprocess={pp})"
            )
            if result.get("files_updated", 0) > 0:
                _run_post_processing(store)

        elif args.command == "status":
            stats = store.get_stats()
            print(f"Nodes: {stats.total_nodes}")
            print(f"Edges: {stats.total_edges}")
            print(f"Files: {stats.files_count}")
            print(f"Languages: {', '.join(stats.languages)}")
            print(f"Last updated: {stats.last_updated or 'never'}")
            # Show branch info and warn if stale
            stored_branch = store.get_metadata("git_branch")
            stored_sha = store.get_metadata("git_head_sha")
            if stored_branch:
                print(f"Built on branch: {stored_branch}")
            if stored_sha:
                print(f"Built at commit: {stored_sha[:12]}")
            from .incremental import _git_branch_info
            current_branch, current_sha = _git_branch_info(repo_root)
            if stored_branch and current_branch and stored_branch != current_branch:
                print(
                    f"WARNING: Graph was built on '{stored_branch}' "
                    f"but you are now on '{current_branch}'. "
                    f"Run 'code-review-graph build' to rebuild."
                )

        elif args.command == "watch":
            watch(repo_root, store)

        elif args.command == "visualize":
            from .visualization import generate_html
            html_path = repo_root / ".code-review-graph" / "graph.html"
            vis_mode = getattr(args, "mode", "auto") or "auto"
            generate_html(store, html_path, mode=vis_mode)
            print(f"Visualization ({vis_mode}): {html_path}")
            if getattr(args, "serve", False):
                import functools
                import http.server

                serve_dir = html_path.parent
                port = 8765
                handler = functools.partial(
                    http.server.SimpleHTTPRequestHandler,
                    directory=str(serve_dir),
                )
                print(f"Serving at http://localhost:{port}/graph.html")
                print("Press Ctrl+C to stop.")
                with http.server.HTTPServer(("localhost", port), handler) as httpd:
                    try:
                        httpd.serve_forever()
                    except KeyboardInterrupt:
                        print("\nServer stopped.")
            else:
                print("Open in browser to explore your codebase graph.")

        elif args.command == "wiki":
            from .wiki import generate_wiki
            wiki_dir = repo_root / ".code-review-graph" / "wiki"
            result = generate_wiki(store, wiki_dir, force=args.force)
            total = result["pages_generated"] + result["pages_updated"] + result["pages_unchanged"]
            print(
                f"Wiki: {result['pages_generated']} new, "
                f"{result['pages_updated']} updated, "
                f"{result['pages_unchanged']} unchanged "
                f"({total} total pages)"
            )
            print(f"Output: {wiki_dir}")

        elif args.command == "detect-changes":
            from .changes import analyze_changes
            from .incremental import get_changed_files, get_staged_and_unstaged

            base = args.base
            changed = get_changed_files(repo_root, base)
            if not changed:
                changed = get_staged_and_unstaged(repo_root)

            if not changed:
                print("No changes detected.")
            else:
                result = analyze_changes(
                    store,
                    changed,
                    repo_root=str(repo_root),
                    base=base,
                )
                if args.brief:
                    print(result.get("summary", "No summary available."))
                else:
                    print(json.dumps(result, indent=2, default=str))

    finally:
        store.close()


def _run_delegated_command(args: argparse.Namespace, repo_root: Path) -> None:
    """Run commands that delegate to tool functions with their own GraphStore."""
    if args.command == "query":
        from .tools import query_graph
        result = query_graph(
            pattern=args.pattern, target=args.target,
            repo_root=str(repo_root),
        )
        print(json.dumps(result, indent=2, default=str))

    elif args.command == "impact":
        from .tools import get_impact_radius
        result = get_impact_radius(
            changed_files=args.files, max_depth=args.depth,
            repo_root=str(repo_root), base=args.base,
        )
        print(json.dumps(result, indent=2, default=str))

    elif args.command == "search":
        from .tools import semantic_search_nodes
        result = semantic_search_nodes(
            query=args.query, kind=args.kind, limit=args.limit,
            repo_root=str(repo_root),
        )
        print(json.dumps(result, indent=2, default=str))

    elif args.command == "flows":
        from .tools import list_flows
        result = list_flows(
            repo_root=str(repo_root), sort_by=args.sort,
            limit=args.limit, kind=args.kind,
        )
        print(json.dumps(result, indent=2, default=str))

    elif args.command == "flow":
        if args.id is None and args.name is None:
            print("Error: provide --id or --name to select a flow.")
            sys.exit(1)
        from .tools import get_flow
        result = get_flow(
            flow_id=args.id, flow_name=args.name,
            include_source=args.source, repo_root=str(repo_root),
        )
        print(json.dumps(result, indent=2, default=str))

    elif args.command == "communities":
        from .tools import list_communities_func
        result = list_communities_func(
            repo_root=str(repo_root), sort_by=args.sort,
            min_size=args.min_size,
        )
        print(json.dumps(result, indent=2, default=str))

    elif args.command == "community":
        if args.id is None and args.name is None:
            print("Error: provide --id or --name to select a community.")
            sys.exit(1)
        from .tools import get_community_func
        result = get_community_func(
            community_name=args.name, community_id=args.id,
            include_members=args.members, repo_root=str(repo_root),
        )
        print(json.dumps(result, indent=2, default=str))

    elif args.command == "architecture":
        from .tools import get_architecture_overview_func
        result = get_architecture_overview_func(repo_root=str(repo_root))
        print(json.dumps(result, indent=2, default=str))

    elif args.command == "large-functions":
        from .tools import find_large_functions
        result = find_large_functions(
            min_lines=args.min_lines, kind=args.kind,
            file_path_pattern=args.path, limit=args.limit,
            repo_root=str(repo_root),
        )
        print(json.dumps(result, indent=2, default=str))

    elif args.command == "refactor":
        if args.mode == "rename" and (not args.old_name or not args.new_name):
            print("Error: refactor rename requires --old-name and --new-name.")
            sys.exit(1)
        from .tools import refactor_func
        result = refactor_func(
            mode=args.mode, old_name=args.old_name,
            new_name=args.new_name, kind=args.kind,
            file_pattern=args.path, repo_root=str(repo_root),
        )
        print(json.dumps(result, indent=2, default=str))
