"""Compose node pipeline orchestration.

Separate pipeline stages for clarity:
1. Input extraction & validation
2. Early returns (short-circuits & doc_ask)
3. Context & prompt building
4. LLM execution (structured compose)
5. Post-processing (reflexion & recovery)
6. Result finalization
"""
from __future__ import annotations

import logging
from typing import Any

from app.core.config import settings
from app.graph.compose_node_policy import (
    can_extract_answer_from_web_context,
    should_continue_compose_iteration,
    should_use_fallback_compose,
)
from app.graph.graph_contexts import ComposeContext
from app.graph.compose_helpers import (
    ReflexionComposeOutput,
    _build_context_text,
    _build_web_context,
    _synthesize_web_fallback,
)
from app.graph.compose_types import ComposeInput, ComposeResult
from app.graph.prompt_builders import build_compose_prompt
from app.schemas.graph import ToolResult

logger = logging.getLogger(__name__)


def _dev_log(event: str, **ctx: Any) -> None:
    if not settings.DEV_VERBOSE_LOGGING:
        return
    logger.info(
        f"graph node: {event}",
        extra={"context": {"component": "langgraph", "event": event, **ctx}},
    )


# ============================================================================
# Stage 1: Input Extraction & Validation
# ============================================================================


def extract_compose_input(state: dict) -> ComposeInput:
    """Extract and validate inputs from graph state into ComposeInput dataclass.
    
    This stage converts raw state dict into a strongly-typed ComposeInput
    with all necessary fields and derived properties.
    
    Args:
        state: LangGraph state dict
    
    Returns:
        Validates ComposeInput with all extracted and derived fields
    """
    context = ComposeContext.from_state(state)
    tool_results: list[ToolResult] = context.tool_results

    user_message = context.current_user_message.strip()
    existing_answer = context.final_answer.strip()
    iterations = int(((context.iterations or context.iteration) or 0) + 1)
    max_iterations = int(context.max_iterations or settings.LANGGRAPH_MAX_ITERATIONS)
    all_failed = all(not result.success for result in tool_results) if tool_results else True
    has_integration = any(str(result.tool or "") == "integration_call" for result in tool_results)
    state_context = list(context.state_context)
    
    _dev_log(
        "compose_extract_input",
        user_msg_len=len(user_message),
        iterations=iterations,
        has_answer=bool(existing_answer),
        tool_count=len(tool_results),
    )
    
    return ComposeInput(
        user_message=user_message,
        history=context.history_messages,
        tool_results=tool_results,
        web_fetch_content=context.web_fetch_content,
        web_search_results=context.web_search_results,
        existing_answer=existing_answer,
        iterations=iterations,
        max_iterations=max_iterations,
        all_failed=all_failed,
        has_integration=has_integration,
        state_context=state_context,
    )


# ============================================================================
# Stage 2: Early Returns (Short-circuits & Special Cases)
# ============================================================================


def try_short_circuit(input_data: ComposeInput) -> ComposeResult | None:
    """Check for short-circuit conditions (existing answer, no new data).
    
    If we already have a complete answer and no new tool/web results,
    skip expensive LLM operations and return immediately.
    
    Args:
        input_data: Extracted compose input
    
    Returns:
        ComposeResult if short-circuit triggered, None to continue pipeline
    """
    # Policy decision: Can we use existing answer without LLM?
    if (
        input_data.existing_answer
        and not input_data.has_tool_results
        and not input_data.has_web_context
    ):
        _dev_log("compose_short_circuit_existing_answer")
        return ComposeResult.complete(
            answer=input_data.existing_answer,
            iterations=input_data.iterations,
        )
    
    # Policy decision: Can we extract answer from web context only?
    if input_data.existing_answer and can_extract_answer_from_web_context(input_data.web_fetch_content):
        _dev_log("compose_short_circuit_web_context_only")
        return ComposeResult.complete(
            answer=input_data.existing_answer,
            iterations=input_data.iterations,
        )
    
    return None


