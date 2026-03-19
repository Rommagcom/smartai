from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Mapping
from uuid import uuid4
from zoneinfo import ZoneInfo
from croniter import croniter

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    and_,
    create_engine,
    delete,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Engine


_metadata = MetaData()
_reminders_table = Table(
    "reminders",
    _metadata,
    Column("id", String(36), primary_key=True),
    Column("chat_id", BigInteger, nullable=False, index=True),
    Column("title", Text, nullable=False),
    Column("prompt", Text, nullable=False),
    Column("notify_text", Text, nullable=False, server_default=""),
    Column("schedule_type", String(16), nullable=False),
    Column("timezone", String(64), nullable=False),
    Column("next_run_at", DateTime(timezone=True), nullable=True),
    Column("interval_seconds", Integer, nullable=True),
    Column("time_of_day", String(5), nullable=True),
    Column("cron_expr", String(128), nullable=True),
    Column("max_runs", Integer, nullable=True),
    Column("run_count", Integer, nullable=False, server_default="0"),
    Column("active", Boolean, nullable=False, server_default="true", index=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)


@dataclass(slots=True)
class ReminderRecord:
    id: str
    chat_id: int
    title: str
    prompt: str
    notify_text: str
    schedule_type: str
    timezone: str
    next_run_at: str
    interval_seconds: int | None
    time_of_day: str | None
    cron_expr: str | None
    max_runs: int | None
    run_count: int
    active: bool
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "chat_id": self.chat_id,
            "title": self.title,
            "prompt": self.prompt,
            "notify_text": self.notify_text,
            "schedule_type": self.schedule_type,
            "timezone": self.timezone,
            "next_run_at": self.next_run_at,
            "interval_seconds": self.interval_seconds,
            "time_of_day": self.time_of_day,
            "cron_expr": self.cron_expr,
            "max_runs": self.max_runs,
            "run_count": self.run_count,
            "active": self.active,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class ReminderStore:
    def __init__(self, db_url: str | None = None) -> None:
        resolved = self._resolve_database_url(db_url)
        if not resolved:
            raise RuntimeError(
                "REMINDER_DATABASE_URL is not configured. Set REMINDER_DATABASE_URL or DATABASE_URL."
            )

        self.engine: Engine = create_engine(
            resolved,
            future=True,
            pool_pre_ping=True,
        )

    @staticmethod
    def _resolve_database_url(explicit: str | None = None) -> str:
        value = (explicit or os.getenv("REMINDER_DATABASE_URL") or os.getenv("DATABASE_URL") or "").strip()
        if value.startswith("postgres://"):
            return "postgresql+psycopg://" + value.removeprefix("postgres://")
        if value.startswith("postgresql://"):
            return "postgresql+psycopg://" + value.removeprefix("postgresql://")
        return value

    def create_reminder(
        self,
        *,
        chat_id: int,
        prompt: str,
        title: str = "",
        notify_text: str = "",
        schedule_type: str = "once",
        once_at: str | None = None,
        interval_seconds: int | None = None,
        time_of_day: str | None = None,
        cron_expr: str | None = None,
        timezone: str = "UTC",
        max_runs: int | None = None,
    ) -> ReminderRecord:
        now = datetime.now(UTC)
        tz = self._validate_timezone(timezone)
        schedule_kind = schedule_type.strip().lower()
        next_run = self._compute_initial_next_run(
            schedule_type=schedule_kind,
            once_at=once_at,
            interval_seconds=interval_seconds,
            time_of_day=time_of_day,
            cron_expr=cron_expr,
            tz=tz,
            now=now,
        )

        reminder_id = str(uuid4())
        values = {
            "id": reminder_id,
            "chat_id": int(chat_id),
            "title": title.strip() or "Reminder",
            "prompt": prompt.strip(),
            "notify_text": notify_text.strip(),
            "schedule_type": schedule_kind,
            "timezone": tz.key,
            "next_run_at": next_run,
            "interval_seconds": int(interval_seconds) if interval_seconds is not None else None,
            "time_of_day": time_of_day.strip() if time_of_day else None,
            "cron_expr": cron_expr.strip() if cron_expr else None,
            "max_runs": int(max_runs) if max_runs is not None else None,
            "run_count": 0,
            "active": True,
            "created_at": now,
            "updated_at": now,
        }

        with self.engine.begin() as conn:
            conn.execute(insert(_reminders_table).values(**values))

        return self._from_raw(values)

    def list_reminders(self, *, chat_id: int | None = None, active_only: bool = True) -> list[ReminderRecord]:
        stmt = select(_reminders_table)
        if chat_id is not None:
            stmt = stmt.where(_reminders_table.c.chat_id == int(chat_id))
        if active_only:
            stmt = stmt.where(_reminders_table.c.active.is_(True))
        stmt = stmt.order_by(_reminders_table.c.next_run_at.asc())

        with self.engine.begin() as conn:
            rows = conn.execute(stmt).mappings().all()

        return [self._from_raw(dict(row)) for row in rows]

    def delete_reminder(self, reminder_id: str, *, chat_id: int | None = None) -> bool:
        stmt = delete(_reminders_table).where(_reminders_table.c.id == reminder_id)
        if chat_id is not None:
            stmt = stmt.where(_reminders_table.c.chat_id == int(chat_id))

        with self.engine.begin() as conn:
            result = conn.execute(stmt)
        return int(result.rowcount or 0) > 0

    def pop_due(self, *, limit: int = 10) -> list[ReminderRecord]:
        now = datetime.now(UTC)
        due: list[ReminderRecord] = []

        with self.engine.begin() as conn:
            stmt = (
                select(_reminders_table)
                .where(
                    and_(
                        _reminders_table.c.active.is_(True),
                        _reminders_table.c.next_run_at.is_not(None),
                        _reminders_table.c.next_run_at <= now,
                    )
                )
                .order_by(_reminders_table.c.next_run_at.asc())
                .limit(max(1, int(limit)))
                .with_for_update(skip_locked=True)
            )
            rows = conn.execute(stmt).mappings().all()

            for row in rows:
                raw = dict(row)
                due.append(self._from_raw(raw))

                raw["run_count"] = int(raw.get("run_count") or 0) + 1
                raw["updated_at"] = now

                next_dt = self._advance_next_run(raw, now=now)
                if next_dt is None:
                    raw["active"] = False
                    raw["next_run_at"] = None
                else:
                    raw["next_run_at"] = next_dt

                conn.execute(
                    update(_reminders_table)
                    .where(_reminders_table.c.id == str(raw.get("id")))
                    .values(
                        run_count=int(raw["run_count"]),
                        updated_at=raw["updated_at"],
                        active=bool(raw.get("active", False)),
                        next_run_at=raw.get("next_run_at"),
                    )
                )

        return due

    def _advance_next_run(self, raw: dict[str, Any], *, now: datetime) -> datetime | None:
        schedule_type = str(raw.get("schedule_type", "once")).strip().lower()
        tz = self._validate_timezone(str(raw.get("timezone", "UTC")))

        run_count = int(raw.get("run_count", 0))
        max_runs = raw.get("max_runs")
        if max_runs is not None and run_count >= int(max_runs):
            return None

        if schedule_type == "once":
            return None

        handlers = {
            "interval": self._advance_interval_next_run,
            "daily": self._advance_daily_next_run,
            "cron": self._advance_cron_next_run,
        }
        handler = handlers.get(schedule_type)
        if handler is None:
            return None
        return handler(raw=raw, tz=tz, now=now)

    def _advance_interval_next_run(self, *, raw: dict[str, Any], tz: ZoneInfo, now: datetime) -> datetime | None:
        del tz
        interval_seconds = int(raw.get("interval_seconds") or 0)
        if interval_seconds <= 0:
            return None
        prev_raw = raw.get("next_run_at")
        prev = self._as_utc(prev_raw) or now
        next_dt = prev
        while next_dt <= now:
            next_dt += timedelta(seconds=interval_seconds)
        return next_dt

    def _advance_daily_next_run(self, *, raw: dict[str, Any], tz: ZoneInfo, now: datetime) -> datetime | None:
        time_of_day = str(raw.get("time_of_day") or "").strip()
        if not time_of_day:
            return None
        return self._next_daily_time(time_of_day=time_of_day, tz=tz, now=now)

    def _advance_cron_next_run(self, *, raw: dict[str, Any], tz: ZoneInfo, now: datetime) -> datetime | None:
        cron_expr = str(raw.get("cron_expr") or "").strip()
        if not cron_expr:
            return None
        return self._next_cron_time(cron_expr=cron_expr, tz=tz, now=now)

    def _compute_initial_next_run(
        self,
        *,
        schedule_type: str,
        once_at: str | None,
        interval_seconds: int | None,
        time_of_day: str | None,
        cron_expr: str | None,
        tz: ZoneInfo,
        now: datetime,
    ) -> datetime:
        if schedule_type == "once":
            return self._compute_initial_once_next_run(once_at=once_at, tz=tz, now=now)
        if schedule_type == "interval":
            return self._compute_initial_interval_next_run(interval_seconds=interval_seconds, now=now)
        if schedule_type == "daily":
            return self._compute_initial_daily_next_run(time_of_day=time_of_day, tz=tz, now=now)
        if schedule_type == "cron":
            return self._compute_initial_cron_next_run(cron_expr=cron_expr, tz=tz, now=now)

        raise ValueError("schedule_type must be one of: once, interval, daily, cron")

    def _compute_initial_once_next_run(self, *, once_at: str | None, tz: ZoneInfo, now: datetime) -> datetime:
        if not once_at:
            raise ValueError("once_at is required for schedule_type=once")
        when = self._parse_iso_flexible(once_at, tz=tz)
        if when <= now:
            raise ValueError("once_at must be in the future")
        return when

    @staticmethod
    def _compute_initial_interval_next_run(*, interval_seconds: int | None, now: datetime) -> datetime:
        if interval_seconds is None:
            raise ValueError("interval_seconds is required for schedule_type=interval")
        seconds = int(interval_seconds)
        if seconds <= 0:
            raise ValueError("interval_seconds must be > 0")
        return now + timedelta(seconds=seconds)

    def _compute_initial_daily_next_run(self, *, time_of_day: str | None, tz: ZoneInfo, now: datetime) -> datetime:
        if not time_of_day:
            raise ValueError("time_of_day is required for schedule_type=daily")
        return self._next_daily_time(time_of_day=time_of_day, tz=tz, now=now)

    def _compute_initial_cron_next_run(self, *, cron_expr: str | None, tz: ZoneInfo, now: datetime) -> datetime:
        if not cron_expr or not cron_expr.strip():
            raise ValueError("cron_expr is required for schedule_type=cron")
        return self._next_cron_time(cron_expr=cron_expr, tz=tz, now=now)

    def _next_daily_time(self, *, time_of_day: str, tz: ZoneInfo, now: datetime) -> datetime:
        hour, minute = self._parse_hhmm(time_of_day)
        local_now = now.astimezone(tz)
        candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= local_now:
            candidate += timedelta(days=1)
        return candidate.astimezone(UTC)

    @staticmethod
    def _next_cron_time(*, cron_expr: str, tz: ZoneInfo, now: datetime) -> datetime:
        expression = cron_expr.strip()
        if not expression:
            raise ValueError("cron_expr must not be empty")

        local_now = now.astimezone(tz)
        try:
            itr = croniter(expression, local_now)
            next_local = itr.get_next(datetime)
        except Exception as exc:
            raise ValueError("cron_expr is invalid") from exc

        if next_local.tzinfo is None:
            next_local = next_local.replace(tzinfo=tz)
        return next_local.astimezone(UTC)

    @staticmethod
    def _parse_hhmm(value: str) -> tuple[int, int]:
        parts = value.strip().split(":")
        if len(parts) != 2:
            raise ValueError("time_of_day must be in HH:MM format")
        hour = int(parts[0])
        minute = int(parts[1])
        if hour < 0 or hour > 23 or minute < 0 or minute > 59:
            raise ValueError("time_of_day must be in HH:MM format")
        return hour, minute

    @staticmethod
    def _validate_timezone(tz_name: str) -> ZoneInfo:
        try:
            return ZoneInfo(tz_name)
        except Exception as exc:
            raise ValueError(f"Invalid timezone: {tz_name}") from exc

    @staticmethod
    def _parse_iso_flexible(value: str, *, tz: ZoneInfo) -> datetime:
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        return dt.astimezone(UTC)

    @staticmethod
    def _as_utc(value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=UTC)
            return value.astimezone(UTC)
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return None
            if raw.endswith("Z"):
                raw = raw[:-1] + "+00:00"
            try:
                parsed = datetime.fromisoformat(raw)
            except ValueError:
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC)
        return None

    @staticmethod
    def _iso_or_empty(value: Any) -> str:
        dt = ReminderStore._as_utc(value)
        return dt.isoformat() if dt is not None else ""

    @staticmethod
    def _from_raw(raw: Mapping[str, Any]) -> ReminderRecord:
        return ReminderRecord(
            id=str(raw.get("id", "")),
            chat_id=int(raw.get("chat_id", 0)),
            title=str(raw.get("title", "Reminder")),
            prompt=str(raw.get("prompt", "")).strip(),
            notify_text=str(raw.get("notify_text", "")).strip(),
            schedule_type=str(raw.get("schedule_type", "once")),
            timezone=str(raw.get("timezone", "UTC")),
            next_run_at=ReminderStore._iso_or_empty(raw.get("next_run_at")),
            interval_seconds=int(raw["interval_seconds"]) if raw.get("interval_seconds") is not None else None,
            time_of_day=str(raw.get("time_of_day")) if raw.get("time_of_day") is not None else None,
            cron_expr=str(raw.get("cron_expr")) if raw.get("cron_expr") is not None else None,
            max_runs=int(raw["max_runs"]) if raw.get("max_runs") is not None else None,
            run_count=int(raw.get("run_count", 0)),
            active=bool(raw.get("active", False)),
            created_at=ReminderStore._iso_or_empty(raw.get("created_at")),
            updated_at=ReminderStore._iso_or_empty(raw.get("updated_at")),
        )
