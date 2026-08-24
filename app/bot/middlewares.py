"""Мидлвари бота: сессия базы, пользователь, ограничения.

Порядок важен: сессия → пользователь. Дальше в хендлер приходят готовые
`session` и `user`, самим их искать не надо.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, Update, User as TgUser

from app.bot import texts
from app.bot.keyboards import MAIN_BUTTONS
from app.db import bind_session, session_scope, unbind_session
from app.services import users
from app.timeutil import to_local

log = logging.getLogger("bot.mw")

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


class DbSessionMiddleware(BaseMiddleware):
    """Одна транзакция на апдейт: либо всё применилось, либо ничего."""

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        async with session_scope() as session:
            token = bind_session(session)
            try:
                data["session"] = session
                return await handler(event, data)
            finally:
                unbind_session(token)


class UserMiddleware(BaseMiddleware):
    """Регистрирует клиента и отсекает заблокированных."""

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        tg_user: TgUser | None = data.get("event_from_user")
        if tg_user is None or tg_user.is_bot:
            return None

        session = data["session"]
        message, callback = _unwrap(event)
        payload = _start_payload(message)
        user, created = await users.touch(
            session,
            tg_id=tg_user.id,
            username=tg_user.username,
            first_name=tg_user.first_name,
            last_name=tg_user.last_name,
            language_code=tg_user.language_code,
            is_premium=bool(tg_user.is_premium),
            payload=payload,
        )
        await users.unban_if_expired(session, user)

        # touch() is a write for a new user and periodically for last_seen_at.
        # Commit it before handlers start talking to Telegram, otherwise sqlite
        # keeps the only writer slot for the whole network round trip.
        await session.commit()

        if user.is_banned:
            await _tell_banned(user, message, callback)
            return None

        data["user"] = user
        data["is_new_user"] = created
        return await handler(event, data)


class StateResetMiddleware(BaseMiddleware):
    """Снимает ожидание ввода, когда человек ушёл из раздела.

    Висит на dp.message: если пришла кнопка главного меню или команда, ждать
    ввода (например, суммы пополнения) больше нечего. Без этого следующее любое
    сообщение уехало бы в хендлер состояния — человек написал бы «привет», а бот
    ответил бы «не понял сумму».
    """

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        state: Any = data.get("state")
        text = (getattr(event, "text", None) or "").strip()
        if state is not None and (text in MAIN_BUTTONS or text.startswith("/")):
            if await state.get_state() is not None:
                await state.clear()
        return await handler(event, data)


def _unwrap(event: TelegramObject) -> tuple[Message | None, CallbackQuery | None]:
    """Мидлварь висит на dp.update, поэтому сюда приходит Update, а не Message."""
    if isinstance(event, Update):
        return event.message, event.callback_query
    if isinstance(event, Message):
        return event, None
    if isinstance(event, CallbackQuery):
        return None, event
    return None, None


def _start_payload(message: Message | None) -> str | None:
    """Достать аргумент из «/start r12» — только там реферальная ссылка и приходит."""
    if message is None or not message.text:
        return None
    if not message.text.startswith("/start"):
        return None
    parts = message.text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else None


async def _tell_banned(user: Any, message: Message | None, callback: CallbackQuery | None) -> None:
    until = to_local(user.banned_until)
    text = texts.banned(user.ban_reason, until.strftime("%d.%m.%Y %H:%M") if until else None)
    try:
        if callback is not None:
            await callback.answer("Доступ ограничен", show_alert=True)
        elif message is not None:
            await message.answer(text)
    except Exception:  # заблокировал бота — не наша беда
        log.debug("не удалось сообщить об ограничении user=%s", user.id)
