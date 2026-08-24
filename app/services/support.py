"""Поддержка: обращения из бота поддержки и переписка из админки.

Устройство переписки диктует сеть, а не вкус. Админка живёт отдельным процессом
и ходит в интернет через httpx, у которого в этом стеке нет socks-транспорта, —
значит отправить сообщение в Telegram она не может. Поэтому ответ админа
сохраняется в базу без tg_message_id, а разносит такие сообщения процесс бота
поддержки (aiohttp-socks у aiogram есть) фоновой задачей delivery_loop.

Обращение — не «письмо», а живая ветка: пока она открыта, все сообщения клиента
идут в неё же. Новая ветка создаётся, когда прошлую закрыли.

Слой без aiogram и без FastAPI: этими же функциями пользуются бот и админка.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import session_scope
from app.enums import LogLevel, LogSection, MessageSender, TicketStatus
from app.models import Ticket, TicketMessage, User
from app.services import settings_store
from app.services.events import log_event

log = logging.getLogger("support")

SUBJECT_LEN = 160
TEXT_MAX = 3000

# Как часто бот поддержки забирает из базы ответы админов.
DELIVERY_INTERVAL = 5.0

# Ветки, которые ещё в работе.
LIVE = (TicketStatus.OPEN.value, TicketStatus.ANSWERED.value)


class SupportError(Exception):
    """Ошибка с текстом, который можно показать как есть."""


@dataclass(frozen=True, slots=True)
class Outgoing:
    """Ответ админа, который ждёт отправки клиенту."""

    message_id: int
    ticket_id: int
    tg_id: int
    text: str


# --------------------------------------------------------------------------- #
#  Настройки раздела
# --------------------------------------------------------------------------- #
async def hours(session: AsyncSession) -> str:
    return (await settings_store.get(session, "support.hours") or "").strip() or "—"


async def auto_reply(session: AsyncSession) -> str | None:
    """Автоответ с подставленными часами работы."""
    template = (await settings_store.get(session, "support.auto_reply") or "").strip()
    if not template:
        return None
    return template.replace("{hours}", await hours(session))


# --------------------------------------------------------------------------- #
#  Ветка обращения
# --------------------------------------------------------------------------- #
async def active(session: AsyncSession, user: User) -> Ticket | None:
    return await session.scalar(
        select(Ticket)
        .where(Ticket.user_id == user.id, Ticket.status.in_(LIVE))
        .order_by(Ticket.id.desc())
        .limit(1)
    )


async def open_ticket(
    session: AsyncSession, user: User, *, subject: str | None = None
) -> tuple[Ticket, bool]:
    """Живая ветка клиента или новая. Второй элемент — «завели новую»."""
    if user.restrict_support:
        raise SupportError("Обращения для вашего аккаунта ограничены.")

    ticket = await active(session, user)
    if ticket is not None:
        return ticket, False

    ticket = Ticket(
        user_id=user.id,
        status=TicketStatus.OPEN.value,
        subject=(subject or "").strip()[:SUBJECT_LEN] or None,
    )
    session.add(ticket)
    await session.flush()
    await log_event(
        LogSection.SUPPORT,
        "ticket_opened",
        user_id=user.id,
        message=f"обращение №{ticket.id}",
        session=session,
    )
    return ticket, True


async def add_user_message(
    session: AsyncSession,
    ticket: Ticket,
    *,
    text: str | None = None,
    media_type: str | None = None,
    media_file_id: str | None = None,
    tg_message_id: int | None = None,
) -> TicketMessage:
    """Сообщение клиента. Ветка снова становится «ждёт ответа»."""
    body = (text or "").strip()[:TEXT_MAX] or None
    row = TicketMessage(
        ticket_id=ticket.id,
        sender=MessageSender.USER.value,
        text=body,
        media_type=media_type,
        media_file_id=media_file_id,
        tg_message_id=tg_message_id,
    )
    session.add(row)

    ticket.status = TicketStatus.OPEN.value
    ticket.unread_admin += 1
    ticket.last_message_at = dt.datetime.now(dt.UTC)
    if not ticket.subject and body:
        ticket.subject = body[:SUBJECT_LEN]

    await log_event(
        LogSection.SUPPORT,
        "user_message",
        user_id=ticket.user_id,
        message=f"обращение №{ticket.id}: {(body or media_type or '')[:200]}",
        session=session,
    )
    await session.commit()
    return row


async def add_admin_message(
    session: AsyncSession, ticket: Ticket, *, text: str, admin_id: int | None = None
) -> TicketMessage:
    """Ответ админа. Уйдёт клиенту, когда его подхватит бот поддержки."""
    body = (text or "").strip()[:TEXT_MAX]
    if not body:
        raise SupportError("Пустой ответ отправить нельзя.")
    if ticket.status == TicketStatus.CLOSED.value:
        raise SupportError("Обращение закрыто — сначала откройте его заново.")

    row = TicketMessage(
        ticket_id=ticket.id,
        sender=MessageSender.ADMIN.value,
        admin_id=admin_id,
        text=body,
    )
    session.add(row)

    ticket.status = TicketStatus.ANSWERED.value
    ticket.unread_admin = 0
    ticket.unread_user += 1
    ticket.last_message_at = dt.datetime.now(dt.UTC)
    if admin_id and not ticket.assigned_admin_id:
        ticket.assigned_admin_id = admin_id

    await log_event(
        LogSection.SUPPORT,
        "admin_message",
        user_id=ticket.user_id,
        admin_id=admin_id,
        message=f"обращение №{ticket.id}: {body[:200]}",
        session=session,
    )
    await session.commit()
    return row


async def add_system_message(
    session: AsyncSession, ticket: Ticket, *, text: str, delivered: bool = True
) -> TicketMessage:
    """Служебная строка в переписке: автоответ, закрытие.

    delivered=False — строку ещё надо донести клиенту (её возьмёт delivery_loop).
    """
    row = TicketMessage(
        ticket_id=ticket.id,
        sender=MessageSender.SYSTEM.value,
        text=text.strip()[:TEXT_MAX],
        tg_message_id=0 if delivered else None,
    )
    session.add(row)
    await session.commit()
    return row


async def mark_read(session: AsyncSession, ticket: Ticket) -> None:
    """Админ открыл ветку — непрочитанных для него больше нет."""
    if ticket.unread_admin:
        ticket.unread_admin = 0
        await session.commit()


async def close(
    session: AsyncSession, ticket: Ticket, *, admin_id: int | None = None, notify: bool = True
) -> None:
    if ticket.status == TicketStatus.CLOSED.value:
        raise SupportError("Обращение уже закрыто.")
    ticket.status = TicketStatus.CLOSED.value
    ticket.closed_at = dt.datetime.now(dt.UTC)
    ticket.unread_admin = 0
    await log_event(
        LogSection.SUPPORT,
        "ticket_closed",
        user_id=ticket.user_id,
        admin_id=admin_id,
        message=f"обращение №{ticket.id}",
        session=session,
    )
    await session.commit()
    if notify:
        await add_system_message(
            session,
            ticket,
            text="Обращение закрыто. Если вопрос остался — напишите ещё раз.",
            delivered=False,
        )


async def reopen(session: AsyncSession, ticket: Ticket, *, admin_id: int | None = None) -> None:
    if ticket.status != TicketStatus.CLOSED.value:
        raise SupportError("Обращение и так открыто.")
    ticket.status = TicketStatus.OPEN.value
    ticket.closed_at = None
    await log_event(
        LogSection.SUPPORT,
        "ticket_reopened",
        user_id=ticket.user_id,
        admin_id=admin_id,
        message=f"обращение №{ticket.id}",
        session=session,
    )
    await session.commit()


# --------------------------------------------------------------------------- #
#  Доставка ответов клиенту
# --------------------------------------------------------------------------- #
async def outbox(session: AsyncSession, limit: int = 20) -> list[Outgoing]:
    """Что админка написала, а клиент ещё не получил.

    Признак неотправленного — tg_message_id IS NULL: у сообщений клиента там его
    id из Telegram, у доставленных ответов — id нашей отправки.
    """
    rows = await session.execute(
        select(TicketMessage.id, TicketMessage.ticket_id, User.tg_id, TicketMessage.text)
        .join(Ticket, Ticket.id == TicketMessage.ticket_id)
        .join(User, User.id == Ticket.user_id)
        .where(
            TicketMessage.sender.in_(
                (MessageSender.ADMIN.value, MessageSender.SYSTEM.value)
            ),
            TicketMessage.tg_message_id.is_(None),
            TicketMessage.text.is_not(None),
            or_(User.auth_provider.is_(None), User.auth_provider == "telegram"),
        )
        .order_by(TicketMessage.id)
        .limit(limit)
    )
    return [
        Outgoing(message_id=mid, ticket_id=tid, tg_id=tg_id, text=text)
        for mid, tid, tg_id, text in rows.all()
        if text
    ]


async def mark_sent(session: AsyncSession, message_id: int, tg_message_id: int) -> None:
    row = await session.get(TicketMessage, message_id)
    if row is not None:
        # 0 — «отправлять не надо»: так помечаются и недоставленные ответы, чтобы
        # клиент не получил одно и то же дважды.
        row.tg_message_id = tg_message_id
        await session.commit()


async def deliver_pending(bot: Any, sender: Any, limit: int = 20) -> int:
    """Разнести ответы админов. sender(bot, item) отправляет одно сообщение.

    Каждое сообщение — своя короткая сессия: в sqlite писатель один, и держать
    транзакцию открытой на время сетевых отправок нельзя.
    """
    async with session_scope() as session:
        items = await outbox(session, limit=limit)

    done = 0
    for item in items:
        permanent = False
        try:
            tg_message_id = await sender(bot, item)
        except Exception as exc:
            # Временный сбой оставляем в очереди: следующий проход повторит
            # доставку. Навсегда закрываем только явный отказ Telegram (бот
            # заблокирован, чат удалён и т.п.), иначе ответ потеряется.
            await log_event(
                LogSection.SUPPORT,
                "deliver_failed",
                level=LogLevel.WARN,
                message=f"обращение №{item.ticket_id}: {exc}"[:400],
            )
            if _delivery_is_permanent(exc):
                tg_message_id = 0
                permanent = True
            else:
                tg_message_id = None
        if tg_message_id is None:
            # Не помечаем transient failure отправленным. При этом продолжаем
            # остальные элементы пачки, чтобы один сбой не блокировал очередь.
            continue
        async with session_scope() as session:
            await mark_sent(session, item.message_id, tg_message_id)
            ticket = await session.get(Ticket, item.ticket_id)
            if ticket is not None and ticket.unread_user:
                ticket.unread_user = max(ticket.unread_user - 1, 0)
        done += 1 if not permanent else 0
    return done


def _delivery_is_permanent(exc: Exception) -> bool:
    """Явные Telegram-отказы, после которых повторять отправку бессмысленно."""
    name = type(exc).__name__.lower()
    if any(marker in name for marker in ("forbidden", "badrequest", "notfound", "unauthorized")):
        return True
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "bot was blocked",
            "user is deactivated",
            "chat not found",
            "have no rights",
        )
    )


async def delivery_loop(bot: Any, sender: Any, interval: float = DELIVERY_INTERVAL) -> None:
    """Фоновая задача бота поддержки: раз в несколько секунд смотрит очередь."""
    log.info("доставка ответов поддержки раз в %.0f с", interval)
    while True:
        try:
            await deliver_pending(bot, sender)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("доставка ответов сорвалась, повторим через %.0f с", interval)
        await asyncio.sleep(interval)


# --------------------------------------------------------------------------- #
#  Выборки для админки
# --------------------------------------------------------------------------- #
async def listing(
    session: AsyncSession,
    *,
    status: str | None = None,
    query: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[tuple[Ticket, User]]:
    stmt = (
        select(Ticket, User)
        .join(User, User.id == Ticket.user_id)
        .order_by(Ticket.last_message_at.desc().nullslast(), Ticket.id.desc())
        .limit(limit)
        .offset(offset)
    )
    if status:
        stmt = stmt.where(Ticket.status == status)
    text = (query or "").strip()
    if text:
        like = f"%{text}%"
        conditions = [User.username.ilike(like), Ticket.subject.ilike(like)]
        if text.isdigit():
            conditions += [User.tg_id == int(text), Ticket.id == int(text)]
        stmt = stmt.where(or_(*conditions))
    rows = await session.execute(stmt)
    return [(ticket, user) for ticket, user in rows.all()]


async def thread(session: AsyncSession, ticket: Ticket) -> list[TicketMessage]:
    return list(
        (
            await session.scalars(
                select(TicketMessage)
                .where(TicketMessage.ticket_id == ticket.id)
                .order_by(TicketMessage.id)
            )
        ).all()
    )


async def counts(session: AsyncSession) -> dict[str, int]:
    rows = await session.execute(select(Ticket.status, func.count()).group_by(Ticket.status))
    out = {status.value: 0 for status in TicketStatus}
    for status, amount in rows.all():
        out[status] = amount
    out["total"] = sum(out.values())
    out["queued"] = await session.scalar(
        select(func.count())
        .select_from(TicketMessage)
        .join(Ticket, Ticket.id == TicketMessage.ticket_id)
        .join(User, User.id == Ticket.user_id)
        .where(
            TicketMessage.sender.in_(
                (MessageSender.ADMIN.value, MessageSender.SYSTEM.value)
            ),
            TicketMessage.tg_message_id.is_(None),
            or_(User.auth_provider.is_(None), User.auth_provider == "telegram"),
        )
    ) or 0
    return out
