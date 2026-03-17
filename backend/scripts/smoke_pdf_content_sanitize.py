from app.services.tool_orchestrator_service import ToolOrchestratorService


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def run() -> None:
    sample = """
Сводка погоды по Алматы за 2026-03-12.

PDF-версия (с тем же сводным описанием) доступна по ссылке ниже.
[ Скачать PDF ](data:application/pdf;base64,JVBERi0xLjQKJeLjz9MK....)
*(Примечание: ссылка содержит итоговый PDF-файл, закодированный в base64.)*

Температура: +12C
Ветер: 3 м/с
""".strip()

    cleaned = ToolOrchestratorService._sanitize_document_text(sample)

    ensure("Сводка погоды" in cleaned, f"core text lost: {cleaned}")
    ensure("Температура" in cleaned, f"useful content lost: {cleaned}")
    ensure("Скачать PDF" not in cleaned, f"download instruction leaked: {cleaned}")
    ensure("base64" not in cleaned.lower(), f"base64 note leaked: {cleaned}")
    ensure("data:application/pdf" not in cleaned.lower(), f"data URL leaked: {cleaned}")

    print("SMOKE_PDF_CONTENT_SANITIZE_OK")


if __name__ == "__main__":
    run()
