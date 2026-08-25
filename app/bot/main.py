"""Основной бот магазина.

Запуск: python -m app.bot.main  (или scripts\\bot.cmd)

Long polling. Вебхуки не берём: админка живёт на localhost, а бот должен
работать и там, где https ещё не настроен. Прокси до api.telegram.org —
BOT_PROXY в .env, без него в сетях с блокировкой Telegram бот не поднимется.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import TypeVar

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramConflictError,
    TelegramForbiddenError,
    TelegramNetworkError,
)
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    ErrorEvent,
    MenuButtonCommands,
    MenuButtonWebApp,
    Message,
    WebAppInfo,
)

from app import db
from app.bot import texts
from app.bot.handlers import ROUTERS
from app.bot.handlers.topup import announce
from app.bot.middlewares import DbSessionMiddleware, StateResetMiddleware, UserMiddleware
from app.bot.polling import DUPLICATE_INSTANCE_EXIT_CODE, SingleInstanceDispatcher
from app.config import settings
from app.db import session_scope
from app.enums import LogLevel, LogSection
from app.integrations.cryptobot import close_cryptobot
from app.integrations.lzt import close_lzt
from app.integrations.xrocket import close_xrocket
from app.logging_setup import setup_logging
from app.process_lock import singleton
from app.services import broadcasts, catalog, payments, reviews, settings_store
from app.services.events import log_event

log = logging.getLogger("bot")

T = TypeVar("T")

COMMANDS = [
    BotCommand(command="start", description="Открыть магазин"),
    BotCommand(command="menu", description="Главное меню"),
]

# Дешёвые прокси рвут часть соединений на рукопожатии. Поллинг переживает это сам,
# а вызовы на старте — нет, поэтому повторяем их.
STARTUP_TRIES = 5
STARTUP_PAUSE = 1.5


async def with_retry(label: str, action: Callable[[], Awaitable[T]]) -> T | None:
    """Повторить сетевой вызов. None — не получилось, но старт из-за этого не отменяем."""
    for attempt in range(1, STARTUP_TRIES + 1):
        try:
            return await action()
        except TelegramNetworkError as exc:
            log.warning("%s: связь оборвалась (%s/%s) — %s", label, attempt, STARTUP_TRIES, exc)
            if attempt < STARTUP_TRIES:
                await asyncio.sleep(STARTUP_PAUSE * attempt)
    log.error("%s: не вышло за %s попыток", label, STARTUP_TRIES)
    return None


def build_bot(token: str) -> Bot:
    """Бот с общими настройками: HTML-разметка и прокси, если он задан."""
    # Баннеры отправляются multipart-запросом. На медленном прокси стандартных
    # 60 секунд недостаточно, поэтому даём Telegram API две минуты.
    session = AiohttpSession(
        proxy=settings.bot_proxy,
        timeout=120,
    )
    return Bot(
        token=token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )


def build_dispatcher() -> Dispatcher:
    dp = SingleInstanceDispatcher()
    # Сессия базы одна на апдейт, дальше пользователь — хендлеры получают готовое.
    dp.update.outer_middleware(DbSessionMiddleware())
    dp.update.outer_middleware(UserMiddleware())
    # Ожидание ввода снимаем до хендлеров, иначе кнопка меню уедет в состояние.
    dp.message.outer_middleware(StateResetMiddleware())
    for router in ROUTERS:
        dp.include_router(router)
    dp.errors.register(on_error)
    return dp


async def on_error(event: ErrorEvent) -> bool:
    """Ошибка в хендлере не должна ронять поллинг и оставлять клиента без ответа."""
    if isinstance(event.exception, TelegramForbiddenError):
        # Пользователь мог отправить команду и сразу заблокировать бота. Это
        # штатный отказ доставки, а не ошибка приложения; отвечать уже некому.
        user = getattr(event.update, "event_from_user", None)
        log.info("пользователь заблокировал бота до отправки ответа: tg=%s", user.id if user else None)
        return True
    log.exception("ошибка обработки апдейта", exc_info=event.exception)

    update = event.update
    user = getattr(update, "event_from_user", None)
    await log_event(
        LogSection.SYSTEM,
        "bot_error",
        level=LogLevel.ERROR,
        message=f"{type(event.exception).__name__}: {event.exception}",
        payload={"tg_id": user.id if user else None, "update_id": update.update_id},
    )

    target: Message | None = None
    if isinstance(update.message, Message):
        target = update.message
    elif isinstance(update.callback_query, CallbackQuery) and isinstance(
        update.callback_query.message, Message
    ):
        target = update.callback_query.message
        try:
            await update.callback_query.answer()
        except Exception:
            pass
    if target is not None:
        try:
            await target.answer(texts.ERROR)
        except Exception:
            log.debug("не удалось отправить сообщение об ошибке")
    return True


async def setup_profile(bot: Bot) -> None:
    """Команды и кнопка меню. Мини-апп подключается только по https."""
    await with_retry("список команд", lambda: bot.set_my_commands(COMMANDS))

    webapp = settings.webapp_base_url.rstrip("/")
    if settings.bot_menu_button and webapp.startswith("https://"):
        button = MenuButtonWebApp(text="Магазин", web_app=WebAppInfo(url=webapp))
    else:
        # http Telegram для мини-аппа не принимает — оставляем обычное меню команд
        button = MenuButtonCommands()
    await with_retry("кнопка меню", lambda: bot.set_chat_menu_button(menu_button=button))


async def check_identity(bot: Bot) -> None:
    """Сверить BOT_USERNAME с реальным: на нём собираются реферальные ссылки."""
    me = await with_retry("проверка токена", bot.get_me)
    if me is None:
        log.warning("Telegram не ответил на старте — поллинг будет пробовать дальше сам")
        return

    configured = settings.bot_username.lstrip("@").lower()
    if not configured:
        log.warning("BOT_USERNAME не задан — реферальные ссылки будут битыми (@%s)", me.username)
    elif me.username and configured != me.username.lower():
        log.warning(
            "BOT_USERNAME=%s не совпадает с реальным @%s — поправьте .env",
            settings.bot_username,
            me.username,
        )
    log.info("бот @%s (id %s) запущен", me.username, me.id)


async def run() -> None:
    setup_logging("bot")

    if not settings.bot_token:
        log.error("BOT_TOKEN не задан в .env — запускать нечего")
        return

    if settings.db_auto_create:
        await db.create_all()
    async with session_scope() as session:
        await settings_store.ensure_defaults(session)

    bot = build_bot(settings.bot_token)
    dp = build_dispatcher()

    # Наличие обновляется в фоне: витрина читает кэш и не ждёт маркет.
    stock = asyncio.create_task(catalog.stock_loop(), name="stock")
    # Оплату счетов узнаём опросом провайдеров: публичного https для вебхуков нет.
    pay = asyncio.create_task(
        payments.poll_loop(lambda item: announce(bot, item)), name="payments"
    )
    # Отзывы в канал шлёт бот, а не админка: у неё нет socks-транспорта до Telegram.
    posts = asyncio.create_task(reviews.publish_loop(bot), name="reviews")
    broadcast = asyncio.create_task(broadcasts.delivery_loop(bot), name="broadcasts")
    background = (stock, pay, posts, broadcast)

    try:
        await check_identity(bot)
        await setup_profile(bot)
        await log_event(LogSection.SYSTEM, "bot_started")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        for task in background:
            task.cancel()
        for task in background:
            with suppress(asyncio.CancelledError):
                await task
        await log_event(LogSection.SYSTEM, "bot_stopped")
        await bot.session.close()
        await close_lzt()
        await close_cryptobot()
        await close_xrocket()
        await db.dispose()


def main() -> int:
    with singleton("telegram-main") as acquired:
        if not acquired:
            log.error("основной бот уже запущен другим процессом; второй polling остановлен")
            return DUPLICATE_INSTANCE_EXIT_CODE
        try:
            asyncio.run(run())
        except TelegramConflictError:
            log.critical(
                "этот BOT_TOKEN уже используется другим polling-процессом; "
                "экземпляр остановлен без перезапуска"
            )
            return DUPLICATE_INSTANCE_EXIT_CODE
        except KeyboardInterrupt:
            log.info("остановлен вручную")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
