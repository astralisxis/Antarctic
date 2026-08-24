"""Отзывы: сбор в боте, публикация карточкой в канал, модерация из админки.

Карточка отзыва — картинка по шаблону: фон-фото магазина, слева круглая
аватарка покупателя, под ней ник звёздочками, справа оценка звёздами, ниже
текст, справа внизу товар. Рисует `review_card` на Pillow, файл кладётся в
`data/reviews` и запоминается в отзыве, чтобы повтор отправки не рисовал заново.
Картинку можно выключить (`reviews.card_image`) или её может быть нечем рисовать
(нет Pillow) — тогда, как раньше, уходит аватарка файлом с подписью текстом, а
без аватарки просто текст.

Отправку делает процесс бота: у админки нет сессии Telegram (она живёт отдельным
процессом и ходит в сеть через httpx без socks). Поэтому админ ставит статус
published, а публикует фоновая задача бота — тот же путь, что у автопубликации.

Слой без aiogram в типах: bot передаётся как объект с методами Telegram, этими же
функциями сможет пользоваться мини-апп.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import io
import logging
from html import escape
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import BASE_DIR, settings
from app.db import session_scope
from app.enums import LogLevel, LogSection, ReviewStatus
from app.models import Order, Review, User
from app.services import orders as orders_service
from app.services import review_card
from app.services import settings_store
from app.services.events import log_event

log = logging.getLogger("reviews")

STARS_MAX = 5
TEXT_MAX = 500

# Как часто фоновая задача забирает отзывы, которым ещё нужна публикация.
PUBLISH_INTERVAL = 20.0


class ReviewError(Exception):
    """Ошибка с текстом, который можно показать покупателю как есть."""


# --------------------------------------------------------------------------- #
#  Настройки раздела
# --------------------------------------------------------------------------- #
async def enabled(session: AsyncSession) -> bool:
    return await settings_store.get_bool(session, "reviews.enabled")


async def auto_publish(session: AsyncSession) -> bool:
    return await settings_store.get_bool(session, "reviews.auto_publish")


async def channel_url(session: AsyncSession) -> str | None:
    return (await settings_store.get(session, "reviews.channel_url") or "").strip() or None


async def channel(session: AsyncSession) -> int | str | None:
    """Куда публиковать: @username или числовой id канала.

    Ключ из админки главнее .env: канал меняют чаще, чем перезапускают процессы.
    """
    raw = (await settings_store.get(session, "reviews.channel_chat") or "").strip()
    if raw:
        if raw.lstrip("-").isdigit():
            return int(raw)
        return raw if raw.startswith("@") else f"@{raw}"
    return settings.reviews_channel_id


async def card_image(session: AsyncSession) -> bool:
    """Публиковать картинкой по шаблону. Без Pillow ответ всегда «нет»."""
    if not review_card.available():
        return False
    return await settings_store.get_bool(session, "reviews.card_image")


async def card_background(session: AsyncSession) -> Path:
    """Файл фона карточки: путь из настройки, относительный — от корня проекта."""
    return review_card.background_path(await settings_store.get(session, "reviews.card_bg"))


# --------------------------------------------------------------------------- #
#  Показ
# --------------------------------------------------------------------------- #
def stars_line(stars: int) -> str:
    """Оценка символами: ★ закрашенные, ☆ пустые. Эмодзи не берём — монохром."""
    value = max(1, min(int(stars or 0), STARS_MAX))
    return "★" * value + "☆" * (STARS_MAX - value)


def mask_raw(raw: str | None) -> str:
    """Ник звёздочками: видно первую и последнюю букву, остального нет.

    Отзыв публичный, а покупка номеров — дело негромкое: полный ник в канале
    покупателю не нужен.
    """
    value = (raw or "").strip()
    if not value:
        return "г***ь"
    if len(value) == 1:
        return f"{value}***"
    body = "*" * min(len(value) - 2, 8) if len(value) > 2 else "*"
    return f"{value[0]}{body}{value[-1]}"


def mask_name(user: User | None) -> str:
    if user is None:
        return "г***ь"
    return mask_raw(user.username or user.first_name)


def display_name(review: Review, user: User | None) -> str:
    """Как отзыв подписан в канале. У тестового клиента нет — ник ввёл админ."""
    if user is not None:
        return mask_name(user)
    return mask_raw(review.author_name)


def display_product(review: Review, product: str | None) -> str | None:
    """Товар: из заказа, а у тестового отзыва — из своей колонки."""
    return product or (review.product_title or "").strip() or None


def card(*, name: str, stars: int, text: str | None, product: str | None) -> str:
    """Подпись карточки отзыва. Товар — последней строкой, как в шаблоне."""
    lines = [f"<b>{escape(name, quote=False)}</b>", stars_line(stars)]
    body = (text or "").strip()
    if body:
        lines += ["", escape(body, quote=False)]
    if product:
        lines += ["", escape(product, quote=False)]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
#  Сбор
# --------------------------------------------------------------------------- #
async def by_order(session: AsyncSession, order_id: int) -> Review | None:
    return await session.scalar(select(Review).where(Review.order_id == order_id))


async def can_leave(session: AsyncSession, order: Order) -> bool:
    """Отзыв предлагаем один раз и только по оплаченному заказу."""
    if order.status not in orders_service.DELIVERED:
        return False
    if not await enabled(session):
        return False
    return await by_order(session, order.id) is None


async def create(
    session: AsyncSession,
    user: User,
    order: Order,
    *,
    stars: int,
    text: str | None,
) -> Review:
    """Записать отзыв. Публикацию делает publish_one — здесь только база."""
    if not await enabled(session):
        raise ReviewError("Отзывы сейчас не собираем.")
    if order.user_id != user.id or order.status not in orders_service.DELIVERED:
        raise ReviewError("Отзыв оставляют по своей покупке.")
    if await by_order(session, order.id) is not None:
        raise ReviewError("По этому номеру отзыв уже есть.")

    value = max(1, min(int(stars), STARS_MAX))
    body = (text or "").strip()[:TEXT_MAX] or None
    auto = await auto_publish(session)

    review = Review(
        user_id=user.id,
        order_id=order.id,
        stars=value,
        text=body,
        status=ReviewStatus.PUBLISHED.value if auto else ReviewStatus.PENDING.value,
    )
    session.add(review)
    await session.flush()
    await log_event(
        LogSection.REVIEW,
        "review_created",
        user_id=user.id,
        order_id=order.id,
        message=f"{value}★, {order.offer_title}, {'сразу в канал' if auto else 'на проверку'}",
        session=session,
    )
    await session.commit()
    return review


async def create_test(
    session: AsyncSession,
    *,
    name: str,
    stars: int,
    text: str | None,
    product: str | None,
    admin_id: int | None = None,
) -> Review:
    """Тестовый отзыв из админки: посмотреть карточку в канале, не покупая номер.

    Клиента и заказа у него нет, ник и товар пишет админ. Дальше он идёт обычным
    путём — статус published, отправляет бот, поэтому в канале видно ровно то же,
    что увидит покупатель.
    """
    label = (name or "").strip()[:64]
    if not label:
        raise ReviewError("Впишите ник — он попадёт в карточку звёздочками.")
    review = Review(
        user_id=None,
        order_id=None,
        stars=max(1, min(int(stars), STARS_MAX)),
        text=(text or "").strip()[:TEXT_MAX] or None,
        author_name=label,
        product_title=(product or "").strip()[:120] or None,
        status=ReviewStatus.PUBLISHED.value,
        moderated_by=admin_id,
    )
    session.add(review)
    await session.flush()
    await log_event(
        LogSection.REVIEW,
        "review_test_created",
        admin_id=admin_id,
        message=f"тестовый отзыв {review.id}: {review.stars}★, {review.product_title or 'без товара'}",
        session=session,
    )
    await session.commit()
    return review


async def delete(session: AsyncSession, review: Review, *, admin_id: int | None = None) -> None:
    """Убрать тестовый отзыв. Клиентские не удаляем — их отклоняют.

    Сообщение в канале при этом остаётся: удалять его из админки нечем, у панели
    нет сессии Telegram.
    """
    if review.user_id is not None:
        raise ReviewError("Удалять можно только тестовые отзывы, клиентский — отклоните.")
    path = review_card.stored(review.image_path)
    await session.delete(review)
    await log_event(
        LogSection.REVIEW,
        "review_test_deleted",
        admin_id=admin_id,
        message=f"тестовый отзыв {review.id}"
        + (f", сообщение {review.channel_message_id} осталось в канале"
           if review.channel_message_id else ""),
        session=session,
    )
    await session.commit()
    if path is not None:
        path.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
#  Публикация
# --------------------------------------------------------------------------- #
async def _avatar_file_id(bot: Any, tg_id: int) -> str | None:
    """file_id аватарки покупателя. Нет аватарки или закрыта — вернём None."""
    try:
        photos = await bot.get_user_profile_photos(user_id=tg_id, limit=1)
    except Exception as exc:  # приватность, бан, обрыв прокси — не повод падать
        log.info("аватарка tg=%s не читается: %s", tg_id, exc)
        return None
    items = getattr(photos, "photos", None) or []
    if not items or not items[0]:
        return None
    return items[0][-1].file_id


async def _avatar_blob(bot: Any, file_id: str) -> bytes | None:
    """Сама аватарка байтами — её нужно вклеить в картинку, а не переслать."""
    try:
        buffer = io.BytesIO()
        await bot.download(file_id, destination=buffer)
        return buffer.getvalue() or None
    except Exception as exc:  # обрыв прокси, файл удалён — рисуем круг с буквой
        log.info("аватарка %s не скачалась: %s", file_id[:16], exc)
        return None


def _input_file(path: Path) -> Any:
    """Файл для отправки. aiogram импортируем внутри: слой сервисов без него."""
    from aiogram.types import FSInputFile

    return FSInputFile(path)


def _stored_card(review: Review) -> Path | None:
    """Уже нарисованная карточка, если файл на месте."""
    return review_card.stored(review.image_path)


async def _card_file(
    session: AsyncSession,
    review: Review,
    *,
    name: str,
    user: User | None,
    product: str | None,
    bot: Any,
) -> Path | None:
    """Нарисовать карточку отзыва файлом. None — рисовать нечем, уйдёт текстом."""
    ready = _stored_card(review)
    if ready is not None:
        return ready

    avatar = None
    file_id = await _avatar_file_id(bot, user.tg_id) if user else None
    if file_id:
        avatar = await _avatar_blob(bot, file_id)

    try:
        path = review_card.render_file(
            review_card.card_path(review.id),
            name=name,
            stars=review.stars,
            text=review.text,
            product=product,
            avatar=avatar,
            background=await card_background(session),
        )
    except Exception as exc:  # шрифт, фон, диск — публикацию не роняем
        log.warning("карточка отзыва %s не нарисовалась: %s", review.id, exc)
        return None

    try:
        review.image_path = str(path.relative_to(BASE_DIR))
    except ValueError:  # файл вне проекта — храним абсолютный путь
        review.image_path = str(path)
    return path


async def publish_one(session: AsyncSession, review: Review, bot: Any) -> bool:
    """Отправить отзыв в канал. True — ушло, False — не сейчас.

    Ошибку отправки не считаем провалом отзыва: статус остаётся published, и
    следующий круг фоновой задачи попробует снова.
    """
    if review.channel_message_id:
        return True
    if review.status != ReviewStatus.PUBLISHED.value:
        return False

    target = await channel(session)
    if not target:
        log.info("канал отзывов не задан — отзыв %s ждёт настройки", review.id)
        return False

    # Связь review.user не трогаем: ленивая загрузка в async-сессии падает.
    user = await session.get(User, review.user_id) if review.user_id else None
    product = None
    if review.order_id:
        product = await session.scalar(
            select(Order.offer_title).where(Order.id == review.order_id)
        )
    product = display_product(review, product)
    name = display_name(review, user)
    caption = card(
        name=name,
        stars=review.stars,
        text=review.text,
        product=product,
    )

    picture = None
    if await card_image(session):
        picture = await _card_file(
            session, review, name=name, user=user, product=product, bot=bot
        )

    # Картинка несёт весь текст сама — подпись к ней не дублируем.
    photo = None
    if picture is None and user is not None:
        photo = await _avatar_file_id(bot, user.tg_id)
    try:
        if picture is not None:
            sent = await bot.send_photo(chat_id=target, photo=_input_file(picture))
        elif photo:
            sent = await bot.send_photo(chat_id=target, photo=photo, caption=caption)
        else:
            sent = await bot.send_message(chat_id=target, text=caption)
    except Exception as exc:
        await log_event(
            LogSection.REVIEW,
            "publish_failed",
            level=LogLevel.WARN,
            user_id=review.user_id,
            message=f"отзыв {review.id}: {exc}"[:400],
        )
        return False

    review.channel_message_id = getattr(sent, "message_id", None)
    review.published_at = dt.datetime.now(dt.UTC)
    if picture is not None:
        how = "картинкой"
    elif photo:
        how = "с аватаркой"
    else:
        how = "текстом"
    await log_event(
        LogSection.REVIEW,
        "review_published",
        user_id=review.user_id,
        order_id=review.order_id,
        message=f"отзыв {review.id}, {review.stars}★, {how}",
        session=session,
    )
    await session.commit()
    return True


async def publish_pending(bot: Any, limit: int = 10) -> int:
    """Разослать всё, что помечено published, но в канале ещё не появилось.

    Каждый отзыв — своя короткая сессия: в sqlite писатель один, и держать
    транзакцию открытой на время сетевых отправок нельзя.
    """
    async with session_scope() as session:
        ids = list(
            (
                await session.scalars(
                    select(Review.id)
                    .where(
                        Review.status == ReviewStatus.PUBLISHED.value,
                        Review.channel_message_id.is_(None),
                    )
                    .order_by(Review.id)
                    .limit(limit)
                )
            ).all()
        )

    done = 0
    for review_id in ids:
        async with session_scope() as session:
            review = await session.get(Review, review_id)
            if review is not None and await publish_one(session, review, bot):
                done += 1
    return done


async def publish_loop(bot: Any, interval: float = PUBLISH_INTERVAL) -> None:
    """Фоновая задача бота: доносит в канал отзывы, одобренные в админке."""
    log.info("публикация отзывов раз в %.0f с", interval)
    while True:
        try:
            await publish_pending(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("публикация отзывов сорвалась, повторим через %.0f с", interval)
        await asyncio.sleep(interval)


# --------------------------------------------------------------------------- #
#  Модерация
# --------------------------------------------------------------------------- #
async def approve(session: AsyncSession, review: Review, *, admin_id: int | None = None) -> None:
    """Одобрить отзыв. В канал его отнесёт фоновая задача бота."""
    if review.status == ReviewStatus.PUBLISHED.value:
        raise ReviewError("Этот отзыв уже одобрен.")
    review.status = ReviewStatus.PUBLISHED.value
    review.reject_reason = None
    review.moderated_by = admin_id
    await log_event(
        LogSection.REVIEW,
        "review_approved",
        user_id=review.user_id,
        order_id=review.order_id,
        admin_id=admin_id,
        message=f"отзыв {review.id}, {review.stars}★",
        session=session,
    )
    await session.commit()


async def reject(
    session: AsyncSession,
    review: Review,
    *,
    reason: str | None = None,
    admin_id: int | None = None,
) -> None:
    if review.channel_message_id:
        raise ReviewError("Отзыв уже в канале — сначала удалите сообщение в канале.")
    review.status = ReviewStatus.REJECTED.value
    review.reject_reason = (reason or "").strip()[:255] or None
    review.moderated_by = admin_id
    await log_event(
        LogSection.REVIEW,
        "review_rejected",
        user_id=review.user_id,
        order_id=review.order_id,
        admin_id=admin_id,
        message=f"отзыв {review.id}: {review.reject_reason or 'без причины'}",
        session=session,
    )
    await session.commit()


# --------------------------------------------------------------------------- #
#  Выборки
# --------------------------------------------------------------------------- #
async def listing(
    session: AsyncSession,
    *,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[tuple[Review, User | None, str | None]]:
    """Отзывы с автором и товаром: связи грузим сразу, лениво их трогать нельзя."""
    stmt = (
        select(Review, User, Order.offer_title)
        .join(User, User.id == Review.user_id, isouter=True)
        .join(Order, Order.id == Review.order_id, isouter=True)
        .order_by(Review.id.desc())
        .limit(limit)
        .offset(offset)
    )
    if status:
        stmt = stmt.where(Review.status == status)
    rows = await session.execute(stmt)
    return [(review, user, title) for review, user, title in rows.all()]


async def counts(session: AsyncSession) -> dict[str, int]:
    rows = await session.execute(select(Review.status, func.count()).group_by(Review.status))
    out = {status.value: 0 for status in ReviewStatus}
    for status, amount in rows.all():
        out[status] = amount
    out["total"] = sum(out.values())
    out["waiting"] = await session.scalar(
        select(func.count())
        .select_from(Review)
        .where(
            Review.status == ReviewStatus.PUBLISHED.value,
            Review.channel_message_id.is_(None),
        )
    ) or 0
    return out
