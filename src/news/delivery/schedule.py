from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from news.delivery.models import ScheduleDef


def next_run(schedule: ScheduleDef, after: datetime) -> datetime:
    """The first scheduled time strictly after `after`, in UTC."""
    tz = ZoneInfo(schedule.tz)
    hh, mm = (int(x) for x in schedule.time.split(":"))
    local = after.astimezone(tz)
    day = local.date()
    for _ in range(9):
        candidate = datetime.combine(day, time(hh, mm), tz)
        if candidate > local and (schedule.cadence == "daily" or candidate.weekday() == schedule.weekday):
            return candidate.astimezone(UTC)
        day += timedelta(days=1)
    raise AssertionError("unreachable")


def edition_date(schedule: ScheduleDef, at: datetime) -> date:
    return at.astimezone(ZoneInfo(schedule.tz)).date()


def window_hours(schedule: ScheduleDef) -> int:
    """How far back a digest looks for stories (a little overlap)."""
    return 36 if schedule.cadence == "daily" else 7 * 24 + 12
