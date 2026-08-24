"""Зависимости админки: сессия админа и доступ."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.enums import AdminRole
from app.models import Admin

SESSION_KEY = "admin_id"


class NotAuthenticated(Exception):
    """Ловится обработчиком в main и превращается в редирект на вход."""

    def __init__(self, next_url: str = "/") -> None:
        self.next_url = next_url


DbSession = Annotated[AsyncSession, Depends(get_session)]


async def current_admin(request: Request, session: DbSession) -> Admin:
    admin_id = request.session.get(SESSION_KEY)
    if not admin_id:
        raise NotAuthenticated(str(request.url.path))

    admin = await session.get(Admin, int(admin_id))
    if admin is None or not admin.is_active:
        request.session.clear()
        raise NotAuthenticated(str(request.url.path))

    if admin.role not in {role.value for role in AdminRole}:
        # Неизвестная роль не должна случайно получить права после опечатки в БД
        # или появления новой роли без явно заданного контракта.
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Недостаточно прав")

    # Чтобы шаблоны видели админа без прокидывания в каждый контекст.
    request.state.admin = admin
    return admin


CurrentAdmin = Annotated[Admin, Depends(current_admin)]


def _deny() -> None:
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Недостаточно прав")


async def staff_admin(admin: CurrentAdmin) -> Admin:
    """Рабочая админка: владелец и администратор, но не поддержка."""
    if admin.role not in {AdminRole.OWNER.value, AdminRole.ADMIN.value}:
        _deny()
    return admin


async def owner_admin(admin: CurrentAdmin) -> Admin:
    """Необратимые системные операции доступны только владельцу."""
    if admin.role != AdminRole.OWNER.value:
        _deny()
    return admin


StaffAdmin = Annotated[Admin, Depends(staff_admin)]
OwnerAdmin = Annotated[Admin, Depends(owner_admin)]
