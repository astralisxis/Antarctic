"""Заказы: список, карточка, выдача кода, возврат, разбор зависших.

Все действия идут через app/services/orders.py — тот же код, что и в боте,
поэтому деньги и статусы считаются одинаково.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.counters import nav_counts
from app.admin.deps import DbSession, StaffAdmin
from app.admin.notice import flash
from app.admin.templating import render
from app.enums import ORDER_STATUS_TITLES, OrderStatus
from app.integrations.lzt import mask_phone
from app.models import CountryOffer, EventLog, Order, User
from app.services import orders as orders_service
from app.services.orders import IN_FLIGHT, STUCK_AFTER, OrderError

router = APIRouter()

PER_PAGE = 40


@router.get("/orders")
async def index(
    request: Request,
    session: DbSession,
    admin: StaffAdmin,
    status: str = "",
    offer: str = "",
    q: str = "",
    stuck: str = "",
    page: int = 1,
):
    page = max(page, 1)
    where: list[Any] = []
    if status in ORDER_STATUS_TITLES:
        where.append(Order.status == status)
    if offer:
        where.append(Order.offer_code == offer)

    query = q.strip().lstrip("№")
    if query:
        if query.isdigit() and len(query) <= 12:
            number = int(query)
            where.append(
                or_(
                    Order.id == number,
                    Order.lzt_item_id == number,
                    Order.phone.like(f"%{query}%"),
                )
            )
        else:
            where.append(Order.phone.like(f"%{query}%"))
    if stuck:
        where.append(Order.status.in_(IN_FLIGHT))
        where.append(Order.created_at < dt.datetime.now(dt.UTC) - STUCK_AFTER)

    total = int(await session.scalar(select(func.count()).select_from(Order).where(*where)) or 0)
    result = await session.execute(
        select(Order, User.tg_id, User.username)
        .join(User, User.id == Order.user_id, isouter=True)
        .where(*where)
        .order_by(Order.id.desc())
        .limit(PER_PAGE)
        .offset((page - 1) * PER_PAGE)
    )
    rows = [{"o": o, "tg_id": tg_id, "username": username} for o, tg_id, username in result.all()]

    codes = list(
        (
            await session.scalars(
                select(CountryOffer.code).order_by(CountryOffer.sort, CountryOffer.title)
            )
        ).all()
    )
    return render(
        request,
        "orders.html",
        {
            "rows": rows,
            "total": total,
            "page": page,
            "pages": max((total + PER_PAGE - 1) // PER_PAGE, 1),
            "statuses": ORDER_STATUS_TITLES,
            "codes": codes,
            "f": {"status": status, "offer": offer, "q": q, "stuck": stuck},
            "counts": await nav_counts(session),
        },
        active="orders",
    )


@router.get("/orders/{order_id}")
async def card(request: Request, session: DbSession, admin: StaffAdmin, order_id: int):
    order = await session.get(Order, order_id)
    if order is None:
        flash(request, f"Заказа №{order_id} нет.", ok=False)
        return RedirectResponse("/orders", status_code=303)

    user = await session.get(User, order.user_id)
    buyer = await session.get(User, order.buyer_user_id or order.user_id)
    events = list(
        (
            await session.scalars(
                select(EventLog).where(EventLog.order_id == order.id).order_by(EventLog.id.desc()).limit(30)
            )
        ).all()
    )
    hours = order.guarantee_hours or await orders_service.code_hours(session)
    return render(
        request,
        "order.html",
        {
            "o": order,
            "u": user,
            "buyer": buyer,
            "creds": orders_service.credentials(order),
            "events": events,
            "code_hours": hours,
            "code_until": orders_service.code_until(order, hours),
            "code_open": orders_service.code_open(order, hours),
            "in_flight": order.status in IN_FLIGHT,
            "delivered": order.status in orders_service.DELIVERED,
            "snapshot": _snapshot_rows(order),
            "replacement_status": order.replacement_status,
            "counts": await nav_counts(session),
        },
        active="orders",
    )


def _snapshot_rows(order: Order) -> list[tuple[str, Any]]:
    """Срез карточки лота без данных входа — они показываются отдельным блоком."""
    raw = order.lzt_raw or {}
    if not isinstance(raw, dict):
        return []
    return [(key, value) for key, value in raw.items() if key != "creds"]


# --------------------------------------------------------------------------- #
#  Действия
# --------------------------------------------------------------------------- #
async def _load(session: AsyncSession, order_id: int) -> Order | None:
    return await session.get(Order, order_id)


@router.post("/orders/{order_id}/code")
async def issue_code(request: Request, session: DbSession, admin: StaffAdmin, order_id: int):
    order = await _load(session, order_id)
    if order is None:
        return RedirectResponse("/orders", status_code=303)
    try:
        # hours=0 — срок самостоятельной выдачи поддержку не ограничивает.
        code = await orders_service.issue_code(session, order, hours=0, admin_id=admin.id)
    except OrderError as exc:
        flash(request, str(exc), ok=False)
    else:
        flash(request, f"Код {code} получен и виден клиенту в боте.")
    return RedirectResponse(f"/orders/{order_id}", status_code=303)


@router.post("/orders/{order_id}/validate")
async def validate_account(request: Request, session: DbSession, admin: StaffAdmin, order_id: int):
    order = await _load(session, order_id)
    if order is None:
        return RedirectResponse("/orders", status_code=303)
    try:
        valid = await orders_service.check_validity(session, order, admin_id=admin.id)
    except OrderError as exc:
        flash(request, str(exc), ok=False)
    else:
        flash(
            request,
            "Аккаунт действителен." if valid else "Аккаунт помечен как недействительный.",
            ok=valid,
        )
    return RedirectResponse(f"/orders/{order_id}", status_code=303)


@router.post("/orders/{order_id}/reset")
async def reset_auth(request: Request, session: DbSession, admin: StaffAdmin, order_id: int):
    order = await _load(session, order_id)
    if order is None:
        return RedirectResponse("/orders", status_code=303)
    try:
        await orders_service.reset_auth(session, order, admin_id=admin.id)
    except OrderError as exc:
        flash(request, str(exc), ok=False)
    else:
        flash(request, f"Сессии номера {mask_phone(order.phone)} сброшены.")
    return RedirectResponse(f"/orders/{order_id}", status_code=303)


@router.post("/orders/{order_id}/recheck")
async def recheck(request: Request, session: DbSession, admin: StaffAdmin, order_id: int):
    order = await _load(session, order_id)
    if order is None:
        return RedirectResponse("/orders", status_code=303)
    try:
        result = await orders_service.recheck(session, order, admin_id=admin.id)
    except OrderError as exc:
        flash(request, str(exc), ok=False)
    else:
        flash(request, result)
    return RedirectResponse(f"/orders/{order_id}", status_code=303)


@router.post("/orders/{order_id}/refund")
async def refund(
    request: Request,
    session: DbSession,
    admin: StaffAdmin,
    order_id: int,
    comment: Annotated[str, Form()] = "",
):
    order = await _load(session, order_id)
    if order is None:
        return RedirectResponse("/orders", status_code=303)
    try:
        await orders_service.refund(session, order, admin_id=admin.id, comment=comment.strip()[:200] or None)
    except OrderError as exc:
        flash(request, str(exc), ok=False)
    else:
        flash(request, "Деньги вернулись на баланс клиента.")
    return RedirectResponse(f"/orders/{order_id}", status_code=303)


@router.post("/orders/{order_id}/done")
async def mark_done(request: Request, session: DbSession, admin: StaffAdmin, order_id: int):
    """Отметить заказ завершённым: клиент подтвердил, что номер работает."""
    order = await _load(session, order_id)
    if order is None:
        return RedirectResponse("/orders", status_code=303)
    if order.status not in orders_service.DELIVERED:
        flash(request, "Завершать можно только оплаченный заказ.", ok=False)
    else:
        order.status = OrderStatus.DONE.value
        await session.commit()
        flash(request, "Заказ завершён.")
    return RedirectResponse(f"/orders/{order_id}", status_code=303)
