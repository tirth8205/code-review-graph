"""Base class for language-specific parsing handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..parser import CodeParser, EdgeInfo, NodeInfo


class BaseLanguageHandler:
    """Override methods where a language differs from default CodeParser logic.

    Methods returning ``NotImplemented`` signal 'use the default code path'.
    Subclasses only need to override what they actually customise.
    """

    language: str = ""
    class_types: list[str] = []
    function_types: list[str] = []
    import_types: list[str] = []
    call_types: list[str] = []
    builtin_names: frozenset[str] = frozenset()

    def get_name(self, node, kind: str) -> str | None:
        return NotImplemented

    def get_bases(self, node, source: bytes) -> list[str]:
        return NotImplemented

    def extract_import_targets(self, node, source: bytes) -> list[str]:
        return NotImplemented

    def collect_import_names(self, node, file_path: str, import_map: dict[str, str]) -> bool:
        """Populate import_map from an import node. Return True if handled."""
        return False

    def resolve_module(self, module: str, caller_file: str) -> str | None:
        """Resolve a module path to a file path. Return NotImplemented to fall back."""
        return NotImplemented

    def extract_constructs(
        self,
        child,
        node_type: str,
        parser: CodeParser,
        source: bytes,
        file_path: str,
        nodes: list[NodeInfo],
        edges: list[EdgeInfo],
        enclosing_class: str | None,
        enclosing_func: str | None,
        import_map: dict[str, str] | None,
        defined_names: set[str] | None,
        depth: int,
    ) -> bool:
        """Handle language-specific AST constructs.

        Returns True if the child was fully handled (skip generic dispatch).
        Default: returns False (no language-specific handling).
        """
        return False
