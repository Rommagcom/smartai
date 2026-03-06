"""Unit tests for $prev / $step[N] chain context placeholder resolution."""
import sys
sys.path.insert(0, ".")

from app.services.tool_orchestrator_service import (
    _resolve_placeholders,
    _resolve_value,
    _deep_get,
    ToolOrchestratorService,
)


def test_deep_get():
    print("=== _deep_get ===")
    assert _deep_get({"a": 1, "b": 2}, "a") == 1
    assert _deep_get({"a": 1}, "missing") == ""
    assert _deep_get({"a": 1}, None) == {"a": 1}
    assert _deep_get(None, "a") == ""
    assert _deep_get(None, None) == ""
    print("PASS")


def test_resolve_value_exact():
    print("=== _resolve_value exact ===")
    prev = {"body": "hello", "items": [1, 2, 3]}
    steps = [
        {"tool": "t0", "result": {"data": "step0data"}},
        {"tool": "t1", "result": {"val": "step1val"}},
    ]

    assert _resolve_value("$prev.body", prev=prev, steps=steps) == "hello"
    assert _resolve_value("$prev.items", prev=prev, steps=steps) == [1, 2, 3]
    assert _resolve_value("$prev", prev=prev, steps=steps) == prev
    assert _resolve_value("$step[0].data", prev=prev, steps=steps) == "step0data"
    assert _resolve_value("$step[1].val", prev=prev, steps=steps) == "step1val"
    assert _resolve_value("$step[99].val", prev=prev, steps=steps) == ""
    # No placeholder — pass through
    assert _resolve_value("plain text", prev=prev, steps=steps) == "plain text"
    # Non-string pass through
    assert _resolve_value(42, prev=prev, steps=steps) == 42
    assert _resolve_value(None, prev=prev, steps=steps) is None
    print("PASS")


def test_resolve_value_inline():
    print("=== _resolve_value inline ===")
    prev = {"body": "hello", "count": 42}
    steps = [{"tool": "t0", "result": {"data": "step0data"}}]

    result = _resolve_value("Prefix $prev.body suffix", prev=prev, steps=steps)
    assert result == "Prefix hello suffix", f"got: {result!r}"

    result2 = _resolve_value("Data: $step[0].data end", prev=prev, steps=steps)
    assert result2 == "Data: step0data end", f"got: {result2!r}"

    # Inline with numeric value
    result3 = _resolve_value("Count is $prev.count items", prev=prev, steps=steps)
    assert result3 == "Count is 42 items", f"got: {result3!r}"
    print("PASS")


def test_resolve_value_nested():
    print("=== _resolve_value nested dict/list ===")
    prev = {"body": "hello"}
    steps = [{"tool": "t0", "result": {"data": "step0data"}}]

    nested = {"a": "$prev.body", "b": ["$step[0].data", "static"]}
    result = _resolve_value(nested, prev=prev, steps=steps)
    assert result == {"a": "hello", "b": ["step0data", "static"]}, f"got: {result!r}"
    print("PASS")


def test_resolve_placeholders():
    print("=== _resolve_placeholders ===")
    prev = {"body": "hello"}
    steps = []

    args = {"content": "$prev.body", "title": "Report"}
    resolved = _resolve_placeholders(args, prev=prev, steps=steps)
    assert resolved == {"content": "hello", "title": "Report"}, f"got: {resolved!r}"
    print("PASS")


def test_augment_step_arguments():
    print("=== _augment_step_arguments ===")
    prev = {"body": "hello"}
    steps = [{"tool": "t0", "result": {"data": "d0"}}]
    context = {"_prev": prev, "_steps": steps}

    result = ToolOrchestratorService._augment_step_arguments(
        "pdf_create", {"content": "$prev.body", "title": "T"}, context
    )
    assert result == {"content": "hello", "title": "T"}, f"got: {result!r}"
    print("PASS")


