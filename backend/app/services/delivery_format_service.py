from __future__ import annotations

from datetime import datetime, timezone


def _result_preview(result: dict | None) -> dict:
    if not isinstance(result, dict):
        return {"raw": str(result)}

    preview = dict(result)
    # Keep file_base64 in the payload so Telegram and WebSocket clients can
    # deliver the actual file to the user (instead of just a hint to "retry").
    if "file_base64" in preview:
        preview["artifact_ready"] = True
    return preview


def _next_action_hint(job_type: str, preview: dict | None) -> str | None:
    if not isinstance(preview, dict):
        return None
    if not preview.get("artifact_ready"):
        return None

    if str(job_type) in ("pdf_create", "excel_create"):
        return "Файл готов и будет отправлен автоматически."

    return "Файл готов и будет отправлен автоматически."


def build_worker_delivery_payload(
    *,
    job_type: str,
    is_success: bool,
    result: dict | None = None,
    error_message: str | None = None,
    human_message: str | None = None,
) -> dict:
    preview = _result_preview(result) if is_success else None
    if human_message:
        message = human_message
    else:
        message = "Фоновая задача выполнена." if is_success else "Фоновая задача завершилась с ошибкой."
    return {
        "type": "worker_result",
        "success": is_success,
        "job_type": job_type,
        "message": message,
        "result_preview": preview,
        "next_action_hint": _next_action_hint(job_type=job_type, preview=preview) if is_success else None,
        "error": None if is_success else {"message": str(error_message or "unknown error")},
        "delivered_at": datetime.now(timezone.utc).isoformat(),
        "status": "success" if is_success else "failed",
        "result": preview,
    }
