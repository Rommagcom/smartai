import asyncio

from app.graph import nodes
from app.schemas.graph import RouterDecision


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


async def run() -> None:
    original_load_user_tool_context = nodes.load_user_tool_context

    try:
        async def fake_load_user_tool_context(_user_id):
            await asyncio.sleep(0)
            return "", ""

        nodes.load_user_tool_context = fake_load_user_tool_context

        state = {
            "user_message": "Какая погода в Алматы",
            "user_id": "505a788e-c6c9-4d69-a768-58860070a9f5",
            "feedback_plan": "",
            "retrieved_tools": [],
            "history_messages": [],
        }
        out = await nodes.router_node(state)
        router_output = out.get("router_output")

        ensure(router_output is not None, f"router_output missing: {out}")
        ensure(router_output.decision == RouterDecision.WEB_SEARCH, f"unexpected decision: {router_output}")
        ensure(len(router_output.steps) == 1, f"unexpected steps: {router_output.steps}")
        ensure(router_output.steps[0].tool == "web_search", f"unexpected tool: {router_output.steps[0]}")
        got_query = router_output.steps[0].arguments.get("query", "")
        expected_query = state["user_message"].strip()
        print(f"DEBUG got_query bytes: {got_query.encode('utf-8')}")
        print(f"DEBUG expected bytes: {expected_query.encode('utf-8')}")
        ensure(got_query == expected_query, f"unexpected args (got={repr(got_query)}, expected={repr(expected_query)}): {router_output.steps[0].arguments}")
        ensure(out.get("next_step") == "web_search", f"unexpected next_step: {out}")

        print("SMOKE_ROUTER_LIVE_DATA_FAST_PATH_OK")
    finally:
        nodes.load_user_tool_context = original_load_user_tool_context


if __name__ == "__main__":
    asyncio.run(run())