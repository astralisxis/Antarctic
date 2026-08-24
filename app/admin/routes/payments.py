"""Платежи: список пополнений, сверка с провайдером, ручное зачисление.

Вся работа со счётами — в app/services/payments.py, тот же код, что и в боте.
Здесь только список, фильтры и кнопки.

Про уведомления: админка живёт отдельным процессом и в Telegram не пишет.
Ручное зачисление меняет баланс сразу, но сообщение клиенту не уходит —
он увидит деньги в профиле. Автоматическое зачисление уведомляет само,
это делает фоновый опрос в боте.
"""

from __future__ import annotations

from typing import Annotated, Any
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.counters import nav_counts
from app.admin.deps import DbSession, StaffAdmin
from app.admin.notice import flash
from app.admin.templating import render
from app.enums import PAYMENT_STATUS_TITLES, PROVIDER_TITLES, PaymentStatus
from app.integrations.pay import PayError
from app.models import Payment, User
from app.money import fmt_money
from app.services import payments as pay_service

router = APIRouter()

PER_PAGE = 40


@router.get("/payments")
async def index(
    request: Request,
    session: DbSession,
    admin: StaffAdmin,
    status: str = "",
    provider: str = "",
    q: str = "",
    page: int = 1,
):
    page = max(page, 1)
    where: list[Any] = []
    if status in PAYMENT_STATUS_TITLES:
        where.append(Payment.status == status)
    if provider in PROVIDER_TITLES:
        where.append(Payment.provider == provider)

    query = q.strip().lstrip("@")
    if query:
        conditions: list[Any] = [Payment.external_id == query]
        if query.isdigit() and len(query) <= 18:
            number = int(query)
            conditions += [Payment.id == number, User.tg_id == number]
        else:
            conditions.append(User.username.ilike(f"%{query}%"))
        where.append(or_(*conditions))

    base = (
        select(Payment.id)
        .select_from(Payment)
        .join(User, User.id == Payment.user_id, isouter=True)
        .where(*where)
    )
    total = int(await session.scalar(select(func.count()).select_from(base.subquery())) or 0)
    # Сумма по фильтру — то, ради чего в этот раздел и заходят: сверить деньги.
    credited = int(
        await session.scalar(
            select(func.coalesce(func.sum(Payment.credited), 0))
            .join(User, User.id == Payment.user_id, isouter=True)
            .where(*where, Payment.status == PaymentStatus.PAID.value)
        )
        or 0
    )

    result = await session.execute(
        select(Payment, User.tg_id, User.username)
        .join(User, User.id == Payment.user_id, isouter=True)
        .where(*where)
        .order_by(Payment.id.desc())
        .limit(PER_PAGE)
        .offset((page - 1) * PER_PAGE)
    )
    rows = [{"p": p, "tg_id": tg_id, "username": username} for p, tg_id, username in result.all()]

    return render(
        request,
        "payments.html",
        {
            "rows": rows,
            "total": total,
            "credited": credited,
            "page": page,
            "pages": max((total + PER_PAGE - 1) // PER_PAGE, 1),
            "statuses": PAYMENT_STATUS_TITLES,
            "providers": PROVIDER_TITLES,
            "ways": {m.provider: m.title for m in await pay_service.methods(session)},
            "f": {"status": status, "provider": provider, "q": q},
            "counts": await nav_counts(session),
        },
        active="payments",
    )


# --------------------------------------------------------------------------- #
#  Действия
# --------------------------------------------------------------------------- #
def _back(request: Request) -> RedirectResponse:
    """Возврат туда, откуда нажали: фильтры и страница не должны сбрасываться.

    Из Referer берём только путь с запросом и только свой раздел — чужой адрес
    в этом заголовке подставляется клиентом, уводить по нему нельзя.
    """
    referer = urlsplit(request.headers.get("referer") or "")
    if referer.path.startswith("/payments"):
        return RedirectResponse(urlunsplit(("", "", referer.path, referer.query, "")), 303)
    return RedirectResponse("/payments", status_code=303)


async def _load(session: AsyncSession, payment_id: int) -> Payment | None:
    return await session.get(Payment, payment_id)


@router.post("/payments/{payment_id}/check")
async def check(request: Request, session: DbSession, admin: StaffAdmin, payment_id: int):
    payment = await _load(session, payment_id)
    if payment is None:
        flash(request, f"Счёта №{payment_id} нет.", ok=False)
        return _back(request)
    if payment.status != PaymentStatus.PENDING.value:
        flash(request, "Счёт уже закрыт — проверять нечего.", ok=False)
        return _back(request)

    try:
        credited = await pay_service.refresh(session, payment)
    except PayError as exc:
        flash(request, f"Платёжный сервис не ответил: {exc}", ok=False)
        return _back(request)

    # Сессия админки сама не коммитит (app/admin/deps.py), а зачисление уже в ней.
    await session.commit()
    if credited is not None:
        flash(request, f"Оплата пришла, зачислено {fmt_money(credited.amount)}.")
    elif payment.status == PaymentStatus.PENDING.value:
        flash(request, "Оплаты ещё нет, счёт продолжает ждать.")
    else:
        flash(request, f"Счёт закрыт: {PAYMENT_STATUS_TITLES.get(payment.status, payment.status)}.")
    return _back(request)


@router.post("/payments/{payment_id}/credit")
async def credit(
    request: Request,
    session: DbSession,
    admin: StaffAdmin,
    payment_id: int,
    comment: Annotated[str, Form()] = "",
):
    """Зачислить вручную: деньги пришли, а провайдер этого не показывает."""
    payment = await _load(session, payment_id)
    if payment is None:
        flash(request, f"Счёта №{payment_id} нет.", ok=False)
        return _back(request)
    if payment.status != PaymentStatus.PENDING.value:
        flash(request, "Зачислять можно только ожидающий счёт.", ok=False)
        return _back(request)

    note = comment.strip()[:200] or f"зачислено вручную ({admin.login})"
    credited = await pay_service.mark_paid(
        session, payment, admin_id=admin.id, comment=note
    )
    await session.commit()
    if credited is None:
        flash(request, "Счёт закрылся раньше — повторно не зачисляем.", ok=False)
    else:
        flash(
            request,
            f"Зачислено {fmt_money(credited.amount)}. "
            "Клиент увидит баланс в профиле, сообщение из админки не уходит.",
        )
    return _back(request)


@router.post("/payments/{payment_id}/expire")
async def expire(request: Request, session: DbSession, admin: StaffAdmin, payment_id: int):
    """Снять счёт: у провайдера и у себя. Оплатить его после этого нельзя."""
    payment = await _load(session, payment_id)
    if payment is None:
        flash(request, f"Счёта №{payment_id} нет.", ok=False)
        return _back(request)
    if payment.status != PaymentStatus.PENDING.value:
        flash(request, "Счёт уже закрыт.", ok=False)
        return _back(request)

    if await pay_service.cancel(session, payment, admin_id=admin.id):
        await session.commit()
        flash(request, f"Счёт №{payment.id} снят.")
    else:
        flash(request, "Не удалось снять счёт.", ok=False)
    return _back(request)


@router.post("/payments/poll")
async def poll(request: Request, session: DbSession, admin: StaffAdmin):
    """Обойти все ожидающие счёта разом. То же, что делает бот в фоне."""
    try:
        credited = await pay_service.poll_pending(session)
    except PayError as exc:
        flash(request, f"Платёжный сервис не ответил: {exc}", ok=False)
        return _back(request)

    await session.commit()
    if not credited:
        flash(request, "Новых оплат нет.")
    else:
        total = sum(item.amount for item in credited)
        flash(request, f"Зачислено счётов: {len(credited)} на {fmt_money(total)}.")
    return _back(request)
