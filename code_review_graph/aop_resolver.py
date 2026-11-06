"""Post-build Spring AOP advice-to-target call resolver.

Tree-sitter parsing sees a ``@Before``/``@Around``/... advice method inside an
``@Aspect`` class and the business methods it intercepts as two unrelated
methods — there is no ``CALLS`` edge between them, because the interception
happens through a Spring AOP proxy at runtime, not through a static method
call in the advice's source text.  This module approximates that missing
edge by reading the pointcut expression each advice annotation carries
(already captured verbatim in ``extra["decorators"]`` by the parser) and
matching it against candidate target methods already in the graph.

Resolution chain:
    ``@Aspect`` class
        → ``@Pointcut``/``@Before``/``@After``/``@Around``/``@AfterReturning``/
          ``@AfterThrowing`` methods inside it
        → pointcut expression (inline, or via a same-class named
          ``@Pointcut`` reference)
        → candidate target methods matched against that expression
        → ``CALLS`` edge, advice → target, tagged ``extra.aop_resolved``

Scope (see issue #592 — Option 1, regex-based approximation):

* ``@annotation(some.pkg.Foo)`` pointcuts are matched by simple annotation
  name (``Foo``) against ``extra["decorators"]`` on the candidate method
  itself. This is exact enough for the common case, but two same-named
  annotations in different packages are indistinguishable — a known
  approximation, consistent with the rest of Option 1. A *class-level*
  ``extra["spring_annotations"]`` match (e.g. a class-level
  ``@Transactional``) is intentionally **not** expanded to every method in
  that class — that is ``@within()`` semantics, a different pointcut
  designator, and conflating the two produced widespread false positives
  on common class-level stereotype annotations (see PR #916 review).
* ``execution(...)`` pointcuts are matched segment by segment against the
  full ``package.ClassName.methodName`` portion of a candidate — the
  return-type pattern and parameter pattern (``(..)``) are discarded, but
  the declaring-type pattern (package included) is matched in full against
  each candidate's resolved Java package (``extra["java_package"]``, set at
  parse time from the file's ``package`` declaration — see
  ``_extract_classes`` in ``parser.py``) joined with its class and method
  name.  A candidate in Java's default (unnamed) package has no
  ``java_package`` and is matched using the bare ``ClassName.methodName``,
  so a pointcut that names a package never matches a default-package class.
  A declaring type of exactly ``*`` (``execution(* *.*(..))``) means any
  type in any package, as in AspectJ.
  This is still an approximation of full AspectJ semantics (no
  supertype/interface expansion, no ``+`` subtype matching) and can produce
  false positives/negatives on those axes — see issue #592 — but no longer
  discards package selectivity: two same-named classes in different
  packages are no longer conflated (see PR #916 review).
* Pointcut expressions combining multiple sub-expressions with ``&&`` or
  ``||`` are compound boolean pointcuts and are skipped entirely rather
  than partially parsed, to avoid emitting a wrong edge. This check also
  applies to the expression a same-class named ``@Pointcut`` reference
  resolves to, not just the reference text itself. A bare ``!`` negation
  is not recognized by either pointcut-kind matcher below, so it is
  skipped too — just by falling through unparsed rather than via an
  explicit compound-operator check.
* Constructors (``extra["java_constructor"]``), test methods and methods of
  ``@Aspect`` classes are never targets: Spring AOP proxies method calls on
  beans, does not auto-proxy aspects, and tests run outside the proxy.
* Advice whose pointcut is not one complete string literal (a constant, or
  literals joined with ``+``) is skipped rather than partially resolved.
* Annotations are compared by simple name, so a fully qualified method or
  advice annotation (``@com.acme.Audited``, ``@org.aspectj.lang.annotation.Before``)
  matches. A fully qualified *class-level* ``@org.aspectj.lang.annotation.Aspect``
  is still not recognized: aspects are found through
  ``extra["spring_annotations"]``, which ``_get_java_annotations`` in
  ``parser.py`` fills with simple-name annotations only. Changing that would
  alter Spring stereotype detection as a whole, so it is left for a separate
  change.
* ``within(Type+)``, ``args()``/``target()``/``this()``, and named pointcut
  references that cross an ``@Aspect`` class boundary are out of scope for
  this pass (see the narrower AOP ``LanguageGap`` note in
  ``uncertainty.py``) and are left for a future PR.

Reuses the existing ``CALLS`` edge kind rather than introducing a new one,
so advice→target edges are automatically picked up by ``callers_of``,
``callees_of``, and ``get_impact_radius`` without touching their edge-kind
allowlists. Two dedicated query patterns, ``advises`` and ``advised_by``,
filter this same data down to only the ``extra.aop_resolved`` edges — use
those when the goal is specifically "what AOP relationships exist here"
rather than every caller/callee regardless of provenance.

Edge *direction* for impact analysis is the one place these edges do not
follow the plain ``CALLS`` policy. ``CALLS`` propagates target→source
(changing a callee impacts its callers), but an advice does not get called
by the method it wraps — plain propagation would report "change the
target, the aspect is impacted" and miss the relationship that actually
matters: "change the advice (its logic runs around every target), what
does that impact". ``graph.py``'s impact-radius traversal (both the SQL and
NetworkX engines) special-cases any edge carrying ``extra.aop_resolved`` to
always propagate source→target (advice → its targets), overriding
``IMPACT_EDGE_DIRECTIONS["CALLS"]`` for just these edges — see PR #916
review. ``callers_of``/``callees_of``/``advises``/``advised_by`` are plain
graph traversal, not impact analysis, and are unaffected by this override.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Optional

from .parser import EdgeInfo

if TYPE_CHECKING:
    from .graph import GraphStore

logger = logging.getLogger(__name__)

_ADVICE_ANNOTATIONS = frozenset(
    {"Before", "After", "Around", "AfterReturning", "AfterThrowing"}
)

# A pointcut expression combining sub-expressions with boolean operators is
# out of scope for this regex-approximation pass — see module docstring.
_COMPOUND_OPERATORS = ("&&", "||")

# A bare same-class named-pointcut reference, e.g. "pointcut1()". A dotted
# reference (e.g. "com.foo.OtherAspect.pointcut1()") is a cross-aspect
# reference, which is explicitly out of scope (see module docstring).
_NAMED_POINTCUT_REF = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\(\s*\)\s*$")
_ANNOTATION_POINTCUT = re.compile(r"^\s*@annotation\s*\((.+)\)\s*$", re.DOTALL)
_EXECUTION_POINTCUT = re.compile(r"^\s*execution\s*\((.*)\)\s*$", re.DOTALL)
# A pointcut that opens with a designator, whitespace allowed before "(" as
# both matchers above allow it.
_DESIGNATOR_PREFIX = re.compile(
    r"^\s*(?:execution|@annotation|within|args|target|this)\s*\("
)

_DERIVED_FLAG = "aop_resolved"


def _extract_annotation_string_arg(deco_text: str) -> Optional[str]:
    """Return the pointcut expression embedded in an advice annotation's raw text.

    Handles the common positional form (``@Before("expr")``) as well as
    named-argument forms (``@AfterThrowing(pointcut = "expr", throwing =
    "ex")`` / ``value = "expr"``). Returns ``None`` when no string literal
    argument can be found, or when literals are joined with ``+``: reading only
    the first would drop whatever the rest adds, such as a ``&&``.
    """
    match = re.search(r'(?:pointcut|value)\s*=\s*"((?:[^"\\]|\\.)*)"', deco_text)
    if not match:
        match = re.search(r'"((?:[^"\\]|\\.)*)"', deco_text)
    if not match:
        return None
    # A literal joined to anything with "+" is a constant expression this
    # pass cannot evaluate; resolving only one piece of it could drop an
    # "&&" and advise the wrong methods, so give up instead.
    if deco_text[: match.start()].rstrip().endswith("+") or deco_text[
        match.end():
    ].lstrip().startswith("+"):
        return None
    return match.group(1)


def _annotation_head(deco_text: str) -> str:
    """Return the simple annotation name from a raw decorator string.

    Arguments and any package qualifier are dropped, so ``com.acme.Audited``
    and ``Audited`` compare equal.
    """
    head = deco_text.split("(", 1)[0].strip().lstrip("@").strip()
    return head.rsplit(".", 1)[-1].strip()


def _glob_matches(pattern: str, text: str) -> bool:
    """Match one segment where ``*`` means any characters, without backtracking."""
    p = t = mark = 0
    star = -1
    while t < len(text):
        if p < len(pattern) and pattern[p] == "*":
            star, mark = p, t
            p += 1
        elif p < len(pattern) and pattern[p] == text[t]:
            p += 1
            t += 1
        elif star >= 0:
            mark += 1
            p, t = star + 1, mark
        else:
            return False
    return not pattern[p:].strip("*")


class _DottedPattern:
    """An AspectJ-style dotted wildcard pattern, matched one segment at a time.

    Rules (approximate — see module docstring for the scope this is applied
    within):

    * ``..`` means "any number of intervening whole segments", zero included
    * a lone ``.`` separates two segments
    * ``*`` within a segment means "any characters but a dot"
    * every other character is matched literally

    The pattern text comes from the repository being indexed, and a regex over
    repeated ``..*`` or ``*a*a`` backtracks catastrophically, so no regex is
    built, not even for one segment. Tracking the reachable positions keeps
    this polynomial on any input.
    """

    def __init__(self, pattern: str, any_package_prefix: bool = False) -> None:
        # ``None`` stands for a ``..`` gap.
        self._parts: list[Optional[str]] = [None] if any_package_prefix else []
        for index, run in enumerate(pattern.split("..")):
            if index:
                self._parts.append(None)
            if run:
                self._parts.extend(run.split("."))

    def match(self, subject: str) -> bool:
        segments = subject.split(".")
        if "" in segments:
            return False
        reachable = {0}
        for part in self._parts:
            if part is None:
                reachable = set(range(min(reachable), len(segments) + 1))
            else:
                reachable = {
                    position + 1
                    for position in reachable
                    if position < len(segments) and _glob_matches(part, segments[position])
                }
            if not reachable:
                return False
        return len(segments) in reachable


def _parse_execution_expression(expr: str) -> Optional[tuple[_DottedPattern, bool]]:
    """Reduce an ``execution(...)`` pointcut expression to a (pattern, needs_class) pair.

    ``needs_class`` is True when the expression's method-pattern token
    included a declaring-type segment (e.g. ``com.foo.*.*``), so the pattern
    must be matched against the candidate's full ``package.ClassName.methodName``
    (see ``resolve_aop_advice``); it is False for a method-name-only pattern
    (e.g. ``*get*Index``), matched against the bare method name.  The full
    declaring-type+method pattern is kept, package included — no segments are
    dropped, so an explicit package qualifier in the pointcut stays selective
    (see module docstring).  Returns ``None`` when the expression cannot be
    safely reduced (e.g. no parameter-pattern parens found).
    """
    m = _EXECUTION_POINTCUT.match(expr)
    if not m:
        return None
    inner = m.group(1).strip()

    # inner = "<ret> <declaring-type?.method-name-pattern>(<params>)"
    m2 = re.match(r"^(.*)\((.*)\)\s*$", inner, re.DOTALL)
    if not m2:
        return None
    before_params = m2.group(1).strip()
    tokens = before_params.split()
    if not tokens:
        return None
    # ``execution(@Foo * *(..))`` and ``execution(!public * *(..))`` narrow the match
    # in ways the name pattern below cannot see, so it would match far too much.
    if any(token.startswith(("@", "!")) for token in tokens):
        return None
    method_class_pattern = tokens[-1]
    if not method_class_pattern or method_class_pattern.endswith("."):
        return None

    needs_class = "." in method_class_pattern
    # A declaring type of exactly "*" means any type, in any package, so
    # only the bare "*.method" shape gets a free package prefix. A pattern
    # that names a package keeps matching that package exactly.
    universal_type = method_class_pattern.split(".")[0] == "*" and (
        method_class_pattern.count(".") == 1
    )
    return _DottedPattern(method_class_pattern, universal_type), needs_class


def _resolve_pointcut_expr(
    expr: str, pointcuts: dict[str, str],
) -> Optional[str]:
    """Follow a same-class named ``@Pointcut`` reference to its expression.

    Returns the expression to actually parse (``expr`` itself when it is
    already an inline expression), or ``None`` when ``expr`` is a
    cross-aspect reference, an unresolvable reference, or a compound boolean
    expression — all out of scope for this pass. The compound check is
    applied both to ``expr`` and, when ``expr`` is a named reference, to the
    ``@Pointcut`` body it resolves to — a bare reference like
    ``"myPointcut()"`` carries no ``&&``/``||`` itself even when the
    pointcut it names does.
    """
    if any(op in expr for op in _COMPOUND_OPERATORS):
        return None

    ref = _NAMED_POINTCUT_REF.match(expr)
    if ref:
        name = ref.group(1)
        resolved = pointcuts.get(name)
        if resolved is None or any(op in resolved for op in _COMPOUND_OPERATORS):
            return None
        return resolved

    if "." in expr and "(" in expr and not _DESIGNATOR_PREFIX.match(expr):
        # Looks like a dotted "Other.pointcut()" cross-aspect reference.
        return None

    return expr


def _load_extra(raw: Optional[str]) -> dict:
    try:
        return json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}


def resolve_aop_advice(store: "GraphStore") -> dict:
    """Resolve Spring AOP advice methods to the target methods their pointcut matches.

    Safe to call multiple times: previously derived edges (tagged
    ``extra.aop_resolved``) are cleared and rebuilt on every call, so a
    changed or removed pointcut cannot leave a stale edge behind.

    Returns a dict with resolution counts for telemetry.
    """
    conn = store._conn

    java_files: set[str] = {
        row["file_path"]
        for row in conn.execute(
            "SELECT DISTINCT file_path FROM nodes WHERE language = 'java'"
        ).fetchall()
    }
    if not java_files:
        return {
            "aspects_indexed": 0,
            "advice_resolved": 0,
            "calls_created": 0,
            "stale_calls_removed": 0,
        }

    # -------------------------------------------------------------------
    # Clear previously derived edges so stale advice→target links from a
    # since-changed pointcut don't linger.
    # -------------------------------------------------------------------
    stale_ids: list[tuple[int]] = []
    for row in conn.execute(
        "SELECT id, extra FROM edges WHERE kind = 'CALLS'"
    ).fetchall():
        if _load_extra(row["extra"]).get(_DERIVED_FLAG):
            stale_ids.append((row["id"],))
    if stale_ids:
        conn.executemany("DELETE FROM edges WHERE id = ?", stale_ids)

    # -------------------------------------------------------------------
    # Index @Aspect classes and every Java method, grouped by (class, file).
    # -------------------------------------------------------------------
    aspect_classes: list[dict] = []
    class_package_by_name_file: dict[tuple[str, str], str] = {}
    for row in conn.execute(
        "SELECT name, qualified_name, file_path, extra FROM nodes "
        "WHERE kind = 'Class' AND language = 'java'"
    ).fetchall():
        extra = _load_extra(row["extra"])
        class_package_by_name_file[(row["name"], row["file_path"])] = (
            extra.get("java_package") or ""
        )
        if "Aspect" in (extra.get("spring_annotations") or []):
            aspect_classes.append({
                "name": row["name"],
                "qualified_name": row["qualified_name"],
                "file_path": row["file_path"],
            })

    if not aspect_classes:
        logger.info("AOP resolver: no @Aspect classes found, skipping")
        return {
            "aspects_indexed": 0,
            "advice_resolved": 0,
            "calls_created": 0,
            "stale_calls_removed": len(stale_ids),
        }

    # Advice is discovered in every method, but only proxied business methods can
    # be its targets. Spring does not proxy aspects, tests run outside the proxy,
    # and a constructor runs before there is an object to wrap.
    aspect_keys = {(aspect["name"], aspect["file_path"]) for aspect in aspect_classes}
    all_methods: list[dict] = []
    methods_by_class_file: dict[tuple[str, str], list[dict]] = {}
    for row in conn.execute(
        "SELECT kind, name, qualified_name, parent_name, file_path, extra FROM nodes "
        "WHERE kind IN ('Function', 'Test') AND language = 'java' "
        "AND parent_name IS NOT NULL"
    ).fetchall():
        extra = _load_extra(row["extra"])
        entry = {
            "name": row["name"],
            "qualified_name": row["qualified_name"],
            "parent_name": row["parent_name"],
            "file_path": row["file_path"],
            "decorators": extra.get("decorators") or [],
            "java_package": class_package_by_name_file.get(
                (row["parent_name"], row["file_path"]), "",
            ),
        }
        key = (row["parent_name"], row["file_path"])
        if (
            row["kind"] == "Function"
            and key not in aspect_keys
            and not extra.get("java_constructor")
        ):
            all_methods.append(entry)
        methods_by_class_file.setdefault(
            (row["parent_name"], row["file_path"]), [],
        ).append(entry)

    advice_resolved = 0
    calls_created = 0

    for aspect in aspect_classes:
        methods = methods_by_class_file.get(
            (aspect["name"], aspect["file_path"]), [],
        )

        pointcuts: dict[str, str] = {}
        advices: list[tuple[dict, str, str]] = []
        for method in methods:
            for deco in method["decorators"]:
                head = _annotation_head(deco)
                if head == "Pointcut":
                    pc_expr = _extract_annotation_string_arg(deco)
                    if pc_expr:
                        pointcuts[method["name"]] = pc_expr
                elif head in _ADVICE_ANNOTATIONS:
                    adv_expr = _extract_annotation_string_arg(deco)
                    if adv_expr:
                        advices.append((method, head, adv_expr))

        for advice_method, advice_kind, raw_expr in advices:
            expr = _resolve_pointcut_expr(raw_expr, pointcuts)
            if not expr:
                continue

            ann_match = _ANNOTATION_POINTCUT.match(expr)
            if ann_match:
                fqcn = ann_match.group(1).strip()
                bare_name = fqcn.rsplit(".", 1)[-1].strip()
                if not bare_name:
                    continue
                pointcut_kind = "@annotation"
                # Method-level match only — a class-level stereotype
                # annotation (e.g. @Transactional) is @within() semantics,
                # a different pointcut designator, and is not expanded here
                # (see module docstring).
                targets = [
                    candidate
                    for candidate in all_methods
                    if any(
                        _annotation_head(d) == bare_name
                        for d in candidate["decorators"]
                    )
                ]
            else:
                parsed = _parse_execution_expression(expr)
                if not parsed:
                    continue
                pattern, needs_class = parsed
                pointcut_kind = "execution"
                targets = []
                for candidate in all_methods:
                    if needs_class:
                        if not candidate["parent_name"]:
                            continue
                        class_part = (
                            f"{candidate['java_package']}.{candidate['parent_name']}"
                            if candidate["java_package"]
                            else candidate["parent_name"]
                        )
                        subject = f"{class_part}.{candidate['name']}"
                    else:
                        subject = candidate["name"]
                    if pattern.match(subject):
                        targets.append(candidate)

            if not targets:
                continue

            advice_resolved += 1
            for target in targets:
                if target["qualified_name"] == advice_method["qualified_name"]:
                    continue
                store.upsert_edge(EdgeInfo(
                    kind="CALLS",
                    source=advice_method["qualified_name"],
                    target=target["qualified_name"],
                    file_path=advice_method["file_path"],
                    line=0,
                    extra={
                        _DERIVED_FLAG: True,
                        "pointcut_kind": pointcut_kind,
                        "pointcut_expr": expr,
                        "advice_kind": advice_kind,
                        "confidence": 0.7,
                        "confidence_tier": "INFERRED",
                    },
                ))
                calls_created += 1

    store.commit()
    logger.info(
        "AOP resolver: %d @Aspect classes, %d advice resolved, %d CALLS edges created",
        len(aspect_classes), advice_resolved, calls_created,
    )
    return {
        "aspects_indexed": len(aspect_classes),
        "advice_resolved": advice_resolved,
        "calls_created": calls_created,
        "stale_calls_removed": len(stale_ids),
    }
