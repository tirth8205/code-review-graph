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
    {g}install{r}     Set up Claude Code integration
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
    {g}eval{r}        Run evaluation benchmarks
    {g}serve{r}       Start MCP server

  {d}Run{r} {b}code-review-graph <command> --help{r} {d}for details{r}
""")


def _handle_init(args: argparse.Namespace) -> None:
    """Set up .mcp.json in the project root for Claude Code integration."""
    from .incremental import find_repo_root

    repo_root = Path(args.repo) if args.repo else find_repo_root()
    if not repo_root:
        repo_root = Path.cwd()

    mcp_path = repo_root / ".mcp.json"
    dry_run = getattr(args, "dry_run", False)

    mcp_config = {
        "mcpServers": {
            "code-review-graph": {
                "command": "uvx",
                "args": ["code-review-graph", "serve"],
            }
        }
    }

    # Merge into existing .mcp.json if present
    if mcp_path.exists():
        try:
            existing = json.loads(mcp_path.read_text())
            if "code-review-graph" in existing.get("mcpServers", {}):
                print(f"Already configured in {mcp_path}")
            else:
                existing.setdefault("mcpServers", {}).update(mcp_config["mcpServers"])
                mcp_config = existing
                if not dry_run:
                    mcp_path.write_text(json.dumps(mcp_config, indent=2) + "\n")
                    print(f"Created {mcp_path}")
        except json.JSONDecodeError:
            print(f"Warning: existing {mcp_path} has invalid JSON, overwriting.")
            if not dry_run:
                mcp_path.write_text(json.dumps(mcp_config, indent=2) + "\n")
                print(f"Created {mcp_path}")
        except (KeyError, TypeError):
            print(f"Warning: existing {mcp_path} has unexpected structure, overwriting.")
            if not dry_run:
                mcp_path.write_text(json.dumps(mcp_config, indent=2) + "\n")
                print(f"Created {mcp_path}")
    else:
        if not dry_run:
            mcp_path.write_text(json.dumps(mcp_config, indent=2) + "\n")
            print(f"Created {mcp_path}")

    if dry_run:
        print(f"[dry-run] Would write to {mcp_path}:")
        print(json.dumps(mcp_config, indent=2))
        print()
        print("[dry-run] No files were modified.")
        return

    # Handle --skills, --hooks, --all flags
    install_all = getattr(args, "install_all", False)
    want_skills = getattr(args, "skills", False) or install_all
    want_hooks = getattr(args, "hooks", False) or install_all

    if want_skills or want_hooks:
        from .skills import generate_skills, inject_claude_md, install_hooks

        if want_skills:
            skills_dir = generate_skills(repo_root)
            print(f"Generated skills in {skills_dir}")
            inject_claude_md(repo_root)
            print("Updated CLAUDE.md with MCP tools reference")

        if want_hooks:
            install_hooks(repo_root)
            print(f"Installed hooks in {repo_root / '.claude' / 'settings.json'}")

    print()
    print("Next steps:")
    print("  1. code-review-graph build    # build the knowledge graph")
    print("  2. Restart Claude Code        # to pick up the new MCP server")


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
        "install", help="Register MCP server with Claude Code (creates .mcp.json)"
    )
    install_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")
    install_cmd.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be done without writing files",
    )
    install_cmd.add_argument(
        "--skills", action="store_true",
        help="Generate Claude Code skill files in .claude/skills/",
    )
    install_cmd.add_argument(
        "--hooks", action="store_true",
        help="Install Claude Code hooks in .claude/settings.json",
    )
    install_cmd.add_argument(
        "--all", action="store_true", dest="install_all",
        help="Install skills, hooks, and CLAUDE.md integration",
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
        "--skills", action="store_true",
        help="Generate Claude Code skill files in .claude/skills/",
    )
    init_cmd.add_argument(
        "--hooks", action="store_true",
        help="Install Claude Code hooks in .claude/settings.json",
    )
    init_cmd.add_argument(
        "--all", action="store_true", dest="install_all",
        help="Install skills, hooks, and CLAUDE.md integration",
    )

    # build
    build_cmd = sub.add_parser("build", help="Full graph build (re-parse all files)")
    build_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")

    # update
    update_cmd = sub.add_parser("update", help="Incremental update (only changed files)")
    update_cmd.add_argument("--base", default="HEAD~1", help="Git diff base (default: HEAD~1)")
    update_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")

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
        "--benchmark", default="token_efficiency",
        help="Benchmark to run (token_efficiency, mrr, precision_recall)",
    )
    eval_cmd.add_argument("--repo", default=None, help="Target repository for benchmarking")

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
        print(f"Benchmark runner for '{args.benchmark}' is not yet implemented.")
        print("The scorer and reporter modules are available for programmatic use:")
        print("  from code_review_graph.eval import (")
        print("      compute_token_efficiency, compute_mrr,")
        print("      compute_precision_recall, generate_markdown_report)")
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

    from .graph import GraphStore
    from .incremental import (
        find_project_root,
        find_repo_root,
        full_build,
        get_db_path,
        incremental_update,
        watch,
    )

    if args.command in ("update", "detect-changes"):
        # update and detect-changes require git for diffing
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
    store = GraphStore(db_path)

    try:
        if args.command == "build":
            result = full_build(repo_root, store)
            print(
                f"Full build: {result['files_parsed']} files, "
                f"{result['total_nodes']} nodes, {result['total_edges']} edges"
            )
            if result["errors"]:
                print(f"Errors: {len(result['errors'])}")

        elif args.command == "update":
            result = incremental_update(repo_root, store, base=args.base)
            print(
                f"Incremental: {result['files_updated']} files updated, "
                f"{result['total_nodes']} nodes, {result['total_edges']} edges"
            )

        elif args.command == "status":
            stats = store.get_stats()
            print(f"Nodes: {stats.total_nodes}")
            print(f"Edges: {stats.total_edges}")
            print(f"Files: {stats.files_count}")
            print(f"Languages: {', '.join(stats.languages)}")
            print(f"Last updated: {stats.last_updated or 'never'}")

        elif args.command == "watch":
            watch(repo_root, store)

        elif args.command == "visualize":
            from .visualization import generate_html
            html_path = repo_root / ".code-review-graph" / "graph.html"
            generate_html(store, html_path)
            print(f"Visualization: {html_path}")
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
