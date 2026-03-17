from __future__ import annotations


def extract_artifacts(tool_calls: list[dict]) -> list[dict]:
    """Extract binary file artifacts from successful tool call results."""
    artifacts: list[dict] = []
    for call in tool_calls:
        if not call.get("success"):
            continue
        result = call.get("result") if isinstance(call.get("result"), dict) else {}
        if result.get("file_base64"):
            artifacts.append(
                {
                    "file_name": result.get("file_name", "artifact.bin"),
                    "mime_type": result.get("mime_type", "application/octet-stream"),
                    "file_base64": result["file_base64"],
                }
            )
    return artifacts
