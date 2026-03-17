# Policy vs Side Effects Architecture

## Overview

To keep the codebase maintainable and testable, we separate **pure policy functions** from **side effect functions**:

### Pure Policy Functions
- **Can be tested without mocks**: Input → Logic → Output
- **No dependencies**: Cannot call LLM, DB, HTTP, external services
- **Deterministic**: Same input always produces same output
- **Live in**: `*_policy.py` files

### Side Effect Functions  
- **Need mocks for testing**: Orchestrate between policy and services
- **Have dependencies**: Call LLM, DB, HTTP, other services
- **Non-deterministic**: Outcomes depend on external systems
- **Live in**: Node implementations (`*_node.py`, `*_postprocess.py`)

## Current Policy Layers

### 1. Router Decision Policy (`router_decision_policy.py`)
**Pure decisions** (no LLM calls):
- `should_skip_router_llm()` — Can we use fast-paths?
- `decide_fallback_mode()` — Which fallback to use when LLM fails?
- `should_attempt_live_data_web_search()` — Live data shortcut possible?
- `extract_router_decision_intent()` — Parse decision from exception text

**Side effect orchestration** (`router_node.py`):
- Tries fast-paths (deterministic, hard_route, etc)
- If needed, calls `llm_provider.chat_structured()`
- On LLM failure, applies fallback policy decisions
- Returns `RouterNodeUpdate` with typed next step

### 2. Compose Iteration Policy (`compose_node_policy.py`)
**Pure decisions** (no LLM calls):
- `should_continue_compose_iteration()` — Continue refining?
- `should_use_fallback_compose()` — Skip LLM fallback?
- `can_extract_answer_from_web_context()` — Direct text composition possible?
- `should_sanitize_false_export_claims()` — False success claim detected?

**Side effect orchestration** (`compose_node.py`):
- Orchestrates iteration loop
- Calls `llm_provider.chat_structured()` for refinement
- Applies fallback when LLM unavailable
- Returns `ComposeNodeUpdate` with final answer

### 3. Output Postprocess Policy (`output_postprocess_policy.py`)
**Pure decisions** (no DB, service, or LLM calls):
- `should_attempt_direct_route()` — Deterministic fallback possible?
- `should_reenqueue_artifact_export()` — Retry export queuing?
- `can_recover_from_guardrail_block()` — Answer recoverable?
- `should_apply_export_success_claim_sanitization()` — Fix false claims?

**Side effect orchestration** (`output_postprocess.py`):
- Loads user from DB if needed
- Executes tool chains via `tool_orchestrator_service`
- Commits DB transactions
- Applies guardrails and post-processing

### 4. Routing Policy (`routing_policy.py`)
**Mostly pure** (text analysis, pattern matching):
- `is_web_search_intent()` — User asking for web search?
- `is_live_data_query()` — Live data pattern detected?
- `deterministic_route()` — Try non-LLM routing patterns
- `fallback_explicit_export_route()` — Export confirmation detected?

**Note**: `deterministic_route()` imports `ChatService` internally
- This is necessary coupling for tool step extraction
- Imports ChatService only, no other services
- Static method call — pure logic with logging side effect


## Design Principles

1. **Policy functions go in `*_policy.py`**
   - Contains logic, no imports of services/DB/LLM
   - Testable with simple assert, no mocking needed

2. **Side effect functions wrap policy**
   - Call policy to decide what to do
   - Execute the decision (LLM, DB, service calls)
   - Return typed results

3. **Avoid coupling in policy**
   - No `from app.services.X import Y` inside policy functions
   - Extract logic to parameters if needed
   - Document necessary coupling with comments

4. **Clear naming convention**
   - Policy: `should_*()`, `can_*()`, `decide_*()`, `extract_*()`
   - Side effect: `*_node()`, `*_postprocess()`, `apply_*()`, `execute_*()`

## Example: Router Node

### Bad (all mixed):
```python
async def router_node(state: dict) -> dict:
    # Policy + LLM + fallback all together
    planner_prompt = _build_router_prompt(...)
    router_output = await llm_provider.chat_structured(...)
    if parse_error:
        salvaged = extract_router_output_from_exception(...)
    return state_update
```

### Good (separated):
```python
# In router_decision_policy.py (pure)
def should_skip_router_llm(feedback_plan):
    return bool(feedback_plan)

def decide_fallback_mode(has_salvage, has_explicit_export, ...):
    if has_salvage: return "salvaged"
    ...

# In router_node.py (side effects)
async def router_node(state: dict) -> dict:
    # Use pure policy
    if should_skip_router_llm(state["feedback_plan"]):
        # Try fast-paths
        ...
    
    # Call LLM (side effect)
    router_output = await llm_provider.chat_structured(...)
    
    # On error: use policy for recovery
    if parse_error:
        fallback_mode = decide_fallback_mode(...)
        salvaged = extract_router_output_from_exception(...)
```

## Testing Benefits

### Policy function testing (no mocks):
```python
def test_should_continue_compose():
    assert should_continue_compose_iteration(0, 5, False, "refine") == True
    assert should_continue_compose_iteration(5, 5, False, "refine") == False
    assert should_continue_compose_iteration(0, 5, True, "") == False
```

### Node testing (with mocks for LLM/DB):
```python
async def test_compose_node_with_llm_failure():
    llm_provider.chat_structured = mock_llm_failure
    out = await compose_node(state)
    # Verify fallback policy was applied
    assert "Не удалось" in out["final_answer"]
```

## Future Work

1. **Uncouple `deterministic_route()`**: Move `ChatService._deterministic_tool_steps()` parsing to pure function
2. **Separate memory extraction**: Pure entity extraction policy from LLM-based extraction
3. **Tool execution policy**: Separate "should execute which tools" from actual execution
4. **Guardrail policy**: Separate verdict logic from enforcement/recovery

## Checking the Boundary

To verify proper separation, ask about each function:

- ✅ Can I test this without mocking LLM/DB/HTTP? → Policy function
- ❌ Do I need mocks for LLM/DB/HTTP? → Side effect wrapper, call policy first
- ❌ Does this function import services? → Likely side effect (or bad coupling)
- ✅ Would this work the same input given same output multiple times? → Policy function
