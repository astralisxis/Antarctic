"""Точка входа админки.

Запуск:  python -m app.admin.main
или:     .venv/Scripts/uvicorn app.admin.main:app --reload --port 8080
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.sessions import SessionMiddleware

from app.admin.deps import NotAuthenticated
from app.admin.routes import (
    auth,
    broadcasts,
    catalog,
    dashboard,
    logs,
    maintenance,
    orders,
    payments,
    promos,
    reviews,
    replacements,
    security,
    settings as settings_routes,
    soon,
    support,
    users,
)
from app.admin.templating import STATIC_DIR
from app.config import settings
from app.db import create_all, dispose, session_scope
from app.enums import AdminRole, LogSection
from app.logging_setup import setup_logging
from app.models import Admin
from app.security import hash_password
from app.services import settings_store
from app.services.events import log_event

log = logging.getLogger("admin")


async def bootstrap_admin(session: AsyncSession) -> None:
    """Первый админ из .env, если в базе нет ни одного."""
    count = int(await session.scalar(select(func.count()).select_from(Admin)) or 0)
    if count:
        return
    if not settings.admin_password:
        log.warning(
            "админов нет, ADMIN_PASSWORD в .env пуст — создайте вход: "
            "python -m app.tools.create_admin <логин> <пароль>"
        )
        return
    session.add(
        Admin(
            login=settings.admin_login,
            password_hash=hash_password(settings.admin_password),
            role=AdminRole.OWNER.value,
        )
    )
    await session.flush()
    log.warning("создан первый админ «%s» с паролем из .env", settings.admin_login)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging("admin")
    if settings.db_auto_create:
        await create_all()
    async with session_scope() as session:
        added = await settings_store.ensure_defaults(session)
        await bootstrap_admin(session)
    if added:
        log.info("настроек по умолчанию добавлено: %s", added)
    await log_event(LogSection.SYSTEM, "admin_started", message=f"env={settings.env}")
    log.info("админка на http://%s:%s", settings.admin_host, settings.admin_port)
    yield
    await dispose()


app = FastAPI(
    title="Админка магазина номеров",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.admin_secret,
    session_cookie="admin_session",
    max_age=8 * 60 * 60,
    same_site="lax",
    https_only=settings.env == "prod",
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

app.include_router(auth.router)
app.include_router(broadcasts.router)
app.include_router(dashboard.router)
app.include_router(support.router)
app.include_router(orders.router)
app.include_router(users.router)
app.include_router(payments.router)
app.include_router(promos.router)
app.include_router(catalog.router)
app.include_router(reviews.router)
app.include_router(replacements.router)
app.include_router(settings_routes.router)
app.include_router(security.router)
app.include_router(maintenance.router)
app.include_router(logs.router)
app.include_router(soon.router)


@app.exception_handler(NotAuthenticated)
async def not_authenticated(request: Request, exc: NotAuthenticated):
    return RedirectResponse(f"/login?next={quote(exc.next_url or '/')}", status_code=303)


@app.get("/healthz", include_in_schema=False)
async def healthz():
    return {"ok": True, "env": settings.env}


def main() -> None:
    import uvicorn

    uvicorn.run(
        "app.admin.main:app",
        host=settings.admin_host,
        port=settings.admin_port,
        reload=settings.debug,
        log_config=None,
    )


if __name__ == "__main__":
    main()
