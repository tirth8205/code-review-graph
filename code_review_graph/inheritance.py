"""Which class a name means, and which ancestor declares an inherited method.

``this.handle()`` in ``Leaf`` runs ``Alpha.handle`` when ``Leaf extends Alpha``
and only ``Alpha`` declares ``handle``. The parser reads one file, so it writes
``<leaf file>::Leaf.handle``, which names no node, and ``callers_of`` on
``Alpha.handle`` finds nothing. The same goes for ``pet.getName()`` when
``getName`` is declared on ``Pet``'s base class. See: #984

This index answers both questions from the stored graph:

* A class is known by its qualified name, never by its short name alone. Two
  files may each declare a ``Widget``.
* A name in an ``extends`` clause, or a declared receiver type, means a class
  only when the file says which one: the same file, a file it imports, a Java
  ``java_type`` or same-package class, or a PHP class in the same namespace.
* An inherited method is the nearest one up the superclass chain. Where a class
  has more than one parent that could hold code (Python), it must be the only
  declaration among all ancestors, so no lookup order can pick another. A
  parent the graph cannot name stops the walk, because it may declare the
  method itself.

Contributed in idea by #1036 (Anthony Ala).
"""

from __future__ import annotations

import json
import sqlite3
from typing import Optional

#: Languages where a class has one superclass and its other parents are
#: interfaces. A parent the graph cannot name is then taken to be an interface
#: when another parent is a known class.
_SINGLE_SUPERCLASS = frozenset({
    "java", "csharp", "typescript", "tsx", "javascript", "php",
})

#: How far up a chain to walk before giving up.
_MAX_DEPTH = 32


def _load_extra(raw: Optional[str]) -> dict:
    try:
        payload = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _is_interface(language: str, extra: dict) -> bool:
    """Does this Class node hold no code a subclass inherits through extends?"""
    if language == "java":
        return extra.get("java_kind") == "interface"
    if language in ("typescript", "tsx"):
        return extra.get("typescript_kind") in (
            "interface_declaration", "type_alias_declaration",
        )
    if language == "php":
        return extra.get("php_kind") in ("interface", "trait")
    return False


def _strip_type(raw: str) -> str:
    """``Base<T>`` -> ``Base``; ``Foo[]`` -> ``Foo``; whitespace trimmed."""
    text = raw.strip()
    for stop in ("<", "(", "["):
        text = text.split(stop, 1)[0]
    return text.strip()