async def try_doc_ask_compose(llm_provider: Any, input_data: ComposeInput) -> ComposeResult | None:
    """Check for doc_ask tool result and compose refined answer if present.
    
    Doc_ask is a special case: tool returns rough answer + sources,
    we refine it with LLM and sources citations, then finalize.
    
    Args:
        llm_provider: LLM provider instance
        input_data: Extracted compose input
    
    Returns:
        ComposeResult if doc_ask found and composed, None to continue main flow
    """
    # Import locally to avoid circular dependency
    from app.graph.compose_node import _compose_doc_ask_answer
    
    if not input_data.tool_results:
        return None
    
    doc_ask_result = await _compose_doc_ask_answer(
        llm_provider=llm_provider,
        user_message=input_data.user_message,
        tool_results=input_data.tool_results,
        iterations=input_data.iterations,
    )
    
    if doc_ask_result is not None:
        _dev_log("compose_doc_ask_complete")
        return ComposeResult(
            final_answer=doc_ask_result.get("final_answer", ""),
            is_complete=doc_ask_result.get("is_complete", True),
            feedback_plan=doc_ask_result.get("feedback_plan", ""),
            iterations=doc_ask_result.get("iterations", input_data.iterations),
            iteration=doc_ask_result.get("iteration", input_data.iterations),
        )
    
    return None


# ============================================================================
# Stage 3: Context & Prompt Building
# ============================================================================


def build_compose_context_and_prompt(input_data: ComposeInput) -> tuple[str, str]:
    """Build context text and compose prompt.
    
    Gathers all available context (state, web, tools) and builds
    the reflexion prompt that will guide the compose LLM.
    
    Args:
        input_data: Extracted compose input
    
    Returns:
        Tuple of (context_text, prompt)
    """
    context_text = _build_context_text(
        state_context=input_data.state_context,
        web_fetch_content=input_data.web_fetch_content,
        web_search_results=input_data.web_search_results,
        tool_results=input_data.tool_results,
    )
    
    prompt = build_compose_prompt(
        user_message=input_data.user_message,
        context_text=context_text,
        iterations=input_data.iterations,
        max_iterations=input_data.max_iterations,
        has_integration=input_data.has_integration,
        all_failed=input_data.all_failed,
        has_web_context=input_data.has_web_context,
    )
    
    _dev_log(
        "compose_context_built",
        context_len=len(context_text),
        prompt_len=len(prompt),
    )
    
    return context_text, prompt


def build_compose_messages(
    prompt: str, history: list[dict], user_message: str
) -> list[dict[str, str]]:
    """Build message list for compose LLM call.
    
    Args:
        prompt: System prompt from build_compose_context_and_prompt
        history: Chat history from input_data
        user_message: Current user message
    
    Returns:
        Messages list ready for LLM
    """
    messages: list[dict[str, str]] = [{"role": "system", "content": prompt}]
    messages.extend(history[-settings.CONTEXT_ALWAYS_KEEP_LAST_MESSAGES:])
    messages.append({"role": "user", "content": user_message})
    
    _dev_log("compose_messages_built", message_count=len(messages))
    
    return messages


# ============================================================================
# Stage 4: LLM Execution (Structured Compose)
# ============================================================================


async def run_structured_compose(
    llm_provider: Any,
    messages: list[dict[str, str]],
    _input_data: ComposeInput,
) -> ReflexionComposeOutput | None:
    """Execute structured LLM call for reflexion compose.
    
    Calls the compose LLM with reflexion prompt to determine:
    - Is the answer complete enough?
    - If not, what feedback/next steps?
    
    Args:
        llm_provider: LLM provider instance
        messages: Message list from build_compose_messages
        input_data: Extracted compose input (for config)
    
    Returns:
        Structured output from LLM, or None if execution fails
    """
    try:
        _dev_log("compose_llm_call_start")
        
        output = await llm_provider.chat_structured(
            messages=messages,
            response_model=ReflexionComposeOutput,
            model=settings.LITELLM_PLANNER_MODEL or None,
            temperature=0.0,
            max_tokens=max(
                int(settings.OLLAMA_NUM_PREDICT),
                int(settings.OLLAMA_NUM_PREDICT_PLANNER),
            ),
        )
        
        _dev_log(
            "compose_llm_call_done",
            is_complete=output.is_complete,
            answer_len=len(output.answer or ""),
        )
        
        return output
    
    except Exception as exc:
        _dev_log("compose_llm_call_failed", error=str(exc))
        return None


# ============================================================================
# Stage 5: Post-processing (Reflexion & Recovery)
# ============================================================================


