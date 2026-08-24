"""Создать базу и настройки по умолчанию.

    python -m app.tools.initdb
"""

from __future__ import annotations

import asyncio

from app.config import settings
from app.db import create_all, dispose, session_scope
from app.services import settings_store


async def run() -> None:
    await create_all()
    async with session_scope() as session:
        added = await settings_store.ensure_defaults(session)
    print(f"база готова: {settings.database_url}")
    print(f"настроек добавлено: {added}")
    print("каталог стран пока пуст — заполнится через LZT: python -m app.tools.lzt_check countries")
    await dispose()


if __name__ == "__main__":
    asyncio.run(run())
