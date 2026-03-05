"""Validation script for Dynamic Tool Injection architecture."""

# 1. Model import
from app.models.dynamic_tool import DynamicTool
print("1. DynamicTool model OK")

# 2. Models __init__
from app.models import DynamicTool as DT2
print("2. models __init__ OK")

# 3. Schemas
from app.schemas.dynamic_tool import (
    ApiRegistrationPayload,
    DynamicToolCreate,
    DynamicToolOut,
    DynamicToolBrief,
    DynamicToolUpdate,
)
print("3. schemas OK")

# 4. Service
from app.services.dynamic_tool_service import dynamic_tool_service, META_REGISTRATION_PROMPT
print("4. dynamic_tool_service OK")
print(f"   META_REGISTRATION_PROMPT length: {len(META_REGISTRATION_PROMPT)}")

# 5. Skills registry
from app.services.skills_registry_service import skills_registry_service
names = skills_registry_service.tool_names()
dyn_tools = sorted(n for n in names if "dynamic" in n)
print(f"5. skills registry OK — dynamic tools: {dyn_tools}")
sigs = skills_registry_service.planner_signatures()
print(f"   planner sigs contain dynamic_tool_register: {'dynamic_tool_register' in sigs}")

# 6. Orchestrator handlers
from app.services.tool_orchestrator_service import tool_orchestrator_service
handlers = tool_orchestrator_service._handlers()
dyn_handlers = sorted(k for k in handlers if "dynamic" in k)
print(f"6. orchestrator handlers OK — dynamic: {dyn_handlers}")
print(f"   is_dynamic_tool('dyn:weather_api'): {tool_orchestrator_service.is_dynamic_tool('dyn:weather_api')}")
print(f"   is_dynamic_tool('dyn_check_order'): {tool_orchestrator_service.is_dynamic_tool('dyn_check_order')}")
print(f"   is_dynamic_tool('cron_add'): {tool_orchestrator_service.is_dynamic_tool('cron_add')}")

# 7. Graph nodes still import fine
from app.graph import agent_graph
g = agent_graph.get_graph()
print(f"7. LangGraph agent OK — nodes: {list(g.nodes)}")

# 8. Validate normalize_steps accepts dyn: tools
steps_raw = [
    {"tool": "dyn:weather_api", "arguments": {"city": "Moscow"}},
    {"tool": "cron_add", "arguments": {"schedule_text": "every day at 9:00"}},
    {"tool": "invalid_tool", "arguments": {}},
]
normalized = tool_orchestrator_service._normalize_steps(steps_raw)
print(f"8. normalize_steps OK — accepted {len(normalized)}/3 steps: {[s['tool'] for s in normalized]}")

print("\nAll Dynamic Tool Injection validations passed!")
