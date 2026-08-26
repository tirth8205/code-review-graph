"""#906 - the automatic full-rebuild fallback must apply the root guard."""
from code_review_graph.incremental import _assert_graph_matches_root


class TestRootGuardImport:
    def test_assert_graph_matches_root_is_importable(self):
        assert callable(_assert_graph_matches_root)


class TestSourceGuard:
    def test_build_py_contains_root_guard_before_auto_full_rebuild(self):
        import inspect

        from code_review_graph.tools import build
        source = inspect.getsource(build)
        assert '_assert_graph_matches_root' in source
        assert 'user_requested_full' in source
