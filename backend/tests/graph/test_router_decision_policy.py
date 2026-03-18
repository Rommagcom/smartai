"""Unit tests for router_decision_policy module.

These tests are pure - no mocks, no external dependencies, just assertions.
Tests validate pure routing policy decisions without needing DB, LLM, or HTTP.
"""
import pytest

from app.graph.router_decision_policy import (
    should_skip_router_llm,
    decide_fallback_mode,
    extract_router_decision_intent,
    should_attempt_live_data_web_search,
    build_router_decision_from_salvage,
    should_apply_live_export_override,
)
from app.schemas.graph import RouterDecision


class TestShouldSkipRouterLLM:
    """Test: when should we skip expensive LLM call with fast-paths?"""
    
    def test_skip_when_feedback_plan_present(self):
        """If user has feedback, use it instead of calling LLM."""
        assert should_skip_router_llm(feedback_plan="refine answer", user_message="test") is True
    
    def test_skip_when_empty_message(self):
        """Empty message = no need for LLM."""
        assert should_skip_router_llm(feedback_plan="", user_message="") is True
    
    def test_continue_normal_flow(self):
        """Normal message + no feedback = call LLM."""
        assert should_skip_router_llm(feedback_plan="", user_message="Hello") is False
    
    def test_skip_on_whitespace_feedback(self):
        """Whitespace-only feedback still means skip."""
        assert should_skip_router_llm(feedback_plan="  \n  ", user_message="test") is True


class TestDecideFallbackMode:
    """Test: which fallback mode should we use when router LLM fails?"""
    
    def test_prioritize_salvage_first(self):
        """Salvaged JSON from exception is most reliable."""
        mode = decide_fallback_mode(
            has_salvage=True,
            has_explicit_export=True,
            has_live_export=True,
            is_web_search_intent=True,
        )
        assert mode == "salvaged"
    
    def test_explicit_export_second(self):
        """Without salvage, try explicit export route."""
        mode = decide_fallback_mode(
            has_salvage=False,
            has_explicit_export=True,
            has_live_export=True,
            is_web_search_intent=True,
        )
        assert mode == "explicit_export"
    
    def test_live_export_third(self):
        """Then live export if web data available."""
        mode = decide_fallback_mode(
            has_salvage=False,
            has_explicit_export=False,
            has_live_export=True,
            is_web_search_intent=True,
        )
        assert mode == "live_export"
    
    def test_web_search_fourth(self):
        """Web search is next fallback."""
        mode = decide_fallback_mode(
            has_salvage=False,
            has_explicit_export=False,
            has_live_export=False,
            is_web_search_intent=True,
        )
        assert mode == "web_search"
    
    def test_chat_default_fallback(self):
        """Chat is always available as last resort."""
        mode = decide_fallback_mode(
            has_salvage=False,
            has_explicit_export=False,
            has_live_export=False,
            is_web_search_intent=False,
        )
        assert mode == "chat"


class TestExtractRouterDecisionIntent:
    """Test: can we extract RouterDecision from malformed JSON in exception?"""
    
    def test_extract_tool_decision(self):
        """Extract 'tool' decision from exception JSON."""
        exc_text = 'Failed to parse: {"decision": "tool", "steps": [...]}'
        decision = extract_router_decision_intent(exc_text)
        assert decision == RouterDecision.TOOL
    
    def test_extract_chat_decision(self):
        """Extract 'chat' decision."""
        exc_text = 'Error: {"decision": "chat", "confidence": 0.8}'
        decision = extract_router_decision_intent(exc_text)
        assert decision == RouterDecision.CHAT
    
    def test_extract_memory_decision(self):
        """Extract 'memory' decision."""
        exc_text = '"decision": "memory"'
        decision = extract_router_decision_intent(exc_text)
        assert decision == RouterDecision.MEMORY
    
    def test_extract_clarify_decision(self):
        """Extract 'clarify' decision."""
        exc_text = 'JSON: {"decision": "clarify"}'
        decision = extract_router_decision_intent(exc_text)
        assert decision == RouterDecision.CLARIFY
    
    def test_extract_web_search_decision(self):
        """Extract 'web_search' decision."""
        exc_text = '"decision": "web_search"'
        decision = extract_router_decision_intent(exc_text)
        assert decision == RouterDecision.WEB_SEARCH
    
    def test_no_decision_in_text_returns_none(self):
        """If no decision field found, return None."""
        exc_text = 'Some error without decision field'
        decision = extract_router_decision_intent(exc_text)
        assert decision is None
    
    def test_case_insensitive_extraction(self):
        """Decision extraction should be case-insensitive."""
        exc_text = '{"DECISION": "TOOL"}'
        decision = extract_router_decision_intent(exc_text)
        assert decision == RouterDecision.TOOL
    
    def test_invalid_decision_value_returns_none(self):
        """Unknown decision value should return None."""
        exc_text = '{"decision": "invalid_decision"}'
        decision = extract_router_decision_intent(exc_text)
        assert decision is None


