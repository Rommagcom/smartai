import hashlib
import hmac
import json
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from integrations.messengers.whatsapp.adapter import WhatsAppAdapter
from integrations.messengers.whatsapp.settings import get_whatsapp_settings

router = APIRouter()
adapter = WhatsAppAdapter()
settings = get_whatsapp_settings()


def _is_valid_signature(raw_body: bytes, header_value: str | None, app_secret: str) -> bool:
    if not app_secret:
        # Signature verification is optional for local/dev usage.
        return True
    if not header_value:
        return False

    expected = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    expected_header = f"sha256={expected}"
    return hmac.compare_digest(expected_header, header_value)


@router.get(
    "/webhook",
    response_class=PlainTextResponse,
    responses={403: {"description": "Webhook verification failed"}},
)
async def verify_webhook(
    hub_mode: Annotated[str | None, Query(alias="hub.mode")] = None,
    hub_verify_token: Annotated[str | None, Query(alias="hub.verify_token")] = None,
    hub_challenge: Annotated[str | None, Query(alias="hub.challenge")] = None,
) -> str:
    if not adapter.verify_webhook(mode=hub_mode, verify_token=hub_verify_token):
        raise HTTPException(status_code=403, detail="Webhook verification failed")
    return str(hub_challenge or "")


@router.post(
    "/webhook",
    responses={
        400: {"description": "Invalid JSON payload"},
        403: {"description": "Invalid or missing webhook signature"},
    },
)
async def ingest_webhook(
    request: Request,
    x_hub_signature_256: Annotated[str | None, Header(alias="X-Hub-Signature-256")] = None,
) -> dict:
    raw_body = await request.body()
    header_signature = x_hub_signature_256 or request.headers.get("X-Hub-Signature-256")
    if not _is_valid_signature(raw_body=raw_body, header_value=header_signature, app_secret=settings.WHATSAPP_APP_SECRET):
        raise HTTPException(status_code=403, detail="Invalid or missing webhook signature")

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc

    await adapter.handle_webhook(payload)
    return {"status": "ok"}
