"""Поддержка: список обращений и переписка с клиентом.

Ответ здесь только записывается в базу — отправляет его процесс бота поддержки
(у админки нет socks-транспорта до Telegram). В ветке такие сообщения помечены
«в очереди», пока бот их не разнёс.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.admin.counters import nav_counts
from app.admin.deps import CurrentAdmin, DbSession
from app.admin.notice import flash
from app.admin.templating import render
from app.enums import TICKET_STATUS_TITLES
from app.models import Order, Ticket, User
from app.services import support as support_service

router = APIRouter()

PER_PAGE = 40


@router.get("/support")
async def index(
    request: Request,
    session: DbSession,
    admin: CurrentAdmin,
    status: str = "",
    q: str = "",
    page: int = 1,
):
    page = max(page, 1)
    picked = status if status in TICKET_STATUS_TITLES else ""
    stats = await support_service.counts(session)
    rows = await support_service.listing(
        session, status=picked or None, query=q, limit=PER_PAGE, offset=(page - 1) * PER_PAGE
    )
    total = stats.get(picked, 0) if picked else stats.get("total", 0)
    return render(
        request,
        "support.html",
        {
            "rows": [{"t": ticket, "u": user} for ticket, user in rows],
            "stats": stats,
            "statuses": TICKET_STATUS_TITLES,
            "f": {"status": picked, "q": q},
            "page": page,
            "pages": max((total + PER_PAGE - 1) // PER_PAGE, 1),
            "hours": await support_service.hours(session),
            "counts": await nav_counts(session),
        },
        active="support",
    )


@router.get("/support/{ticket_id}")
async def card(request: Request, session: DbSession, admin: CurrentAdmin, ticket_id: int):
    ticket = await session.get(Ticket, ticket_id)
    if ticket is None:
        flash(request, f"Обращения №{ticket_id} нет.", ok=False)
        return RedirectResponse("/support", status_code=303)

    user = await session.get(User, ticket.user_id)
    # Открыли ветку — значит прочитали: счётчик в меню должен упасть.
    await support_service.mark_read(session, ticket)
    # Поддержке достаточно переписки. Заказы, баланс и реквизиты аккаунта
    # принадлежат staff-разделам и не должны даже загружаться для этой роли.
    orders = []
    if admin.role != "support":
        orders = list(
            (
                await session.scalars(
                    select(Order)
                    .where(Order.user_id == ticket.user_id)
                    .order_by(Order.id.desc())
                    .limit(5)
                )
            ).all()
        )
    return render(
        request,
        "ticket.html",
        {
            "t": ticket,
            "u": user,
            "messages": await support_service.thread(session, ticket),
            "orders": orders,
            "counts": await nav_counts(session),
        },
        active="support",
    )


@router.post("/support/{ticket_id}/reply")
async def reply(
    request: Request,
    session: DbSession,
    admin: CurrentAdmin,
    ticket_id: int,
    text: Annotated[str, Form()] = "",
):
    ticket = await session.get(Ticket, ticket_id)
    if ticket is None:
        return RedirectResponse("/support", status_code=303)
    try:
        await support_service.add_admin_message(session, ticket, text=text, admin_id=admin.id)
    except support_service.SupportError as exc:
        flash(request, str(exc), ok=False)
    else:
        flash(request, "Ответ записан — бот поддержки отправит его клиенту.")
    return RedirectResponse(f"/support/{ticket_id}", status_code=303)


def _back(ticket_id: int, back: str) -> str:
    """Куда вернуться после действия: закрывают и из списка, и из переписки."""
    return "/support" if back == "list" else f"/support/{ticket_id}"


@router.post("/support/{ticket_id}/close")
async def close(
    request: Request,
    session: DbSession,
    admin: CurrentAdmin,
    ticket_id: int,
    back: Annotated[str, Form()] = "",
):
    ticket = await session.get(Ticket, ticket_id)
    if ticket is None:
        return RedirectResponse("/support", status_code=303)
    try:
        await support_service.close(session, ticket, admin_id=admin.id)
    except support_service.SupportError as exc:
        flash(request, str(exc), ok=False)
    else:
        flash(request, f"Обращение №{ticket_id} закрыто, клиенту уйдёт короткая пометка.")
    return RedirectResponse(_back(ticket_id, back), status_code=303)


@router.post("/support/{ticket_id}/reopen")
async def reopen(
    request: Request,
    session: DbSession,
    admin: CurrentAdmin,
    ticket_id: int,
    back: Annotated[str, Form()] = "",
):
    ticket = await session.get(Ticket, ticket_id)
    if ticket is None:
        return RedirectResponse("/support", status_code=303)
    try:
        await support_service.reopen(session, ticket, admin_id=admin.id)
    except support_service.SupportError as exc:
        flash(request, str(exc), ok=False)
    else:
        flash(request, f"Обращение №{ticket_id} открыто заново.")
    return RedirectResponse(_back(ticket_id, back), status_code=303)
