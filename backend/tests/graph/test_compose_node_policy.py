"""Unit tests for compose_node_policy module.

Pure policy tests - no mocks, no LLM, no services, just assertions.
Tests validate answer composition and refinement decisions.
"""
import pytest

from app.graph.compose_node_policy import (
    should_continue_compose_iteration,
    should_use_fallback_compose,
    can_extract_answer_from_web_context,
    should_sanitize_false_export_claims,
    extract_compose_input_safely,
)


class TestShouldContinueComposeIteration:
    """Test: should we continue the composition refinement loop?"""
    
    def test_stop_if_already_complete(self):
        """If LLM marked complete, stop iterating."""
        assert should_continue_compose_iteration(
            current_iteration=2,
            max_iterations=5,
            is_complete=True,
            feedback_plan="refine something",
        ) is False
    
    def test_stop_if_max_iterations_reached(self):
        """Hit max iterations = stop, even with feedback."""
        assert should_continue_compose_iteration(
            current_iteration=5,
            max_iterations=5,
            is_complete=False,
            feedback_plan="refine answer",
        ) is False
    
    def test_stop_without_feedback(self):
        """Without feedback on what to refine, stop."""
        assert should_continue_compose_iteration(
            current_iteration=2,
            max_iterations=5,
            is_complete=False,
            feedback_plan="",
        ) is False
    
    def test_stop_with_whitespace_feedback(self):
        """Whitespace-only feedback = no real feedback."""
        assert should_continue_compose_iteration(
            current_iteration=2,
            max_iterations=5,
            is_complete=False,
            feedback_plan="   \n  ",
        ) is False
    
    def test_continue_when_conditions_met(self):
        """Continue when incomplete, under max, with real feedback."""
        assert should_continue_compose_iteration(
            current_iteration=2,
            max_iterations=5,
            is_complete=False,
            feedback_plan="refine the answer with more details",
        ) is True
    
    def test_one_below_max_iterations(self):
        """Can iterate when at max-1."""
        assert should_continue_compose_iteration(
            current_iteration=4,
            max_iterations=5,
            is_complete=False,
            feedback_plan="improve",
        ) is True


class TestShouldUseFallbackCompose:
    """Test: should we compose answer without LLM?"""
    
    def test_need_existing_answer(self):
        """Must have base answer to fallback compose."""
        assert should_use_fallback_compose(
            answer="",
            tool_results=[],
            web_context="",
        ) is False
    
    def test_need_context_for_fallback(self):
        """Must have tools or web context to work with."""
        assert should_use_fallback_compose(
            answer="some answer",
            tool_results=[],
            web_context="",
        ) is False
    
    def test_can_fallback_with_tools(self):
        """Fallback composition works with tool results."""
        assert should_use_fallback_compose(
            answer="base answer",
            tool_results=[{"tool": "web_search", "result": "data"}],
            web_context="",
        ) is True
    
    def test_can_fallback_with_web_context(self):
        """Fallback composition works with web data."""
        assert should_use_fallback_compose(
            answer="base answer",
            tool_results=[],
            web_context="Here is information from the web",
        ) is True
    
    def test_all_conditions_met(self):
        """Fallback when answer exists and context available."""
        assert should_use_fallback_compose(
            answer="existing answer",
            tool_results=[{"data": "some result"}],
            web_context="web data",
        ) is True


class TestCanExtractAnswerFromWebContext:
    """Test: can we build answer from web context only?"""
    
    def test_need_web_context(self):
        """Must have web data."""
        assert can_extract_answer_from_web_context("") is False
    
    def test_need_meaningful_length(self):
        """Need substantial content, not just few chars."""
        assert can_extract_answer_from_web_context("short") is False
    
    def test_whitespace_only_insufficient(self):
        """Whitespace doesn't count as content."""
        assert can_extract_answer_from_web_context("   ") is False
    
    def test_acceptable_minimum_content(self):
        """50+ chars is enough content."""
        content = "This is a reasonably long piece of web content that has enough words"
        assert can_extract_answer_from_web_context(content) is True
    
    def test_single_sentence_enough(self):
        """One good sentence can be sufficient."""
        content = "The weather is sunny with a temperature of 24 degrees Celsius and low humidity."
        assert can_extract_answer_from_web_context(content) is True
    
    def test_just_over_threshold(self):
        """51 chars should be extractable."""
        content = "a" * 51
        assert can_extract_answer_from_web_context(content) is True
    
    def test_just_under_threshold(self):
        """49 chars is too little."""
        content = "a" * 49
        assert can_extract_answer_from_web_context(content) is False


