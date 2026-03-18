from __future__ import annotations

from contextvars import ContextVar, Token

_current_user_id: ContextVar[str | None] = ContextVar("llm_usage_user_id", default=None)


def set_llm_usage_user_id(user_id: str | None) -> Token:
    return _current_user_id.set(user_id)


def reset_llm_usage_user_id(token: Token) -> None:
    _current_user_id.reset(token)


def get_llm_usage_user_id() -> str | None:
    return _current_user_id.get()
