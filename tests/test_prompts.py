"""Tests for MCP prompt templates."""

from fastmcp.prompts.prompt import Message

from code_review_graph.prompts import (
    architecture_map_prompt,
    debug_issue_prompt,
    onboard_developer_prompt,
    pre_merge_check_prompt,
    review_changes_prompt,
)


def _text(msg: Message) -> str:
    """Extract the text content from a fastmcp Message."""
    return msg.content.text


class TestReviewChangesPrompt:
    def test_returns_list_with_messages(self):
        result = review_changes_prompt()
        assert isinstance(result, list)
        assert len(result) >= 1

    def test_message_has_role_and_content(self):
        result = review_changes_prompt()
        for msg in result:
            assert isinstance(msg, Message)
            assert msg.role == "user"
            assert _text(msg)

    def test_default_base(self):
        result = review_changes_prompt()
        assert "HEAD~1" in _text(result[0])

    def test_custom_base(self):
        result = review_changes_prompt(base="main")
        assert "main" in _text(result[0])

    def test_mentions_detect_changes(self):
        result = review_changes_prompt()
        assert "detect_changes" in _text(result[0])

    def test_mentions_affected_flows(self):
        result = review_changes_prompt()
        assert "affected_flows" in _text(result[0])

    def test_mentions_test_gaps(self):
        result = review_changes_prompt()
        assert "test" in _text(result[0]).lower()


class TestArchitectureMapPrompt:
    def test_returns_list_with_messages(self):
        result = architecture_map_prompt()
        assert isinstance(result, list)
        assert len(result) >= 1

    def test_message_has_role_and_content(self):
        result = architecture_map_prompt()
        for msg in result:
            assert isinstance(msg, Message)
            assert msg.role == "user"
            assert _text(msg)

    def test_mentions_communities(self):
        result = architecture_map_prompt()
        assert "communities" in _text(result[0]).lower()

    def test_mentions_mermaid(self):
        result = architecture_map_prompt()
        assert "Mermaid" in _text(result[0])


class TestDebugIssuePrompt:
    def test_returns_list_with_messages(self):
        result = debug_issue_prompt()
        assert isinstance(result, list)
        assert len(result) >= 1

    def test_message_has_role_and_content(self):
        result = debug_issue_prompt()
        for msg in result:
            assert isinstance(msg, Message)
            assert msg.role == "user"
            assert _text(msg)

    def test_includes_description(self):
        result = debug_issue_prompt(description="login fails with 500 error")
        assert "login fails with 500 error" in _text(result[0])

    def test_empty_description(self):
        result = debug_issue_prompt()
        content = _text(result[0])
        assert "debug" in content.lower()

    def test_mentions_search(self):
        result = debug_issue_prompt(description="test issue")
        assert "semantic_search_nodes" in _text(result[0])

    def test_mentions_get_minimal_context(self):
        result = debug_issue_prompt()
        assert "get_minimal_context" in _text(result[0])


class TestOnboardDeveloperPrompt:
    def test_returns_list_with_messages(self):
        result = onboard_developer_prompt()
        assert isinstance(result, list)
        assert len(result) >= 1

    def test_message_has_role_and_content(self):
        result = onboard_developer_prompt()
        for msg in result:
            assert isinstance(msg, Message)
            assert msg.role == "user"
            assert _text(msg)

    def test_mentions_stats(self):
        result = onboard_developer_prompt()
        assert "list_graph_stats" in _text(result[0])

    def test_mentions_architecture(self):
        result = onboard_developer_prompt()
        assert "architecture" in _text(result[0]).lower()

    def test_mentions_critical_flows(self):
        result = onboard_developer_prompt()
        assert "critical" in _text(result[0]).lower()


class TestPreMergeCheckPrompt:
    def test_returns_list_with_messages(self):
        result = pre_merge_check_prompt()
        assert isinstance(result, list)
        assert len(result) >= 1

    def test_message_has_role_and_content(self):
        result = pre_merge_check_prompt()
        for msg in result:
            assert isinstance(msg, Message)
            assert msg.role == "user"
            assert _text(msg)

    def test_default_base(self):
        result = pre_merge_check_prompt()
        # The pre-merge prompt is now generic (doesn't embed the base ref)
        assert "pre-merge" in _text(result[0]).lower()

    def test_custom_base(self):
        # pre_merge_check_prompt still accepts base but the workflow
        # is now generic — just verify it returns valid prompt
        result = pre_merge_check_prompt(base="develop")
        assert isinstance(result, list)
        assert len(result) >= 1

    def test_mentions_risk_scoring(self):
        result = pre_merge_check_prompt()
        assert "risk" in _text(result[0]).lower()

    def test_mentions_test_gaps(self):
        result = pre_merge_check_prompt()
        assert "tests_for" in _text(result[0])

    def test_mentions_dead_code(self):
        result = pre_merge_check_prompt()
        assert "dead_code" in _text(result[0])


class TestTokenEfficiencyPreamble:
    """All prompts should include the token efficiency preamble."""

    def test_review_has_preamble(self):
        result = review_changes_prompt()
        assert "get_minimal_context" in _text(result[0])
        assert "detail_level" in _text(result[0])

    def test_architecture_has_preamble(self):
        result = architecture_map_prompt()
        assert "get_minimal_context" in _text(result[0])

    def test_debug_has_preamble(self):
        result = debug_issue_prompt()
        assert "get_minimal_context" in _text(result[0])

    def test_onboard_has_preamble(self):
        result = onboard_developer_prompt()
        assert "get_minimal_context" in _text(result[0])

    def test_pre_merge_has_preamble(self):
        result = pre_merge_check_prompt()
        assert "get_minimal_context" in _text(result[0])
