"""Проверка серии неверных входов: каждый запрос должен вернуть страницу входа."""

from __future__ import annotations

import asyncio
import secrets

import httpx

from app.admin.main import app


async def main() -> None:
    transport = httpx.ASGITransport(app=app)
    # Не переиспользуем адрес между запусками: защита входа хранит счётчик в БД.
    ip = f"198.51.100.{secrets.randbelow(240) + 10}"
    async with httpx.AsyncClient(transport=transport, base_url="http://smoke") as client:
        for attempt in range(1, 4):
            response = await client.post(
                "/login",
                data={"login": "wrong-smoke", "password": "wrong-password", "next": "/"},
                headers={"x-forwarded-for": ip},
            )
            assert response.status_code == 401, (attempt, response.status_code)
            assert "Логин или пароль не подходят" in response.text
            print(f"attempt {attempt}: {response.status_code}")


if __name__ == "__main__":
    asyncio.run(main())
