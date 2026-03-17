"""Output node pipeline orchestration.

Explicit 6-stage pipeline replacing inline output_node logic:
1. extract_output_input() - Parse state into typed container
2. setup_export_context() - Determine export kind and reenqueue flag
3. enqueue_and_merge_export() - Enqueue export if needed, merge with existing
4. apply_bridges_and_status() - Apply output bridges (direct, cron, integration)
5. sanitize_claim_and_recover() - Sanitize claims, recover truncated web answers
6. finalize_and_persist() - Final sanitization, memory operations, create result

Each stage is independently testable with clear contracts.
"""
from typing import Any

from app.core.config import settings
from app.graph.artifact_utils import extract_artifacts
from app.graph.graph_contexts import OutputContext
from app.graph.output_policy import (
    requested_export_kind,
    sanitize_false_attachment_claims,
)
from app.graph.output_postprocess import (
    apply_output_guardrail,
    enqueue_export_if_needed,
)
from app.graph.output_helpers import (
    _dev_log,
    append_export_status,
    append_memory_and_schedule_ltm,
    apply_output_bridges,
    determine_export_reenqueue,
    recover_web_answer_if_needed,
    sanitize_answer_final,
)
from app.graph.output_types import OutputInput, OutputResult
from app.schemas.graph import GuardrailVerdict


def extract_output_input(state: dict) -> OutputInput:
    """Stage 1: Extract state variables into typed input container.
    
    Args:
        state: LangGraph state dict
    
    Returns:
        OutputInput with all needed variables
    """
    context = OutputContext.from_state(state)
    return OutputInput(
        final_answer=context.final_answer,
        user_message=context.user_message,
        user_id=context.user_id,
        web_fetch_content=context.web_fetch_content,
        web_search_results=context.web_search_results,
        tool_results=context.tool_results,
        existing_calls=context.tool_calls_log,
        existing_artifacts=context.artifacts,
    )


def setup_export_context(
    input_data: OutputInput,
) -> tuple[str | None, bool]:
    """Stage 2: Determine export kind and whether to reenqueue.
    
    Checks user message for export request and evaluates previous export attempts.
    
    Args:
        input_data: Output input container
    
    Returns:
        Tuple of (export_kind, should_reenqueue)
    """
    export_kind = requested_export_kind(input_data.user_message)
    should_reenqueue = determine_export_reenqueue(
        export_kind=export_kind,
        existing_calls=input_data.existing_calls,
        existing_artifacts=input_data.existing_artifacts,
    )
    return export_kind, should_reenqueue


