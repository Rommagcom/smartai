from __future__ import annotations

import json
from typing import Any
from urllib import error as urllib_error

import pytest

from search_agent.memory.long_term import LongTermMemoryReliabilityPolicy, LongTermMemoryStore


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload, ensure_ascii=True).encode("utf-8")

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        del exc_type, exc, tb


def _build_store(**overrides: Any) -> LongTermMemoryStore:
    defaults = {
        "database_url": "sqlite+pysqlite:///:memory:",
        "ollama_base_url": "http://localhost:11434",
        "embedding_model": "nomic-embed-text:latest",
        "api_key": None,
        "top_k": 4,
        "max_entry_chars": 2000,
    }
    policy_defaults = {
        "embedding_timeout_seconds": 5,
        "embedding_retry_attempts": 3,
        "embedding_retry_base_delay_seconds": 0.1,
        "embedding_retry_max_delay_seconds": 0.5,
        "circuit_breaker_failure_threshold": 2,
        "circuit_breaker_recovery_seconds": 30,
        "retention_days": 90,
        "archive_batch_size": 100,
        "maintenance_interval_seconds": 60,
    }

    policy_override_keys = set(policy_defaults.keys())
    policy_overrides = {k: v for k, v in overrides.items() if k in policy_override_keys}
    ctor_overrides = {k: v for k, v in overrides.items() if k not in policy_override_keys}

    policy_defaults.update(policy_overrides)
    defaults.update(ctor_overrides)
    defaults["reliability_policy"] = LongTermMemoryReliabilityPolicy(**policy_defaults)
    return LongTermMemoryStore(**defaults)


def _raise_runtime_error(text: str) -> list[float]:
    raise RuntimeError(text)


def _raise_embed_failed(_: str) -> list[float]:
    raise RuntimeError("embed failed")


def test_embedding_retry_with_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _build_store(
        embedding_retry_attempts=3,
        embedding_retry_base_delay_seconds=0.1,
        embedding_retry_max_delay_seconds=0.2,
    )

    attempts = {"count": 0}
    sleep_calls: list[float] = []

    def fake_urlopen(req, timeout):  # type: ignore[no-untyped-def]
        del req, timeout
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise urllib_error.URLError("temporary")
        return _FakeResponse({"embedding": [0.1, 0.2, 0.3]})

    monkeypatch.setattr("search_agent.memory.long_term.urllib_request.urlopen", fake_urlopen)
    monkeypatch.setattr("search_agent.memory.long_term.time.sleep", lambda seconds: sleep_calls.append(seconds))

    embedding = store._embed_text("retry please")

    assert embedding == [0.1, 0.2, 0.3]
    assert attempts["count"] == 3
    assert sleep_calls == [0.1, 0.2]

    metrics = store.get_metrics_snapshot()
    assert int(metrics["embedding_success_total"]) == 1
    assert int(metrics["embedding_retries_total"]) == 2


def test_circuit_breaker_opens_after_consecutive_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _build_store(
        embedding_retry_attempts=1,
        circuit_breaker_failure_threshold=2,
        circuit_breaker_recovery_seconds=60,
    )

    attempts = {"count": 0}

    def always_fails(req, timeout):  # type: ignore[no-untyped-def]
        del req, timeout
        attempts["count"] += 1
        raise urllib_error.URLError("down")

    monkeypatch.setattr("search_agent.memory.long_term.urllib_request.urlopen", always_fails)

    with pytest.raises(RuntimeError):
        store._embed_text("1")
    with pytest.raises(RuntimeError):
        store._embed_text("2")
    with pytest.raises(RuntimeError):
        store._embed_text("3")

    assert attempts["count"] == 2
    metrics = store.get_metrics_snapshot()
    assert int(metrics["circuit_open_total"]) == 1
    assert int(metrics["circuit_open_rejections_total"]) == 1
    assert bool(metrics["circuit_is_open"]) is True


def test_remember_degrades_when_embedding_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _build_store()

    monkeypatch.setattr(store, "_embed_text", _raise_runtime_error)

    store.remember(
        org_id="acme",
        team_id="finance",
        user_id=10,
        chat_id=99,
        user_text="hi",
        assistant_text="hello",
        source="chat",
    )

    metrics = store.get_metrics_snapshot()
    assert int(metrics["remember_degraded_total"]) == 1


def test_recall_degrades_to_empty_on_embedding_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _build_store()

    monkeypatch.setattr(store, "_embed_text", _raise_embed_failed)

    results = store.recall(
        org_id="acme",
        team_id="finance",
        user_id=7,
        chat_id=42,
        query_text="budget",
        limit=3,
    )

    assert results == []
    metrics = store.get_metrics_snapshot()
    assert int(metrics["recall_total"]) == 1
    assert int(metrics["recall_degraded_total"]) == 1


def test_retention_maintenance_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _build_store(retention_days=30, maintenance_interval_seconds=600)

    archived_calls = {"count": 0}

    def fake_archive() -> int:
        archived_calls["count"] += 1
        return 5

    monkeypatch.setattr(store, "_archive_expired_memories", fake_archive)

    store._run_retention_maintenance_if_due()
    store._run_retention_maintenance_if_due()

    metrics = store.get_metrics_snapshot()
    assert archived_calls["count"] == 1
    assert int(metrics["retention_runs_total"]) == 1
    assert int(metrics["retention_archived_total"]) == 5
