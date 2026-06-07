"""Tests for the DSL output mode added to graph and tool layers.

Covers:
  * Unit tests for ``node_to_dsl`` / ``edge_to_dsl`` covering happy path,
    optional fields (parent_name, is_test), unknown kinds, file_path prefix
    stripping, and security parity with the dict encoder (control-char
    sanitisation).
  * ``encode_nodes`` / ``encode_edges`` dispatch on both ``"dict"`` and
    ``"dsl"`` modes.
  * Integration tests for ``get_impact_radius`` and ``query_graph`` with
    ``format="dsl"`` — verifying the response shape, presence of the
    legend, and that DSL output is meaningfully smaller than dict output.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from code_review_graph.graph import (
    DSL_LEGEND,
    EdgeInfo,
    GraphEdge,
    GraphNode,
    GraphStore,
    NodeInfo,
    edge_to_dict,
    edge_to_dsl,
    encode_edges,
    encode_nodes,
    node_to_dict,
    node_to_dsl,
)
from code_review_graph.tools.query import get_impact_radius, query_graph

# ---------------------------------------------------------------------------
# Encoder unit tests
# ---------------------------------------------------------------------------

def _mk_node(**overrides) -> GraphNode:
    base = dict(
        id=1, kind="Function", name="login",
        qualified_name="/repo/auth.py::AuthService::login",
        file_path="/repo/auth.py", line_start=10, line_end=20,
        language="python",
        parent_name="/repo/auth.py::AuthService",
        params="self, user, pw", return_type="bool",
        is_test=False, file_hash=None, extra={},
    )
    base.update(overrides)
    return GraphNode(**base)


def _mk_edge(**overrides) -> GraphEdge:
    base = dict(
        id=1, kind="CALLS",
        source_qualified="/repo/main.py::process",
        target_qualified="/repo/auth.py::AuthService::login",
        file_path="/repo/main.py", line=42, extra={},
        confidence=0.95, confidence_tier="EXTRACTED",
    )
    base.update(overrides)
    return GraphEdge(**base)


class TestNodeToDsl:
    def test_function_with_parent(self):
        out = node_to_dsl(_mk_node())
        assert out == "fn login@/repo/auth.py:10-20 py parent=AuthService"

    def test_function_top_level(self):
        out = node_to_dsl(_mk_node(parent_name=None))
        # No parent= clause when there is no parent
        assert out == "fn login@/repo/auth.py:10-20 py"
        assert "parent=" not in out

    def test_class(self):
        out = node_to_dsl(_mk_node(
            kind="Class", name="AuthService",
            qualified_name="/repo/auth.py::AuthService",
            line_start=5, line_end=40, parent_name=None,
        ))
        assert out == "cl AuthService@/repo/auth.py:5-40 py"

    def test_file(self):
        out = node_to_dsl(_mk_node(
            kind="File", name="/repo/auth.py",
            qualified_name="/repo/auth.py",
            file_path="/repo/auth.py", line_start=1, line_end=50,
            parent_name=None,
        ))
        assert out.startswith("fi ")
        assert "/repo/auth.py" in out

    def test_is_test_flag(self):
        out = node_to_dsl(_mk_node(
            kind="Test", name="test_login",
            file_path="/repo/test_auth.py",
            line_start=1, line_end=10, is_test=True, parent_name=None,
        ))
        assert "[T]" in out
        assert out.startswith("tst ")

    def test_unknown_language_falls_through(self):
        out = node_to_dsl(_mk_node(language="brainfuck"))
        # Unknown languages are emitted as-is rather than dropped
        assert " brainfuck " in out + " "

    def test_lua_and_luau_distinguishable(self):
        """lua and luau must produce different DSL tokens — no collision."""
        lua_out = node_to_dsl(_mk_node(language="lua", parent_name=None))
        luau_out = node_to_dsl(_mk_node(language="luau", parent_name=None))
        assert lua_out != luau_out
        assert " lu" in lua_out  # short code for lua
        assert " luau" in luau_out  # falls through to verbose

    def test_unknown_kind_falls_through(self):
        # Custom kinds (e.g. a future "Macro" type) should not crash;
        # they degrade to their lowercased form.
        out = node_to_dsl(_mk_node(kind="Macro", parent_name=None))
        assert out.startswith("macro ")

    def test_control_chars_sanitised(self):
        """Security parity with node_to_dict — control chars stripped."""
        n = _mk_node(
            name="evil\x00name",
            qualified_name="/repo/auth.py::evil\x00name",
            parent_name=None,
        )
        out = node_to_dsl(n)
        assert "\x00" not in out

    def test_newlines_in_name_collapsed(self):
        """Newlines/tabs must not break the one-line-per-row DSL contract."""
        n = _mk_node(name="weird\nname\twith\nbreaks", parent_name=None)
        out = node_to_dsl(n)
        assert "\n" not in out
        assert "\t" not in out
        # Should still preserve the readable content as one line
        assert "weird" in out and "name" in out

    def test_none_language_handled(self):
        """A node with language=None must not emit literal 'None'."""
        n = _mk_node(language=None, parent_name=None)
        out = node_to_dsl(n)
        assert "None" not in out
        assert " none " not in out.lower()


class TestEdgeToDsl:
    def test_basic_call(self):
        out = edge_to_dsl(_mk_edge())
        # source file_path (/repo/main.py) IS the edge file_path,
        # so the prefix gets stripped
        assert out == "process\u2192/repo/auth.py::AuthService::login c @/repo/main.py:42 0.95"

    def test_source_prefix_stripped(self):
        """Edge source's file_path prefix gets stripped when it equals the
        edge's file_path — that's the common case for intra-file relations."""
        out = edge_to_dsl(_mk_edge(
            source_qualified="/repo/a.py::foo",
            file_path="/repo/a.py",
        ))
        assert out.startswith("foo\u2192")
        assert "/repo/a.py::foo" not in out  # full source qn stripped

    def test_source_prefix_not_stripped_when_mismatch(self):
        """If source's file_path differs from edge's file_path (rare but
        possible for cross-file relations), no stripping happens."""
        out = edge_to_dsl(_mk_edge(
            source_qualified="/repo/other.py::foo",
            file_path="/repo/a.py",
        ))
        assert "/repo/other.py::foo" in out

    def test_edge_kind_codes(self):
        for kind, code in [
            ("CALLS", "c"), ("IMPORTS_FROM", "i"),
            ("INHERITS", "h"), ("CONTAINS", "n"),
            ("TESTED_BY", "t"), ("REFERENCES", "r"),
        ]:
            out = edge_to_dsl(_mk_edge(kind=kind))
            assert f" {code} @" in out, f"{kind} did not produce ' {code} @'"

    def test_unknown_edge_kind_falls_through(self):
        out = edge_to_dsl(_mk_edge(kind="FOLLOWS"))
        assert " follows @" in out

    def test_control_chars_sanitised(self):
        e = _mk_edge(
            source_qualified="/repo/main.py::ev\x00il",
            target_qualified="/repo/auth.py::lo\x01gin",
        )
        out = edge_to_dsl(e)
        assert "\x00" not in out and "\x01" not in out


# ---------------------------------------------------------------------------
# Dispatch helpers
# ---------------------------------------------------------------------------

class TestEncodeHelpers:
    def test_encode_nodes_dict(self):
        out = encode_nodes([_mk_node()], fmt="dict")
        assert isinstance(out[0], dict)
        assert out[0]["name"] == "login"

    def test_encode_nodes_dsl(self):
        out = encode_nodes([_mk_node()], fmt="dsl")
        assert isinstance(out[0], str)
        assert out[0].startswith("fn login@")

    def test_encode_edges_dict(self):
        out = encode_edges([_mk_edge()], fmt="dict")
        assert isinstance(out[0], dict)
        assert out[0]["kind"] == "CALLS"

    def test_encode_edges_dsl(self):
        out = encode_edges([_mk_edge()], fmt="dsl")
        assert isinstance(out[0], str)
        assert "\u2192" in out[0]  # arrow present

    def test_encode_nodes_default_is_dict(self):
        out = encode_nodes([_mk_node()])
        assert isinstance(out[0], dict)


# ---------------------------------------------------------------------------
# Compression sanity check
# ---------------------------------------------------------------------------

class TestCompression:
    def test_node_dsl_significantly_smaller(self):
        n = _mk_node(
            qualified_name="/repo/services/orders/validator.py"
                           "::OrderValidator::validateOrder",
            file_path="/repo/services/orders/validator.py",
            parent_name="/repo/services/orders/validator.py::OrderValidator",
            line_start=142, line_end=178,
        )
        dict_len = len(json.dumps(node_to_dict(n)))
        dsl_len = len(node_to_dsl(n))
        # Realistic nodes compress at least 3×
        assert dict_len / dsl_len >= 3.0, (
            f"expected ≥3× compression, got {dict_len/dsl_len:.2f}× "
            f"({dict_len} → {dsl_len})"
        )

    def test_edge_dsl_smaller(self):
        e = _mk_edge(
            source_qualified="/repo/services/orders/validator.py"
                             "::OrderValidator::validateOrder",
            target_qualified="/repo/services/orders/db.py::Database::persist",
            file_path="/repo/services/orders/validator.py",
        )
        dict_len = len(json.dumps(edge_to_dict(e)))
        dsl_len = len(edge_to_dsl(e))
        assert dict_len / dsl_len >= 2.0, (
            f"expected ≥2× compression, got {dict_len/dsl_len:.2f}× "
            f"({dict_len} → {dsl_len})"
        )


# ---------------------------------------------------------------------------
# Tool integration tests (use a real GraphStore on disk)
# ---------------------------------------------------------------------------

class TestToolIntegration:
    def setup_method(self):
        # Build a fake repo dir so _validate_repo_root accepts it
        self.tmp_dir = tempfile.mkdtemp()
        self.root = Path(self.tmp_dir).resolve()
        (self.root / ".git").mkdir()
        crg_dir = self.root / ".code-review-graph"
        crg_dir.mkdir()
        self.db_path = crg_dir / "graph.db"
        self.store = GraphStore(self.db_path)
        self._seed()

    def teardown_method(self):
        self.store.close()
        import shutil
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _seed(self):
        s = self.store
        auth_path = str(self.root / "auth.py")
        main_path = str(self.root / "main.py")

        s.upsert_node(NodeInfo(
            kind="File", name=auth_path, file_path=auth_path,
            line_start=1, line_end=50, language="python",
        ))
        s.upsert_node(NodeInfo(
            kind="File", name=main_path, file_path=main_path,
            line_start=1, line_end=30, language="python",
        ))
        s.upsert_node(NodeInfo(
            kind="Class", name="AuthService", file_path=auth_path,
            line_start=5, line_end=40, language="python",
        ))
        s.upsert_node(NodeInfo(
            kind="Function", name="login", file_path=auth_path,
            line_start=10, line_end=20, language="python",
            parent_name="AuthService",
        ))
        s.upsert_node(NodeInfo(
            kind="Function", name="process", file_path=main_path,
            line_start=5, line_end=15, language="python",
        ))
        s.upsert_edge(EdgeInfo(
            kind="CALLS", source=f"{main_path}::process",
            target=f"{auth_path}::AuthService.login",
            file_path=main_path, line=10,
        ))
        s.commit()
        self.auth_path = auth_path
        self.main_path = main_path

    def test_get_impact_radius_dsl_mode(self):
        result = get_impact_radius(
            changed_files=[self.main_path],
            repo_root=str(self.root),
            format="dsl",
        )
        assert result["status"] == "ok"
        assert "legend" in result
        assert result["legend"] == DSL_LEGEND
        # changed_nodes should now be DSL strings, not dicts
        for line in result["changed_nodes"]:
            assert isinstance(line, str)
            # Each DSL line carries '@' between name and file_path
            assert "@" in line

    def test_get_impact_radius_dict_mode_unchanged(self):
        """Default mode must remain identical to pre-PR behaviour."""
        result = get_impact_radius(
            changed_files=[self.main_path],
            repo_root=str(self.root),
        )
        assert result["status"] == "ok"
        assert "legend" not in result
        for n in result["changed_nodes"]:
            assert isinstance(n, dict)
            assert "name" in n and "kind" in n

    def test_get_impact_radius_dsl_smaller_than_dict(self):
        dict_resp = get_impact_radius(
            changed_files=[self.main_path],
            repo_root=str(self.root),
            format="dict",
        )
        dsl_resp = get_impact_radius(
            changed_files=[self.main_path],
            repo_root=str(self.root),
            format="dsl",
        )
        dict_size = len(json.dumps(dict_resp))
        dsl_size = len(json.dumps(dsl_resp))
        # Even on a tiny 5-node graph, DSL should not be larger
        assert dsl_size <= dict_size, (
            f"DSL response ({dsl_size} chars) larger than dict ({dict_size})"
        )

    def test_query_graph_dsl_mode(self):
        result = query_graph(
            pattern="callees_of",
            target=f"{self.main_path}::process",
            repo_root=str(self.root),
            format="dsl",
        )
        assert result["status"] == "ok"
        assert result["legend"] == DSL_LEGEND
        # results may be DSL strings (real nodes) or small synthetic dicts
        for r in result["results"]:
            assert isinstance(r, (str, dict))

    def test_query_graph_dict_mode_default(self):
        result = query_graph(
            pattern="callees_of",
            target=f"{self.main_path}::process",
            repo_root=str(self.root),
        )
        assert "legend" not in result
        for r in result["results"]:
            assert isinstance(r, dict)
