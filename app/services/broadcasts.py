"""Рассылки: снимок аудитории, очередь и доставка через основной бот.

Админка только готовит рассылку и строки получателей. К Telegram ходит процесс
основного бота: у него уже настроены токен и прокси, а незавершённая очередь
переживает перезапуск процесса.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import io
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import func, insert, literal, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import BASE_DIR, MEDIA_DIR
from app.db import session_scope
from app.enums import BroadcastStatus, DeliveryStatus, LogLevel, LogSection
from app.models import Broadcast, BroadcastDelivery, User
from app.services.events import log_event

log = logging.getLogger("bot.broadcasts")

AUDIENCES: dict[str, str] = {
    "all": "Все клиенты",
    "paid": "Пополняли баланс",
    "buyers": "Покупали номера",
    "premium": "Telegram Premium",
}

IMAGE_LIMIT = 10 * 1024 * 1024
IMAGE_MAX_SIDE = 4096
IMAGE_MAX_PIXELS = 40_000_000
MESSAGE_LIMIT = 4096
CAPTION_LIMIT = 1024
BUTTON_LIMIT = 8
LOOP_IDLE = 2.0
SEND_PAUSE = 0.07  # не упираемся в общий лимит Telegram примерно 30 сообщений/с

_PARSE_ERRORS = ("can't parse entities", "unsupported start tag", "can't find end tag")


class BroadcastError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PendingDelivery:
    delivery_id: int
    broadcast_id: int
    tg_id: int
    text: str
    parse_mode: str
    image_file_id: str | None
    image_path: str | None
    buttons: list | None


def parse_buttons(raw: str) -> list[dict[str, str]] | None:
    """Разобрать строки ``Подпись | https://...`` в inline-кнопки."""
    rows: list[dict[str, str]] = []
    for number, source in enumerate(raw.splitlines(), 1):
        line = source.strip()
        if not line:
            continue
        title, sep, url = line.partition("|")
        title, url = title.strip(), url.strip()
        if not sep or not title or not url:
            raise BroadcastError(
                f"Кнопка в строке {number}: нужен формат «Подпись | https://ссылка»."
            )
        if len(title) > 64:
            raise BroadcastError(f"Кнопка в строке {number}: подпись длиннее 64 символов.")
        if len(url) > 512:
            raise BroadcastError(f"Кнопка в строке {number}: ссылка слишком длинная.")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https", "tg"} or not parsed.netloc:
            raise BroadcastError(
                f"Кнопка в строке {number}: ссылка должна начинаться с https:// или tg://."
            )
        rows.append({"text": title, "url": url})
    if len(rows) > BUTTON_LIMIT:
        raise BroadcastError(f"Кнопок может быть не больше {BUTTON_LIMIT}.")
    return rows or None


def normalize_image(blob: bytes) -> bytes:
    """Проверить картинку и привести её к безопасному JPEG для Telegram."""
    if not blob:
        raise BroadcastError("Выбранная картинка пустая.")
    if len(blob) > IMAGE_LIMIT:
        raise BroadcastError("Картинка больше 10 МБ — выберите файл поменьше.")
    try:
        with Image.open(io.BytesIO(blob)) as opened:
            if opened.width * opened.height > IMAGE_MAX_PIXELS:
                raise BroadcastError("У картинки слишком большое разрешение.")
            opened.verify()
        with Image.open(io.BytesIO(blob)) as opened:
            image = ImageOps.exif_transpose(opened)
            image.thumbnail((IMAGE_MAX_SIDE, IMAGE_MAX_SIDE), Image.Resampling.LANCZOS)
            if image.mode in {"RGBA", "LA"}:
                background = Image.new("RGB", image.size, "white")
                background.paste(image, mask=image.getchannel("A"))
                image = background
            elif image.mode != "RGB":
                image = image.convert("RGB")
            out = io.BytesIO()
            image.save(out, format="JPEG", quality=90, optimize=True)
            return out.getvalue()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise BroadcastError("Файл не читается как изображение.") from exc


def validate_text(text: str, *, has_image: bool) -> str:
    value = text.strip()
    if not value:
        raise BroadcastError("Напишите текст рассылки.")
    limit = CAPTION_LIMIT if has_image else MESSAGE_LIMIT
    if len(value) > limit:
        place = "подписи к картинке" if has_image else "сообщения"
        raise BroadcastError(f"Текст длиннее лимита {place}: {limit} символов.")
    return value