async def enqueue_and_merge_export(
    *,
    input_data: OutputInput,
    export_kind: str | None,
    should_reenqueue: bool,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Stage 3: Enqueue export if needed, merge with existing calls/artifacts.
    
    Args:
        input_data: Output input container
        export_kind: Type of export requested
        should_reenqueue: Whether to reenqueue failed export
    
    Returns:
        Tuple of (all_calls, all_artifacts, extra_calls)
    """
    extra_calls = await enqueue_export_if_needed(  # type: ignore
        user_id=input_data.user_id,
        final_answer=input_data.final_answer,
        export_kind=export_kind,
        should_reenqueue=should_reenqueue,
    )
    
    all_calls = [*input_data.existing_calls, *extra_calls]
    all_artifacts = [*input_data.existing_artifacts, *extract_artifacts(extra_calls)]
    
    return all_calls, all_artifacts, extra_calls


async def apply_bridges_and_status(
    *,
    input_data: OutputInput,
    final_answer: str,
    all_calls: list[dict],
    all_artifacts: list[dict],
    extra_calls: list[dict],
    export_kind: str | None,
) -> tuple[str, list[dict], list[dict]]:
    """Stage 4: Apply output bridges and append export status message.
    
    Bridges:
    - Direct route fallback: Fallback for specific query types
    - Inline cron bridge: Cron job result injection
    - Inline integration bridge: Integration result injection
    
    Args:
        input_data: Output input container
        final_answer: Current answer text
        all_calls: Current tool calls log
        all_artifacts: Current artifacts
        extra_calls: New export calls
        export_kind: Type of export
    
    Returns:
        Tuple of (final_answer, all_calls, all_artifacts) after bridges
    """
    final_answer, all_calls, all_artifacts = await apply_output_bridges(
        user_id=input_data.user_id,
        user_message=input_data.user_message,
        final_answer=final_answer,
        all_calls=all_calls,
        all_artifacts=all_artifacts,
    )
    
    final_answer = append_export_status(
        final_answer,
        extra_calls=extra_calls,
        export_kind=export_kind,
    )
    
    return final_answer, all_calls, all_artifacts


async def sanitize_claim_and_recover(
    *,
    llm_provider: Any,
    input_data: OutputInput,
    final_answer: str,
    all_calls: list[dict],
    all_artifacts: list[dict],
) -> str:
    """Stage 5: Sanitize false attachment claims and recover web answers.
    
    Two sub-operations:
    1. sanitize_false_attachment_claims: Remove claims about non-existent attachments
    2. recover_web_answer_if_needed: Recover truncated web fetch responses
    
    Args:
        llm_provider: LLM provider instance
        input_data: Output input container
        final_answer: Current answer text
        all_calls: Tool calls log
        all_artifacts: Artifacts log
    
    Returns:
        Updated final answer
    """
    final_answer = sanitize_false_attachment_claims(
        answer=final_answer,
        tool_calls=all_calls,
        artifacts=all_artifacts,
    )
    
    final_answer = await recover_web_answer_if_needed(
        llm_provider=llm_provider,
        final_answer=final_answer,
        user_message=input_data.user_message,
        web_fetch_content=input_data.web_fetch_content,
        web_search_results=input_data.web_search_results,
        tool_results=input_data.tool_results,
    )
    
    return final_answer


async def finalize_and_persist(
    *,
    input_data: OutputInput,
    final_answer: str,
    output_guardrail: tuple[bool, str],
    all_calls: list[dict],
    all_artifacts: list[dict],
) -> OutputResult:
    """Stage 6: Final sanitization, memory append, create result.
    
    Sub-operations:
    1. Final LLM answer sanitization (guardrail + cleanup already applied in earlier stage)
    2. Append to STM and schedule LTM extraction
    3. Create OutputResult for return
    
    Args:
        input_data: Output input container
        final_answer: Current answer text
        output_guardrail: Guardrail check result tuple
        all_calls: Tool calls log
        all_artifacts: Artifacts log
    
    Returns:
        OutputResult ready to be converted to state update
    """
    # Memory operations (fire and forget for LTM)
    await append_memory_and_schedule_ltm(
        user_id=input_data.user_id,
        user_message=input_data.user_message,
        final_answer=final_answer,
    )
    
    return OutputResult(
        final_answer=final_answer,
        output_guardrail=output_guardrail,
        tool_calls_log=all_calls,
        artifacts=all_artifacts,
    )


async def run_output_pipeline(state: dict) -> dict:
    """Main pipeline entry point.
    
    Orchestrates 6-stage pipeline:
    1. Extract typed input from state
    2. Setup export kind and reenqueue flag
    3. Enqueue export and merge with existing
    4. Apply output bridges and append export status
    5. Sanitize false claims and recover web answers
    6. Finalize and persist to memory
    
    Args:
        state: LangGraph state dict
    
    Returns:
        State update dict with final results
    """
    from app.llm import llm_provider
    
    _dev_log("output_pipeline_start")
    
    # Stage 1: Extract typed input
    input_data = extract_output_input(state)
    _dev_log(
        "output_extracted_input",
        has_answer=bool(input_data.final_answer),
        has_web=input_data.has_web_context,
    )
    
    # Apply guardrail at beginning so later stages can use guardrailed answer
    final_answer, guardrail_result = apply_output_guardrail(input_data.final_answer)
    _dev_log(
        "output_guardrail_applied",
        guardrail_passed=guardrail_result.verdict != GuardrailVerdict.BLOCK,
    )
    
    # Stage 2: Setup export context
    export_kind, should_reenqueue = setup_export_context(input_data)
    _dev_log(
        "output_export_context_setup",
        export_kind=export_kind,
        should_reenqueue=should_reenqueue,
    )
    
    # Stage 3: Enqueue and merge
    all_calls, all_artifacts, extra_calls = await enqueue_and_merge_export(
        input_data=input_data,
        export_kind=export_kind,
        should_reenqueue=should_reenqueue,
    )
    _dev_log("output_export_enqueued", extra_calls_count=len(extra_calls))
    
    # Stage 4: Apply bridges and status  
    final_answer, all_calls, all_artifacts = await apply_bridges_and_status(
        input_data=input_data,
        final_answer=final_answer,
        all_calls=all_calls,
        all_artifacts=all_artifacts,
        extra_calls=extra_calls,
        export_kind=export_kind,
    )
    _dev_log("output_bridges_applied", answer_len=len(str(final_answer or "")))
    
    # Stage 5: Sanitize claims and recover web
    final_answer = await sanitize_claim_and_recover(
        llm_provider=llm_provider,
        input_data=input_data,
        final_answer=final_answer,
        all_calls=all_calls,
        all_artifacts=all_artifacts,
    )
    _dev_log("output_claim_sanitized_and_recovered", answer_len=len(str(final_answer or "")))
    
    # Apply final LLM sanitization (guardrail already applied, this is cleanup)
    sanitized_final, _ = sanitize_answer_final(final_answer)
    if sanitized_final != final_answer:
        _dev_log(
            "output_final_answer_sanitized",
            before_len=len(str(final_answer or "")),
            after_len=len(str(sanitized_final or "")),
        )
    final_answer = sanitized_final
    
    # Stage 6: Finalize and persist
    result = await finalize_and_persist(
        input_data=input_data,
        final_answer=final_answer,
        output_guardrail=guardrail_result,
        all_calls=all_calls,
        all_artifacts=all_artifacts,
    )
    _dev_log("output_pipeline_complete")
    
    return result.to_state_update()
