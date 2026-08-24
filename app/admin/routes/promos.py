"""Создание и управление промокодами."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.admin.counters import nav_counts
from app.admin.deps import DbSession, StaffAdmin
from app.admin.notice import flash
from app.admin.templating import render
from app.enums import LogSection
from app.models import PromoCode
from app.money import parse_rub
from app.services import promos
from app.services.events import log_event
from app.timeutil import local_tz

router = APIRouter()


@router.get("/promos")
async def index(request: Request, session: DbSession, admin: StaffAdmin):
    rows = list(
        (await session.scalars(select(PromoCode).order_by(PromoCode.id.desc()).limit(300))).all()
    )
    return render(
        request,
        "promos.html",
        {"rows": rows, "counts": await nav_counts(session)},
        active="promos",
    )


@router.post("/promos")
async def create(request: Request, session: DbSession, admin: StaffAdmin):
    form = await request.form()
    code = promos.normalize(str(form.get("code") or ""))
    title = str(form.get("title") or "").strip()[:120] or None
    bonus = parse_rub(str(form.get("bonus") or ""))
    max_uses_raw = str(form.get("max_uses") or "").strip()
    expires_raw = str(form.get("expires_at") or "").strip()
    if not promos.CODE_RE.fullmatch(code):
        flash(request, "Код: 3–32 латинских букв, цифр, _ или -.", ok=False)
        return RedirectResponse("/promos", status_code=303)
    if bonus is None or bonus <= 0:
        flash(request, "Бонус должен быть положительной суммой в рублях.", ok=False)
        return RedirectResponse("/promos", status_code=303)
    try:
        max_uses = int(max_uses_raw) if max_uses_raw else None
        if max_uses is not None and max_uses <= 0:
            raise ValueError
    except ValueError:
        flash(request, "Лимит активаций должен быть целым числом больше нуля.", ok=False)
        return RedirectResponse("/promos", status_code=303)
    expires_at = None
    if expires_raw:
        try:
            expires_at = dt.datetime.fromisoformat(expires_raw).replace(tzinfo=local_tz()).astimezone(dt.UTC)
        except ValueError:
            flash(request, "Не удалось разобрать срок действия.", ok=False)
            return RedirectResponse("/promos", status_code=303)
    if await session.scalar(select(PromoCode.id).where(PromoCode.code == code)):
        flash(request, "Промокод с таким кодом уже существует.", ok=False)
        return RedirectResponse("/promos", status_code=303)
    promo = PromoCode(
        code=code,
        title=title,
        bonus=bonus,
        max_uses=max_uses,
        expires_at=expires_at,
        created_by=admin.id,
    )
    session.add(promo)
    await session.flush()
    await log_event(
        LogSection.ADMIN,
        "promo_created",
        admin_id=admin.id,
        message=f"{code}, бонус {bonus} коп.",
        payload={"promo_id": promo.id, "max_uses": max_uses},
        session=session,
    )
    await session.commit()
    flash(request, f"Промокод {code} создан.")
    return RedirectResponse("/promos", status_code=303)


@router.post("/promos/{promo_id}/toggle")
async def toggle(
    request: Request, session: DbSession, admin: StaffAdmin, promo_id: int
):
    promo = await session.get(PromoCode, promo_id)
    if promo is None:
        flash(request, "Промокод не найден.", ok=False)
        return RedirectResponse("/promos", status_code=303)
    promo.is_active = not promo.is_active
    await session.commit()
    flash(request, f"{promo.code}: {'включён' if promo.is_active else 'выключен'}.")
    return RedirectResponse("/promos", status_code=303)
