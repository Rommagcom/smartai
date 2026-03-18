from __future__ import annotations

from contextvars import ContextVar, Token

_current_user_id: ContextVar[str | None] = ContextVar("llm_usage_user_id", default=None)
_buffered_tokens: ContextVar[int] = ContextVar("llm_usage_tokens", default=0)


def set_llm_usage_user_id(user_id: str | None) -> Token:
    return _current_user_id.set(user_id)


def reset_llm_usage_user_id(token: Token) -> None:
    _current_user_id.reset(token)


def get_llm_usage_user_id() -> str | None:
    return _current_user_id.get()


def add_llm_usage_tokens(tokens: int) -> None:
    safe_tokens = int(tokens or 0)
    if safe_tokens <= 0:
        return
    current = int(_buffered_tokens.get() or 0)
    _buffered_tokens.set(current + safe_tokens)


def pop_llm_usage_tokens() -> int:
    current = int(_buffered_tokens.get() or 0)
    _buffered_tokens.set(0)
    return max(0, current)
