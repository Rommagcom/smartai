import base64
import hashlib
import hmac
from binascii import Error as BinasciiError
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Request

from integrations.messengers.teams.adapter import TeamsAdapter
from integrations.messengers.teams.settings import get_teams_settings

router = APIRouter()
adapter = TeamsAdapter()
settings = get_teams_settings()


def _is_secret_valid(header_secret: str | None) -> bool:
    expected = str(settings.TEAMS_WEBHOOK_SECRET or "").strip()
    if not expected:
        # Shared-secret check is optional for local/dev usage.
        return True
    return str(header_secret or "").strip() == expected


def _is_outgoing_hmac_valid(authorization: str | None, raw_body: bytes, secret: str) -> bool:
    if not secret:
        # Signature verification is optional for local/dev usage.
        return True
    if not authorization:
        return False

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "hmac" or not token:
        return False

    try:
        key = base64.b64decode(secret)
    except (BinasciiError,):
        return False

    digest = hmac.new(key, raw_body, hashlib.sha256).digest()
    expected_token = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected_token, token.strip())


def _is_request_authorized(raw_body: bytes, x_teams_secret: str | None, authorization: str | None) -> bool:
    mode = str(settings.TEAMS_AUTH_MODE or "header-secret").strip().lower()
    if mode == "outgoing-hmac":
        return _is_outgoing_hmac_valid(
            authorization=authorization,
            raw_body=raw_body,
            secret=str(settings.TEAMS_WEBHOOK_SECRET or ""),
        )
    return _is_secret_valid(header_secret=x_teams_secret)


@router.post(
    "/webhook",
    responses={
        401: {"description": "Invalid Teams webhook secret"},
    },
)
async def ingest_teams_webhook(
    request: Request,
    payload: dict,
    x_teams_secret: Annotated[str | None, Header(alias="X-Teams-Secret")] = None,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> dict:
    raw_body = await request.body()
    if not _is_request_authorized(raw_body=raw_body, x_teams_secret=x_teams_secret, authorization=authorization):
        raise HTTPException(status_code=401, detail="Invalid Teams webhook secret")
    return await adapter.handle_activity(payload)
