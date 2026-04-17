"""Graph-powered refactoring operations.

Provides rename previews, dead code detection, refactoring suggestions,
and safe application of refactoring edits to source files. All file writes
go through a preview-then-apply workflow with expiry enforcement and path
traversal prevention.
"""

from __future__ import annotations

import functools
import logging
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from .flows import _has_framework_decorator, _matches_entry_name
from .graph import GraphStore, _sanitize_name

logger = logging.getLogger(__name__)

# Base class names that indicate a framework-managed class (ORM models,
# Pydantic schemas, settings).  Classes inheriting from these are invoked
# via metaclass/framework magic and should not be flagged as dead code.
_FRAMEWORK_BASE_CLASSES = frozenset({
    "Base", "DeclarativeBase", "Model", "BaseModel", "BaseSettings",
    "db.Model", "TableBase",
    # AWS CDK constructs -- instantiated by CDK app wiring, not explicit CALLS.
    "Stack", "NestedStack", "Construct", "Resource",
})

# Class name suffixes that indicate CDK/IaC constructs.
# These are instantiated by framework wiring, not direct CALLS edges.
# Used as fallback when INHERITS edges to external base classes are absent.
_CDK_CLASS_SUFFIXES = ("Stack", "Construct", "Pipeline", "Resources", "Layer")

