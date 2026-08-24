"""Обслуживание: обнуление накопленных данных после тестов.

Стирает `app/services/maintenance.py`, здесь только выбор групп и подтверждение.
Одной кнопкой этого не делаем: страница показывает, сколько строк уедет по
каждой группе, и требует набрать слово-подтверждение — вернуть удалённое
неоткуда.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app.admin.counters import nav_counts
from app.admin.deps import DbSession, OwnerAdmin
from app.admin.notice import flash
from app.admin.templating import render
from app.services import maintenance

router = APIRouter()

# Слово в поле подтверждения. Русское и короткое: печатается не глядя на раскладку.
CONFIRM = "ОБНУЛИТЬ"


@router.get("/maintenance")
async def index(request: Request, session: DbSession, admin: OwnerAdmin):
    return render(
        request,
        "maintenance.html",
        {
            "groups": maintenance.GROUPS,
            "rows": await maintenance.counts(session),
            "confirm": CONFIRM,
            "counts": await nav_counts(session),
        },
        active="settings",
    )


@router.post("/maintenance")
async def purge(
    request: Request,
    session: DbSession,
    admin: OwnerAdmin,
    confirm: Annotated[str, Form()] = "",
):
    form = await request.form()
    picked = [str(v) for v in form.getlist("group") if str(v) in maintenance.BY_KEY]

    if not picked:
        flash(request, "Ничего не отмечено — стирать нечего.", ok=False)
        return RedirectResponse("/maintenance", status_code=303)
    if confirm.strip().upper() != CONFIRM:
        flash(request, f"Не то слово в подтверждении — нужно «{CONFIRM}». Ничего не тронуто.", ok=False)
        return RedirectResponse("/maintenance", status_code=303)

    full = maintenance.expand(picked)
    added = [k for k in full if k not in picked]
    done = await maintenance.purge(session, full, admin_id=admin.id)

    total = sum(done.values())
    text = f"Обнулено, строк убрано: {total}."
    if added:
        titles = ", ".join(maintenance.BY_KEY[k].title.lower() for k in added)
        text += f" Вместе с выбранным уехало связанное: {titles}."
    flash(request, text)
    return RedirectResponse("/maintenance", status_code=303)
