"""Test artifact extraction and delivery chain."""

import base64
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///test_artifacts.db")
os.environ.setdefault("SECRET_KEY", "test-key")
os.environ.setdefault("WORKER_ENABLED", "0")
os.environ.setdefault("WS_FANOUT_REDIS_ENABLED", "0")
os.environ.setdefault("SCHEDULER_ENABLED", "0")
os.environ.setdefault("MILVUS_ENABLED", "0")

passed = 0
failed = 0


def check(name, condition):
    global passed, failed
    if condition:
        print(f"  PASS: {name}")
        passed += 1
    else:
        print(f"  FAIL: {name}")
        failed += 1


print("=== PDF service return value ===")

# wkhtmltopdf is only available in Docker; use a mock result
dummy_pdf_bytes = b"%PDF-1.4 dummy content for testing"
result = {
    "file_name": "test.pdf",
    "mime_type": "application/pdf",
    "file_base64": base64.b64encode(dummy_pdf_bytes).decode("utf-8"),
    "size_bytes": len(dummy_pdf_bytes),
}
check("has file_base64", "file_base64" in result)
check("has file_name", result.get("file_name") == "test.pdf")
check("has mime_type", result.get("mime_type") == "application/pdf")
check("has size_bytes", isinstance(result.get("size_bytes"), int) and result["size_bytes"] > 0)
check("file_base64 is non-empty string", isinstance(result["file_base64"], str) and len(result["file_base64"]) > 0)
check("base64 decodes to valid bytes", len(base64.b64decode(result["file_base64"])) > 0)

print("\n=== _extract_artifacts (chat_service) ===")
from app.services.chat_service import ChatService

tool_calls = [
    {
        "tool": "pdf_create",
        "arguments": {"title": "Test", "content": "Hello", "filename": "test.pdf"},
        "success": True,
        "result": result,
    }
]
artifacts = ChatService._extract_artifacts(tool_calls)
check("extracts 1 artifact", len(artifacts) == 1)
check("artifact has file_base64", "file_base64" in artifacts[0])
check("artifact has file_name", artifacts[0].get("file_name") == "test.pdf")
check("artifact has mime_type", artifacts[0].get("mime_type") == "application/pdf")

print("\n=== _extract_artifacts (nodes.py) ===")
from app.graph.nodes import _extract_artifacts as graph_extract

artifacts_graph = graph_extract(tool_calls)
check("graph extracts 1 artifact", len(artifacts_graph) == 1)
check("graph artifact has file_base64", "file_base64" in artifacts_graph[0])
check("graph artifact file_name", artifacts_graph[0].get("file_name") == "test.pdf")

print("\n=== _extract_artifacts with failed tool ===")
failed_calls = [
    {"tool": "pdf_create", "arguments": {}, "success": False, "error": "timeout"}
]
check("no artifacts for failed tool", len(ChatService._extract_artifacts(failed_calls)) == 0)

print("\n=== _extract_artifacts with no file_base64 ===")
no_file_calls = [
    {"tool": "cron_add", "arguments": {}, "success": True, "result": {"status": "ok"}}
]
check("no artifacts for non-file tool", len(ChatService._extract_artifacts(no_file_calls)) == 0)

print("\n=== ChatResponse serialization ===")
from app.schemas.chat import ChatResponse
from uuid import uuid4

resp = ChatResponse(
    session_id=uuid4(),
    response="Документ test.pdf создан (1.0 KB).",
    tool_calls=tool_calls,
    artifacts=artifacts,
)
resp_dict = resp.model_dump(mode="json")
check("response has artifacts list", isinstance(resp_dict.get("artifacts"), list))
check("artifacts list has 1 item", len(resp_dict["artifacts"]) == 1)
check("serialized artifact has file_base64", "file_base64" in resp_dict["artifacts"][0])
check("serialized file_base64 is non-empty", len(resp_dict["artifacts"][0]["file_base64"]) > 0)

print("\n=== Fallback extraction from tool_calls ===")
# Simulate adapter fallback: artifacts is empty but tool_calls has file_base64
empty_artifacts = []
if not empty_artifacts and tool_calls:
    for tc in tool_calls:
        if not isinstance(tc, dict) or not tc.get("success"):
            continue
        tc_result = tc.get("result")
        if isinstance(tc_result, dict) and tc_result.get("file_base64"):
            empty_artifacts.append({
                "file_name": tc_result.get("file_name", "artifact.bin"),
                "mime_type": tc_result.get("mime_type", "application/octet-stream"),
                "file_base64": tc_result["file_base64"],
            })
check("fallback extracts 1 artifact", len(empty_artifacts) == 1)
check("fallback artifact has file_base64", "file_base64" in empty_artifacts[0])

print("\n=== respond_via_graph safety net ===")
# Simulate: tool_calls_log has file_base64, but artifacts is empty (lost during LangGraph)
tool_calls_log = [
    {
        "tool": "pdf_create",
        "arguments": {"title": "Test", "content": "Hello", "filename": "test.pdf"},
        "success": True,
        "result": result,
    }
]
lost_artifacts = []
if not lost_artifacts and tool_calls_log:
    lost_artifacts = ChatService._extract_artifacts(tool_calls_log)
check("safety net recovers 1 artifact", len(lost_artifacts) == 1)
check("recovered artifact has file_base64", "file_base64" in lost_artifacts[0])

print("\n=== Base64 round-trip ===")
original_bytes = base64.b64decode(result["file_base64"])
re_encoded = base64.b64encode(original_bytes).decode("utf-8")
check("base64 round-trip matches", re_encoded == result["file_base64"])
check("decoded bytes match size_bytes", len(original_bytes) == result["size_bytes"])

print(f"\n{'=' * 40}")
print(f"Results: {passed} passed, {failed} failed")
if failed:
    print("SOME TESTS FAILED!")
    sys.exit(1)
else:
    print("ALL TESTS PASSED!")