def _audience_where(audience: str) -> list[Any]:
    if audience not in AUDIENCES:
        raise BroadcastError("Неизвестная аудитория рассылки.")
    # Заблокированный магазином клиент не должен получать маркетинговые сообщения.
    where: list[Any] = [
        User.is_banned.is_(False),
        # Google-only website accounts use an internal synthetic tg_id and do
        # not have a Telegram chat where the bot can deliver a broadcast.
        or_(User.auth_provider.is_(None), User.auth_provider == "telegram"),
    ]
    if audience == "paid":
        where.append(User.total_topup > 0)
    elif audience == "buyers":
        where.append(User.orders_count > 0)
    elif audience == "premium":
        where.append(User.is_tg_premium.is_(True))
    return where


async def audience_counts(session: AsyncSession) -> dict[str, int]:
    out: dict[str, int] = {}
    for key in AUDIENCES:
        out[key] = int(
            await session.scalar(select(func.count()).select_from(User).where(*_audience_where(key)))
            or 0
        )
    return out


async def create(
    session: AsyncSession,
    *,
    admin_id: int,
    title: str,
    text: str,
    audience: str,
    buttons: list[dict[str, str]] | None = None,
    image: bytes | None = None,
) -> Broadcast:
    """Сохранить черновик. Получатели фиксируются только при запуске."""
    _audience_where(audience)  # валидация до записи
    body = validate_text(text, has_image=image is not None)
    row = Broadcast(
        admin_id=admin_id,
        title=title.strip()[:120] or None,
        text=body,
        parse_mode="HTML",
        buttons=buttons,
        audience=audience,
        audience_filter={"exclude_banned": True},
        status=BroadcastStatus.DRAFT.value,
    )
    session.add(row)
    await session.flush()

    path: Path | None = None
    if image is not None:
        folder = MEDIA_DIR / "broadcasts"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"broadcast_{row.id}.jpg"
        try:
            path.write_bytes(image)
            row.image_path = str(path.relative_to(BASE_DIR))
        except Exception:
            path.unlink(missing_ok=True)
            raise

    await log_event(
        LogSection.BROADCAST,
        "broadcast_created",
        admin_id=admin_id,
        message=f"рассылка {row.id}, аудитория {audience}",
        payload={"broadcast_id": row.id, "audience": audience, "image": image is not None},
        session=session,
    )
    try:
        await session.commit()
    except Exception:
        if path is not None:
            path.unlink(missing_ok=True)
        raise
    return row


async def start(session: AsyncSession, row: Broadcast, *, admin_id: int) -> int:
    """Зафиксировать получателей и отдать очередь основному боту."""
    if row.status != BroadcastStatus.DRAFT.value:
        raise BroadcastError("Запустить можно только черновик.")

    # Два почти одновременных клика не должны дважды собирать аудиторию. PAUSED
    # здесь — короткое внутреннее состояние подготовки в той же транзакции.
    claimed = await session.execute(
        update(Broadcast)
        .where(
            Broadcast.id == row.id,
            Broadcast.status == BroadcastStatus.DRAFT.value,
        )
        .values(status=BroadcastStatus.PAUSED.value)
    )
    if claimed.rowcount != 1:
        raise BroadcastError("Рассылка уже запущена или отменена.")
    row.status = BroadcastStatus.PAUSED.value

    recipients = select(literal(row.id), User.id).where(*_audience_where(row.audience))
    await session.execute(
        insert(BroadcastDelivery).from_select(
            [BroadcastDelivery.broadcast_id, BroadcastDelivery.user_id], recipients
        )
    )
    row.total = int(
        await session.scalar(
            select(func.count())
            .select_from(BroadcastDelivery)
            .where(BroadcastDelivery.broadcast_id == row.id)
        )
        or 0
    )
    row.started_at = dt.datetime.now(dt.UTC)
    row.error = None
    if row.total:
        row.status = BroadcastStatus.SENDING.value
    else:
        row.status = BroadcastStatus.DONE.value
        row.finished_at = row.started_at
    await log_event(
        LogSection.BROADCAST,
        "broadcast_started",
        admin_id=admin_id,
        message=f"рассылка {row.id}, получателей {row.total}",
        payload={"broadcast_id": row.id, "total": row.total, "audience": row.audience},
        session=session,
    )
    await session.commit()
    return row.total


