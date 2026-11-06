"""#903 - tests_for bare-name fallback."""
import inspect

from code_review_graph.tools import query


class TestBareNameFallback:
    def test_query_source_contains_fallback(self):
        source = inspect.getsource(query)
        assert 'node.name != qn' in source, (
            'tests_for must retry with the bare name when the '
            'qualified-name lookup returns nothing (#903)'
        )