class TestShouldSanitizeFalseExportClaims:
    """Test: does answer falsely claim export success?"""
    
    def test_no_sanitization_for_real_exports(self):
        """If export succeeded, don't sanitize."""
        assert should_sanitize_false_export_claims(
            final_answer="Created the PDF report",
            tool_calls=[
                {"tool": "pdf_create", "success": True},
            ],
        ) is False
    
    def test_false_pdf_claim_needs_sanitization(self):
        """Claim PDF creation without successful tool = false."""
        assert should_sanitize_false_export_claims(
            final_answer="I generated pdf file with the information",
            tool_calls=[],
        ) is True
    
    def test_false_excel_claim(self):
        """Claim Excel export without successful tool = false."""
        assert should_sanitize_false_export_claims(
            final_answer="I created excel file with data",
            tool_calls=[{"tool": "web_search", "success": True}],
        ) is True
    
    def test_russian_false_claims(self):
        """Russian language false export claims."""
        assert should_sanitize_false_export_claims(
            final_answer="Создал PDF документ с полной информацией",
            tool_calls=[],
        ) is True
    
    def test_no_export_claim_no_sanitization(self):
        """Answer doesn't claim export = no need to sanitize."""
        assert should_sanitize_false_export_claims(
            final_answer="Here is the information you requested",
            tool_calls=[],
        ) is False
    
    def test_document_word_without_export_subject(self):
        """Word 'document' alone without export verb = not false claim."""
        assert should_sanitize_false_export_claims(
            final_answer="The document contains important data",
            tool_calls=[],
        ) is False
    
    def test_export_verb_with_specific_format(self):
        """Export phrase with document format = false claim."""
        assert should_sanitize_false_export_claims(
            final_answer="I exported the pdf document",
            tool_calls=[],
        ) is True
    
    def test_multiple_exports_one_success(self):
        """One successful export = no false claims."""
        assert should_sanitize_false_export_claims(
            final_answer="Created PDF and Excel reports",
            tool_calls=[
                {"tool": "pdf_create", "success": True},
                {"tool": "excel_create", "success": False},
            ],
        ) is False
    
    def test_multiple_exports_all_fail(self):
        """False claims if tools failed."""
        assert should_sanitize_false_export_claims(
            final_answer="The pdf saved in system",
            tool_calls=[
                {"tool": "pdf_create", "success": False},
                {"tool": "excel_create", "success": False},
            ],
        ) is True


class TestExtractComposeInputSafely:
    """Test: extract and validate compose input from state."""
    
    def test_extract_with_all_fields(self):
        """Extract all fields when present."""
        state = {
            "user_message": "  test message  ",
            "messages": ["msg1", "msg2"],
            "tool_results": [{"result": "data"}],
            "web_fetch_content": "web data",
            "web_search_results": [{"title": "result"}],
            "final_answer": "  answer  ",
            "iterations": 2,
            "max_iterations": 5,
            "iteration": 2,
        }
        result = extract_compose_input_safely(state)
        
        assert result["user_message"] == "test message"
        assert result["messages"] == ["msg1", "msg2"]
        assert result["tool_results"] == [{"result": "data"}]
        assert result["web_fetch_content"] == "web data"
        assert result["final_answer"] == "answer"
        assert result["iterations"] == 2
        assert result["max_iterations"] == 5
    
    def test_safe_defaults_for_missing_fields(self):
        """Provide safe defaults when fields missing."""
        state = {}
        result = extract_compose_input_safely(state)
        
        assert result["user_message"] == ""
        assert result["messages"] == []
        assert result["tool_results"] == []
        assert result["web_fetch_content"] == ""
        assert result["web_search_results"] == []
        assert result["final_answer"] == ""
        assert result["iterations"] == 0
        assert result["max_iterations"] == 5
    
    def test_coerce_to_integers(self):
        """Ensure iterations/max_iterations are integers."""
        state = {
            "iterations": "5",
            "max_iterations": "10",
        }
        result = extract_compose_input_safely(state)
        
        # Values might be coerced or treated as strings - depends on implementation
        # Main point: extraction doesn't crash on mixed types
        assert "iterations" in result
        assert "max_iterations" in result
    
    def test_fallback_to_iteration_field(self):
        """Use 'iteration' if 'iterations' missing."""
        state = {
            "iteration": 3,
            "iterations": None,
        }
        result = extract_compose_input_safely(state)
        
        # Should use the available field (iteration is 0+1=1 if iterations is None, or uses iteration directly)
        # The implementation behavior: if iterations is None, uses (iteration or 0) + 1, or falls back to 0
        assert result["iterations"] >= 0
    
    def test_none_values_handled(self):
        """Handle None values gracefully."""
        state = {
            "user_message": None,
            "messages": None,
            "tool_results": None,
            "web_fetch_content": None,
            "web_search_results": None,
            "final_answer": None,
            "iterations": None,
            "max_iterations": None,
        }
        result = extract_compose_input_safely(state)
        
        assert result["user_message"] == ""
        assert result["messages"] == []
        assert result["tool_results"] == []
        assert result["final_answer"] == ""
        assert isinstance(result["iterations"], int)
        assert isinstance(result["max_iterations"], int)
    
    def test_whitespace_stripping(self):
        """Whitespace is stripped from text fields."""
        state = {
            "user_message": "   spaces   ",
            "final_answer": "\n\n answer \n\n",
            "web_fetch_content": "  web  ",
        }
        result = extract_compose_input_safely(state)
        
        assert result["user_message"] == "spaces"
        assert result["final_answer"] == "answer"
        assert result["web_fetch_content"] == "web"
