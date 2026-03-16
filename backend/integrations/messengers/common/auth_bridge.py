import hashlib
import hmac


def build_backend_credentials_for_subject(subject_prefix: str, subject_id: str | int, secret: str) -> tuple[str, str]:
    normalized_subject = str(subject_id).strip()
    username = f"{subject_prefix}_{normalized_subject}"
    digest = hmac.new(secret.encode("utf-8"), normalized_subject.encode("utf-8"), hashlib.sha256).hexdigest()
    generated_secret = f"{subject_prefix.capitalize()}Bridge_{digest[:40]}"
    return username, generated_secret


def build_backend_credentials(telegram_user_id: int, secret: str) -> tuple[str, str]:
    # Backward-compatible wrapper used by Telegram bridge.
    username, password = build_backend_credentials_for_subject(
        subject_prefix="tg",
        subject_id=telegram_user_id,
        secret=secret,
    )
    return username, password
