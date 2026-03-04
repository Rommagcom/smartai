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
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
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
    "понедельникам": "mon",
    "вторникам": "tue",
    "средам": "wed",
    "четвергам": "thu",
    "пятницам": "fri",
    "субботам": "sat",
    "воскресеньям": "sun",
    "monday": "mon",
    "mon": "mon",
    "tuesday": "tue",
    "tue": "tue",
    "wednesday": "wed",
    "wed": "wed",
    "thursday": "thu",
    "thurs": "thu",
    "thu": "thu",
    "friday": "fri",
    "fri": "fri",
    "saturday": "sat",
    "sat": "sat",
    "sunday": "sun",
    "sun": "sun",
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
TIME_AMPM_PATTERN = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", re.IGNORECASE)
TIME_SIMPLE_PATTERN = re.compile(r"в?\s*(\d{1,2})\s*(утра|дня|вечера|ночи)?")


@dataclass
class ScheduleParseResult:
    cron_expression: str
    is_one_time: bool
    run_at_iso: str | None


class ScheduleParserService:
    @staticmethod
    def _contains_token(text: str, token: str) -> bool:
        return re.search(rf"(?<!\w){re.escape(token)}(?!\w)", text) is not None

    def parse(self, schedule_text: str, timezone_name: str = "UTC +5") -> ScheduleParseResult:
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

        recurring_yearly = self._recurring_yearly(text)
        if recurring_yearly is not None:
            day, month = recurring_yearly
            return ScheduleParseResult(
                cron_expression=f"{minute} {hour} {day} {month} *",
                is_one_time=False,
                run_at_iso=None,
            )

        recurring_quarterly = self._recurring_quarterly(text)
        if recurring_quarterly is not None:
            day, months = recurring_quarterly
            return ScheduleParseResult(
                cron_expression=f"{minute} {hour} {day} {months} *",
                is_one_time=False,
                run_at_iso=None,
            )

        recurring_dom = self._recurring_monthly_day(text)
        if recurring_dom is not None:
            return ScheduleParseResult(
                cron_expression=f"{minute} {hour} {recurring_dom} * *",
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
            "Не удалось распознать время. Примеры: 'завтра в 9:00', 'через 30 минут', 'послезавтра в 10:00', "
            "'25 февраля в 9:00', 'каждый день в 9:00', 'каждую пятницу в 9:00'."
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
            return ZoneInfo("Asia/Almaty")

    def _parse_time(self, text: str) -> tuple[int, int]:
        match = TIME_HHMM_PATTERN.search(text)
        if match:
            hour = int(match.group(1))
            minute = int(match.group(2))
        else:
            ampm = TIME_AMPM_PATTERN.search(text)
            if ampm:
                hour = int(ampm.group(1))
                minute = int(ampm.group(2) or "0")
                marker = ampm.group(3).lower()
                if marker == "pm" and hour < 12:
                    hour += 12
                elif marker == "am" and hour == 12:
                    hour = 0
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
        return any(
            token in text
            for token in ["каждый день", "ежедневно", "каждое утро", "каждый вечер", "daily", "every day", "everyday", "every morning", "every evening"]
        )

    def _recurring_weekday(self, text: str) -> str | None:
        weekday: str | None = None
        for candidate, dow in WEEKDAYS.items():
            if self._contains_token(text, candidate):
                weekday = dow
                break
        if not weekday:
            return None

        recurring_hint = any(
            token in text
            for token in [
                "weekly",
                "every week",
                "every",
                "еженед",
                "кажд",
                "по понедельникам",
                "по вторникам",
                "по средам",
                "по четвергам",
                "по пятницам",
                "по субботам",
                "по воскресеньям",
            ]
        )
        if recurring_hint:
            return weekday
        return None

    @staticmethod
    def _recurring_monthly_day(text: str) -> int | None:
        monthly_hint = any(
            token in text
            for token in [
                "monthly",
                "every month",
                "each month",
                "ежемесяч",
                "каждый месяц",
            ]
        )
        if not monthly_hint:
            return None

        day_match = (
            re.search(r"\bday\s*(\d{1,2})\b", text)
            or re.search(r"\bon\s+(\d{1,2})(?:st|nd|rd|th)?\b", text)
            or re.search(r"\b(\d{1,2})\s*(?:числа|число)\b", text)
            or re.search(r"\b(\d{1,2})(?:st|nd|rd|th)\b", text)
        )
        if not day_match:
            return None

        day = int(day_match.group(1))
        if 1 <= day <= 31:
            return day
        return None

    @staticmethod
    def _extract_month_number(text: str) -> int | None:
        explicit = DATE_PATTERN.search(text)
        if explicit:
            return MONTHS.get(explicit.group(2))

        month_match = (
            re.search(r"\bmonth\s*(\d{1,2})\b", text)
            or re.search(r"\bмесяц\s*(\d{1,2})\b", text)
            or re.search(r"\bmonth\s*[:=]\s*(\d{1,2})\b", text)
            or re.search(r"\bмесяц\s*[:=]\s*(\d{1,2})\b", text)
        )
        if month_match:
            value = int(month_match.group(1))
            if 1 <= value <= 12:
                return value

        for name, number in MONTHS.items():
            if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text):
                return number
        return None

    @staticmethod
    def _extract_day_number(text: str) -> int | None:
        explicit = DATE_PATTERN.search(text)
        if explicit:
            value = int(explicit.group(1))
            return value if 1 <= value <= 31 else None

        day_match = (
            re.search(r"\bday\s*(\d{1,2})\b", text)
            or re.search(r"\bon\s+(\d{1,2})(?:st|nd|rd|th)?\b", text)
            or re.search(r"\b(\d{1,2})\s*(?:числа|число)\b", text)
            or re.search(r"\b(\d{1,2})(?:st|nd|rd|th)\b", text)
        )
        if not day_match:
            return None
        value = int(day_match.group(1))
        return value if 1 <= value <= 31 else None

    def _recurring_yearly(self, text: str) -> tuple[int, int] | None:
        yearly_hint = any(
            token in text
            for token in ["yearly", "annual", "annually", "every year", "каждый год", "ежегод", "раз в год"]
        )
        if not yearly_hint:
            return None

        day = self._extract_day_number(text)
        month = self._extract_month_number(text)
        if day is None or month is None:
            return None
        return day, month

    def _recurring_quarterly(self, text: str) -> tuple[int, str] | None:
        quarterly_hint = any(
            token in text
            for token in ["quarterly", "every quarter", "ежекварт", "каждый квартал"]
        )
        if not quarterly_hint:
            return None

        day = self._extract_day_number(text)
        if day is None:
            return None

        start_month = self._extract_month_number(text) or 1
        quarter_start = ((start_month - 1) // 3) * 3 + 1
        months: list[int] = []
        value = quarter_start
        for _ in range(4):
            months.append(value)
            value += 3
            if value > 12:
                value -= 12
        month_expr = ",".join(str(item) for item in months)
        return day, month_expr

    def _absolute_or_relative_datetime(self, text: str, now: datetime, hour: int, minute: int) -> datetime | None:
        relative = self._extract_relative_offset(text, now)
        if relative is not None:
            return relative

        if "сегодня" in text or "today" in text:
            dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            return dt if dt > now else dt + timedelta(days=1)

        if "послезавтра" in text or "day after tomorrow" in text:
            base = now + timedelta(days=2)
            return base.replace(hour=hour, minute=minute, second=0, microsecond=0)

        if "завтра" in text or "tomorrow" in text:
            base = now + timedelta(days=1)
            return base.replace(hour=hour, minute=minute, second=0, microsecond=0)

        explicit_date = self._extract_explicit_date(text, now)
        if explicit_date:
            return explicit_date.replace(hour=hour, minute=minute, second=0, microsecond=0)

        weekday_once = self._extract_weekday_once(text, now)
        if weekday_once:
            return weekday_once.replace(hour=hour, minute=minute, second=0, microsecond=0)

        # Fallback: explicit time mentioned (HH:MM or am/pm) but no date anchor
        # → treat as today at that time; if already past, push to tomorrow.
        if TIME_HHMM_PATTERN.search(text) or TIME_AMPM_PATTERN.search(text):
            dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            return dt if dt > now else dt + timedelta(days=1)

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

    @staticmethod
    def _extract_relative_offset(text: str, now: datetime) -> datetime | None:
        """Parse relative offsets like 'через N минут', '+5 minutes', 'in 5 minutes'."""
        UNIT_MAP = {
            "минут": "minutes", "мин": "minutes",
            "час": "hours", "часа": "hours", "часов": "hours",
            "день": "days", "дня": "days", "дней": "days",
            "minute": "minutes", "minutes": "minutes", "min": "minutes", "mins": "minutes",
            "hour": "hours", "hours": "hours", "hr": "hours", "hrs": "hours",
            "day": "days", "days": "days",
        }
        unit_pattern = "|".join(sorted((re.escape(u) for u in UNIT_MAP), key=len, reverse=True))
        match = (
            re.search(rf"через\s+(\d+)\s*({unit_pattern})", text)
            or re.search(rf"\+\s*(\d+)\s*({unit_pattern})", text)
            or re.search(rf"\bin\s+(\d+)\s*({unit_pattern})\b", text)
        )
        if not match:
            return None
        amount = int(match.group(1))
        unit_key = match.group(2)
        unit = UNIT_MAP.get(unit_key)
        if not unit or amount <= 0:
            return None
        delta = timedelta(**{unit: amount})
        return now + delta

    def _extract_weekday_once(self, text: str, now: datetime) -> datetime | None:
        for ru_name, dow in WEEKDAYS.items():
            if not self._contains_token(text, ru_name):
                continue
            target_idx = WEEKDAY_INDEX[dow]
            days_ahead = (target_idx - now.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7
            candidate = now + timedelta(days=days_ahead)
            return candidate.replace(hour=0, minute=0, second=0, microsecond=0)
        return None


schedule_parser_service = ScheduleParserService()
