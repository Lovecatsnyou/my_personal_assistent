"""Reminder schedule rules: parsing of user input and calculation of the next UTC moment.

A schedule is anchored to a local date and time in an IANA timezone. Daily, weekly,
monthly and weekday rules repeat at the same local wall-clock time; hourly repeats
every real hour (3600 s) from the first moment, so DST changes do not shift it.
"""

import calendar
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import re
from zoneinfo import ZoneInfo, available_timezones

REPEATS = ("once", "daily", "weekly", "monthly", "weekdays", "hourly")
WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
HOUR = timedelta(hours=1)
_ZONES = {name.lower(): name for name in available_timezones()}


@dataclass(frozen=True)
class Schedule:
    timezone: str
    local_date: date
    local_time: time
    fold: int = 0
    repeat: str = "once"
    days: frozenset[int] = frozenset()


def find_timezone(name: str) -> str | None:
    """Canonical IANA name, case-insensitive; only names known to the tz database."""
    return _ZONES.get(name.strip().lower())


def parse_date(text: str, today: date) -> date:
    text = text.strip().lower()
    shortcuts = {"сегодня": 0, "завтра": 1, "послезавтра": 2}
    if text in shortcuts:
        return today + timedelta(days=shortcuts[text])
    try:
        if match := re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", text):
            return date(int(match[1]), int(match[2]), int(match[3]))
        if match := re.fullmatch(r"(\d{1,2})[./](\d{1,2})[./](\d{4})", text):
            return date(int(match[3]), int(match[2]), int(match[1]))
        if match := re.fullmatch(r"(\d{1,2})[./](\d{1,2})", text):
            # Without a year: the nearest such date from today (29.02 — the next leap year).
            day, month = int(match[1]), int(match[2])
            date(2000, month, day)  # validates day/month against a leap year
            year = today.year
            while True:
                if (month, day) != (2, 29) or calendar.isleap(year):
                    if date(year, month, day) >= today:
                        return date(year, month, day)
                year += 1
    except ValueError:
        raise ValueError("Такой даты нет в календаре.") from None
    raise ValueError("Не понял дату. Формат: ДД.ММ.ГГГГ, ДД.ММ или «завтра».")


def parse_time(text: str) -> time:
    match = re.fullmatch(r"(\d{1,2})[:.\s](\d{2})", text.strip())
    if not match or int(match[1]) > 23 or int(match[2]) > 59:
        raise ValueError("Не понял время. Формат: ЧЧ:ММ в 24-часовом виде, например 09:30.")
    return time(int(match[1]), int(match[2]))


def to_utc(day: date, moment: time, zone: str, fold: int = 0) -> datetime:
    """For a time skipped by DST, zoneinfo with fold=0 yields the later wall time (02:30 → 03:30)."""
    return datetime.combine(day, moment, ZoneInfo(zone)).replace(fold=fold).astimezone(timezone.utc)


def local_status(day: date, moment: time, zone: str) -> str:
    """'ok', 'ambiguous' (happens twice when clocks go back) or 'nonexistent' (skipped)."""
    naive = datetime.combine(day, moment)
    variants = [to_utc(day, moment, zone, fold) for fold in (0, 1)]
    real = [v for v in variants if v.astimezone(ZoneInfo(zone)).replace(tzinfo=None) == naive]
    if not real:
        return "nonexistent"
    return "ambiguous" if variants[0] != variants[1] else "ok"


def utc_offset(day: date, moment: time, zone: str, fold: int) -> str:
    offset = datetime.combine(day, moment, ZoneInfo(zone)).replace(fold=fold).utcoffset()
    minutes = int(offset.total_seconds()) // 60
    sign = "+" if minutes >= 0 else "-"
    return f"UTC{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


def next_run(schedule: Schedule, after: datetime) -> datetime | None:
    """First occurrence strictly after `after` (aware UTC), never before the anchor.

    Starts the search near `after`, so a long downtime does not replay missed occurrences.
    """
    anchor = to_utc(schedule.local_date, schedule.local_time, schedule.timezone, schedule.fold)
    if schedule.repeat == "once":
        return anchor if anchor > after else None
    if schedule.repeat == "hourly":
        if anchor > after:
            return anchor
        return anchor + HOUR * ((after - anchor) // HOUR + 1)
    local_after = after.astimezone(ZoneInfo(schedule.timezone)).date()
    if schedule.repeat == "monthly":
        start = max(schedule.local_date.year * 12 + schedule.local_date.month - 1,
                    local_after.year * 12 + local_after.month - 2)
        for index in range(start, start + 4):
            year, month = divmod(index, 12)
            month += 1
            day = date(year, month, min(schedule.local_date.day, calendar.monthrange(year, month)[1]))
            if day >= schedule.local_date:
                moment = to_utc(day, schedule.local_time, schedule.timezone, schedule.fold)
                if moment > after:
                    return moment
        raise AssertionError("monthly search window is too small")
    if schedule.repeat not in ("daily", "weekly", "weekdays") or (schedule.repeat != "daily" and not schedule.days):
        raise ValueError("Некорректное правило повторения.")
    day = max(schedule.local_date, local_after - timedelta(days=1))
    for _ in range(16):
        if schedule.repeat == "daily" or day.weekday() in schedule.days:
            moment = to_utc(day, schedule.local_time, schedule.timezone, schedule.fold)
            if moment > after:
                return moment
        day += timedelta(days=1)
    raise AssertionError("weekly search window is too small")


def format_local(moment: datetime, zone: str) -> str:
    return moment.astimezone(ZoneInfo(zone)).strftime("%d.%m.%Y %H:%M")


def describe(schedule: Schedule) -> str:
    if schedule.repeat == "once":
        return "без повторения"
    if schedule.repeat == "daily":
        return f"каждый день в {schedule.local_time:%H:%M}"
    if schedule.repeat == "weekly":
        return f"каждую неделю: {', '.join(WEEKDAYS[d] for d in sorted(schedule.days))} в {schedule.local_time:%H:%M}"
    if schedule.repeat == "weekdays":
        return f"по дням: {', '.join(WEEKDAYS[d] for d in sorted(schedule.days))} в {schedule.local_time:%H:%M}"
    if schedule.repeat == "monthly":
        tail = " (в коротких месяцах — в последний день)" if schedule.local_date.day > 28 else ""
        return f"каждый месяц {schedule.local_date.day}-го в {schedule.local_time:%H:%M}{tail}"
    return f"каждый час, начиная с {schedule.local_time:%H:%M}"
