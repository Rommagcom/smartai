from app.graph import nodes


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def run() -> None:
    # Case 1: queued export + no artifact should never claim immediate delivery.
    queued_text = "PDF-документ успешно создан и готов к скачиванию."
    queued_calls = [
        {
            "tool": "pdf_create",
            "success": True,
            "result": {"status": "queued"},
        }
    ]
    out_queued = nodes.sanitize_false_attachment_claims(queued_text, queued_calls, [])
    ensure("поставлен в очередь" in out_queued.lower(), f"queued guard mismatch: {out_queued}")
    ensure("не содержит вложения" in out_queued.lower(), f"queued attachment note missing: {out_queued}")

    # Case 2: claim without artifact should be rewritten to delivery-pending note.
    plain_claim = "Файл PDF успешно создан."
    plain_calls = [
        {
            "tool": "pdf_create",
            "success": True,
            "result": {"status": "ok"},
        }
    ]
    out_plain = nodes.sanitize_false_attachment_claims(plain_claim, plain_calls, [])
    ensure("будет отправлен отдельным сообщением" in out_plain.lower(), f"plain claim not rewritten: {out_plain}")
    ensure("не содержит вложения" in out_plain.lower(), f"missing attachment note for plain claim: {out_plain}")

    # Case 3: if artifact is present, answer must stay untouched.
    with_artifact = nodes.sanitize_false_attachment_claims(
        queued_text,
        queued_calls,
        [{"file_name": "a.pdf", "mime_type": "application/pdf", "file_base64": "Zm9v"}],
    )
    ensure(with_artifact == queued_text, "answer changed despite attached artifact")

    print("SMOKE_EXPORT_CLAIM_GUARD_OK")


if __name__ == "__main__":
    run()
