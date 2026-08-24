"""Логи: единый журнал с фильтрами по разделам."""

from __future__ import annotations

from fastapi import APIRouter, Request
from sqlalchemy import func, select

from app.admin.counters import nav_counts
from app.admin.deps import DbSession, StaffAdmin
from app.admin.templating import SECTION_TITLES, render
from app.enums import LogLevel
from app.models import EventLog, User

router = APIRouter()

PER_PAGE = 60


@router.get("/logs")
async def logs(
    request: Request,
    session: DbSession,
    admin: StaffAdmin,
    section: str = "",
    level: str = "",
    event: str = "",
    user_id: str = "",
    page: int = 1,
):
    page = max(page, 1)
    user_id_value = int(user_id) if user_id.strip().isdigit() else None
    where = []
    if section in SECTION_TITLES:
        where.append(EventLog.section == section)
    if level in {lv.value for lv in LogLevel}:
        where.append(EventLog.level == level)
    if event.strip():
        where.append(EventLog.event.like(f"%{event.strip()}%"))
    if user_id_value:
        where.append(EventLog.user_id == user_id_value)

    total = int(await session.scalar(select(func.count()).select_from(EventLog).where(*where)) or 0)

    # Тянем имя пользователя одним запросом, без отдельного обращения на строку.
    result = await session.execute(
        select(EventLog, User.tg_id, User.username)
        .join(User, User.id == EventLog.user_id, isouter=True)
        .where(*where)
        .order_by(EventLog.id.desc())
        .limit(PER_PAGE)
        .offset((page - 1) * PER_PAGE)
    )
    rows = [
        {"log": log, "tg_id": tg_id, "username": username} for log, tg_id, username in result.all()
    ]

    pages = max((total + PER_PAGE - 1) // PER_PAGE, 1)
    return render(
        request,
        "logs.html",
        {
            "rows": rows,
            "total": total,
            "page": page,
            "pages": pages,
            "sections": SECTION_TITLES,
            "levels": [lv.value for lv in LogLevel],
            "f": {"section": section, "level": level, "event": event, "user_id": user_id},
            "counts": await nav_counts(session),
        },
        active="logs",
    )
