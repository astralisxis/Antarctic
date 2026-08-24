"""Отзывы: список, модерация, шаблон карточки, удаление из канала.

Публикацию делает бот: у админки нет сессии Telegram (отдельный процесс, httpx
без socks-транспорта). Здесь ставится статус, а карточку в канал отнесёт фоновая
задача бота — обычно за считаные секунды.

Карточку рисует тот же код, что и бот, поэтому предпросмотр здесь честный: что
видно на странице, то и уедет в канал. Аватарки в предпросмотре нет — за ней
нужно идти в Telegram, а этого админка не умеет; вместо неё круг с буквой.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select

from app.admin.counters import nav_counts
from app.admin.deps import DbSession, StaffAdmin
from app.admin.notice import flash
from app.admin.templating import render
from app.enums import REVIEW_STATUS_TITLES, LogSection
from app.models import Order, Review, User
from app.services import review_card
from app.services import reviews as reviews_service
from app.services.events import log_event

router = APIRouter()

PER_PAGE = 40

# Больше в фон не нужно: карточка всё равно 1280 в ширину, а грузить в память
# сотню мегабайт из-за случайно выбранного файла незачем.
BG_LIMIT = 12 * 1024 * 1024

# Пример для предпросмотра: те же поля, что у настоящего отзыва.
DEMO = {
    "name": "м***н",
    "stars": 4,
    "text": (
        "Номер пришёл сразу, код получил через минуту. Всё честно, "
        "буду брать ещё."
    ),
    "product": "Индия +91",
}


def _png(data: bytes) -> Response:
    """Картинка без кеша: фон меняют и сразу смотрят результат."""
    return Response(
        content=data,
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/reviews")
async def index(
    request: Request,
    session: DbSession,
    admin: StaffAdmin,
    status: str = "",
    page: int = 1,
):
    page = max(page, 1)
    picked = status if status in REVIEW_STATUS_TITLES else ""
    stats = await reviews_service.counts(session)
    rows = await reviews_service.listing(
        session, status=picked or None, limit=PER_PAGE, offset=(page - 1) * PER_PAGE
    )
    total = stats.get(picked, 0) if picked else stats.get("total", 0)
    background = await reviews_service.card_background(session)
    return render(
        request,
        "reviews.html",
        {
            "rows": [
                {
                    "r": review,
                    "u": user,
                    "product": reviews_service.display_product(review, product),
                    "name": reviews_service.display_name(review, user),
                }
                for review, user, product in rows
            ],
            "stats": stats,
            "statuses": REVIEW_STATUS_TITLES,
            "f": {"status": picked},
            "page": page,
            "pages": max((total + PER_PAGE - 1) // PER_PAGE, 1),
            "channel": await reviews_service.channel(session),
            "channel_url": await reviews_service.channel_url(session),
            "on": await reviews_service.enabled(session),
            "auto": await reviews_service.auto_publish(session),
            "as_image": await reviews_service.card_image(session),
            "can_draw": review_card.available(),
            "font": review_card.font_name(),
            "bg_path": str(background),
            "bg_ready": background.exists(),
            "counts": await nav_counts(session),
        },
        active="reviews",
    )


@router.get("/reviews/card.png")
async def card_preview(session: DbSession, admin: StaffAdmin):
    """Как выглядит шаблон: пример отзыва на текущем фоне."""
    if not review_card.available():
        return Response(status_code=404)
    data = review_card.render(
        **DEMO, background=await reviews_service.card_background(session)
    )
    return _png(data)


@router.post("/reviews/background")
async def background_upload(
    request: Request,
    session: DbSession,
    admin: StaffAdmin,
    picture: Annotated[UploadFile, File()],
):
    """Загрузить фон карточки. Кладём в путь из настройки, перекодировав в JPEG."""
    if not review_card.available():
        flash(request, "Картинки рисовать нечем: Pillow не установлен.", ok=False)
        return RedirectResponse("/reviews", status_code=303)

    blob = await picture.read(BG_LIMIT + 1)
    if not blob:
        flash(request, "Файл пустой — фон не тронут.", ok=False)
        return RedirectResponse("/reviews", status_code=303)
    if len(blob) > BG_LIMIT:
        flash(request, "Файл больше 12 МБ — возьмите картинку поменьше.", ok=False)
        return RedirectResponse("/reviews", status_code=303)

    target = await reviews_service.card_background(session)
    try:
        size = review_card.save_background(blob, target)
    except Exception as exc:
        flash(request, f"Это не картинка или формат не читается: {exc}", ok=False)
        return RedirectResponse("/reviews", status_code=303)

    await log_event(
        LogSection.REVIEW,
        "card_bg_uploaded",
        admin_id=admin.id,
        message=f"{target.name}, {size[0]}×{size[1]}",
    )
    flash(request, f"Фон обновлён: {size[0]}×{size[1]}. Проверьте предпросмотр ниже.")
    return RedirectResponse("/reviews", status_code=303)


@router.get("/reviews/background/delete")
async def background_delete_form(request: Request, session: DbSession, admin: StaffAdmin):
    path = await reviews_service.card_background(session)
    return render(
        request,
        "review_bg_delete.html",
        {
            "path": str(path),
            "exists": path.exists(),
            "size_kb": round(path.stat().st_size / 1024) if path.exists() else 0,
            "counts": await nav_counts(session),
        },
        active="reviews",
    )


@router.post("/reviews/background/delete")
async def background_delete(request: Request, session: DbSession, admin: StaffAdmin):
    """Убрать фон. Карточки останутся — станут светлыми, на бумажном фоне."""
    path = await reviews_service.card_background(session)
    if not path.exists():
        flash(request, "Фона и так нет.", ok=False)
        return RedirectResponse("/reviews", status_code=303)
    try:
        path.unlink()
    except OSError as exc:
        flash(request, f"Файл не удалился: {exc}", ok=False)
        return RedirectResponse("/reviews", status_code=303)
    await log_event(
        LogSection.REVIEW, "card_bg_deleted", admin_id=admin.id, message=path.name
    )
    flash(request, "Фон убран — карточки будут светлыми.")
    return RedirectResponse("/reviews", status_code=303)


@router.post("/reviews/test")
async def test_review(
    request: Request,
    session: DbSession,
    admin: StaffAdmin,
    name: Annotated[str, Form()] = "",
    stars: Annotated[int, Form()] = 5,
    text: Annotated[str, Form()] = "",
    product: Annotated[str, Form()] = "",
):
    """Отправить в канал тестовый отзыв: видно, как выглядит, без покупки номера."""
    if not await reviews_service.channel(session):
        flash(request, "Канал не задан — сначала заполните его в настройках.", ok=False)
        return RedirectResponse("/reviews", status_code=303)
    try:
        review = await reviews_service.create_test(
            session,
            name=name,
            stars=stars,
            text=text,
            product=product,
            admin_id=admin.id,
        )
    except reviews_service.ReviewError as exc:
        flash(request, str(exc), ok=False)
        return RedirectResponse("/reviews", status_code=303)
    flash(
        request,
        f"Тестовый отзыв №{review.id} готов — бот отправит его в канал в течение минуты. "
        "Потом удалите его здесь же.",
    )
    return RedirectResponse("/reviews", status_code=303)


@router.get("/reviews/{review_id}/delete")
async def delete_form(request: Request, session: DbSession, admin: StaffAdmin, review_id: int):
    review = await session.get(Review, review_id)
    if review is None:
        flash(request, f"Отзыва №{review_id} нет.", ok=False)
        return RedirectResponse("/reviews", status_code=303)
    user = await session.get(User, review.user_id) if review.user_id else None
    return render(
        request,
        "review_delete.html",
        {
            "r": review,
            "name": reviews_service.display_name(review, user),
            "is_test": review.user_id is None,
            "counts": await nav_counts(session),
        },
        active="reviews",
    )


@router.post("/reviews/{review_id}/delete")
async def delete_review(request: Request, session: DbSession, admin: StaffAdmin, review_id: int):
    review = await session.get(Review, review_id)
    if review is None:
        flash(request, f"Отзыва №{review_id} нет.", ok=False)
        return RedirectResponse("/reviews", status_code=303)
    message_id = review.channel_message_id
    try:
        await reviews_service.delete(session, review, admin_id=admin.id)
    except reviews_service.ReviewError as exc:
        flash(request, str(exc), ok=False)
        return RedirectResponse("/reviews", status_code=303)
    if message_id:
        flash(
            request,
            f"Тестовый отзыв удалён из панели. Сообщение {message_id} в канале "
            "уберите руками — из админки Telegram недоступен.",
        )
    else:
        flash(request, "Тестовый отзыв удалён.")
    return RedirectResponse("/reviews", status_code=303)


@router.get("/reviews/{review_id}/card.png")
async def card_of_review(session: DbSession, admin: StaffAdmin, review_id: int):
    """Карточка отзыва: отправленный файл или свежий рисунок по его данным."""
    review = await session.get(Review, review_id)
    if review is None or not review_card.available():
        return Response(status_code=404)

    ready = review_card.stored(review.image_path)
    if ready is not None:
        return _png(ready.read_bytes())

    user = await session.get(User, review.user_id) if review.user_id else None
    product = None
    if review.order_id:
        product = await session.scalar(
            select(Order.offer_title).where(Order.id == review.order_id)
        )
    data = review_card.render(
        name=reviews_service.display_name(review, user),
        stars=review.stars,
        text=review.text,
        product=reviews_service.display_product(review, product),
        background=await reviews_service.card_background(session),
    )
    return _png(data)


@router.post("/reviews/{review_id}/approve")
async def approve(request: Request, session: DbSession, admin: StaffAdmin, review_id: int):
    review = await session.get(Review, review_id)
    if review is None:
        flash(request, f"Отзыва №{review_id} нет.", ok=False)
        return RedirectResponse("/reviews", status_code=303)
    try:
        await reviews_service.approve(session, review, admin_id=admin.id)
    except reviews_service.ReviewError as exc:
        flash(request, str(exc), ok=False)
    else:
        flash(request, "Отзыв одобрен — бот отправит его в канал в течение минуты.")
    return RedirectResponse("/reviews", status_code=303)


@router.post("/reviews/{review_id}/reject")
async def reject(
    request: Request,
    session: DbSession,
    admin: StaffAdmin,
    review_id: int,
    reason: Annotated[str, Form()] = "",
):
    review = await session.get(Review, review_id)
    if review is None:
        flash(request, f"Отзыва №{review_id} нет.", ok=False)
        return RedirectResponse("/reviews", status_code=303)
    try:
        await reviews_service.reject(
            session, review, reason=reason.strip()[:255] or None, admin_id=admin.id
        )
    except reviews_service.ReviewError as exc:
        flash(request, str(exc), ok=False)
    else:
        flash(request, "Отзыв отклонён, в канал он не пойдёт.")
    return RedirectResponse("/reviews", status_code=303)
