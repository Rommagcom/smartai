import hashlib
import hmac


def build_backend_credentials(telegram_user_id: int, secret: str) -> tuple[str, str]:
    username = f"tg_{telegram_user_id}"
    digest = hmac.new(secret.encode("utf-8"), str(telegram_user_id).encode("utf-8"), hashlib.sha256).hexdigest()
    password = f"TgBridge_{digest[:40]}"
    return username, password