def test_augment_empty_context():
    print("=== _augment_step_arguments empty context ===")
    context: dict = {}
    result = ToolOrchestratorService._augment_step_arguments(
        "pdf_create", {"content": "static text", "title": "T"}, context
    )
    assert result == {"content": "static text", "title": "T"}, f"got: {result!r}"
    print("PASS")


def test_update_chain_context():
    print("=== _update_chain_context ===")
    ctx: dict = {}

    ToolOrchestratorService._update_chain_context("integration_call", {"body": "data1"}, ctx)
    assert ctx["_prev"] == {"body": "data1"}
    assert len(ctx["_steps"]) == 1
    assert ctx["_steps"][0] == {"tool": "integration_call", "result": {"body": "data1"}}

    ToolOrchestratorService._update_chain_context("pdf_create", {"file_name": "f.pdf"}, ctx)
    assert ctx["_prev"] == {"file_name": "f.pdf"}
    assert len(ctx["_steps"]) == 2
    assert ctx["_steps"][1] == {"tool": "pdf_create", "result": {"file_name": "f.pdf"}}
    print("PASS")


def test_full_chain_simulation():
    print("=== Full chain simulation ===")
    ctx: dict = {}

    # Step 1: integration_call → body with XML
    step1_result = {
        "status_code": 200,
        "body": "<xml>exchange rate 450.5</xml>",
        "headers": {},
    }
    ToolOrchestratorService._update_chain_context("integration_call", step1_result, ctx)

    # Step 2: pdf_create with $prev.body
    step2_args = {"title": "Currency Report", "content": "$prev.body"}
    resolved = ToolOrchestratorService._augment_step_arguments("pdf_create", step2_args, ctx)
    assert resolved["content"] == "<xml>exchange rate 450.5</xml>", f"got: {resolved['content']!r}"
    assert resolved["title"] == "Currency Report"

    # After step 2, update context
    step2_result = {"file_name": "report.pdf", "size_bytes": 1234}
    ToolOrchestratorService._update_chain_context("pdf_create", step2_result, ctx)

    # Verify both steps accessible via $step[N]
    assert ctx["_steps"][0]["result"]["body"] == "<xml>exchange rate 450.5</xml>"
    assert ctx["_steps"][1]["result"]["file_name"] == "report.pdf"
    assert ctx["_prev"] == step2_result
    print("PASS")


def test_step_index_reference():
    print("=== $step[N] index reference ===")
    ctx: dict = {}

    ToolOrchestratorService._update_chain_context("memory_search", {"items": ["fact1", "fact2"]}, ctx)
    ToolOrchestratorService._update_chain_context("integration_call", {"body": "api data"}, ctx)

    # Reference step[0] (memory_search) from step 3
    args = {"data": "$step[0].items"}
    resolved = ToolOrchestratorService._augment_step_arguments("pdf_create", args, ctx)
    assert resolved["data"] == ["fact1", "fact2"], f"got: {resolved['data']!r}"
    print("PASS")


def test_onboarding_legacy_still_works():
    print("=== Onboarding legacy context ===")
    ctx: dict = {}
    draft = {"service_name": "test", "base_url": "http://example.com"}
    ToolOrchestratorService._update_chain_context(
        "integration_onboarding_connect",
        {"draft": draft, "draft_id": "abc123"},
        ctx,
    )
    assert ctx["integration_onboarding"]["draft"] == draft
    assert ctx["integration_onboarding"]["draft_id"] == "abc123"

    # _augment should propagate draft to test step
    args = ToolOrchestratorService._augment_step_arguments(
        "integration_onboarding_test", {}, ctx
    )
    assert args["draft"] == draft
    assert args["draft_id"] == "abc123"
    print("PASS")


if __name__ == "__main__":
    test_deep_get()
    test_resolve_value_exact()
    test_resolve_value_inline()
    test_resolve_value_nested()
    test_resolve_placeholders()
    test_augment_step_arguments()
    test_augment_empty_context()
    test_update_chain_context()
    test_full_chain_simulation()
    test_step_index_reference()
    test_onboarding_legacy_still_works()

    print()
    print("ALL TESTS PASSED!")
