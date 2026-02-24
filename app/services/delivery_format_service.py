from __future__ import annotations

from datetime import datetime, timezone


def _result_preview(result: dict | None) -> dict:
    if not isinstance(result, dict):
        return {"raw": str(result)}

    preview = dict(result)
    file_base64 = preview.pop("file_base64", None)
    if file_base64 is not None:
        preview["artifact_ready"] = True
        preview["artifact_note"] = "Результат содержит файл. Для передачи файла используйте чатовый tool-вызов без фоновой очереди."
    return preview


def _next_action_hint(job_type: str, preview: dict | None) -> str | None:
    if not isinstance(preview, dict):
        return None
    if not preview.get("artifact_ready"):
        return None

    if str(job_type) == "pdf_create":
        return "Файл готов. Чтобы получить PDF-файл, запусти задачу напрямую без фоновой очереди (например через /make_pdf или обычный chat tool flow)."

    return "Файл готов. Чтобы получить файл, повтори задачу без фразы про фон/очередь — тогда артефакт вернётся сразу в ответе."


def build_worker_delivery_payload(
    *,
    job_type: str,
    is_success: bool,
    result: dict | None = None,
    error_message: str | None = None,
) -> dict:
    preview = _result_preview(result) if is_success else None
    return {
        "type": "worker_result",
        "success": is_success,
        "job_type": job_type,
        "message": "Фоновая задача выполнена." if is_success else "Фоновая задача завершилась с ошибкой.",
        "result_preview": preview,
        "next_action_hint": _next_action_hint(job_type=job_type, preview=preview) if is_success else None,
        "error": None if is_success else {"message": str(error_message or "unknown error")},
        "delivered_at": datetime.now(timezone.utc).isoformat(),
        "status": "success" if is_success else "failed",
        "result": preview,
    }
