from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

MONTHS = {
    "января": 1,
    "январь": 1,
    "февраля": 2,
    "февраль": 2,
    "марта": 3,
    "март": 3,
    "апреля": 4,
    "апрель": 4,
    "мая": 5,
    "май": 5,
    "июня": 6,
    "июнь": 6,
    "июля": 7,
    "июль": 7,
    "августа": 8,
    "август": 8,
    "сентября": 9,
    "сентябрь": 9,
    "октября": 10,
    "октябрь": 10,
    "ноября": 11,
    "ноябрь": 11,
    "декабря": 12,
    "декабрь": 12,
}

WEEKDAYS = {
    "понедельник": "mon",
    "понедельника": "mon",
    "вторник": "tue",
    "вторника": "tue",
    "среда": "wed",
    "среду": "wed",
    "среды": "wed",
    "четверг": "thu",
    "четверга": "thu",
    "пятница": "fri",
    "пятницу": "fri",
    "пятницы": "fri",
    "суббота": "sat",
    "субботу": "sat",
    "субботы": "sat",
    "воскресенье": "sun",
    "воскресенья": "sun",
}

WEEKDAY_INDEX = {
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}

MONTH_PATTERN = "|".join(sorted((re.escape(month) for month in MONTHS), key=len, reverse=True))
DATE_PATTERN = re.compile(rf"(\d{{1,2}})\s+({MONTH_PATTERN})(?:\s+(\d{{4}}))?")
TIME_HHMM_PATTERN = re.compile(r"в?\s*(\d{1,2}):(\d{2})")
TIME_SIMPLE_PATTERN = re.compile(r"в?\s*(\d{1,2})\s*(утра|дня|вечера|ночи)?")


@dataclass
class ScheduleParseResult:
    cron_expression: str
    is_one_time: bool
    run_at_iso: str | None


class ScheduleParserService:
    def parse(self, schedule_text: str, timezone_name: str = "Europe/Moscow") -> ScheduleParseResult:
        text = self._normalize(schedule_text)
        tz = self._resolve_timezone(timezone_name)
        now = datetime.now(tz)
        hour, minute = self._parse_time(text)

        recurring_dow = self._recurring_weekday(text)
        if recurring_dow:
            return ScheduleParseResult(
                cron_expression=f"{minute} {hour} * * {recurring_dow}",
                is_one_time=False,
                run_at_iso=None,
            )

        if self._is_daily(text):
            return ScheduleParseResult(
                cron_expression=f"{minute} {hour} * * *",
                is_one_time=False,
                run_at_iso=None,
            )

        target_dt = self._absolute_or_relative_datetime(text, now, hour, minute)
        if target_dt:
            run_utc = target_dt.astimezone(ZoneInfo("UTC"))
            return ScheduleParseResult(
                cron_expression=f"@once:{run_utc.isoformat()}",
                is_one_time=True,
                run_at_iso=run_utc.isoformat(),
            )

        raise ValueError(
            "Не удалось распознать время. Примеры: 'завтра в 9:00', '25 февраля в 9:00', 'каждый день в 9:00', 'каждую пятницу в 9:00'."
        )

    @staticmethod
    def _normalize(text: str) -> str:
        return re.sub(r"\s+", " ", (text or "").strip().lower().replace("ё", "е"))

    @staticmethod
    def _resolve_timezone(timezone_name: str) -> tzinfo:
        offset_match = re.fullmatch(r"UTC\s*([+-])(\d{1,2})(?::?(\d{2}))?", (timezone_name or "").strip(), re.IGNORECASE)
        if offset_match:
            sign = 1 if offset_match.group(1) == "+" else -1
            hours = int(offset_match.group(2))
            minutes = int(offset_match.group(3) or "0")
            delta = timedelta(hours=hours, minutes=minutes)
            return timezone(sign * delta)
        try:
            return ZoneInfo(timezone_name)
        except Exception:
            return ZoneInfo("Europe/Moscow")

    def _parse_time(self, text: str) -> tuple[int, int]:
        match = TIME_HHMM_PATTERN.search(text)
        if match:
            hour = int(match.group(1))
            minute = int(match.group(2))
        else:
            simple = TIME_SIMPLE_PATTERN.search(text)
            if not simple:
                return 9, 0
            hour = int(simple.group(1))
            minute = 0
            marker = (simple.group(2) or "").strip()
            if marker in {"дня", "вечера"} and hour < 12:
                hour += 12
            if marker == "ночи" and hour == 12:
                hour = 0

        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("Некорректное время")
        return hour, minute

    @staticmethod
    def _is_daily(text: str) -> bool:
        return any(token in text for token in ["каждый день", "ежедневно", "каждое утро", "каждый вечер"])

    def _recurring_weekday(self, text: str) -> str | None:
        if "кажд" not in text:
            return None
        for ru_name, dow in WEEKDAYS.items():
            if ru_name in text:
                return dow
        return None

    def _absolute_or_relative_datetime(self, text: str, now: datetime, hour: int, minute: int) -> datetime | None:
        if "сегодня" in text:
            dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            return dt if dt > now else dt + timedelta(days=1)

        if "завтра" in text:
            base = now + timedelta(days=1)
            return base.replace(hour=hour, minute=minute, second=0, microsecond=0)

        explicit_date = self._extract_explicit_date(text, now)
        if explicit_date:
            return explicit_date.replace(hour=hour, minute=minute, second=0, microsecond=0)

        weekday_once = self._extract_weekday_once(text, now)
        if weekday_once:
            return weekday_once.replace(hour=hour, minute=minute, second=0, microsecond=0)

        return None

    def _extract_explicit_date(self, text: str, now: datetime) -> datetime | None:
        match = DATE_PATTERN.search(text)
        if not match:
            return None

        day = int(match.group(1))
        month = MONTHS[match.group(2)]
        year = int(match.group(3)) if match.group(3) else now.year

        try:
            candidate = now.replace(year=year, month=month, day=day, hour=0, minute=0, second=0, microsecond=0)
        except ValueError:
            raise ValueError("Некорректная дата")

        if not match.group(3) and candidate.date() < now.date():
            candidate = candidate.replace(year=year + 1)
        return candidate

    def _extract_weekday_once(self, text: str, now: datetime) -> datetime | None:
        for ru_name, dow in WEEKDAYS.items():
            if ru_name not in text:
                continue
            target_idx = WEEKDAY_INDEX[dow]
            days_ahead = (target_idx - now.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7
            candidate = now + timedelta(days=days_ahead)
            return candidate.replace(hour=0, minute=0, second=0, microsecond=0)
        return None


schedule_parser_service = ScheduleParserService()
