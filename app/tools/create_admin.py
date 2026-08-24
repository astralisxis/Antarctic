"""Создать или обновить вход в админку.

    python -m app.tools.create_admin admin МойПароль
    python -m app.tools.create_admin support Пароль --role support
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import func, select

from app.db import create_all, dispose, session_scope
from app.enums import AdminRole
from app.models import Admin
from app.security import hash_password


async def run(login: str, password: str, role: str) -> None:
    await create_all()
    async with session_scope() as session:
        existing = await session.scalar(
            select(Admin).where(func.lower(Admin.login) == login.lower())
        )
        if existing:
            existing.password_hash = hash_password(password)
            existing.role = role
            existing.is_active = True
            print(f"пароль обновлён: {login} ({role})")
        else:
            session.add(
                Admin(login=login, password_hash=hash_password(password), role=role)
            )
            print(f"создан админ: {login} ({role})")
    await dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Создать или обновить админа")
    parser.add_argument("login")
    parser.add_argument("password")
    parser.add_argument(
        "--role", default=AdminRole.ADMIN.value, choices=[r.value for r in AdminRole]
    )
    args = parser.parse_args()
    if len(args.password) < 8:
        raise SystemExit("пароль короче 8 символов — так не годится")
    asyncio.run(run(args.login.strip(), args.password, args.role))


if __name__ == "__main__":
    main()
