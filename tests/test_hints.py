"""Tests for the context-aware hints system."""

from code_review_graph.hints import (
    _MAX_PER_CATEGORY,
    SessionState,
    generate_hints,
    get_session,
    infer_intent,
    reset_session,
)


class TestSessionState:
    def test_fresh_session_exploring(self):
        """A brand-new session with no history should infer 'exploring'."""
        session = SessionState()
        assert infer_intent(session) == "exploring"

    def test_review_intent_detected(self):
        """Recording review-oriented tools should infer 'reviewing'."""
        session = SessionState()
        for tool in ("detect_changes_tool", "get_review_context_tool", "get_affected_flows_tool"):
            session.record_tool_call(tool)
        assert infer_intent(session) == "reviewing"

    def test_debug_intent_detected(self):
        """Recording debug-oriented tools should infer 'debugging'."""
        session = SessionState()
        for tool in ("query_graph_tool", "get_flow_tool", "semantic_search_nodes_tool"):
            session.record_tool_call(tool)
        assert infer_intent(session) == "debugging"

    def test_refactoring_intent_detected(self):
        """Recording refactoring-oriented tools should infer 'refactoring'."""
        session = SessionState()
        for tool in ("refactor_tool", "apply_refactor_tool"):
            session.record_tool_call(tool)
        assert infer_intent(session) == "refactoring"

    def test_large_functions_tool_does_not_force_refactoring_intent(self):
        """find_large_functions_tool is a size query, not a refactor action."""
        session = SessionState()
        session.record_tool_call("find_large_functions_tool")
        assert infer_intent(session) == "exploring"

    def test_session_caps_history(self):
        """tools_called should never exceed 100 entries (FIFO)."""
        session = SessionState()
        for i in range(150):
            session.record_tool_call(f"tool_{i}")
        assert len(session.tools_called) == 100
        # Oldest entries should have been evicted
        assert "tool_0" not in session.tools_called
        assert "tool_149" in session.tools_called

    def test_nodes_capped_at_1000(self):
        """nodes_queried should stop growing at 1000."""
        session = SessionState()
        session.record_nodes([f"node_{i}" for i in range(1200)])
        assert len(session.nodes_queried) == 1000


