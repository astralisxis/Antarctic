"""Бот поддержки.

Запуск: python -m app.bot.support_main  (или scripts\\support.cmd)

Отдельный процесс и отдельный токен: в основном боте раздел «Поддержка» только
показывает часы работы и ссылку сюда. Здесь человек пишет вопрос, а ответы
админов из панели этот же процесс разносит фоновой задачей — админка ходит в
сеть через httpx, socks-транспорта у неё нет, до Telegram она не достучится.

Переписка и её правила — в app/services/support.py, здесь только приём сообщений.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import CommandStart
from aiogram.types import BotCommand, ErrorEvent, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.bot import common, texts
from app.bot.main import build_bot, with_retry
from app.bot.middlewares import DbSessionMiddleware, UserMiddleware
from app.config import settings
from app.db import session_scope
from app.enums import LogLevel, LogSection
from app.logging_setup import setup_logging
from app.process_lock import singleton
from app.models import User
from app.services import settings_store, support
from app.services.events import log_event

log = logging.getLogger("support.bot")

router = Router(name="support-bot")

COMMANDS = [BotCommand(command="start", description="Написать в поддержку")]

# Сколько сообщений подряд принимаем без ответа админа. Дальше — просьба подождать:
# иначе один человек забьёт ленту админки и утопит остальных.
FLOOD_LIMIT = 10


@router.message(CommandStart())
async def start(message: Message, session: AsyncSession, user: User) -> None:
    await log_event(LogSection.SUPPORT, "bot_start", user_id=user.id, session=session)
    await common.answer(message, texts.support_start(hours=await support.hours(session)))


@router.message(F.text | F.photo)
async def incoming(message: Message, session: AsyncSession, user: User) -> None:
    """Вопрос от клиента. Текст и картинка — всё, что нам нужно принять."""
    text = message.text or message.caption
    if text and len(text) > support.TEXT_MAX:
        await common.answer(message, texts.SUPPORT_TOO_LONG)
        return

    try:
        ticket, fresh = await support.open_ticket(session, user, subject=text)
    except support.SupportError as exc:
        await common.answer(message, texts.esc(exc))
        return

    if ticket.unread_admin >= FLOOD_LIMIT:
        await common.answer(message, texts.SUPPORT_FLOOD)
        return

    photo = message.photo[-1].file_id if message.photo else None
    await support.add_user_message(
        session,
        ticket,
        text=text,
        media_type="photo" if photo else None,
        media_file_id=photo,
        tg_message_id=message.message_id,
    )

    # Автоответ — только на первое сообщение ветки, дальше он превратился бы в шум.
    note = (await support.auto_reply(session)) if fresh else None
    await common.answer(message, note or texts.SUPPORT_SENT)


@router.message()
async def other(message: Message) -> None:
    """Голосовые, кружки, файлы: в админке их не открыть, честнее сказать сразу."""
    await common.answer(message, texts.SUPPORT_ONLY_TEXT)


async def on_error(event: ErrorEvent) -> bool:
    if isinstance(event.exception, TelegramForbiddenError):
        log.info("пользователь заблокировал бота поддержки до отправки ответа")
        return True
    log.exception("ошибка обработки апдейта", exc_info=event.exception)
    await log_event(
        LogSection.SUPPORT,
        "bot_error",
        level=LogLevel.ERROR,
        message=f"{type(event.exception).__name__}: {event.exception}",
    )
    return True


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.update.outer_middleware(DbSessionMiddleware())
    dp.update.outer_middleware(UserMiddleware())
    dp.include_router(router)
    dp.errors.register(on_error)
    return dp


async def send_reply(bot: Bot, item: support.Outgoing) -> int:
    """Отправить клиенту один ответ из админки. Возвращает id сообщения."""
    sent = await common.send_text(bot, item.tg_id, texts.support_reply(text=item.text))
    return sent.message_id


async def run() -> None:
    setup_logging("support")

    if not settings.support_bot_token:
        log.error("SUPPORT_BOT_TOKEN не задан в .env — запускать нечего")
        return

    if settings.db_auto_create:
        await db.create_all()
    async with session_scope() as session:
        await settings_store.ensure_defaults(session)

    bot = build_bot(settings.support_bot_token)
    dp = build_dispatcher()

    # Ответы админов лежат в базе без tg_message_id — забираем и отправляем.
    delivery = asyncio.create_task(
        support.delivery_loop(bot, send_reply), name="support-delivery"
    )

    try:
        me = await with_retry("проверка токена", bot.get_me)
        if me is not None:
            log.info("бот поддержки @%s (id %s) запущен", me.username, me.id)
        await with_retry("список команд", lambda: bot.set_my_commands(COMMANDS))
        await log_event(LogSection.SYSTEM, "support_bot_started")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        delivery.cancel()
        with suppress(asyncio.CancelledError):
            await delivery
        await log_event(LogSection.SYSTEM, "support_bot_stopped")
        await bot.session.close()
        await db.dispose()


def main() -> None:
    with singleton("telegram-support") as acquired:
        if not acquired:
            log.error("бот поддержки уже запущен другим процессом; второй polling остановлен")
            return
        try:
            asyncio.run(run())
        except (KeyboardInterrupt, SystemExit):
            log.info("остановлен вручную")


if __name__ == "__main__":
    main()
