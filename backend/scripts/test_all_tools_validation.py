"""Unit tests for all tools: deterministic routes, answers, signatures, handlers."""
import sys
sys.path.insert(0, ".")

from app.services.chat_service import ChatService
from app.graph.nodes import _format_deterministic_tool_answer, _deterministic_route
from app.schemas.graph import ToolResult


def test_existing_routes():
    print("=== Existing tool routes ===")
    tests = [
        ("удали все напоминания", "cron_delete_all"),
        ("какие мои напоминания", "cron_list"),
        ("удали все интеграции", "integrations_delete_all"),
    ]
    for msg, expected_tool in tests:
        result = ChatService._deterministic_tool_steps(msg)
        assert result and result[0]["tool"] == expected_tool, (
            f"{msg!r}: expected {expected_tool}, got {result}"
        )
        print(f"  OK: {msg!r} -> {result[0]['tool']}")

    # PDF/Excel go through LLM planner, no deterministic route
    for msg in ["создай pdf с текстом тут", "сделай excel таблицу"]:
        result = ChatService._deterministic_tool_steps(msg)
        assert result is None, f"{msg!r}: expected None (LLM route), got {result}"
        print(f"  OK: {msg!r} -> None (LLM planner)")
    print("PASS")


def test_existing_answers():
    print("=== Existing tool answers ===")

    tr = ToolResult(tool="pdf_create", arguments={}, success=True,
                    result={"file_name": "doc.pdf", "size_bytes": 2048})
    ans = _format_deterministic_tool_answer([tr])
    assert ans and "doc.pdf" in ans, f"got: {ans!r}"
    print(f"  pdf_create OK: {ans}")

    tr = ToolResult(tool="excel_create", arguments={}, success=True,
                    result={"file_name": "data.xlsx", "size_bytes": 4096})
    ans = _format_deterministic_tool_answer([tr])
    assert ans and "data.xlsx" in ans, f"got: {ans!r}"
    print(f"  excel_create OK: {ans}")

    tr = ToolResult(tool="cron_add", arguments={}, success=True,
                    result={"cron_expression": "0 9 * * *",
                            "payload": {"message": "wake up"},
                            "action_type": "send_message"})
    ans = _format_deterministic_tool_answer([tr])
    assert ans and "wake up" in ans, f"got: {ans!r}"
    print(f"  cron_add OK: {ans}")

    tr = ToolResult(tool="cron_list", arguments={}, success=True,
                    result={"items": [{"name": "daily", "cron_expression": "0 9 * * *"}]})
    ans = _format_deterministic_tool_answer([tr])
    assert ans and "daily" in ans, f"got: {ans!r}"
    print("  cron_list OK")

    tr = ToolResult(tool="cron_delete_all", arguments={}, success=True, result={"deleted": True})
    ans = _format_deterministic_tool_answer([tr])
    assert ans and "удалены" in ans.lower(), f"got: {ans!r}"
    print("  cron_delete_all OK")

    tr = ToolResult(tool="memory_delete_all", arguments={}, success=True, result={"deleted": True})
    ans = _format_deterministic_tool_answer([tr])
    assert ans and "очищена" in ans.lower(), f"got: {ans!r}"
    print("  memory_delete_all OK")

    print("PASS")


def test_doc_routes():
    print("=== Doc tool routes ===")
    tests = [
        ("покажи мои документы", "doc_list"),
        ("удали документ report.pdf", "doc_delete"),
        ("удали все документы", "doc_delete_all"),
        ("удалить все мои файлы", "doc_delete_all"),
        ("список документов", "doc_list"),
        ("какие у меня файлы", "doc_list"),
    ]
    for msg, expected_tool in tests:
        result = ChatService._deterministic_tool_steps(msg)
        assert result and result[0]["tool"] == expected_tool, (
            f"{msg!r}: expected {expected_tool}, got {result}"
        )
        print(f"  OK: {msg!r} -> {result[0]['tool']}")
    print("PASS")