async def process_compose_output(
    llm_provider: Any,
    output: ReflexionComposeOutput | None,
    input_data: ComposeInput,
) -> ComposeResult:
    """Post-process compose LLM output.
    
    Handles:
    - LLM execution failure → recovery fallback
    - Incomplete answer → feedback loop
    - Complete answer → finalization (truncation recovery, etc.)
    
    Args:
        llm_provider: LLM provider instance
        output: Structured output from LLM (or None if failed)
        input_data: Extracted compose input
    
    Returns:
        Final ComposeResult ready for state update
    """
    # Import locally to avoid circular dependency
    from app.graph.compose_node import _finalize_complete_compose, _recover_compose_failure
    
    # Handle LLM failure
    if output is None:
        _dev_log("compose_recovery_fallback_start")

        # Prefer deterministic fallback from collected web context first.
        web_fallback = _synthesize_web_fallback(
            user_message=input_data.user_message,
            web_fetch_content=input_data.web_fetch_content,
            web_search_results=input_data.web_search_results,
        )
        if web_fallback:
            return ComposeResult.complete(
                answer=web_fallback,
                iterations=input_data.iterations,
            )

        if input_data.existing_answer:
            return ComposeResult.complete(
                answer=input_data.existing_answer,
                iterations=input_data.iterations,
            )
        
        # Fallback recovery
        result = await _recover_compose_failure(
            llm_provider=llm_provider,
            exc=Exception("LLM structured compose failed"),
            user_message=input_data.user_message,
            existing_answer=input_data.existing_answer,
            iterations=input_data.iterations,
            web_fetch_content=input_data.web_fetch_content,
            web_search_results=input_data.web_search_results,
            tool_results=input_data.tool_results,
        )
        
        # Convert dict result to ComposeResult
        return ComposeResult(
            final_answer=result.get("final_answer", ""),
            is_complete=result.get("is_complete", True),
            feedback_plan=result.get("feedback_plan", ""),
            iterations=result.get("iterations", input_data.iterations),
            iteration=result.get("iteration", input_data.iterations),
        )
    
    # Policy: Determine if we should continue iterations or finalize
    # apply policy: should_continue_compose_iteration tells us if we have reason to loop again
    should_continue = should_continue_compose_iteration(
        current_iteration=input_data.iterations,
        max_iterations=input_data.max_iterations,
        is_complete=bool(output.is_complete),
        feedback_plan=str(output.feedback_plan or "").strip(),
    )
    
    is_complete = not should_continue
    
    if is_complete:
        _dev_log("compose_finalize_start")
        
        # Finalize with truncation recovery, sanitization, etc.
        result = await _finalize_complete_compose(
            llm_provider=llm_provider,
            output=output,
            existing_answer=input_data.existing_answer,
            iterations=input_data.iterations,
            user_message=input_data.user_message,
            web_fetch_content=input_data.web_fetch_content,
            web_search_results=input_data.web_search_results,
            tool_results=input_data.tool_results,
        )
        
        return ComposeResult(
            final_answer=result.get("final_answer", ""),
            is_complete=result.get("is_complete", True),
            feedback_plan=result.get("feedback_plan", ""),
            iterations=result.get("iterations", input_data.iterations),
            iteration=result.get("iteration", input_data.iterations),
        )
    
    # Incomplete: return feedback plan for next iteration
    _dev_log("compose_incomplete", feedback_len=len(output.feedback_plan or ""))
    
    feedback_plan = str(output.feedback_plan or "").strip()
    if not feedback_plan:
        feedback_plan = (
            "Нужен дополнительный поиск данных: уточнить недостающие факты и источники."
        )
    
    return ComposeResult.incomplete(
        feedback_plan=feedback_plan,
        iterations=input_data.iterations,
    )


# ============================================================================
# Public Entry Point
# ============================================================================


async def run_compose_pipeline(state: dict) -> dict:
    """Execute complete compose pipeline.
    
    Coordinates all stages: input extraction → short-circuits → 
    doc_ask → context/prompt building → LLM execution → post-processing.
    
    Args:
        state: LangGraph state dict
    
    Returns:
        State update dict with compose result
    """
    from app.llm import llm_provider
    
    # Stage 1: Extract input
    input_data = extract_compose_input(state)
    
    # Stage 2a: Try short-circuit
    short_circuit = try_short_circuit(input_data)
    if short_circuit:
        return short_circuit.to_state_update()
    
    # Stage 2b: Try doc_ask special case
    doc_result = await try_doc_ask_compose(llm_provider, input_data)
    if doc_result:
        return doc_result.to_state_update()
    
    # Stage 3: Build context & prompt
    _, prompt = build_compose_context_and_prompt(input_data)
    messages = build_compose_messages(prompt, input_data.history, input_data.user_message)
    
    # Stage 4: Run LLM
    output = await run_structured_compose(llm_provider, messages, input_data)
    
    # Stage 5: Post-process
    result = await process_compose_output(llm_provider, output, input_data)
    
    # Convert to state update
    return result.to_state_update()