# Patterns for mock/stub variables in test files that should not be flagged dead.
_MOCK_NAME_RE = re.compile(
    r"^(mock[A-Z_]|Mock[A-Z]|createMock[A-Z])|"  # mockDynamoClient, MockService, createMockX
    r"(Mock|Stub|Fake|Spy)$",                      # s3ClientMock, dbStub
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Thread-safe pending refactors storage
# ---------------------------------------------------------------------------

_refactor_lock = threading.Lock()
_pending_refactors: dict[str, dict] = {}
REFACTOR_EXPIRY_SECONDS = 600  # 10 minutes


def _cleanup_expired() -> int:
    """Remove expired refactors from the pending dict.  Returns count removed."""
    now = time.time()
    expired = [
        rid for rid, r in _pending_refactors.items()
        if now - r["created_at"] > REFACTOR_EXPIRY_SECONDS
    ]
    for rid in expired:
        del _pending_refactors[rid]
    return len(expired)


# ---------------------------------------------------------------------------
# 1. rename_preview
# ---------------------------------------------------------------------------


def rename_preview(
    store: GraphStore,
    old_name: str,
    new_name: str,
) -> Optional[dict[str, Any]]:
    """Build a rename edit list for *old_name* -> *new_name*.

    Finds the node via ``store.search_nodes(old_name)``, collects
    definition and reference sites, generates a unique ``refactor_id``,
    and stores the preview in the thread-safe ``_pending_refactors`` dict.

    Returns:
        A refactor preview dict, or ``None`` if the node is not found.
    """
    candidates = store.search_nodes(old_name, limit=10)
    # Pick the best match: prefer exact name match.
    node = None
    for c in candidates:
        if c.name == old_name:
            node = c
            break
    if node is None and candidates:
        node = candidates[0]
    if node is None:
        logger.warning("rename_preview: node %r not found", old_name)
        return None

    edits: list[dict[str, Any]] = []

    # --- Definition site ---
    edits.append({
        "file": node.file_path,
        "line": node.line_start,
        "old": old_name,
        "new": new_name,
        "confidence": "high",
    })

    # --- Call sites (CALLS edges targeting this node) ---
    call_edges = store.get_edges_by_target(node.qualified_name)
    for edge in call_edges:
        if edge.kind == "CALLS":
            edits.append({
                "file": edge.file_path,
                "line": edge.line,
                "old": old_name,
                "new": new_name,
                "confidence": "high",
            })

    # Also search by bare name for unqualified edges.
    bare_edges = store.search_edges_by_target_name(old_name, kind="CALLS")
    seen = {(e["file"], e["line"]) for e in edits}
    for edge in bare_edges:
        key = (edge.file_path, edge.line)
        if key not in seen:
            edits.append({
                "file": edge.file_path,
                "line": edge.line,
                "old": old_name,
                "new": new_name,
                "confidence": "high",
            })
            seen.add(key)

    # --- Import sites (IMPORTS_FROM edges targeting this node) ---
    import_edges = store.get_edges_by_target(node.qualified_name)
    for edge in import_edges:
        if edge.kind == "IMPORTS_FROM":
            key = (edge.file_path, edge.line)
            if key not in seen:
                edits.append({
                    "file": edge.file_path,
                    "line": edge.line,
                    "old": old_name,
                    "new": new_name,
                    "confidence": "high",
                })
                seen.add(key)

    # --- Stats ---
    stats = {"high": 0, "medium": 0, "low": 0}
    for e in edits:
        stats[e["confidence"]] += 1

    refactor_id = uuid.uuid4().hex[:8]
    preview: dict[str, Any] = {
        "refactor_id": refactor_id,
        "type": "rename",
        "old_name": _sanitize_name(old_name),
        "new_name": _sanitize_name(new_name),
        "edits": edits,
        "stats": stats,
        "created_at": time.time(),
    }

    with _refactor_lock:
        _cleanup_expired()
        _pending_refactors[refactor_id] = preview

    logger.info(
        "rename_preview: created refactor %s (%s -> %s, %d edits)",
        refactor_id, old_name, new_name, len(edits),
    )
    return preview


# ---------------------------------------------------------------------------
# 2. find_dead_code
# ---------------------------------------------------------------------------


def _is_entry_point(node: Any) -> bool:
    """Check if a node looks like an entry point by name or decorator.

    Unlike ``flows.detect_entry_points()`` which treats ALL uncalled functions
    as entry points, this checks only for conventional name patterns and
    framework decorators -- the indicators that a function is *intentionally*
    an entry point rather than simply unreferenced dead code.
    """
    if _has_framework_decorator(node):
        return True
    if _matches_entry_name(node):
        return True
    return False


# Matches identifiers inside type annotations (e.g. "GoalCreate" in
# "body: GoalCreate", "Optional[UserResponse]", "list[Item]").
_TEST_FILE_RE = re.compile(
    r"([\\/]__tests__[\\/]|\.spec\.[jt]sx?$|\.test\.[jt]sx?$|[\\/]test_[^/\\]*\.py$"
    r"|[\\/]e2e[_-]?tests?[\\/]|[\\/]test[_-]utils?[\\/])",
)


def _is_test_file(file_path: str) -> bool:
    """Return True if *file_path* looks like a test file."""
    return bool(_TEST_FILE_RE.search(file_path))


_MIN_PKG_SEGMENT_LEN = 4  # ignore short dirs like "src", "lib", "app"


@functools.lru_cache(maxsize=4096)
def _path_segments(file_path: str) -> tuple[str, ...]:
    """Return directory segments long enough to serve as package-name anchors."""
    parts = file_path.replace("\\", "/").split("/")
    return tuple(
        p for p in parts[:-1]  # skip the filename itself
        if len(p) >= _MIN_PKG_SEGMENT_LEN and p not in ("home", "src", "lib", "app")
    )


_TYPE_IDENT_RE = re.compile(r"[A-Z][A-Za-z0-9_]*")


def _collect_type_referenced_names(store: GraphStore) -> set[str]:
    """Collect class names that appear in function params or return types."""
    funcs = store.get_nodes_by_kind(kinds=["Function", "Test"])
    names: set[str] = set()
    for f in funcs:
        for text in (f.params, f.return_type):
            if text:
                names.update(_TYPE_IDENT_RE.findall(text))
    return names


def find_dead_code(
    store: GraphStore,
    kind: Optional[str] = None,
    file_pattern: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Find functions/classes with no callers, no test refs, no importers, and no references.

    Entry points (functions matching framework decorators or conventional name
    patterns like ``main``, ``test_*``, ``handle_*``) are excluded.

    .. note::

        **Caveats — dynamic dispatch patterns.**  Static analysis cannot track
        all runtime-determined call patterns.  Functions registered via fully
        dynamic keys (``map[computedKey()] = fn``), ``Reflect.apply``, or
        runtime ``require()`` may still appear as dead code.  Treat results as
        hints, especially for TypeScript projects that use map-based dispatch,
        plugin registries, or dynamic requires.

    Args:
        store: The GraphStore instance.
        kind: Optional filter (e.g. ``"Function"`` or ``"Class"``).
        file_pattern: Optional file-path substring filter.

    Returns:
        List of dead-code dicts with name, qualified_name, kind, file, line,
        and a top-level ``caveats`` note.
    """
    # Query candidate nodes.
    candidates = store.get_nodes_by_kind(
        kinds=[kind] if kind else ["Function", "Class"],
        file_pattern=file_pattern,
    )

    # Build set of class names referenced in function type annotations.
    type_ref_names = _collect_type_referenced_names(store)

    # Build class hierarchy: class_qualified_name -> [bare_base_names]
    class_bases: dict[str, list[str]] = {}
    conn = store._conn
    for row in conn.execute(
        "SELECT source_qualified, target_qualified FROM edges WHERE kind = 'INHERITS'"
    ).fetchall():
        base = row[1].rsplit("::", 1)[-1] if "::" in row[1] else row[1]
        class_bases.setdefault(row[0], []).append(base)

    # Build import graph: file_path -> set of file_paths it imports from.
    # Used to filter bare-name caller matches to plausible callers.
    importer_files: dict[str, set[str]] = {}
    for row in conn.execute(
        "SELECT file_path, target_qualified FROM edges WHERE kind = 'IMPORTS_FROM'"
    ).fetchall():
        importer_files.setdefault(row[0], set()).add(row[1])

    # Build set of globally unique names (only one non-test node with that name).
    # For unique names, any bare-name CALLS edge is reliable — no ambiguity.
    name_counts: dict[str, int] = {}
    for row in conn.execute(
        "SELECT name, COUNT(*) FROM nodes "
        "WHERE kind IN ('Function', 'Class') AND is_test = 0 "
        "GROUP BY name"
    ).fetchall():
        name_counts[row[0]] = row[1]

    def _is_plausible_caller(
        edge_file: str, node_file: str, node_name: str = "",
    ) -> bool:
        """A bare-name edge is plausible if it comes from the same file,
        from a file that has an IMPORTS_FROM edge whose target matches
        the node's file path, or the name is globally unique (no ambiguity)."""
        if edge_file == node_file:
            return True
        # Unique names (only one definition) have no ambiguity -- accept all callers.
        if node_name and name_counts.get(node_name, 0) == 1:
            return True
        for imp_target in importer_files.get(edge_file, ()):
            # Strip "::name" suffix — workspace-resolved imports may include it
            imp_path = imp_target.split("::")[0] if "::" in imp_target else imp_target
            # __init__.py represents its parent package directory
            if imp_path.endswith("/__init__.py"):
                imp_dir = imp_path[:-12]  # strip "/__init__.py"
                if node_file.startswith(imp_dir + "/"):
                    return True
            if imp_path.startswith(node_file) or node_file.startswith(imp_path + "/"):
                return True
            # 2-hop: edge_file imports X, X re-exports from node_file (barrel files)
            for imp2 in importer_files.get(imp_target, ()):
                imp2_path = imp2.split("::")[0] if "::" in imp2 else imp2
                if imp2_path.endswith("/__init__.py"):
                    imp2_dir = imp2_path[:-12]
                    if node_file.startswith(imp2_dir + "/"):
                        return True
                if imp2_path.startswith(node_file) or node_file.startswith(imp2_path + "/"):
                    return True
            # Package-alias heuristic: monorepo imports like "@scope/pkg-name"
            # contain the directory name of the target package.  Check if the
            # import target string contains a significant directory segment from
            # the node's file path (e.g. "lambda-common" in both the import
            # "@cova-utils/lambda-common" and the path "libraries/lambda-common/...").
            if not imp_target.startswith("/"):
                # imp_target is a package specifier, not a file path
                for seg in _path_segments(node_file):
                    if seg in imp_target:
                        return True
        return False

    dead: list[dict[str, Any]] = []

    for node in candidates:

        # Skip test nodes and anything defined in test files.
        if node.is_test or _is_test_file(node.file_path):
            continue

        # Skip ambient type declarations (.d.ts) — they describe external APIs.
        if node.file_path.endswith(".d.ts"):
            continue

        # Skip dunder methods -- invoked by runtime, never have explicit callers.
        if node.name.startswith("__") and node.name.endswith("__"):
            continue

        # Skip JS/TS/Java constructors -- invoked via `new ClassName()`, which
        # creates a CALLS edge to the class, not to `constructor`.
        if node.name == "constructor" and node.parent_name:
            continue

        # Skip mock/stub variables in test files -- these are test helpers
        # referenced via variable assignment, not function calls.
        if node.is_test or _is_test_file(node.file_path):
            if _MOCK_NAME_RE.search(node.name):
                continue

        # Skip entry points (by name pattern or decorator, not just "uncalled").
        if _is_entry_point(node):
            continue

        # Check for callers (CALLS), test refs (TESTED_BY), importers (IMPORTS_FROM),
        # and value references (REFERENCES -- function-as-value in maps, arrays, etc.).

        # Skip classes referenced in type annotations (Pydantic schemas, etc.).
        if node.kind == "Class" and node.name in type_ref_names:
            continue

        # Skip Angular/NestJS decorated classes -- they are framework-managed
        # and instantiated by the DI container, not direct CALLS edges.
        if node.kind == "Class" and _has_framework_decorator(node):
            continue

        # Skip classes (and their methods) inheriting from known framework bases.
        _is_framework_class = False
        _check_qn = node.qualified_name if node.kind == "Class" else (
            node.qualified_name.rsplit(".", 1)[0] if node.parent_name else None
        )
        if _check_qn:
            outgoing = store.get_edges_by_source(_check_qn)
            base_names = {
                e.target_qualified.rsplit("::", 1)[-1]
                for e in outgoing if e.kind == "INHERITS"
            }
            if base_names & _FRAMEWORK_BASE_CLASSES:
                _is_framework_class = True
        if node.kind == "Class":
            if _is_framework_class:
                continue
            # Fallback: CDK class name suffixes (no INHERITS edge for external bases)
            if any(node.name.endswith(s) for s in _CDK_CLASS_SUFFIXES):
                continue
        if node.kind == "Function" and _is_framework_class:
            continue
        # Also skip methods whose parent class name matches CDK suffixes
        # (fallback for external base classes without INHERITS edges).
        if (
            node.kind == "Function"
            and node.parent_name
            and any(node.parent_name.endswith(s) for s in _CDK_CLASS_SUFFIXES)
        ):
            continue

        # Skip decorated functions/classes that are invoked implicitly rather
        # than via explicit CALLS edges.
        decorators = node.extra.get("decorators", ())
        if isinstance(decorators, (list, tuple)) and decorators:
            if node.kind in ("Function", "Test"):
                # @property -- invoked via attribute access
                # @abstractmethod -- polymorphic dispatch, never called directly
                # @classmethod/@staticmethod -- called via Class.method()
                if any(
                    d in ("property", "abstractmethod", "classmethod", "staticmethod")
                    or d.endswith(".abstractmethod")
                    # Angular @HostListener -- method called by framework event system
                    or d.startswith("HostListener")
                    for d in decorators
                ):
                    continue
            if node.kind == "Class":
                # @dataclass classes are instantiated as types, not via CALLS
                if any("dataclass" in d for d in decorators):
                    continue

        # Skip methods that override an @abstractmethod in a base class --
        # they are called polymorphically via the base class reference.
        if node.kind == "Function" and node.parent_name:
            parent_qn = node.qualified_name.rsplit(".", 1)[0]
            parent_edges = store.get_edges_by_source(parent_qn)
            base_class_names = [
                e.target_qualified for e in parent_edges if e.kind == "INHERITS"
            ]
            for base_name in base_class_names:
                # Try fully-qualified base first, then bare name match
                base_method_qn = f"{base_name}.{node.name}"
                base_nodes = store.get_node(base_method_qn)
                if base_nodes is None:
                    # Base class may be bare name -- search in same file
                    base_method_qn2 = (
                        node.file_path + "::" + base_name + "." + node.name
                    )
                    base_nodes = store.get_node(base_method_qn2)
                if base_nodes is not None:
                    base_decos = base_nodes.extra.get("decorators", ())
                    if isinstance(base_decos, (list, tuple)) and any(
                        "abstractmethod" in d for d in base_decos
                    ):
                        break
            else:
                base_name = None  # no abstract override found
            if base_name is not None:
                continue

        incoming = store.get_edges_by_target(node.qualified_name)
        # Also check class-qualified edges (e.g. "ClassName::method") which
        # lack the file-path prefix used in node.qualified_name.
        if not any(e.kind == "CALLS" for e in incoming) and node.parent_name:
            class_qn = f"{node.parent_name}::{node.name}"
            incoming = incoming + store.get_edges_by_target(class_qn)
        # Also check bare-name and partially-qualified edges.
        # CALLS targets may be bare ("funcName"), class-qualified
        # ("Class::method"), or workspace-qualified ("pkg/dir::funcName").
        if not any(e.kind == "CALLS" for e in incoming):
            bare = store.search_edges_by_target_name(node.name, kind="CALLS")
            # Also search for partially-qualified targets ending with ::name
            suffix_rows = conn.execute(
                "SELECT * FROM edges WHERE kind = 'CALLS'"
                " AND target_qualified LIKE ?",
                (f"%::{node.name}",),
            ).fetchall()
            suffix_edges = [store._row_to_edge(r) for r in suffix_rows]
            all_bare = bare + suffix_edges
            all_bare = [
                e for e in all_bare
                if _is_plausible_caller(e.file_path, node.file_path, node.name)
            ]
            incoming = incoming + all_bare
        if not any(e.kind == "TESTED_BY" for e in incoming):
            bare_tb = store.search_edges_by_target_name(node.name, kind="TESTED_BY")
            bare_tb = [
                e for e in bare_tb
                if _is_plausible_caller(e.file_path, node.file_path, node.name)
            ]
            incoming = incoming + bare_tb
        # Check INHERITS -- classes with subclasses are not dead.
        if node.kind == "Class" and not any(e.kind == "INHERITS" for e in incoming):
            bare_inh = store.search_edges_by_target_name(node.name, kind="INHERITS")
            incoming = incoming + bare_inh
        has_callers = any(e.kind == "CALLS" for e in incoming)
        has_test_refs = any(e.kind == "TESTED_BY" for e in incoming)
        has_importers = any(e.kind == "IMPORTS_FROM" for e in incoming)
        has_references = any(e.kind == "REFERENCES" for e in incoming)
        has_subclasses = any(e.kind == "INHERITS" for e in incoming)

        # For classes with no direct references, check if any member has callers.
        no_refs = not (
            has_callers or has_test_refs or has_importers
            or has_references or has_subclasses
        )
        if node.kind == "Class" and no_refs:
            member_prefix = node.qualified_name + "."
            # Also check bare class-name pattern (unresolved CALLS targets)
            bare_prefix = node.name + "."
            member_calls = conn.execute(
                "SELECT COUNT(*) FROM edges WHERE kind = 'CALLS'"
                " AND (target_qualified LIKE ? OR target_qualified LIKE ?)",
                (f"%{member_prefix}%", f"%{bare_prefix}%"),
            ).fetchone()[0]
            if member_calls > 0:
                has_callers = True

        if not (
            has_callers or has_test_refs or has_importers
            or has_references or has_subclasses
        ):
            # Check if this is a method override where the base class method
            # has callers (polymorphic dispatch: callers of Base.method()
            # implicitly call SubClass.method() at runtime).
            if node.kind == "Function" and node.parent_name and not has_callers:
                method_suffix = "." + node.name
                if node.qualified_name.endswith(method_suffix):
                    class_qn = node.qualified_name[: -len(method_suffix)]
                    for base_name in class_bases.get(class_qn, []):
                        rows = conn.execute(
                            "SELECT n.qualified_name FROM nodes n "
                            "WHERE n.parent_name = ? AND n.name = ? "
                            "AND n.kind IN ('Function', 'Test')",
                            (base_name, node.name),
                        ).fetchall()
                        for (base_method_qn,) in rows:
                            if conn.execute(
                                "SELECT 1 FROM edges "
                                "WHERE target_qualified = ? AND kind = 'CALLS' "
                                "LIMIT 1",
                                (base_method_qn,),
                            ).fetchone():
                                has_callers = True
                                break
                        if has_callers:
                            break

            if not has_callers:
                dead.append({
                    "name": _sanitize_name(node.name),
                    "qualified_name": _sanitize_name(node.qualified_name),
                    "kind": node.kind,
                    "file": node.file_path,
                    "line": node.line_start,
                })

    logger.info("find_dead_code: found %d dead symbols", len(dead))
    return dead


# ---------------------------------------------------------------------------
# 3. suggest_refactorings
# ---------------------------------------------------------------------------


def suggest_refactorings(store: GraphStore) -> list[dict[str, Any]]:
    """Produce community-driven refactoring suggestions.

    Currently two categories:
    - **move**: Functions in Community A only called by Community B.
    - **remove**: Dead code (no callers, tests, or importers and not entry points).

    Returns:
        List of suggestion dicts with type, description, symbols, rationale.
    """
    suggestions: list[dict[str, Any]] = []

    # --- Dead code suggestions ---
    dead = find_dead_code(store)
    for d in dead:
        suggestions.append({
            "type": "remove",
            "description": f"Remove unused {d['kind'].lower()} '{d['name']}'",
            "symbols": [d["qualified_name"]],
            "rationale": "No callers, no test references, no importers, not an entry point.",
        })

    # --- Cross-community move suggestions ---
    # Only attempt if communities table exists and has data.
    community_rows = store.get_communities_list()

    if community_rows:
        # Build node -> community_id mapping.
        node_community: dict[str, int] = {}
        for crow in community_rows:
            cid = crow["id"]
            member_qns = store.get_community_member_qns(cid)
            for qn in member_qns:
                node_community[qn] = cid

        community_names: dict[int, str] = {
            r["id"]: r["name"] for r in community_rows
        }

        # Check functions called only by members of a different community.
        all_funcs = store.get_nodes_by_kind(["Function"])

        for fnode in all_funcs:
            f_community = node_community.get(fnode.qualified_name)
            if f_community is None:
                continue

            incoming_calls = [
                e for e in store.get_edges_by_target(fnode.qualified_name)
                if e.kind == "CALLS"
            ]
            if not incoming_calls:
                continue

            caller_communities = set()
            for edge in incoming_calls:
                c_community = node_community.get(edge.source_qualified)
                if c_community is not None:
                    caller_communities.add(c_community)

            # If ALL callers are from a single *different* community, suggest move.
            if len(caller_communities) == 1:
                target_community = next(iter(caller_communities))
                if target_community != f_community:
                    src_name = community_names.get(f_community, f"community-{f_community}")
                    tgt_name = community_names.get(
                        target_community, f"community-{target_community}"
                    )
                    suggestions.append({
                        "type": "move",
                        "description": (
                            f"Move '{_sanitize_name(fnode.name)}' from "
                            f"'{src_name}' to '{tgt_name}'"
                        ),
                        "symbols": [_sanitize_name(fnode.qualified_name)],
                        "rationale": (
                            f"Function is in community '{src_name}' but only "
                            f"called by members of community '{tgt_name}'."
                        ),
                    })

    logger.info("suggest_refactorings: produced %d suggestions", len(suggestions))
    return suggestions


# ---------------------------------------------------------------------------
# 4. apply_refactor
# ---------------------------------------------------------------------------


def apply_refactor(
    refactor_id: str,
    repo_root: Path,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Apply a previously previewed refactoring to source files.

    Validates the refactor_id, checks expiry, ensures all edit paths are
    within the repo root, then performs exact string replacements on the
    target files.

    Args:
        refactor_id: ID from a prior ``rename_preview`` call.
        repo_root: Validated repository root path.
        dry_run: If True, compute the would-be changes and return a
            unified-diff representation per affected file, but do NOT
            write anything to disk. The ``refactor_id`` is preserved so
            the same preview can be committed afterwards via a second
            call without ``dry_run``. See: #176

    Returns:
        Status dict with applied count and modified files. When
        ``dry_run=True`` the dict additionally contains:

        - ``dry_run``: ``True``
        - ``would_modify``: list of file paths that would be changed
        - ``diffs``: map of file path → unified diff string showing the
          proposed change
    """
    repo_root = repo_root.resolve()

    with _refactor_lock:
        _cleanup_expired()
        preview = _pending_refactors.get(refactor_id)

    if preview is None:
        logger.warning("apply_refactor: unknown or expired refactor_id %s", refactor_id)
        return {"status": "error", "error": f"Refactor '{refactor_id}' not found or expired."}

    # Check expiry explicitly.
    age = time.time() - preview["created_at"]
    if age > REFACTOR_EXPIRY_SECONDS:
        with _refactor_lock:
            _pending_refactors.pop(refactor_id, None)
        logger.warning("apply_refactor: refactor %s expired (%.0fs old)", refactor_id, age)
        return {"status": "error", "error": f"Refactor '{refactor_id}' has expired."}

    edits = preview.get("edits", [])
    if not edits:
        if dry_run:
            return {
                "status": "ok", "dry_run": True, "applied": 0,
                "files_modified": [], "edits_applied": 0,
                "would_modify": [], "diffs": {},
            }
        return {"status": "ok", "applied": 0, "files_modified": [], "edits_applied": 0}

    # --- Path traversal validation ---
    for edit in edits:
        edit_path = Path(edit["file"]).resolve()
        try:
            edit_path.relative_to(repo_root)
        except ValueError:
            logger.error(
                "apply_refactor: path traversal blocked for %s (repo_root=%s)",
                edit_path, repo_root,
            )
            return {
                "status": "error",
                "error": f"Edit path '{edit['file']}' is outside repo root.",
            }

    # --- Compute new content for every edit (shared by dry-run and write paths) ---
    # Group edits by file so multiple edits to the same file apply
    # sequentially against the updated content rather than stomping each
    # other. Dry-run and write modes then share this computation.
    from collections import defaultdict
    edits_by_file: dict[str, list[dict]] = defaultdict(list)
    for edit in edits:
        edits_by_file[edit["file"]].append(edit)

    planned: dict[str, tuple[str, str, int]] = {}  # file -> (old_content, new_content, edit_count)
    for file_str, file_edits in edits_by_file.items():
        file_path = Path(file_str)
        if not file_path.is_file():
            logger.warning("apply_refactor: file not found: %s", file_path)
            continue
        try:
            original = file_path.read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning("apply_refactor: could not read %s: %s", file_path, exc)
            continue

        content = original
        file_edits_applied = 0
        for edit in file_edits:
            old_text = edit["old"]
            new_text = edit["new"]
            if old_text not in content:
                logger.warning(
                    "apply_refactor: old text %r not found in %s",
                    old_text, file_path,
                )
                continue
            target_line = edit.get("line")
            if target_line is not None:
                lines = content.splitlines(keepends=True)
                idx = target_line - 1
                if 0 <= idx < len(lines) and old_text in lines[idx]:
                    lines[idx] = lines[idx].replace(old_text, new_text, 1)
                    content = "".join(lines)
                else:
                    content = content.replace(old_text, new_text, 1)
            else:
                content = content.replace(old_text, new_text, 1)
            file_edits_applied += 1

        if file_edits_applied > 0:
            planned[file_str] = (original, content, file_edits_applied)

    # --- Dry-run path: return diffs, no writes ---
    if dry_run:
        import difflib
        diffs: dict[str, str] = {}
        for file_str, (original, new_content, _count) in planned.items():
            diff_lines = list(difflib.unified_diff(
                original.splitlines(keepends=True),
                new_content.splitlines(keepends=True),
                fromfile=f"a/{file_str}",
                tofile=f"b/{file_str}",
                n=3,
            ))
            diffs[file_str] = "".join(diff_lines)
        total_edits = sum(count for _o, _n, count in planned.values())
        result = {
            "status": "ok",
            "dry_run": True,
            "applied": 0,
            "edits_applied": total_edits,
            "would_modify": sorted(planned.keys()),
            "files_modified": [],
            "diffs": diffs,
        }
        logger.info(
            "apply_refactor: dry-run %s — %d edits would be applied to %d files",
            refactor_id, total_edits, len(planned),
        )
        # Do NOT pop the pending refactor — let the user commit via a
        # second call with dry_run=False.
        return result

    # --- Real-write path: write the pre-computed new content ---
    files_modified: set[str] = set()
    edits_applied = 0
    for file_str, (_original, new_content, count) in planned.items():
        file_path = Path(file_str)
        try:
            file_path.write_text(new_content, encoding="utf-8")
            edits_applied += count
            files_modified.add(str(file_path))
            logger.info("apply_refactor: applied %d edit(s) to %s", count, file_path)
        except OSError as exc:
            logger.error("apply_refactor: could not write %s: %s", file_path, exc)

    # Remove from pending after successful application.
    with _refactor_lock:
        _pending_refactors.pop(refactor_id, None)

    result = {
        "status": "ok",
        "applied": edits_applied,
        "files_modified": sorted(files_modified),
        "edits_applied": edits_applied,
    }
    logger.info("apply_refactor: completed %s — %d edits applied", refactor_id, edits_applied)
    return result