async def cancel(session: AsyncSession, row: Broadcast, *, admin_id: int) -> None:
    if row.status not in {
        BroadcastStatus.DRAFT.value,
        BroadcastStatus.SENDING.value,
        BroadcastStatus.PAUSED.value,
    }:
        raise BroadcastError("Эту рассылку уже нельзя отменить.")
    # A worker may have claimed rows while the admin opened the page. Return
    # those claims to the queue so a crash cannot leave an orphaned ``sending``
    # row in a campaign that is no longer processed.
    await session.execute(
        update(BroadcastDelivery)
        .where(
            BroadcastDelivery.broadcast_id == row.id,
            BroadcastDelivery.status == DeliveryStatus.SENDING.value,
        )
        .values(status=DeliveryStatus.QUEUED.value, error=None)
    )
    row.status = BroadcastStatus.CANCELLED.value
    row.finished_at = dt.datetime.now(dt.UTC)
    await log_event(
        LogSection.BROADCAST,
        "broadcast_cancelled",
        admin_id=admin_id,
        message=f"рассылка {row.id}, обработано {row.sent + row.failed + row.blocked}/{row.total}",
        payload={"broadcast_id": row.id},
        session=session,
    )
    await session.commit()


async def listing(
    session: AsyncSession, *, limit: int = 50, offset: int = 0
) -> list[Broadcast]:
    return list(
        (
            await session.scalars(
                select(Broadcast).order_by(Broadcast.id.desc()).limit(limit).offset(offset)
            )
        ).all()
    )


async def status_counts(session: AsyncSession) -> dict[str, int]:
    rows = await session.execute(
        select(Broadcast.status, func.count()).group_by(Broadcast.status)
    )
    out = {status.value: 0 for status in BroadcastStatus}
    for status, amount in rows.all():
        out[status] = int(amount)
    out["total"] = sum(out.values())
    return out


async def delivery_errors(
    session: AsyncSession, broadcast_id: int, *, limit: int = 50
) -> list[tuple[BroadcastDelivery, User]]:
    rows = await session.execute(
        select(BroadcastDelivery, User)
        .join(User, User.id == BroadcastDelivery.user_id)
        .where(
            BroadcastDelivery.broadcast_id == broadcast_id,
            BroadcastDelivery.status.in_(
                [DeliveryStatus.FAILED.value, DeliveryStatus.BLOCKED.value]
            ),
        )
        .order_by(BroadcastDelivery.id.desc())
        .limit(limit)
    )
    return [(delivery, user) for delivery, user in rows.all()]


def _keyboard(raw: list | None) -> InlineKeyboardMarkup | None:
    if not raw:
        return None
    rows: list[list[InlineKeyboardButton]] = []
    for item in raw[:BUTTON_LIMIT]:
        if not isinstance(item, dict):
            continue
        title, url = str(item.get("text") or "").strip(), str(item.get("url") or "").strip()
        if title and url:
            rows.append([InlineKeyboardButton(text=title[:64], url=url)])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def _image(path_value: str | None) -> Path | None:
    if not path_value:
        return None
    path = Path(path_value)
    path = path if path.is_absolute() else BASE_DIR / path
    return path if path.is_file() else None


def _is_parse_error(exc: TelegramBadRequest) -> bool:
    value = str(exc).lower()
    return any(marker in value for marker in _PARSE_ERRORS)


async def _send(bot: Any, item: PendingDelivery) -> tuple[Any, str | None]:
    keyboard = _keyboard(item.buttons)
    if item.image_file_id or (picture := _image(item.image_path)) is not None:
        photo: Any = item.image_file_id or FSInputFile(picture)
        try:
            sent = await bot.send_photo(
                chat_id=item.tg_id,
                photo=photo,
                caption=item.text,
                parse_mode=item.parse_mode,
                reply_markup=keyboard,
            )
        except TelegramBadRequest as exc:
            if not _is_parse_error(exc):
                raise
            sent = await bot.send_photo(
                chat_id=item.tg_id,
                photo=photo,
                caption=item.text,
                parse_mode=None,
                reply_markup=keyboard,
            )
        photos = getattr(sent, "photo", None) or []
        file_id = getattr(photos[-1], "file_id", None) if photos else None
        return sent, file_id

    try:
        sent = await bot.send_message(
            item.tg_id,
            item.text,
            reply_markup=keyboard,
            parse_mode=item.parse_mode,
        )
    except TelegramBadRequest as exc:
        if not _is_parse_error(exc):
            raise
        sent = await bot.send_message(
            item.tg_id,
            item.text,
            reply_markup=keyboard,
            parse_mode=None,
        )
    return sent, None


