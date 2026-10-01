"""Grammar dispatch names must exist in the bundled grammar (issue #986)."""

import pytest
from tree_sitter_language_pack import get_language

from code_review_graph.parser import _CALL_TYPES, _CLASS_TYPES, _FUNCTION_TYPES, _IMPORT_TYPES


@pytest.mark.parametrize(
    ("table", "language", "node_type"),
    [
        (table, language, node_type)
        for table, mapping in (
            ("classes", _CLASS_TYPES), ("functions", _FUNCTION_TYPES),
            ("imports", _IMPORT_TYPES), ("calls", _CALL_TYPES),
        )
        for language, names in mapping.items()
        for node_type in names
    ],
)
def test_dispatch_type_exists_in_grammar(table, language, node_type):
    grammar = get_language(language)
    assert grammar.id_for_node_kind(node_type, True) is not None, (
        f"{table}[{language!r}] contains nonexistent named node {node_type!r}"
    )