def test_doc_answers():
    print("=== Doc tool answers ===")

    tr = ToolResult(tool="doc_list", arguments={}, success=True,
                    result={"items": [{"source_doc": "a.pdf", "chunk_count": 3},
                                      {"source_doc": "b.txt"}]})
    ans = _format_deterministic_tool_answer([tr])
    assert ans and "a.pdf" in ans and "b.txt" in ans, f"got: {ans!r}"
    print("  doc_list with items OK")

    tr2 = ToolResult(tool="doc_list", arguments={}, success=True, result={"items": []})
    ans2 = _format_deterministic_tool_answer([tr2])
    assert ans2 and "нет" in ans2.lower(), f"got: {ans2!r}"
    print("  doc_list empty OK")

    tr3 = ToolResult(tool="doc_delete", arguments={"source_doc": "x.pdf"}, success=True,
                     result={"deleted": True, "source_doc": "x.pdf", "deleted_chunks": 5})
    ans3 = _format_deterministic_tool_answer([tr3])
    assert ans3 and "x.pdf" in ans3, f"got: {ans3!r}"
    print("  doc_delete success OK")

    tr4 = ToolResult(tool="doc_delete", arguments={"source_doc": "y.pdf"}, success=True,
                     result={"deleted": False, "source_doc": "y.pdf", "deleted_chunks": 0})
    ans4 = _format_deterministic_tool_answer([tr4])
    assert ans4 and "не найден" in ans4.lower(), f"got: {ans4!r}"
    print("  doc_delete not found OK")

    tr5 = ToolResult(tool="doc_delete_all", arguments={}, success=True,
                     result={"deleted": True, "deleted_count": 10})
    ans5 = _format_deterministic_tool_answer([tr5])
    assert ans5 and "удалены" in ans5.lower() and "10" in ans5, f"got: {ans5!r}"
    print("  doc_delete_all OK")

    tr6 = ToolResult(tool="doc_delete_all", arguments={}, success=True, result={"deleted_count": 0})
    ans6 = _format_deterministic_tool_answer([tr6])
    assert ans6 and "не было" in ans6.lower(), f"got: {ans6!r}"
    print("  doc_delete_all empty OK")

    print("PASS")


def test_planner_signatures():
    print("=== Planner signatures ===")
    from app.services.skills_registry_service import skills_registry_service

    sigs = skills_registry_service.planner_signatures()
    for tool in ["pdf_create", "excel_create", "doc_list", "doc_delete", "doc_delete_all",
                 "cron_add", "cron_list", "cron_delete_all",
                 "memory_add", "memory_list", "memory_delete_all",
                 "integration_call", "register_api_tool"]:
        assert tool in sigs, f"Missing {tool} in planner_signatures"
        print(f"  OK: {tool} in signatures")
    print("PASS")


def test_handler_coverage():
    print("=== Handler coverage ===")
    from app.services.tool_orchestrator_service import tool_orchestrator_service

    handlers = tool_orchestrator_service._handlers()
    expected = [
        "pdf_create", "excel_create",
        "memory_add", "memory_list", "memory_search", "memory_delete", "memory_delete_all",
        "doc_search", "doc_list", "doc_delete", "doc_delete_all",
        "cron_add", "cron_list", "cron_delete", "cron_delete_all",
        "integration_call", "integration_add", "integrations_list", "integrations_delete_all",
        "dynamic_tool_register", "dynamic_tool_call", "dynamic_tool_list",
        "dynamic_tool_delete", "dynamic_tool_delete_all",
        "register_api_tool",
    ]
    for tool in expected:
        assert tool in handlers, f"Missing handler for {tool}"
        print(f"  OK: {tool}")
    print("PASS")


if __name__ == "__main__":
    test_existing_routes()
    test_existing_answers()
    test_doc_routes()
    test_doc_answers()
    test_planner_signatures()
    test_handler_coverage()
    print()
    print("ALL TESTS PASSED!")
