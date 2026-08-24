"""Пользователи: карточка клиента, баланс, ограничения, рефералы."""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, or_, select

from app.admin.counters import nav_counts
from app.admin.deps import DbSession, StaffAdmin
from app.admin.notice import flash
from app.admin.templating import render
from app.enums import LogSection
from app.models import BalanceTx, Order, Payment, User
from app.money import fmt_money, parse_rub
from app.services import balance, users as users_service
from app.services.events import log_event

router = APIRouter()

PER_PAGE = 40

SORTS: dict[str, Any] = {
    "new": User.id.desc(),
    "balance": User.balance.desc(),
    "spent": User.total_spent.desc(),
    "topup": User.total_topup.desc(),
    "seen": User.last_seen_at.desc(),
}
SORT_TITLES = {
    "new": "новые",
    "balance": "по балансу",
    "spent": "по покупкам",
    "topup": "по пополнениям",
    "seen": "по активности",
}


@router.get("/users")
async def index(
    request: Request,
    session: DbSession,
    admin: StaffAdmin,
    q: str = "",
    only: str = "",
    sort: str = "new",
    page: int = 1,
):
    page = max(page, 1)
    sort = sort if sort in SORTS else "new"
    where: list[Any] = []

    query = q.strip().lstrip("@")
    if query:
        like = f"%{query}%"
        parts = [User.username.ilike(like), User.first_name.ilike(like)]
        if query.isdigit() and len(query) <= 18:
            number = int(query)
            parts += [User.tg_id == number, User.id == number]
        where.append(or_(*parts))
    if only == "banned":
        where.append(User.is_banned.is_(True))
    elif only == "limited":
        where.append(
            or_(
                User.restrict_buy.is_(True),
                User.restrict_topup.is_(True),
                User.restrict_support.is_(True),
            )
        )
    elif only == "paid":
        where.append(User.total_topup > 0)

    total = int(await session.scalar(select(func.count()).select_from(User).where(*where)) or 0)
    rows = list(
        (
            await session.scalars(
                select(User)
                .where(*where)
                .order_by(SORTS[sort])
                .limit(PER_PAGE)
                .offset((page - 1) * PER_PAGE)
            )
        ).all()
    )
    return render(
        request,
        "users.html",
        {
            "rows": rows,
            "total": total,
            "page": page,
            "pages": max((total + PER_PAGE - 1) // PER_PAGE, 1),
            "sorts": SORT_TITLES,
            "f": {"q": q, "only": only, "sort": sort},
            "counts": await nav_counts(session),
        },
        active="users",
    )


@router.get("/users/{user_id}")
async def card(request: Request, session: DbSession, admin: StaffAdmin, user_id: int):
    user = await session.get(User, user_id)
    if user is None:
        flash(request, f"Клиента №{user_id} нет.", ok=False)
        return RedirectResponse("/users", status_code=303)

    orders = list(
        (
            await session.scalars(
                select(Order).where(Order.user_id == user.id).order_by(Order.id.desc()).limit(15)
            )
        ).all()
    )
    txs = list(
        (
            await session.scalars(
                select(BalanceTx)
                .where(BalanceTx.user_id == user.id)
                .order_by(BalanceTx.id.desc())
                .limit(15)
            )
        ).all()
    )
    payments = list(
        (
            await session.scalars(
                select(Payment)
                .where(Payment.user_id == user.id)
                .order_by(Payment.id.desc())
                .limit(10)
            )
        ).all()
    )
    referrer = await session.get(User, user.referrer_id) if user.referrer_id else None
    return render(
        request,
        "user.html",
        {
            "u": user,
            "orders": orders,
            "txs": txs,
            "payments": payments,
            "referrer": referrer,
            "invited": await users_service.referrals_count(session, user),
            "percent": await users_service.percent_for(session, user),
            "counts": await nav_counts(session),
        },
        active="users",
    )


# --------------------------------------------------------------------------- #
#  Действия
# --------------------------------------------------------------------------- #
@router.post("/users/{user_id}/balance")
async def change_balance(
    request: Request,
    session: DbSession,
    admin: StaffAdmin,
    user_id: int,
    amount: Annotated[str, Form()] = "",
    comment: Annotated[str, Form()] = "",
):
    user = await session.get(User, user_id)
    if user is None:
        return RedirectResponse("/users", status_code=303)

    raw = amount.strip()
    negative = raw.startswith("-") or raw.startswith("−")
    value = parse_rub(raw.lstrip("+-−"))
    if not value:
        flash(request, "Сумма не разобралась. Пример: 500 или -250.", ok=False)
        return RedirectResponse(f"/users/{user_id}", status_code=303)

    delta = -value if negative else value
    before = user.balance
    try:
        after = await balance.adjust(
            session, user, delta, admin_id=admin.id, comment=comment.strip()[:200] or None
        )
    except balance.NotEnoughMoney as exc:
        flash(request, f"Списать нечего: {exc}.", ok=False)
        return RedirectResponse(f"/users/{user_id}", status_code=303)

    await session.commit()
    # Показываем фактическое движение: списание упирается в остаток, и сообщение
    # «−999 999 ₽» при балансе 350 ₽ было бы неправдой.
    moved = after - before
    text = f"Баланс изменён на {fmt_money(moved, sign=True)}, стало {fmt_money(after)}."
    if moved != delta:
        text += f" Запрошено было {fmt_money(delta, sign=True)} — списали до нуля."
    flash(request, text)
    return RedirectResponse(f"/users/{user_id}", status_code=303)


@router.post("/users/{user_id}/ban")
async def ban(
    request: Request,
    session: DbSession,
    admin: StaffAdmin,
    user_id: int,
    reason: Annotated[str, Form()] = "",
    days: Annotated[str, Form()] = "",
):
    user = await session.get(User, user_id)
    if user is None:
        return RedirectResponse("/users", status_code=303)

    try:
        period = int(days.strip() or 0)
    except ValueError:
        period = 0

    user.is_banned = True
    user.ban_reason = reason.strip()[:255] or None
    user.banned_until = (
        dt.datetime.now(dt.UTC) + dt.timedelta(days=period) if period > 0 else None
    )
    await log_event(
        LogSection.ADMIN,
        "user_banned",
        user_id=user.id,
        admin_id=admin.id,
        message=f"{'на ' + str(period) + ' дн.' if period > 0 else 'бессрочно'}: {user.ban_reason or 'без причины'}",
        session=session,
    )
    await session.commit()
    flash(request, "Доступ к магазину закрыт.")
    return RedirectResponse(f"/users/{user_id}", status_code=303)


@router.post("/users/{user_id}/unban")
async def unban(request: Request, session: DbSession, admin: StaffAdmin, user_id: int):
    user = await session.get(User, user_id)
    if user is None:
        return RedirectResponse("/users", status_code=303)
    user.is_banned = False
    user.ban_reason = None
    user.banned_until = None
    await log_event(
        LogSection.ADMIN, "user_unbanned", user_id=user.id, admin_id=admin.id, session=session
    )
    await session.commit()
    flash(request, "Ограничение снято.")
    return RedirectResponse(f"/users/{user_id}", status_code=303)


@router.post("/users/{user_id}/settings")
async def user_settings(request: Request, session: DbSession, admin: StaffAdmin, user_id: int):
    user = await session.get(User, user_id)
    if user is None:
        return RedirectResponse("/users", status_code=303)

    form = await request.form()
    before = (user.restrict_buy, user.restrict_topup, user.restrict_support, user.ref_percent)

    user.restrict_buy = bool(form.get("restrict_buy"))
    user.restrict_topup = bool(form.get("restrict_topup"))
    user.restrict_support = bool(form.get("restrict_support"))

    percent_raw = str(form.get("ref_percent") or "").strip()
    if percent_raw:
        try:
            percent = int(percent_raw)
        except ValueError:
            flash(request, "Процент — целое число от 0 до 100.", ok=False)
            return RedirectResponse(f"/users/{user_id}", status_code=303)
        if not 0 <= percent <= 100:
            flash(request, "Процент — целое число от 0 до 100.", ok=False)
            return RedirectResponse(f"/users/{user_id}", status_code=303)
        user.ref_percent = percent
    else:
        user.ref_percent = None

    note = str(form.get("admin_note") or "").strip()
    user.admin_note = note[:2000] or None

    after = (user.restrict_buy, user.restrict_topup, user.restrict_support, user.ref_percent)
    if before != after:
        await log_event(
            LogSection.ADMIN,
            "user_limits",
            user_id=user.id,
            admin_id=admin.id,
            message=(
                f"покупки: {'нет' if user.restrict_buy else 'да'}, "
                f"пополнение: {'нет' if user.restrict_topup else 'да'}, "
                f"поддержка: {'нет' if user.restrict_support else 'да'}, "
                f"процент: {user.ref_percent if user.ref_percent is not None else 'общий'}"
            ),
            session=session,
        )
    await session.commit()
    flash(request, "Сохранено.")
    return RedirectResponse(f"/users/{user_id}", status_code=303)
