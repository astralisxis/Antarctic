"""Рассылки: редактор, запуск, отмена и статистика доставки."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select

from app.admin.counters import nav_counts
from app.admin.deps import DbSession, StaffAdmin
from app.admin.notice import flash
from app.admin.templating import render
from app.enums import BROADCAST_STATUS_TITLES, DELIVERY_STATUS_TITLES
from app.models import Broadcast
from app.services import broadcasts as service

router = APIRouter()
PER_PAGE = 30


@router.get("/broadcasts")
async def index(
    request: Request,
    session: DbSession,
    admin: StaffAdmin,
    page: int = 1,
):
    page = max(page, 1)
    total = int(await session.scalar(select(func.count()).select_from(Broadcast)) or 0)
    return render(
        request,
        "broadcasts.html",
        {
            "rows": await service.listing(
                session, limit=PER_PAGE, offset=(page - 1) * PER_PAGE
            ),
            "stats": await service.status_counts(session),
            "audiences": service.AUDIENCES,
            "audience_counts": await service.audience_counts(session),
            "statuses": BROADCAST_STATUS_TITLES,
            "page": page,
            "pages": max((total + PER_PAGE - 1) // PER_PAGE, 1),
            "counts": await nav_counts(session),
        },
        active="broadcasts",
    )


@router.post("/broadcasts")
async def create(
    request: Request,
    session: DbSession,
    admin: StaffAdmin,
    title: Annotated[str, Form()] = "",
    text: Annotated[str, Form()] = "",
    audience: Annotated[str, Form()] = "all",
    buttons: Annotated[str, Form()] = "",
    image: Annotated[UploadFile | None, File()] = None,
):
    try:
        picture = None
        if image is not None and image.filename:
            blob = await image.read(service.IMAGE_LIMIT + 1)
            picture = service.normalize_image(blob)
        row = await service.create(
            session,
            admin_id=admin.id,
            title=title,
            text=text,
            audience=audience,
            buttons=service.parse_buttons(buttons),
            image=picture,
        )
    except service.BroadcastError as exc:
        flash(request, str(exc), ok=False)
        return RedirectResponse("/broadcasts", status_code=303)
    flash(request, f"Черновик рассылки №{row.id} сохранён. Проверьте его и запустите.")
    return RedirectResponse(f"/broadcasts/{row.id}", status_code=303)


@router.get("/broadcasts/{broadcast_id}")
async def detail(
    request: Request,
    session: DbSession,
    admin: StaffAdmin,
    broadcast_id: int,
):
    row = await session.get(Broadcast, broadcast_id)
    if row is None:
        flash(request, f"Рассылки №{broadcast_id} нет.", ok=False)
        return RedirectResponse("/broadcasts", status_code=303)
    return render(
        request,
        "broadcast_detail.html",
        {
            "b": row,
            "audiences": service.AUDIENCES,
            "statuses": BROADCAST_STATUS_TITLES,
            "delivery_statuses": DELIVERY_STATUS_TITLES,
            "errors": await service.delivery_errors(session, row.id),
            "counts": await nav_counts(session),
        },
        active="broadcasts",
    )


@router.post("/broadcasts/{broadcast_id}/start")
async def start(
    request: Request,
    session: DbSession,
    admin: StaffAdmin,
    broadcast_id: int,
):
    row = await session.get(Broadcast, broadcast_id)
    if row is None:
        flash(request, f"Рассылки №{broadcast_id} нет.", ok=False)
        return RedirectResponse("/broadcasts", status_code=303)
    try:
        total = await service.start(session, row, admin_id=admin.id)
    except service.BroadcastError as exc:
        flash(request, str(exc), ok=False)
    else:
        if total:
            flash(request, f"Рассылка запущена: в очереди {total} получателей.")
        else:
            flash(request, "В этой аудитории нет получателей — рассылка завершена без отправки.")
    return RedirectResponse(f"/broadcasts/{broadcast_id}", status_code=303)


@router.post("/broadcasts/{broadcast_id}/cancel")
async def cancel(
    request: Request,
    session: DbSession,
    admin: StaffAdmin,
    broadcast_id: int,
):
    row = await session.get(Broadcast, broadcast_id)
    if row is None:
        flash(request, f"Рассылки №{broadcast_id} нет.", ok=False)
        return RedirectResponse("/broadcasts", status_code=303)
    try:
        await service.cancel(session, row, admin_id=admin.id)
    except service.BroadcastError as exc:
        flash(request, str(exc), ok=False)
    else:
        flash(request, "Рассылка отменена. Уже доставленные сообщения остаются у получателей.")
    return RedirectResponse(f"/broadcasts/{broadcast_id}", status_code=303)