class TestGenerateHints:
    def test_hints_no_repeat(self):
        """Already-called tools must not appear in next_steps."""
        session = SessionState()
        # Call list_flows_tool, then generate hints for it
        # list_flows_tool suggests get_flow_tool, get_affected_flows_tool, get_architecture_overview_tool
        generate_hints("list_flows_tool", {"status": "ok"}, session)

        # Now call get_flow_tool and regenerate hints for list_flows_tool
        hints2 = generate_hints("list_flows_tool", {"status": "ok"}, session)
        suggested_tools2 = {s["tool"] for s in hints2["next_steps"]}
        # list_flows_tool itself was called, so it should not be suggested by the get_flow_tool workflow
        # Also, the first list_flows_tool call should be excluded from next suggestions
        assert "list_flows_tool" not in suggested_tools2

    def test_hints_max_three(self):
        """Each hints category should have at most 3 entries."""
        session = SessionState()
        # detect_changes_tool has 4 workflow entries
        result = {
            "status": "ok",
            "test_gaps": [{"name": f"gap_{i}"} for i in range(10)],
            "risk_score": 0.9,
            "warnings": ["coupling warning 1", "coupling warning 2"],
        }
        hints = generate_hints("detect_changes_tool", result, session)
        assert len(hints["next_steps"]) <= _MAX_PER_CATEGORY
        assert len(hints["warnings"]) <= _MAX_PER_CATEGORY
        assert len(hints["related"]) <= _MAX_PER_CATEGORY

    def test_warnings_from_result_test_gaps(self):
        """test_gaps in result should produce a warning."""
        session = SessionState()
        result = {
            "status": "ok",
            "test_gaps": [{"name": "untested_func"}, {"name": "another_func"}],
        }
        hints = generate_hints("detect_changes_tool", result, session)
        assert any("Test coverage gaps" in w for w in hints["warnings"])
        assert any("untested_func" in w for w in hints["warnings"])

    def test_warnings_from_result_risk_score(self):
        """High risk_score in result should produce a warning."""
        session = SessionState()
        result = {"status": "ok", "risk_score": 0.85}
        hints = generate_hints("detect_changes_tool", result, session)
        assert any("High risk score" in w for w in hints["warnings"])

    def test_warnings_low_risk_no_warning(self):
        """Low risk_score should NOT produce a warning."""
        session = SessionState()
        result = {"status": "ok", "risk_score": 0.3}
        hints = generate_hints("detect_changes_tool", result, session)
        assert not any("High risk score" in w for w in hints["warnings"])

    def test_generate_hints_empty_result(self):
        """An empty/minimal result should still return valid hints structure."""
        session = SessionState()
        hints = generate_hints("list_flows_tool", {}, session)
        assert "next_steps" in hints
        assert "related" in hints
        assert "warnings" in hints
        assert isinstance(hints["next_steps"], list)
        assert isinstance(hints["related"], list)
        assert isinstance(hints["warnings"], list)

    def test_generate_hints_unknown_tool(self):
        """An unrecognized tool name should still return valid hints."""
        session = SessionState()
        hints = generate_hints("nonexistent_tool", {"status": "ok"}, session)
        assert hints["next_steps"] == []
        assert hints["warnings"] == []

    def test_session_records_files(self):
        """Files from result should be tracked in session state."""
        session = SessionState()
        result = {"status": "ok", "changed_files": ["a.py", "b.py"]}
        generate_hints("detect_changes_tool", result, session)
        assert "a.py" in session.files_touched
        assert "b.py" in session.files_touched

    def test_session_records_nodes(self):
        """Nodes from result should be tracked in session state."""
        session = SessionState()
        result = {
            "status": "ok",
            "results": [
                {"qualified_name": "mod.py::Foo", "name": "Foo"},
                {"qualified_name": "mod.py::Bar", "name": "Bar"},
            ],
        }
        generate_hints("semantic_search_nodes_tool", result, session)
        assert "mod.py::Foo" in session.nodes_queried
        assert "mod.py::Bar" in session.nodes_queried

    def test_related_suggests_untouched_files(self):
        """Related should suggest impacted files not yet touched."""
        session = SessionState()
        session.record_files(["already_seen.py"])
        result = {
            "status": "ok",
            "impacted_files": ["already_seen.py", "new_file.py", "other.py"],
        }
        hints = generate_hints("detect_changes_tool", result, session)
        assert "already_seen.py" not in hints["related"]
        assert "new_file.py" in hints["related"]

    def test_apply_refactor_hints(self):
        """apply_refactor_tool should surface follow-up review actions."""
        session = SessionState()
        result = {
            "status": "ok",
            "applied": True,
            "changed_files": ["a.py"],
        }
        hints = generate_hints("apply_refactor_tool", result, session)
        suggested_tools = {step["tool"] for step in hints["next_steps"]}
        assert "detect_changes_tool" in suggested_tools
        assert "get_affected_flows_tool" in suggested_tools

    def test_refactor_hints_include_related_symbol_search(self):
        """refactor_tool should suggest searching related symbols to rename."""
        session = SessionState()
        result = {
            "status": "ok",
            "previewed": True,
            "changes": [],
        }
        hints = generate_hints("refactor_tool", result, session)
        suggested_tools = {step["tool"] for step in hints["next_steps"]}
        assert "semantic_search_nodes_tool" in suggested_tools


class TestGlobalSession:
    def test_get_session_returns_singleton(self):
        """get_session should return the same object each time."""
        reset_session()
        s1 = get_session()
        s2 = get_session()
        assert s1 is s2

    def test_reset_session_creates_new(self):
        """reset_session should replace the global session."""
        reset_session()
        s1 = get_session()
        s1.record_tool_call("foo")
        reset_session()
        s2 = get_session()
        assert len(s2.tools_called) == 0
        assert s1 is not s2

    def test_warnings_from_arch_overview_dict(self):
        """Architecture warnings as dicts with 'message' key should be extracted."""
        session = SessionState()
        result = {
            "status": "ok",
            "warnings": [
                {"message": "High coupling between A and B"},
                {"message": "Circular dependency detected"},
            ],
        }
        hints = generate_hints("get_architecture_overview_tool", result, session)
        assert any("High coupling" in w for w in hints["warnings"])
        assert any("Circular dependency" in w for w in hints["warnings"])