async def _next() -> PendingDelivery | None:
    # Select + условный update дают дешёвый claim без удержания транзакции во
    # время сети. Если два воркера увидели одну строку, rowcount=1 будет только
    # у одного; второй возьмёт следующую.
    for _ in range(3):
        async with session_scope() as session:
            row = (
                await session.execute(
                    select(
                        BroadcastDelivery.id,
                        Broadcast.id,
                        User.tg_id,
                        Broadcast.text,
                        Broadcast.parse_mode,
                        Broadcast.image_file_id,
                        Broadcast.image_path,
                        Broadcast.buttons,
                    )
                    .join(Broadcast, Broadcast.id == BroadcastDelivery.broadcast_id)
                    .join(User, User.id == BroadcastDelivery.user_id)
                    .where(
                        Broadcast.status == BroadcastStatus.SENDING.value,
                        BroadcastDelivery.status == DeliveryStatus.QUEUED.value,
                    )
                    .order_by(Broadcast.id, BroadcastDelivery.id)
                    .limit(1)
                )
            ).one_or_none()
            if row is None:
                return None
            claimed = await session.execute(
                update(BroadcastDelivery)
                .where(
                    BroadcastDelivery.id == row[0],
                    BroadcastDelivery.status == DeliveryStatus.QUEUED.value,
                )
                .values(status=DeliveryStatus.SENDING.value, error=None)
            )
            if claimed.rowcount == 1:
                return PendingDelivery(*row)
    return None


async def _remember_retry(item: PendingDelivery, exc: Exception) -> None:
    """Временная ошибка: строка остаётся в очереди и попробуется ещё раз."""
    async with session_scope() as session:
        delivery = await session.get(BroadcastDelivery, item.delivery_id)
        if delivery is not None and delivery.status == DeliveryStatus.SENDING.value:
            delivery.status = DeliveryStatus.QUEUED.value
            delivery.error = str(exc)[:255]
        row = await session.get(Broadcast, item.broadcast_id)
        if row is not None:
            row.error = str(exc)[:500]


async def _preflight(item: PendingDelivery) -> tuple[bool, bool]:
    """Не начинать отправку после отмены или нового бана клиента.

    Проверка и перевод доставки в конечное состояние идут в одной транзакции,
    чтобы поздний бан не оставил строку в ``sending`` после перезапуска.
    Возвращает ``(остановиться, работа выполнена)``.
    """
    async with session_scope() as session:
        row = await session.get(Broadcast, item.broadcast_id)
        delivery = await session.get(BroadcastDelivery, item.delivery_id)
        if row is None or delivery is None:
            return True, False
        if row.status == BroadcastStatus.SENDING.value:
            banned = bool(
                await session.scalar(
                    select(User.is_banned).where(User.id == delivery.user_id)
                )
            )
            if banned and delivery.status == DeliveryStatus.SENDING.value:
                delivery.status = DeliveryStatus.BLOCKED.value
                delivery.error = "клиент заблокирован до отправки"
                delivery.sent_at = dt.datetime.now(dt.UTC)
                await session.execute(
                    update(Broadcast)
                    .where(Broadcast.id == row.id)
                    .values(
                        blocked=Broadcast.blocked + 1,
                        error=None,
                    )
                    .execution_options(synchronize_session=False)
                )
                await session.flush()
                await session.refresh(row)
                pending = int(
                    await session.scalar(
                        select(func.count())
                        .select_from(BroadcastDelivery)
                        .where(
                            BroadcastDelivery.broadcast_id == row.id,
                            BroadcastDelivery.status.in_(
                                [DeliveryStatus.QUEUED.value, DeliveryStatus.SENDING.value]
                            ),
                        )
                    )
                    or 0
                )
                if pending == 0:
                    row.status = BroadcastStatus.DONE.value
                    row.finished_at = dt.datetime.now(dt.UTC)
                    await log_event(
                        LogSection.BROADCAST,
                        "broadcast_finished",
                        message=(
                            f"рассылка {row.id}: доставлено {row.sent}, "
                            f"не дошло {row.failed}, блок {row.blocked}"
                        ),
                        payload={
                            "broadcast_id": row.id,
                            "sent": row.sent,
                            "failed": row.failed,
                            "blocked": row.blocked,
                        },
                        session=session,
                    )
                return True, True
            return False, False
        if delivery is not None and delivery.status == DeliveryStatus.SENDING.value:
            delivery.status = DeliveryStatus.QUEUED.value
        return True, False


