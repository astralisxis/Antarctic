"""Обзор: воронка, деньги, состояние заказов."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from app.admin.counters import nav_counts
from app.admin.deps import CurrentAdmin, DbSession
from app.admin.templating import render
from app.enums import AdminRole
from app.services import stats

router = APIRouter()


@router.get("/")
async def dashboard(request: Request, session: DbSession, admin: CurrentAdmin):
    if admin.role == AdminRole.SUPPORT.value:
        return RedirectResponse("/support", status_code=303)
    data = await stats.collect(session)
    return render(
        request,
        "dashboard.html",
        {"d": data, "counts": await nav_counts(session)},
        active="dashboard",
    )
