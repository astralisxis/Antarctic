"""Защита входа в админку: счётчик неудач по адресу и бан за перебор.

Считаем неудачные входы по IP. Десять подряд — адрес в бане, и снимается он
только в панели: подбор пароля никогда не «истекает сам», иначе перебор просто
подождёт. Тихие сутки счётчик обнуляют — свои опечатки не должны накапливаться
месяцами в один бан.

Живёт в базе, а не в памяти процесса: перезапуск админки не должен открывать
перебор заново, да и список забаненных нужно показать на экране.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import LogLevel, LogSection
from app.models import AdminIpBan
from app.services.events import log_event

# Столько неудач подряд — и адрес в бане.
LIMIT = 10
# Столько тишины — и счётчик начинается заново (свои опечатки не копятся).
QUIET = dt.timedelta(hours=24)
# Сколько логинов помним в строке: по ним видно, перебор это или своя опечатка.
LOGINS_KEPT = 5


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _utc(value: dt.datetime | None) -> dt.datetime | None:
    """Вернуть значение с UTC-зоной независимо от драйвера базы.

    SQLite не хранит timezone-aware datetime и после чтения отдаёт naive-дату.
    Сравнение такой даты с ``datetime.now(dt.UTC)`` вызывает TypeError на втором
    неверном входе в админку.
    """
    if value is None:
        return None
    return value.replace(tzinfo=dt.UTC) if value.tzinfo is None else value.astimezone(dt.UTC)


async def _row(session: AsyncSession, ip: str) -> AdminIpBan | None:
    return await session.scalar(select(AdminIpBan).where(AdminIpBan.ip == ip))


async def banned(session: AsyncSession, ip: str) -> AdminIpBan | None:
    """Запись бана, если адрес забанен. None — пускаем к форме."""
    row = await _row(session, ip)
    return row if row is not None and row.banned else None


async def note_fail(session: AsyncSession, ip: str, login: str) -> AdminIpBan:
    """Записать неудачный вход. На LIMIT-й ставит бан и пишет это в журнал."""
    now = _now()
    row = await _row(session, ip)
    if row is None:
        row = AdminIpBan(ip=ip[:64], fails=0, first_fail_at=now)
        session.add(row)
    elif not row.banned and row.last_fail_at and now - _utc(row.last_fail_at) > QUIET:
        # Сутки тишины — считаем заново, это уже не та серия.
        row.fails = 0
        row.logins = None
        row.first_fail_at = now

    row.fails += 1
    row.last_fail_at = now
    row.logins = _add_login(row.logins, login)

    if row.fails >= LIMIT and not row.banned:
        row.banned = True
        row.banned_at = now
        row.unbanned_by = None
        await log_event(
            LogSection.ADMIN,
            "login_ip_banned",
            level=LogLevel.WARN,
            message=f"перебор пароля: {row.fails} неудач, адрес забанен",
            payload={"logins": row.logins},
            ip=ip,
            session=session,
        )
    await session.commit()
    return row


def _add_login(saved: str | None, login: str) -> str | None:
    """Дописать логин к списку перебираемых, без повторов и в пределах поля."""
    value = (login or "").strip()[:32]
    if not value:
        return saved
    kept = [p for p in (saved or "").split(", ") if p]
    if value in kept:
        return saved
    kept.append(value)
    return ", ".join(kept[-LOGINS_KEPT:])[:255]


async def note_success(session: AsyncSession, ip: str) -> None:
    """Удачный вход снимает счётчик: серия закончилась, это был свой."""
    row = await _row(session, ip)
    if row is None or row.banned:
        return
    await session.delete(row)
    await session.commit()


async def unban(session: AsyncSession, ip: str, *, admin_id: int | None = None) -> bool:
    """Снять бан. False — такого адреса в списке нет."""
    row = await _row(session, ip)
    if row is None:
        return False
    was = row.banned
    await session.delete(row)
    await log_event(
        LogSection.ADMIN,
        "login_ip_unbanned",
        admin_id=admin_id,
        message=f"бан снят: {ip}" if was else f"счётчик неудач сброшен: {ip}",
        ip=ip,
        session=session,
    )
    await session.commit()
    return True


async def rows(session: AsyncSession, *, limit: int = 200) -> list[AdminIpBan]:
    """Забаненные сверху, потом те, кто ещё только копит неудачи."""
    return list(
        (
            await session.scalars(
                select(AdminIpBan)
                .order_by(AdminIpBan.banned.desc(), AdminIpBan.last_fail_at.desc())
                .limit(limit)
            )
        ).all()
    )


async def counts(session: AsyncSession) -> dict[str, int]:
    total = int(await session.scalar(select(func.count()).select_from(AdminIpBan)) or 0)
    hit = int(
        await session.scalar(
            select(func.count()).select_from(AdminIpBan).where(AdminIpBan.banned.is_(True))
        )
        or 0
    )
    return {"total": total, "banned": hit, "watching": total - hit}