async def _finish(
    item: PendingDelivery,
    status: DeliveryStatus,
    *,
    error: str | None = None,
    image_file_id: str | None = None,
) -> None:
    async with session_scope() as session:
        delivery = await session.get(BroadcastDelivery, item.delivery_id)
        row = await session.get(Broadcast, item.broadcast_id)
        if delivery is None or row is None or delivery.status != DeliveryStatus.SENDING.value:
            return

        delivery.status = status.value
        delivery.error = (error or "")[:255] or None
        delivery.sent_at = dt.datetime.now(dt.UTC)
        values: dict[Any, Any] = {Broadcast.error: None}
        if status is DeliveryStatus.SENT:
            values[Broadcast.sent] = Broadcast.sent + 1
            if image_file_id:
                values[Broadcast.image_file_id] = func.coalesce(
                    Broadcast.image_file_id, image_file_id
                )
        elif status is DeliveryStatus.BLOCKED:
            values[Broadcast.blocked] = Broadcast.blocked + 1
        else:
            values[Broadcast.failed] = Broadcast.failed + 1
        # SQL-выражение, а не read-modify-write: два параллельных воркера не
        # потеряют один из инкрементов счётчика.
        await session.execute(
            update(Broadcast)
            .where(Broadcast.id == item.broadcast_id)
            .values(values)
            .execution_options(synchronize_session=False)
        )
        await session.flush()
        await session.refresh(row)

        pending = int(
            await session.scalar(
                select(func.count())
                .select_from(BroadcastDelivery)
                .where(
                    BroadcastDelivery.broadcast_id == row.id,
                    BroadcastDelivery.status.in_(
                        [DeliveryStatus.QUEUED.value, DeliveryStatus.SENDING.value]
                    ),
                )
            )
            or 0
        )
        if pending == 0 and row.status == BroadcastStatus.SENDING.value:
            row.status = BroadcastStatus.DONE.value
            row.finished_at = dt.datetime.now(dt.UTC)
            await log_event(
                LogSection.BROADCAST,
                "broadcast_finished",
                message=(
                    f"рассылка {row.id}: доставлено {row.sent}, "
                    f"не дошло {row.failed}, блок {row.blocked}"
                ),
                payload={
                    "broadcast_id": row.id,
                    "sent": row.sent,
                    "failed": row.failed,
                    "blocked": row.blocked,
                },
                session=session,
            )


async def deliver_next(bot: Any) -> bool:
    """Обработать одного получателя. False означает, что готовой работы нет."""
    item = await _next()
    if item is None:
        return False
    stop, worked = await _preflight(item)
    if stop:
        return worked
    try:
        _, file_id = await _send(bot, item)
    except TelegramForbiddenError as exc:
        await _finish(item, DeliveryStatus.BLOCKED, error=str(exc))
    except (TelegramNotFound, TelegramBadRequest) as exc:
        await _finish(item, DeliveryStatus.FAILED, error=str(exc))
    except TelegramRetryAfter as exc:
        await _remember_retry(item, exc)
        await asyncio.sleep(min(float(exc.retry_after) + 0.2, 60.0))
        return False
    except (TelegramNetworkError, TelegramServerError) as exc:
        await _remember_retry(item, exc)
        return False
    except Exception as exc:
        # Неизвестная локальная ошибка одной записи не должна навсегда остановить
        # всю очередь. Сохраняем её в статистике и идём к следующему получателю.
        log.exception("рассылка %s, доставка %s сорвалась", item.broadcast_id, item.delivery_id)
        await _finish(item, DeliveryStatus.FAILED, error=f"{type(exc).__name__}: {exc}")
        await log_event(
            LogSection.BROADCAST,
            "delivery_failed",
            level=LogLevel.WARN,
            message=f"рассылка {item.broadcast_id}, tg={item.tg_id}: {exc}"[:500],
        )
    else:
        await _finish(item, DeliveryStatus.SENT, image_file_id=file_id)
    return True


async def recover_claims() -> int:
    """Вернуть незавершённые claims после аварийного завершения процесса."""
    async with session_scope() as session:
        active = select(Broadcast.id).where(
            Broadcast.status.in_(
                [BroadcastStatus.SENDING.value, BroadcastStatus.CANCELLED.value]
            )
        )
        result = await session.execute(
            update(BroadcastDelivery)
            .where(
                BroadcastDelivery.status == DeliveryStatus.SENDING.value,
                BroadcastDelivery.broadcast_id.in_(active),
            )
            .values(status=DeliveryStatus.QUEUED.value)
        )
        return int(result.rowcount or 0)


async def delivery_loop(bot: Any, *, idle: float = LOOP_IDLE) -> None:
    recovered = await recover_claims()
    log.info("очередь рассылок запущена, восстановлено строк: %s", recovered)
    while True:
        try:
            worked = await deliver_next(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("цикл рассылок сорвался, повторим")
            worked = False
        await asyncio.sleep(SEND_PAUSE if worked else idle)


__all__ = [
    "AUDIENCES",
    "BroadcastError",
    "audience_counts",
    "cancel",
    "create",
    "deliver_next",
    "delivery_errors",
    "delivery_loop",
    "listing",
    "normalize_image",
    "parse_buttons",
    "recover_claims",
    "start",
    "status_counts",
]
