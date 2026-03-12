from __future__ import annotations

import re

_EXPORT_SUBJECT_RE = re.compile(r"(?:pdf|пдф|excel|xlsx|документ|файл)", re.IGNORECASE)
_EXPORT_READY_RE = re.compile(
    r"(?:успешно\s+создан|создан|готов|готов\s+к\s+скачиванию|приложен|вложен|attached|uploaded)",
    re.IGNORECASE,
)


def requested_export_kind(user_message: str) -> str | None:
    """Return explicit export target kind requested by user: pdf | excel | None."""
    lowered = str(user_message or "").strip().lower()
    if not lowered:
        return None

    has_explicit_export = bool(
        re.search(
            r"\b(?:сохрани|сохранить|выгрузи|выгрузить|экспорт|экспортируй|создай\s+файл|"
            r"скачай|download|export|save|attach)\b",
            lowered,
        )
    )
    if not has_explicit_export:
        return None

    if re.search(r"\b(?:pdf|пдф)\b|\bв\s+pdf\b", lowered):
        return "pdf"
    if re.search(r"\b(?:excel|xlsx|таблиц)\b|\bв\s+excel\b", lowered):
        return "excel"
    return None


def has_successful_export_call(calls: list[dict], export_kind: str) -> bool:
    tool_name = _export_tool_name(export_kind)
    for call in calls:
        if not isinstance(call, dict):
            continue
        if str(call.get("tool") or "").strip().lower() != tool_name:
            continue
        if not bool(call.get("success")):
            continue
        result = call.get("result") if isinstance(call.get("result"), dict) else {}
        status = str(result.get("status") or "").strip().lower()
        if status in {"queued", "deduplicated", "ok", "created", "processing"}:
            return True
        if result.get("file_base64"):
            return True
    return False


def should_reenqueue_export(export_kind: str, existing_calls: list[dict], existing_artifacts: list[dict]) -> bool:
    """Re-enqueue export if user asked for it and no successful signal is present."""
    if _has_matching_artifact(export_kind, existing_artifacts):
        return False
    if has_successful_export_call(existing_calls, export_kind):
        return False
    return True


def sanitize_false_attachment_claims(answer: str, tool_calls: list[dict], artifacts: list[dict]) -> str:
    """Prevent claiming export delivery when no artifact is actually attached."""
    text = str(answer or "").strip()
    if not text or artifacts or not _contains_export_success_claim(text):
        return text

    if _has_queued_export(tool_calls):
        return (
            "Файл поставлен в очередь и будет отправлен отдельным сообщением после обработки. "
            "Текущий ответ не содержит вложения."
        )

    if _has_any_export_success(tool_calls):
        return (
            f"{_delivery_subject(tool_calls)} сформирован и будет отправлен отдельным сообщением. "
            "Текущий ответ не содержит вложения."
        )

    return text


def _export_tool_name(export_kind: str) -> str:
    return "pdf_create" if export_kind == "pdf" else "excel_create"


def _has_matching_artifact(export_kind: str, artifacts: list[dict]) -> bool:
    for item in artifacts:
        if not isinstance(item, dict):
            continue
        mime = str(item.get("mime_type") or "").lower()
        if export_kind == "pdf" and "pdf" in mime:
            return True
        if export_kind == "excel" and ("spreadsheet" in mime or "excel" in mime):
            return True
        file_name = str(item.get("file_name") or "").lower()
        if export_kind == "pdf" and file_name.endswith(".pdf"):
            return True
        if export_kind == "excel" and (file_name.endswith(".xlsx") or file_name.endswith(".xls")):
            return True
    return False


def _has_queued_export(tool_calls: list[dict]) -> bool:
    for call in tool_calls:
        if not isinstance(call, dict) or not bool(call.get("success")):
            continue
        tool_name = str(call.get("tool") or "").strip().lower()
        if tool_name not in {"pdf_create", "excel_create"}:
            continue
        result = call.get("result") if isinstance(call.get("result"), dict) else {}
        status = str(result.get("status") or "").strip().lower()
        if status in {"queued", "deduplicated"}:
            return True
    return False


def _has_any_export_success(tool_calls: list[dict]) -> bool:
    for call in tool_calls:
        if not isinstance(call, dict) or not bool(call.get("success")):
            continue
        tool_name = str(call.get("tool") or "").strip().lower()
        if tool_name in {"pdf_create", "excel_create"}:
            return True
    return False


def _delivery_subject(tool_calls: list[dict]) -> str:
    for call in tool_calls:
        if not isinstance(call, dict) or not bool(call.get("success")):
            continue
        tool_name = str(call.get("tool") or "").strip().lower()
        if tool_name == "excel_create":
            return "Excel-файл"
        if tool_name == "pdf_create":
            return "PDF-файл"
    return "Файл"


def _contains_export_success_claim(text: str) -> bool:
    return bool(_EXPORT_SUBJECT_RE.search(text) and _EXPORT_READY_RE.search(text))
