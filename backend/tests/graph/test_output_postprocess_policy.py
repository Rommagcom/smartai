"""Unit tests for output_postprocess_policy module.

Pure policy tests - no mocks, no DB, no services, just assertions.
Tests validate output post-processing decisions.
"""
import pytest

from app.graph.output_postprocess_policy import (
    should_attempt_direct_route,
    should_reenqueue_artifact_export,
    can_recover_from_guardrail_block,
    should_apply_export_success_claim_sanitization,
)


class TestShouldAttemptDirectRoute:
    """Test: should we attempt deterministic tool routing as fallback?"""
    
    def test_skip_if_already_have_calls(self):
        """If we already executed tools, don't try again."""
        assert should_attempt_direct_route(
            user_id="user-123",
            user_message="do something",
            all_calls=[{"tool": "web_search", "success": True}],
        ) is False
    
    def test_skip_without_user_id(self):
        """Need user context for tool execution."""
        assert should_attempt_direct_route(
            user_id=None,
            user_message="do something",
            all_calls=[],
        ) is False
    
    def test_skip_without_message(self):
        """Need message to parse for tool hints."""
        assert should_attempt_direct_route(
            user_id="user-123",
            user_message="",
            all_calls=[],
        ) is False
    
    def test_attempt_when_conditions_met(self):
        """Try direct route when all conditions align."""
        assert should_attempt_direct_route(
            user_id="user-123",
            user_message="create a pdf of this",
            all_calls=[],
        ) is True
    
    def test_empty_calls_list_is_not_calls(self):
        """Empty list means no calls executed."""
        assert should_attempt_direct_route(
            user_id="user-123",
            user_message="test",
            all_calls=[],
        ) is True


class TestShouldRenqueueArtifactExport:
    """Test: should we reenqueue export when it was delayed?"""
    
    def test_need_export_status(self):
        """Can't decide without export status."""
        assert should_reenqueue_artifact_export(
            export_status=None,
            final_answer="some answer",
        ) is False
    
    def test_only_queued_or_delayed_status(self):
        """Only 'queued' or 'delayed' statuses trigger reenqueue."""
        assert should_reenqueue_artifact_export(
            export_status="queued",
            final_answer="some answer",
        ) is True
        
        assert should_reenqueue_artifact_export(
            export_status="delayed",
            final_answer="some answer",
        ) is True
    
    def test_skip_completed_status(self):
        """If export already completed, don't reenqueue."""
        assert should_reenqueue_artifact_export(
            export_status="completed",
            final_answer="some answer",
        ) is False
    
    def test_skip_failed_status(self):
        """If export failed, reenqueue may not help."""
        assert should_reenqueue_artifact_export(
            export_status="failed",
            final_answer="some answer",
        ) is False
    
    def test_need_content_to_export(self):
        """Must have actual answer content."""
        assert should_reenqueue_artifact_export(
            export_status="queued",
            final_answer="",
        ) is False
    
    def test_whitespace_only_is_no_content(self):
        """Whitespace-only answer = no real content."""
        assert should_reenqueue_artifact_export(
            export_status="queued",
            final_answer="   \n  ",
        ) is False
    
    def test_valid_reenqueue_scenario(self):
        """Should reenqueue when all conditions met."""
        assert should_reenqueue_artifact_export(
            export_status="delayed",
            final_answer="Here is the PDF content with lots of data.",
        ) is True


class TestCanRecoverFromGuardrailBlock:
    """Test: can we recover when guardrail blocks the answer?"""
    
    def test_no_recovery_from_security_threat(self):
        """Security threats are hard blocks."""
        assert can_recover_from_guardrail_block(
            guardrail_verdict="security_threat",
            has_fallback_content=True,
        ) is False
    
    def test_no_recovery_from_abuse(self):
        """Abuse is a hard block."""
        assert can_recover_from_guardrail_block(
            guardrail_verdict="abuse",
            has_fallback_content=True,
        ) is False
    
    def test_recovery_depends_on_fallback(self):
        """For other verdicts, recovery needs fallback."""
        assert can_recover_from_guardrail_block(
            guardrail_verdict="mild_concern",
            has_fallback_content=True,
        ) is True
        
        assert can_recover_from_guardrail_block(
            guardrail_verdict="mild_concern",
            has_fallback_content=False,
        ) is False
    
    def test_pass_verdict_depends_on_fallback(self):
        """Pass verdict is not security_threat/abuse, so depends on fallback."""
        assert can_recover_from_guardrail_block(
            guardrail_verdict="pass",
            has_fallback_content=True,
        ) is True


class TestShouldApplyExportSuccessClaimSanitization:
    """Test: does answer falsely claim export success?"""
    
    def test_real_exports_no_sanitization(self):
        """If export actually succeeded, don't sanitize."""
        assert should_apply_export_success_claim_sanitization(
            final_answer="I created a PDF report with all data",
            tool_calls=[
                {"tool": "pdf_create", "success": True},
            ],
        ) is False
    
    def test_excel_success_no_sanitization(self):
        """Real Excel export doesn't need sanitization."""
        assert should_apply_export_success_claim_sanitization(
            final_answer="Generated Excel spreadsheet",
            tool_calls=[
                {"tool": "excel_create", "success": True},
            ],
        ) is False
    
    def test_false_pdf_claim(self):
        """Claim of PDF creation without tool call = false claim."""
        assert should_apply_export_success_claim_sanitization(
            final_answer="I created a PDF report",
            tool_calls=[],
        ) is True
    
    def test_false_excel_claim(self):
        """Claim of Excel generation without tool call = false claim."""
        assert should_apply_export_success_claim_sanitization(
            final_answer="I created an excel file with data",
            tool_calls=[],
        ) is True
    
    def test_russian_false_claims(self):
        """Russian language export claims without actual export."""
        assert should_apply_export_success_claim_sanitization(
            final_answer="Создал PDF документ с информацией",
            tool_calls=[],
        ) is True
    
    def test_no_export_claim_no_sanitization(self):
        """No export claim = no need to sanitize."""
        assert should_apply_export_success_claim_sanitization(
            final_answer="Here is the information you requested",
            tool_calls=[],
        ) is False
    
    def test_failed_export_claim_true(self):
        """Claims export but tool failed = false claim."""
        assert should_apply_export_success_claim_sanitization(
            final_answer="I created the PDF document",
            tool_calls=[
                {"tool": "pdf_create", "success": False},
            ],
        ) is True
    
    def test_multiple_tools_one_successful(self):
        """If at least one export succeeded, no false claim."""
        assert should_apply_export_success_claim_sanitization(
            final_answer="Created PDF and ran web search",
            tool_calls=[
                {"tool": "web_search", "success": True},
                {"tool": "pdf_create", "success": True},
            ],
        ) is False
    
    def test_word_document_false_claim(self):
        """Any document export claim without execution = false."""
        assert should_apply_export_success_claim_sanitization(
            final_answer="Сформировал таблицу в Excel",
            tool_calls=[],
        ) is True
    
    def test_case_insensitive_claim_detection(self):
        """Should detect claims regardless of case."""
        assert should_apply_export_success_claim_sanitization(
            final_answer="CREATED PDF REPORT",
            tool_calls=[],
        ) is True
