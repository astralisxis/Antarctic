"""Заглушки разделов, которые появятся в следующих итерациях.

Пункт меню есть сразу — навигация не переезжает, когда раздел наполнится.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.admin.counters import nav_counts
from app.admin.deps import DbSession, StaffAdmin
from app.admin.templating import render

router = APIRouter()

PLAN: dict[str, tuple[str, str]] = {}


def _make(key: str):
    title, description = PLAN[key]

    async def handler(request: Request, session: DbSession, admin: StaffAdmin):
        return render(
            request,
            "soon.html",
            {"title": title, "description": description, "counts": await nav_counts(session)},
            active=key,
        )

    return handler


for _key in PLAN:
    router.add_api_route(f"/{_key}", _make(_key), methods=["GET"], name=f"soon_{_key}")
