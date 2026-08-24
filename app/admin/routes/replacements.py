"""Гарантийные замены: решение администратора и закупка эквивалентного лота."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.admin.counters import nav_counts
from app.admin.deps import DbSession, StaffAdmin
from app.admin.notice import flash
from app.admin.templating import render
from app.models import Order, User
from app.services import orders as orders_service

router = APIRouter()


@router.get("/replacements")
async def index(request: Request, session: DbSession, admin: StaffAdmin):
    rows = list(
        (
            await session.scalars(
                select(Order)
                .where(Order.replacement_requested_at.is_not(None))
                .order_by(Order.replacement_requested_at.desc(), Order.id.desc())
                .limit(100)
            )
        ).all()
    )
    users = {}
    for order in rows:
        users[order.id] = await session.get(User, order.user_id)
    return render(
        request,
        "replacements.html",
        {"rows": rows, "users": users, "counts": await nav_counts(session)},
        active="replacements",
    )


@router.post("/replacements/{order_id}/approve")
async def approve(request: Request, session: DbSession, admin: StaffAdmin, order_id: int):
    order = await session.get(Order, order_id)
    if order is None:
        flash(request, f"Заказа №{order_id} нет.", ok=False)
        return RedirectResponse("/replacements", status_code=303)
    try:
        message = await orders_service.approve_replacement(session, order, admin_id=admin.id)
    except orders_service.OrderError as exc:
        flash(request, str(exc), ok=False)
    else:
        flash(request, message)
    return RedirectResponse("/replacements", status_code=303)


@router.post("/replacements/{order_id}/reject")
async def reject(
    request: Request,
    session: DbSession,
    admin: StaffAdmin,
    order_id: int,
    reason: Annotated[str, Form()] = "",
):
    order = await session.get(Order, order_id)
    if order is None:
        flash(request, f"Заказа №{order_id} нет.", ok=False)
        return RedirectResponse("/replacements", status_code=303)
    try:
        message = await orders_service.reject_replacement(
            session, order, reason=reason.strip()[:500] or None, admin_id=admin.id
        )
    except orders_service.OrderError as exc:
        flash(request, str(exc), ok=False)
    else:
        flash(request, message)
    return RedirectResponse("/replacements", status_code=303)


@router.post("/replacements/{order_id}/recheck")
async def recheck(request: Request, session: DbSession, admin: StaffAdmin, order_id: int):
    order = await session.get(Order, order_id)
    if order is None:
        flash(request, f"Заказа №{order_id} нет.", ok=False)
        return RedirectResponse("/replacements", status_code=303)
    try:
        message = await orders_service.recheck_replacement(session, order, admin_id=admin.id)
    except orders_service.OrderError as exc:
        flash(request, str(exc), ok=False)
    else:
        flash(request, message)
    return RedirectResponse("/replacements", status_code=303)


@router.post("/replacements/{order_id}/retry")
async def retry(request: Request, session: DbSession, admin: StaffAdmin, order_id: int):
    order = await session.get(Order, order_id)
    if order is None:
        flash(request, f"Заказа №{order_id} нет.", ok=False)
        return RedirectResponse("/replacements", status_code=303)
    try:
        message = await orders_service.retry_processing_replacement(
            session, order, admin_id=admin.id
        )
    except orders_service.OrderError as exc:
        flash(request, str(exc), ok=False)
    else:
        flash(request, message)
    return RedirectResponse("/replacements", status_code=303)