class TestShouldAttemptLiveDataWebSearch:
    """Test: should we use live-data web search shortcut?"""
    
    def test_require_user_id(self):
        """Need user ID for live data."""
        assert should_attempt_live_data_web_search(
            user_id=None,
            integrations_block="",
            dynamic_tools_block="",
            is_live_data_query=True,
        ) is False
    
    def test_fail_with_integrations(self):
        """Can't use live data if integrations already available."""
        assert should_attempt_live_data_web_search(
            user_id="user-123",
            integrations_block="Some integrations",
            dynamic_tools_block="",
            is_live_data_query=True,
        ) is False
    
    def test_fail_with_dynamic_tools(self):
        """Can't use live data if tools already loaded."""
        assert should_attempt_live_data_web_search(
            user_id="user-123",
            integrations_block="",
            dynamic_tools_block="Tool list",
            is_live_data_query=True,
        ) is False
    
    def test_fail_if_not_live_data_query(self):
        """Need explicit live data intent."""
        assert should_attempt_live_data_web_search(
            user_id="user-123",
            integrations_block="",
            dynamic_tools_block="",
            is_live_data_query=False,
        ) is False
    
    def test_success_all_conditions_met(self):
        """Live data shortcut when all conditions met."""
        assert should_attempt_live_data_web_search(
            user_id="user-123",
            integrations_block="",
            dynamic_tools_block="",
            is_live_data_query=True,
        ) is True


class TestBuildRouterDecisionFromSalvage:
    """Test: what to do with partially salvaged router decision?"""
    
    def test_reject_tool_without_steps(self):
        """Can't safely execute tool calls without steps."""
        result = build_router_decision_from_salvage(
            extracted_decision=RouterDecision.TOOL,
            has_tool_call=False,
        )
        assert result is None
    
    def test_reject_tool_even_with_steps(self):
        """Tool salvage intentionally skipped (separate logic for PDF/Excel only)."""
        result = build_router_decision_from_salvage(
            extracted_decision=RouterDecision.TOOL,
            has_tool_call=True,
        )
        assert result is None
    
    def test_salvage_memory_decision(self):
        """MEMORY decision can be salvaged."""
        result = build_router_decision_from_salvage(
            extracted_decision=RouterDecision.MEMORY,
            has_tool_call=False,
        )
        assert result is not None
        assert result.decision == RouterDecision.MEMORY
        assert result.confidence == 0.4
    
    def test_salvage_clarify_decision(self):
        """CLARIFY decision can be salvaged."""
        result = build_router_decision_from_salvage(
            extracted_decision=RouterDecision.CLARIFY,
            has_tool_call=False,
        )
        assert result is not None
        assert result.decision == RouterDecision.CLARIFY
        assert result.confidence == 0.4
    
    def test_default_to_chat(self):
        """Unknown or WEB_SEARCH defaults to CHAT."""
        result = build_router_decision_from_salvage(
            extracted_decision=RouterDecision.WEB_SEARCH,
            has_tool_call=False,
        )
        assert result is not None
        assert result.decision == RouterDecision.CHAT
        assert result.confidence == 0.4


class TestShouldApplyLiveExportOverride:
    """Test: should we override router decision with live-export?"""
    
    def test_override_only_tool_decisions(self):
        """Only TOOL decisions get overridden."""
        assert should_apply_live_export_override(
            router_decision=RouterDecision.TOOL,
            is_live_data_query=True,
        ) is True
    
    def test_dont_override_chat_decision(self):
        """Don't override chat decisions."""
        assert should_apply_live_export_override(
            router_decision=RouterDecision.CHAT,
            is_live_data_query=True,
        ) is False
    
    def test_require_live_data_intent(self):
        """Need explicit live data query intent."""
        assert should_apply_live_export_override(
            router_decision=RouterDecision.TOOL,
            is_live_data_query=False,
        ) is False
    
    def test_both_conditions_needed(self):
        """Both TOOL decision and live data intent required."""
        assert should_apply_live_export_override(
            router_decision=RouterDecision.TOOL,
            is_live_data_query=True,
        ) is True
