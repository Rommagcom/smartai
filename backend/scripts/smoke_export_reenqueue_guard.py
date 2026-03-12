from app.graph import nodes


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def run() -> None:
    # Requested PDF with failed first attempt should trigger re-enqueue.
    failed_calls = [
        {
            "tool": "pdf_create",
            "success": False,
            "result": {"status": "error"},
            "error": "pdf_create requires non-empty content",
        }
    ]
    ensure(
        nodes.should_reenqueue_export("pdf", failed_calls, []),
        "expected re-enqueue when pdf_create failed and artifact missing",
    )

    # Successful queued status should not re-enqueue.
    queued_calls = [
        {
            "tool": "pdf_create",
            "success": True,
            "result": {"status": "queued"},
        }
    ]
    ensure(
        not nodes.should_reenqueue_export("pdf", queued_calls, []),
        "unexpected re-enqueue when export already queued",
    )

    # Existing artifact should not re-enqueue.
    artifacts = [
        {"file_name": "result.pdf", "mime_type": "application/pdf", "file_base64": "Zm9v"}
    ]
    ensure(
        not nodes.should_reenqueue_export("pdf", [], artifacts),
        "unexpected re-enqueue when pdf artifact exists",
    )

    print("SMOKE_EXPORT_REENQUEUE_GUARD_OK")


if __name__ == "__main__":
    run()
