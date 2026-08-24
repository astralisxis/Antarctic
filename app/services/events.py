"""Журнал событий: одна запись в базу + строка в файловый лог.

Пишем через отдельную короткую сессию, чтобы падение лога не роняло бизнес-транзакцию.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Session
from app.enums import LogLevel, LogSection
from app.models import EventLog

file_log = logging.getLogger("events")


async def log_event(
    section: LogSection | str,
    event: str,
    *,
    level: LogLevel | str = LogLevel.INFO,
    user_id: int | None = None,
    admin_id: int | None = None,
    order_id: int | None = None,
    message: str | None = None,
    payload: dict[str, Any] | None = None,
    ip: str | None = None,
    session: AsyncSession | None = None,
) -> None:
    """Записать событие.

    session — передать, если нужно попасть в ту же транзакцию (например, списание
    баланса и лог должны быть атомарны). Иначе пишем отдельной сессией.
    """
    section_v = str(getattr(section, "value", section))
    level_v = str(getattr(level, "value", level))

    row = EventLog(
        section=section_v,
        level=level_v,
        event=event,
        user_id=user_id,
        admin_id=admin_id,
        order_id=order_id,
        message=(message or "")[:500] or None,
        payload=payload,
        ip=ip,
    )

    file_log.log(
        {"info": logging.INFO, "warn": logging.WARNING, "error": logging.ERROR}.get(
            level_v, logging.INFO
        ),
        "[%s] %s user=%s %s",
        section_v,
        event,
        user_id,
        message or "",
    )

    if session is not None:
        session.add(row)
        return

    try:
        async with Session() as own:
            own.add(row)
            await own.commit()
    except Exception:  # лог не должен ломать основной поток
        file_log.exception("не удалось записать событие в базу: %s/%s", section_v, event)
