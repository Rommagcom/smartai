"""Quick validation of the structured output parsing."""
from app.schemas.graph import RouterOutput, RouterDecision, ToolStep
from app.llm import LLMProvider
import uuid

from scripts.smoke_env import apply_smoke_env_defaults, smoke_user_uuid

apply_smoke_env_defaults()

provider = LLMProvider()

# Test structured parsing
raw_json = '{"decision": "tool", "steps": [{"tool": "cron_add", "arguments": {"schedule_text": "завтра в 9:00", "task_text": "совещание"}}], "response_hint": "", "confidence": 0.9}'
result = provider._parse_structured_response(raw_json, RouterOutput)
print(f"Decision: {result.decision.value}")
print(f"Steps: {len(result.steps)}, first tool: {result.steps[0].tool}")
print(f"Confidence: {result.confidence}")

# Test with markdown fences
fenced = '```json\n{"decision": "chat", "steps": [], "response_hint": "hello", "confidence": 0.8}\n```'
result2 = provider._parse_structured_response(fenced, RouterOutput)
print(f"Fenced parse: {result2.decision.value}")

# Test AgentState creation
from app.schemas.graph import AgentState
_smoke_user_id = smoke_user_uuid()
state = AgentState(
    user_id=_smoke_user_id,
    session_id=uuid.uuid5(uuid.NAMESPACE_DNS, f"smoke-session-{_smoke_user_id}"),
    user_message="тестовое сообщение",
)
print(f"AgentState: user_message={state.user_message}, iteration={state.iteration}")

print("\nAll structured output tests passed!")
