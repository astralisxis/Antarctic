"""Вход в админку.

Перебор пароля стоит адреса: десять неудач — и IP в бане до снятия в панели
(`app/services/admin_guard.py`). Короткий счётчик в памяти процесса остался
поверх этого — он тормозит частые попытки, не дожидаясь десятой.
"""

from __future__ import annotations

import datetime as dt
import time
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select

from app.admin.deps import SESSION_KEY, DbSession
from app.admin.templating import render
from app.enums import AdminRole, LogLevel, LogSection
from app.models import Admin
from app.security import verify_password
from app.services import admin_guard
from app.services.events import log_event

router = APIRouter()

# Примитивная защита от частых попыток: память процесса, без Redis.
# Бан по адресу — отдельно и в базе, см. app/services/admin_guard.py.
_ATTEMPTS: dict[str, list[float]] = {}
_WINDOW = 300.0
_LIMIT = 7


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "?"


# Прежнее имя оставлено: им пользуются другие модули админки.
_client_ip = client_ip


def _too_many(ip: str) -> bool:
    now = time.monotonic()
    hits = [t for t in _ATTEMPTS.get(ip, []) if now - t < _WINDOW]
    _ATTEMPTS[ip] = hits
    return len(hits) >= _LIMIT


def _register_attempt(ip: str) -> None:
    _ATTEMPTS.setdefault(ip, []).append(time.monotonic())


def _banned_page(request: Request, next: str, ip: str):
    """Экран для забаненного адреса: без формы, чтобы не было куда стучать."""
    return render(
        request,
        "login.html",
        {
            "next": next,
            "error": (
                "Адрес заблокирован за перебор пароля. Снять блокировку может "
                "администратор в панели."
            ),
            "banned_ip": ip,
        },
        status_code=403,
    )


@router.get("/login")
async def login_form(request: Request, session: DbSession, next: str = "/"):
    if request.session.get(SESSION_KEY):
        return RedirectResponse("/", status_code=303)
    ip = client_ip(request)
    if await admin_guard.banned(session, ip):
        return _banned_page(request, next, ip)
    return render(request, "login.html", {"next": next, "error": None})


@router.post("/login")
async def login_submit(
    request: Request,
    session: DbSession,
    login: Annotated[str, Form()],
    password: Annotated[str, Form()],
    next: Annotated[str, Form()] = "/",
):
    ip = client_ip(request)
    if await admin_guard.banned(session, ip):
        return _banned_page(request, next, ip)
    if _too_many(ip):
        return render(
            request,
            "login.html",
            {"next": next, "error": "Слишком много попыток. Попробуйте через пять минут."},
            status_code=429,
        )

    admin = await session.scalar(select(Admin).where(func.lower(Admin.login) == login.strip().lower()))
    ok = admin is not None and admin.is_active and verify_password(password, admin.password_hash)

    if not ok:
        _register_attempt(ip)
        row = await admin_guard.note_fail(session, ip, login.strip())
        await log_event(
            LogSection.ADMIN,
            "login_failed",
            level=LogLevel.WARN,
            message=f"неудачный вход: {login[:64]} ({row.fails} из {admin_guard.LIMIT})",
            ip=ip,
        )
        if row.banned:
            return _banned_page(request, next, ip)
        left = admin_guard.LIMIT - row.fails
        return render(
            request,
            "login.html",
            {
                "next": next,
                "error": "Логин или пароль не подходят."
                + (f" Осталось попыток: {left}." if left <= 4 else ""),
            },
            status_code=401,
        )

    assert admin is not None
    admin.last_login_at = dt.datetime.now(dt.UTC)
    await session.commit()

    request.session.clear()
    request.session[SESSION_KEY] = admin.id
    _ATTEMPTS.pop(ip, None)
    await admin_guard.note_success(session, ip)

    await log_event(
        LogSection.ADMIN, "login", admin_id=admin.id, message=f"вход {admin.login}", ip=ip
    )
    target = next if next.startswith("/") else "/"
    if admin.role == AdminRole.SUPPORT.value and not target.startswith("/support"):
        target = "/support"
    return RedirectResponse(target, status_code=303)


@router.post("/logout")
async def logout(request: Request):
    admin_id = request.session.get(SESSION_KEY)
    request.session.clear()
    if admin_id:
        await log_event(LogSection.ADMIN, "logout", admin_id=int(admin_id))
    return RedirectResponse("/login", status_code=303)
