"""CLI entry point for code-review-graph.

Usage:
    code-review-graph install
    code-review-graph init
    code-review-graph build [--base BASE]
    code-review-graph update [--base BASE]
    code-review-graph watch
    code-review-graph status
    code-review-graph serve [--http] [--host ADDR] [--port PORT]
    code-review-graph visualize
    code-review-graph wiki
    code-review-graph detect-changes [--base BASE] [--brief]
    code-review-graph register <path> [--alias name]
    code-review-graph unregister <path_or_alias>
    code-review-graph repos
    code-review-graph daemon start [--foreground]
    code-review-graph daemon stop
    code-review-graph daemon restart [--foreground]
    code-review-graph daemon status
    code-review-graph daemon logs [--repo ALIAS] [-f] [-n N]
    code-review-graph daemon add <path> [--alias NAME]
    code-review-graph daemon remove <path_or_alias>
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
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .graph import GraphStore

logger = logging.getLogger(__name__)

# Shared platform choices for install and init commands
_PLATFORM_CHOICES = [
    "codex", "claude", "claude-code", "cursor", "windsurf", "zed",
    "continue", "opencode", "antigravity", "gemini-cli", "qwen", "kiro", "qoder", "all",
]


def _get_version() -> str:
    """Get the installed package version."""
    try:
        return pkg_version("code-review-graph")
    except PackageNotFoundError as exc:
        logger.debug("Package metadata unavailable, falling back to 'dev': %s", exc)
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
    c = "\033[36m" if color else ""  # cyan — graph art
    y = "\033[33m" if color else ""  # yellow — center node
    b = "\033[1m" if color else ""  # bold
    d = "\033[2m" if color else ""  # dim
    g = "\033[32m" if color else ""  # green — commands
    r = "\033[0m" if color else ""  # reset

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
    {g}daemon{r}      Multi-repo watch daemon management
    {g}eval{r}        Run evaluation benchmarks
    {g}serve{r}       Start MCP server {d}(stdio, or {g}--http{r} on localhost:5555){r}

  {d}Run{r} {b}code-review-graph <command> --help{r} {d}for details{r}
""")


def _instruction_files_to_modify(
    repo_root: Path,
    target: str,
) -> list[str]:
    """Return the list of instruction files that ``install`` would write
    or modify, given the current state of the repo and the selected
    platform target. Used for the dry-run / confirm preview (#173).
    """
    from .skills import _CLAUDE_MD_SECTION_MARKER, _PLATFORM_INSTRUCTION_FILES

    targets: list[str] = []

    if target in ("claude", "all"):
        claude_md = repo_root / "CLAUDE.md"
        if claude_md.exists():
            content = claude_md.read_text(encoding="utf-8")
            if _CLAUDE_MD_SECTION_MARKER not in content:
                targets.append("CLAUDE.md (append)")
        else:
            targets.append("CLAUDE.md (new)")

    for filename, owners in _PLATFORM_INSTRUCTION_FILES.items():
        if target != "all" and target not in owners:
            continue
        path = repo_root / filename
        if path.exists():
            content = path.read_text(encoding="utf-8")
            if _CLAUDE_MD_SECTION_MARKER not in content:
                targets.append(f"{filename} (append)")
        else:
            targets.append(f"{filename} (new)")

    return targets