class InheritanceIndex:
    """Class identities, their parents and their methods, read once from a graph.

    ``import_targets`` maps each file to the files it imports, as
    ``GraphStore._import_evidence`` builds it. ``raw_imports`` maps a file to
    the imports that name no file, short name to full name: a Java test under
    ``src/test`` imports ``org.acme.owner.Owner`` that way. The full name finds
    the class, or shows it comes from outside the graph, so a same-package class
    with that short name is not mistaken for it.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        import_targets: dict[str, set[str]],
        raw_imports: dict[str, dict[str, str]],
    ) -> None:
        self._imports = import_targets
        self._raw_imports = raw_imports
        # qualified name -> (short name, file, language, is_interface)
        self._classes: dict[str, tuple[str, str, str, bool]] = {}
        self._by_name: dict[str, list[str]] = {}
        self._java_fqcn: dict[str, Optional[str]] = {}
        self._java_package: dict[str, str] = {}
        for row in conn.execute(
            "SELECT qualified_name, name, file_path, parent_name, language, extra "
            "FROM nodes WHERE kind = 'Class'"
        ):
            qualified = row["qualified_name"]
            language = row["language"] or ""
            extra = _load_extra(row["extra"])
            self._classes[qualified] = (
                row["name"], row["file_path"], language,
                _is_interface(language, extra),
            )
            self._by_name.setdefault(row["name"], []).append(qualified)
            package = extra.get("java_package")
            if language == "java" and isinstance(package, str) and package:
                self._java_package.setdefault(row["file_path"], package)
                outer = f"{row['parent_name']}." if row["parent_name"] else ""
                fqcn = f"{package}.{outer}{row['name']}"
                # Two nodes for one name is no evidence for either.
                self._java_fqcn[fqcn] = (
                    qualified if fqcn not in self._java_fqcn else None
                )

        self._php_namespaces: dict[str, set[str]] = {}
        for row in conn.execute(
            "SELECT file_path, extra FROM nodes "
            "WHERE kind = 'File' AND extra LIKE '%\"php_namespaces\"%'"
        ):
            declared = _load_extra(row["extra"]).get("php_namespaces")
            if isinstance(declared, list):
                self._php_namespaces[row["file_path"]] = {
                    ns for ns in declared if isinstance(ns, str) and ns
                }
        # ``App\Mail\Mailer`` -> its class node, like ``_java_fqcn``.
        self._php_fqn: dict[str, Optional[str]] = {}
        for qualified, (name, file_path, language, _) in self._classes.items():
            if language != "php":
                continue
            for namespace in self._php_namespaces.get(file_path, ()):
                fqn = f"{namespace}\\{name}"
                self._php_fqn[fqn] = qualified if fqn not in self._php_fqn else None

        # (class, method name) -> the method node. A method's qualified name is
        # ``<file>::<parent_name>.<name>``, so its class is everything before
        # the last dot.
        self._methods: dict[tuple[str, str], str] = {}
        for row in conn.execute(
            "SELECT qualified_name, name FROM nodes "
            "WHERE kind IN ('Function', 'Test') AND parent_name IS NOT NULL"
        ):
            owner = row["qualified_name"].rpartition(".")[0]
            if owner in self._classes:
                self._methods.setdefault((owner, row["name"]), row["qualified_name"])

        self._raw_parents: dict[str, list[tuple[str, dict]]] = {}
        for row in conn.execute(
            "SELECT source_qualified, target_qualified, extra FROM edges "
            "WHERE kind = 'INHERITS'"
        ):
            if row["source_qualified"] in self._classes:
                self._raw_parents.setdefault(row["source_qualified"], []).append(
                    (row["target_qualified"], _load_extra(row["extra"])),
                )

        self._parents: dict[str, tuple[list[str], bool]] = {}
        self._inherited: dict[tuple[str, str], Optional[str]] = {}

    # -- Lookups ------------------------------------------------------------

    def is_class(self, qualified_name: str) -> bool:
        return qualified_name in self._classes

    def has_parents(self, qualified_name: str) -> bool:
        return qualified_name in self._raw_parents

    def java_package(self, file_path: str) -> Optional[str]:
        """The package a Java file declares, as the parser recorded it."""
        return self._java_package.get(file_path)

    def method_of(self, class_name: str, method: str) -> Optional[str]:
        """The method *class_name* declares itself, if any."""
        return self._methods.get((class_name, method))

    def resolve_type(
        self, raw: str, context_file: str, language: str,
    ) -> Optional[str]:
        """The one class *raw* can mean when written in *context_file*.

        None when the file gives no evidence, when two classes fit, or when the
        name comes from outside the graph.
        """
        text = _strip_type(raw)
        if language == "php":
            absolute = text.startswith("\\")
            namespace, _, name = text.strip("\\").rpartition("\\")
        else:
            absolute = False
            namespace, name = "", text.rpartition(".")[2]
        candidates = self._by_name.get(name, [])
        if not candidates:
            return None
        if namespace:
            # ``\App\Base`` or ``Sub\Base``: the namespace says which. A
            # relative one sits under the file's namespace, or under the
            # namespace a ``use`` gave its first part.
            wanted = {namespace}
            if not absolute:
                head, _, rest = namespace.partition("\\")
                wanted = {
                    f"{ns}\\{namespace}"
                    for ns in self._php_namespaces.get(context_file, ())
                }
                for target in self._imports.get(context_file, ()):
                    if target.rpartition("\\")[2] == head and "\\" in target:
                        wanted.add(f"{target}\\{rest}" if rest else target)
            return self._only([
                c for c in candidates
                if self._php_namespaces.get(self._classes[c][1], set()) & wanted
            ])
        same_file = [c for c in candidates if self._classes[c][1] == context_file]
        if same_file:
            return self._only(same_file)
        full_name = self._raw_imports.get(context_file, {}).get(name)
        if full_name is not None and language == "java":
            # None when the class is outside the graph (``java.util.Date``).
            return self._java_fqcn.get(full_name)
        if full_name is not None and language == "php":
            return self._php_fqn.get(full_name.strip("\\"))
        imported_files = self._imports.get(context_file, set())
        imported = [c for c in candidates if self._classes[c][1] in imported_files]
        if imported:
            return self._only(imported)
        if language == "java":
            package = self._java_package.get(context_file)
            if package:
                return self._only([
                    c for c in candidates
                    if self._java_package.get(self._classes[c][1]) == package
                ])
        if language == "php":
            namespaces = self._php_namespaces.get(context_file, set())
            if namespaces:
                return self._only([
                    c for c in candidates
                    if self._php_namespaces.get(self._classes[c][1], set())
                    & namespaces
                ])
        return None

    def inherited_method(self, class_name: str, method: str) -> Optional[str]:
        """The ancestor method a call to *method* on *class_name* runs, if sure.

        The class itself is not consulted: callers check ``method_of`` first.
        """
        key = (class_name, method)
        if key not in self._inherited:
            self._inherited[key] = self._walk(class_name, method)
        return self._inherited[key]

    # -- Internals ------------------------------------------------------------

    @staticmethod
    def _only(candidates: list[str]) -> Optional[str]:
        unique = set(candidates)
        return next(iter(unique)) if len(unique) == 1 else None

    def _resolve_parent(self, child: str, raw: str, extra: dict) -> Optional[str]:
        if raw in self._classes:
            return raw
        _, child_file, language, _ = self._classes[child]
        if language == "java":
            java_type = extra.get("java_type")
            if isinstance(java_type, str) and "." in _strip_type(java_type):
                # The parser read the imports and the package for us.
                return self._java_fqcn.get(_strip_type(java_type))
        return self.resolve_type(raw, child_file, language)

    def _parents_of(self, class_name: str) -> tuple[list[str], bool]:
        """(parents that can hold inherited code, whether any parent is unknown)."""
        if class_name not in self._parents:
            child_is_interface = self._classes[class_name][3]
            known: list[str] = []
            unknown = False
            for raw, extra in self._raw_parents.get(class_name, ()):
                parent = self._resolve_parent(class_name, raw, extra)
                if parent is None:
                    unknown = True
                elif parent == class_name:
                    continue
                elif self._classes[parent][3] and not child_is_interface:
                    # An implemented interface hands down no method body.
                    continue
                elif parent not in known:
                    known.append(parent)
            self._parents[class_name] = (known, unknown)
        return self._parents[class_name]

    def _walk(self, class_name: str, method: str) -> Optional[str]:
        current = class_name
        seen = {current}
        for _ in range(_MAX_DEPTH):
            parents, unknown = self._parents_of(current)
            if len(parents) > 1:
                return self._only_declaration(current, method)
            if not parents:
                return None
            if unknown and self._classes[current][2] not in _SINGLE_SUPERCLASS:
                return None
            parent = parents[0]
            if parent in seen:
                return None
            seen.add(parent)
            found = self._methods.get((parent, method))
            if found:
                return found
            current = parent
        return None

    def _only_declaration(self, class_name: str, method: str) -> Optional[str]:
        """The one ancestor declaring *method*, when every ancestor is known."""
        found: set[str] = set()
        seen = {class_name}
        frontier = [class_name]
        for _ in range(_MAX_DEPTH):
            if not frontier:
                break
            next_frontier: list[str] = []
            for current in frontier:
                parents, unknown = self._parents_of(current)
                if unknown:
                    return None
                for parent in parents:
                    if parent in seen:
                        continue
                    seen.add(parent)
                    declared = self._methods.get((parent, method))
                    if declared:
                        found.add(declared)
                    next_frontier.append(parent)
            frontier = next_frontier
        else:
            return None
        return next(iter(found)) if len(found) == 1 else None
