"""Время: единый часовой пояс проекта.

На Windows нет системной базы зон, поэтому в зависимостях есть tzdata.
Если ключ TZ всё же не резолвится — работаем в UTC и пишем предупреждение,
но не роняем страницу.
"""

from __future__ import annotations

import datetime as dt
import logging
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import settings

log = logging.getLogger("time")


@lru_cache(maxsize=4)
def local_tz() -> dt.tzinfo:
    try:
        return ZoneInfo(settings.tz)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("часовой пояс «%s» не найден, работаем в UTC", settings.tz)
        return dt.UTC


def to_local(value: dt.datetime | None) -> dt.datetime | None:
    """Наивное время из базы считаем UTC — так его туда и писали."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.UTC)
    return value.astimezone(local_tz())


def as_utc(value: dt.datetime | None) -> dt.datetime | None:
    """То же приведение, но без смены пояса: для арифметики со сроками.

    sqlite возвращает наивное время, и сравнивать его с datetime.now(dt.UTC)
    нельзя — Python бросит TypeError.
    """
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)


def now_local() -> dt.datetime:
    return dt.datetime.now(local_tz())


def day_start(days_ago: int = 0) -> dt.datetime:
    """Начало суток по поясу проекта, приведённое к UTC."""
    start = now_local().replace(hour=0, minute=0, second=0, microsecond=0) - dt.timedelta(
        days=days_ago
    )
    return start.astimezone(dt.UTC)