def _confirm_yes_no(prompt: str, default_yes: bool = True) -> bool:
    """Prompt the user [Y/n] and return True for yes.

    Non-interactive environments (no TTY on stdin, e.g. an MCP wrapper
    piping the CLI) return ``default_yes`` without blocking — the
    stdio transport cannot safely read from stdin without corrupting
    the JSON-RPC stream. See: #173, #174
    """
    if not sys.stdin.isatty():
        return default_yes
    suffix = "[Y/n]" if default_yes else "[y/N]"
    try:
        answer = input(f"{prompt} {suffix} ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if not answer:
        return default_yes
    return answer in ("y", "yes")


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
    auto_yes = getattr(args, "yes", False)
    skip_instructions = getattr(args, "no_instructions", False)

    print("Installing MCP server config...")
    configured = install_platform_configs(repo_root, target=target, dry_run=dry_run)

    if not configured:
        print("No platforms detected.")
    else:
        print(f"\nConfigured {len(configured)} platform(s): {', '.join(configured)}")

    # Preview the instruction files that would be touched (#173).
    instr_targets = _instruction_files_to_modify(repo_root, target)
    if instr_targets:
        print()
        print("Graph instructions will be injected into:")
        for t in instr_targets:
            print(f"  {t}")

    if dry_run:
        print("\n[dry-run] Would ensure .gitignore ignores .code-review-graph/.")
        print("[dry-run] No files were modified.")
        return

    gitignore_state = ensure_repo_gitignore_excludes_crg(repo_root)
    if gitignore_state == "created":
        print("Created .gitignore and added .code-review-graph/.")
    elif gitignore_state == "updated":
        print("Updated .gitignore with .code-review-graph/.")
    else:
        print(".gitignore already contains .code-review-graph/.")

    # Skills and hooks are installed by default so Claude actually uses the
    # graph tools proactively.  Use --no-skills / --no-hooks / --no-instructions
    # to opt out.
    skip_skills = getattr(args, "no_skills", False)
    skip_hooks = getattr(args, "no_hooks", False)
    # Legacy: --skills/--hooks/--all still accepted (no-op, everything is default)

    from .skills import (
        PLATFORMS,
        generate_skills,
        install_gemini_cli_hooks,
        install_gemini_cli_skills,
        inject_claude_md,
        inject_platform_instructions,
        install_cursor_hooks,
        install_git_hook,
        install_hooks,
        install_opencode_plugin,
        install_qoder_skills,
    )

    if not skip_skills:
        # Claude Code skills are only relevant for Claude (or full install).
        if target in ("claude", "all"):
            skills_dir = generate_skills(repo_root)
            print(f"Generated Claude Code skills in {skills_dir}")

        # Gemini CLI skills are workspace-scoped under .gemini/.
        if target in ("gemini-cli", "all"):
            gemini_skills_dir = install_gemini_cli_skills(repo_root)
            print(f"Installed Gemini CLI skills in {gemini_skills_dir}")

    # Confirm before writing instruction files (#173). --yes skips the
    # prompt; --no-instructions skips the whole block.
    if not skip_instructions and instr_targets:
        if auto_yes or _confirm_yes_no(
            "Inject graph instructions into the files above?",
            default_yes=True,
        ):
            if target in ("claude", "all"):
                inject_claude_md(repo_root)
            inject_platform_instructions(repo_root, target=target)
            # Use the precomputed instr_targets list for the confirmation
            # message; we don't need the fresh return value from
            # inject_platform_instructions here.
            names = [t.split(" ")[0] for t in instr_targets]
            print(f"Injected graph instructions into: {', '.join(names)}")
        else:
            print("Skipped instruction injection (user declined).")
    elif skip_instructions:
        print("Skipped instruction injection (--no-instructions).")


    # Install Qoder skills (global user-level skills directory)
    if not skip_skills and target in ("qoder", "all"):
        qoder_skills_dir = install_qoder_skills(repo_root)
        if qoder_skills_dir:
            print(f"Installed Qoder skills to {qoder_skills_dir}")
    if not skip_hooks and target in ("claude", "qoder", "all"):
        platforms_to_install = [target] if target != "all" else ["claude", "qoder"]
        for plat in platforms_to_install:
            install_hooks(repo_root, platform=plat)
            print(f"Installed hooks in {repo_root / f'.{plat}' / 'settings.json'}")
        git_hook = install_git_hook(repo_root)
        if git_hook:
            print(f"Installed git pre-commit hook in {git_hook}")

        # Cursor hooks (user-level, only if ~/.cursor exists — matching MCP detect)
        if target in ("all", "cursor") and PLATFORMS["cursor"]["detect"]():
            try:
                hooks_path = install_cursor_hooks()
                print(f"Installed Cursor hooks in {hooks_path}")
            except Exception as exc:
                logger.warning("Could not install Cursor hooks: %s", exc)

    if not skip_hooks and target in ("gemini-cli", "all"):
        try:
            gemini_settings = install_gemini_cli_hooks(repo_root)
            print(f"Installed Gemini CLI hooks in {gemini_settings}")
        except Exception as exc:
            logger.warning("Could not install Gemini CLI hooks: %s", exc)

    # OpenCode plugin (user-level, gated by same detect() as MCP config)
    if not skip_hooks and target in ("all", "opencode") and PLATFORMS["opencode"]["detect"]():
        try:
            plugin_path = install_opencode_plugin()
            print(f"Installed OpenCode plugin in {plugin_path}")
        except Exception as exc:
            logger.warning("Could not install OpenCode plugin: %s", exc)

    print()
    print("Next steps:")
    print("  1. code-review-graph build    # build the knowledge graph")
    print("  2. Restart your AI coding tool to pick up the new config")


def _cli_post_process(store: GraphStore) -> None:
    """Run post-build pipeline and print a summary line for each step."""
    from .postprocessing import run_post_processing

    pp = run_post_processing(store)
    if pp.get("signatures_computed"):
        print(f"Signatures: {pp['signatures_computed']} nodes")
    if pp.get("fts_indexed"):
        print(f"FTS indexed: {pp['fts_indexed']} nodes")
    if pp.get("flows_detected") is not None:
        print(f"Flows: {pp['flows_detected']}")
    if pp.get("communities_detected") is not None:
        print(f"Communities: {pp['communities_detected']}")


def main() -> None:
    """Main CLI entry point."""
    ap = argparse.ArgumentParser(
        prog="code-review-graph",
        description="Persistent incremental knowledge graph for code reviews",
    )
    ap.add_argument("-v", "--version", action="store_true", help="Show version and exit")
    sub = ap.add_subparsers(dest="command")

    # install (primary) + init (alias)
    install_cmd = sub.add_parser("install", help="Register MCP server with AI coding platforms")
    install_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")
    install_cmd.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without writing files",
    )
    install_cmd.add_argument(
        "--no-skills",
        action="store_true",
        help="Skip generating platform skill files",
    )
    install_cmd.add_argument(
        "--no-hooks",
        action="store_true",
        help="Skip installing platform hooks",
    )
    install_cmd.add_argument(
        "--no-instructions",
        action="store_true",
        help="Skip injecting graph instructions into CLAUDE.md / AGENTS.md / etc.",
    )
    install_cmd.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Auto-confirm instruction injection without an interactive prompt",
    )
    # Legacy flags (kept for backwards compat, now no-ops since all is default)
    install_cmd.add_argument("--skills", action="store_true", help=argparse.SUPPRESS)
    install_cmd.add_argument("--hooks", action="store_true", help=argparse.SUPPRESS)
    install_cmd.add_argument(
        "--all", action="store_true", dest="install_all", help=argparse.SUPPRESS
    )
    install_cmd.add_argument(
        "--platform",
        choices=_PLATFORM_CHOICES,
        default="all",
        help="Target platform for MCP config (default: all detected)",
    )

    init_cmd = sub.add_parser("init", help="Alias for install")
    init_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")
    init_cmd.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without writing files",
    )
    init_cmd.add_argument(
        "--no-skills",
        action="store_true",
        help="Skip generating platform skill files",
    )
    init_cmd.add_argument(
        "--no-hooks",
        action="store_true",
        help="Skip installing platform hooks",
    )
    init_cmd.add_argument(
        "--no-instructions",
        action="store_true",
        help="Skip injecting graph instructions into CLAUDE.md / AGENTS.md / etc.",
    )
    init_cmd.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Auto-confirm instruction injection without an interactive prompt",
    )
    init_cmd.add_argument("--skills", action="store_true", help=argparse.SUPPRESS)
    init_cmd.add_argument("--hooks", action="store_true", help=argparse.SUPPRESS)
    init_cmd.add_argument("--all", action="store_true", dest="install_all", help=argparse.SUPPRESS)
    init_cmd.add_argument(
        "--platform",
        choices=_PLATFORM_CHOICES,
        default="all",
        help="Target platform for MCP config (default: all detected)",
    )

    # build
    build_cmd = sub.add_parser("build", help="Full graph build (re-parse all files)")
    build_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")
    build_cmd.add_argument(
        "--skip-flows",
        action="store_true",
        help="Skip flow/community detection (signatures + FTS only)",
    )
    build_cmd.add_argument(
        "--skip-postprocess",
        action="store_true",
        help="Skip all post-processing (raw parse only)",
    )

    # update
    update_cmd = sub.add_parser("update", help="Incremental update (only changed files)")
    update_cmd.add_argument("--base", default="HEAD~1", help="Git diff base (default: HEAD~1)")
    update_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")
    update_cmd.add_argument(
        "--skip-flows",
        action="store_true",
        help="Skip flow/community detection (signatures + FTS only)",
    )
    update_cmd.add_argument(
        "--skip-postprocess",
        action="store_true",
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
        "--serve",
        action="store_true",
        help="Start a local HTTP server to view the visualization (localhost:8765)",
    )
    vis_cmd.add_argument(
        "--format",
        choices=["html", "graphml", "cypher", "obsidian", "svg"],
        default="html",
        help="Export format (default: html)",
    )

    # wiki
    wiki_cmd = sub.add_parser("wiki", help="Generate markdown wiki from community structure")
    wiki_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")
    wiki_cmd.add_argument(
        "--force",
        action="store_true",
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
        "--benchmark",
        default=None,
        help="Comma-separated benchmarks to run (token_efficiency, impact_accuracy, "
        "flow_completeness, search_quality, build_performance)",
    )
    eval_cmd.add_argument("--repo", default=None, help="Comma-separated repo config names")
    eval_cmd.add_argument("--all", action="store_true", dest="run_all", help="Run all benchmarks")
    eval_cmd.add_argument("--report", action="store_true", help="Generate report from results")
    eval_cmd.add_argument("--output-dir", default=None, help="Output directory for results")

    # detect-changes
    detect_cmd = sub.add_parser("detect-changes", help="Analyze change impact")
    detect_cmd.add_argument("--base", default="HEAD~1", help="Git diff base (default: HEAD~1)")
    detect_cmd.add_argument("--brief", action="store_true", help="Show brief summary only")
    detect_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")

    # serve
    serve_cmd = sub.add_parser(
        "serve",
        help="Start MCP server (stdio by default, or HTTP on localhost with --http)",
    )
    serve_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")
    serve_cmd.add_argument(
        "--tools", default=None,
        help=(
            "Comma-separated list of tool names to expose "
            "(e.g. query_graph_tool,semantic_search_nodes_tool). "
            "Unlisted tools are removed. Falls back to CRG_TOOLS env var. "
            "When unset, all tools are available."
        ),
    )
    serve_cmd.add_argument(
        "--http",
        action="store_true",
        help="Listen for MCP over Streamable HTTP on localhost (default port 5555)",
    )
    serve_cmd.add_argument(
        "--host",
        default=None,
        metavar="ADDR",
        help="Bind address for --http (default: 127.0.0.1)",
    )
    serve_cmd.add_argument(
        "--port",
        type=int,
        default=None,
        metavar="PORT",
        help="Port for --http (default: 5555)",
    )

    # daemon
    daemon_cmd = sub.add_parser(
        "daemon",
        help="Multi-repo watch daemon (start/stop/status/add/remove)",
    )
    daemon_sub = daemon_cmd.add_subparsers(dest="daemon_command")

    daemon_start = daemon_sub.add_parser(
        "start",
        help="Start the watch daemon",
    )
    daemon_start.add_argument(
        "--foreground",
        action="store_true",
        help="Run in foreground instead of daemonizing",
    )

    daemon_sub.add_parser(
        "stop",
        help="Stop the watch daemon",
    )

    daemon_restart = daemon_sub.add_parser(
        "restart",
        help="Restart the watch daemon",
    )
    daemon_restart.add_argument(
        "--foreground",
        action="store_true",
        help="Run in foreground instead of daemonizing",
    )

    daemon_sub.add_parser("status", help="Show daemon and watcher status")

    daemon_logs = daemon_sub.add_parser(
        "logs",
        help="View daemon or watcher logs",
    )
    daemon_logs.add_argument(
        "--repo",
        default=None,
        help="Show logs for a specific repo alias",
    )
    daemon_logs.add_argument(
        "--follow",
        action="store_true",
        help="Follow log output (tail -f)",
    )
    daemon_logs.add_argument(
        "--lines",
        type=int,
        default=50,
        help="Number of lines to show (default: 50)",
    )

    daemon_add = daemon_sub.add_parser(
        "add",
        help="Add a repo to the watch config",
    )
    daemon_add.add_argument("path", help="Path to the repository")
    daemon_add.add_argument(
        "--alias",
        default=None,
        help="Short alias for the repo",
    )

    daemon_remove = daemon_sub.add_parser(
        "remove",
        help="Remove a repo from the watch config",
    )
    daemon_remove.add_argument(
        "path_or_alias",
        help="Repository path or alias to remove",
    )

    args = ap.parse_args()

    if args.version:
        print(f"code-review-graph {_get_version()}")
        return

    if not args.command:
        _print_banner()
        return

    if args.command == "serve":
        from .main import main as serve_main

        if args.port is not None and not args.http:
            serve_cmd.error("--port requires --http")
        if args.host is not None and not args.http:
            serve_cmd.error("--host requires --http")
        if args.http:
            host = args.host if args.host is not None else "127.0.0.1"
            port = args.port if args.port is not None else 5555
            serve_main(
                repo_root=args.repo,
                transport="streamable-http",
                host=host,
                port=port,
                tools=args.tools,
            )
        else:
            serve_main(repo_root=args.repo, tools=args.tools)
        return

    if args.command == "daemon":
        if not args.daemon_command:
            daemon_cmd.print_help()
            return
        from .daemon_cli import (
            _handle_add,
            _handle_logs,
            _handle_remove,
            _handle_restart,
            _handle_start,
            _handle_status,
            _handle_stop,
        )

        handlers = {
            "start": _handle_start,
            "stop": _handle_stop,
            "restart": _handle_restart,
            "status": _handle_status,
            "logs": _handle_logs,
            "add": _handle_add,
            "remove": _handle_remove,
        }
        handler = handlers.get(args.daemon_command)
        if handler:
            handler(args)
        return

    if args.command == "eval":
        from .eval.reporter import generate_full_report, generate_readme_tables
        from .eval.runner import run_eval

        if getattr(args, "report", False):
            output_dir = Path(getattr(args, "output_dir", None) or "evaluate/results")
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
                [r.strip() for r in args.repo.split(",")] if getattr(args, "repo", None) else None
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

    from .graph import GraphStore
    from .incremental import (
        find_project_root,
        find_repo_root,
        get_db_path,
        watch,
    )

    if args.command == "postprocess":
        repo_root = Path(args.repo) if args.repo else find_project_root()
        db_path = get_db_path(repo_root)
        store = GraphStore(db_path)
        try:
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
        finally:
            store.close()
        return

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
            pp = (
                "none"
                if getattr(args, "skip_postprocess", False)
                else ("minimal" if getattr(args, "skip_flows", False) else "full")
            )
            from .tools.build import build_or_update_graph

            result = build_or_update_graph(
                full_rebuild=True,
                repo_root=str(repo_root),
                postprocess=pp,
            )
            parsed = result.get("files_parsed", 0)
            nodes = result.get("total_nodes", 0)
            edges = result.get("total_edges", 0)
            print(f"Full build: {parsed} files, {nodes} nodes, {edges} edges (postprocess={pp})")
            if result.get("errors"):
                print(f"Errors: {len(result['errors'])}")
            _cli_post_process(store)

        elif args.command == "update":
            pp = (
                "none"
                if getattr(args, "skip_postprocess", False)
                else ("minimal" if getattr(args, "skip_flows", False) else "full")
            )
            from .tools.build import build_or_update_graph

            result = build_or_update_graph(
                full_rebuild=False,
                repo_root=str(repo_root),
                base=args.base,
                postprocess=pp,
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
                _cli_post_process(store)

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
            from .incremental import _git_branch_info, detect_vcs
            vcs = detect_vcs(repo_root)
            if vcs == "git":
                current_branch, current_sha = _git_branch_info(repo_root)
                if stored_branch and current_branch and stored_branch != current_branch:
                    print(
                        f"WARNING: Graph was built on '{stored_branch}' "
                        f"but you are now on '{current_branch}'. "
                        f"Run 'code-review-graph build' to rebuild."
                    )
            elif vcs == "svn":
                stored_rev = store.get_metadata("svn_revision")
                stored_svn_branch = store.get_metadata("svn_branch")
                if stored_svn_branch:
                    print(f"SVN branch: {stored_svn_branch}")
                if stored_rev:
                    print(f"SVN revision at build: {stored_rev}")

        elif args.command == "watch":
            from .postprocessing import run_post_processing

            watch(repo_root, store, on_files_updated=run_post_processing)

        elif args.command == "visualize":
            from .incremental import get_data_dir

            data_dir = get_data_dir(repo_root)
            fmt = getattr(args, "format", "html") or "html"

            if fmt == "graphml":
                from .exports import export_graphml

                out = data_dir / "graph.graphml"
                export_graphml(store, out)
                print(f"GraphML exported: {out}")
            elif fmt == "cypher":
                from .exports import export_neo4j_cypher

                out = data_dir / "graph.cypher"
                export_neo4j_cypher(store, out)
                print(f"Neo4j Cypher exported: {out}")
            elif fmt == "obsidian":
                from .exports import export_obsidian_vault

                out = data_dir / "obsidian"
                export_obsidian_vault(store, out)
                print(f"Obsidian vault exported: {out}")
            elif fmt == "svg":
                from .exports import export_svg

                out = data_dir / "graph.svg"
                export_svg(store, out)
                print(f"SVG exported: {out}")
            else:
                from .visualization import generate_html

                html_path = data_dir / "graph.html"
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
                    print("Open in browser to explore.")

        elif args.command == "wiki":
            from .incremental import get_data_dir
            from .wiki import generate_wiki

            wiki_dir = get_data_dir(repo_root) / "wiki"
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
