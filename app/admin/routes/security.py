"""Безопасность входа: адреса, забаненные за перебор пароля.

Бан ставит `app/services/admin_guard.py` на десятой неудаче подряд и сам он не
истекает — снимают его здесь. Тут же видно и тех, кто пока только копит неудачи:
по списку логинов сразу понятно, перебор это или свои опечатки.

Снятие — через страницу подтверждения, как и всё остальное удаление в панели.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from app.admin.counters import nav_counts
from app.admin.deps import DbSession, OwnerAdmin
from app.admin.notice import flash
from app.admin.routes.auth import client_ip
from app.admin.templating import render
from app.models import AdminIpBan
from app.services import admin_guard

router = APIRouter()


@router.get("/security/ip")
async def index(request: Request, session: DbSession, admin: OwnerAdmin):
    return render(
        request,
        "ip_bans.html",
        {
            "rows": await admin_guard.rows(session),
            "stat": await admin_guard.counts(session),
            "limit": admin_guard.LIMIT,
            "quiet_hours": int(admin_guard.QUIET.total_seconds() // 3600),
            "my_ip": client_ip(request),
            "counts": await nav_counts(session),
        },
        active="settings",
    )


@router.get("/security/ip/{row_id}/unban")
async def unban_form(request: Request, session: DbSession, admin: OwnerAdmin, row_id: int):
    row = await session.get(AdminIpBan, row_id)
    if row is None:
        flash(request, "Такой записи уже нет.", ok=False)
        return RedirectResponse("/security/ip", status_code=303)
    return render(
        request,
        "ip_ban_delete.html",
        {
            "row": row,
            "limit": admin_guard.LIMIT,
            "mine": row.ip == client_ip(request),
            "counts": await nav_counts(session),
        },
        active="settings",
    )


@router.post("/security/ip/{row_id}/unban")
async def unban(request: Request, session: DbSession, admin: OwnerAdmin, row_id: int):
    row = await session.get(AdminIpBan, row_id)
    if row is None:
        flash(request, "Такой записи уже нет.", ok=False)
        return RedirectResponse("/security/ip", status_code=303)
    ip, was = row.ip, row.banned
    await admin_guard.unban(session, ip, admin_id=admin.id)
    flash(request, f"Бан с {ip} снят." if was else f"Счётчик неудач для {ip} сброшен.")
    return RedirectResponse("/security/ip", status_code=303)
