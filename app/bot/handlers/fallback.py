"""Ответ на всё, что не разобрали раньше. Подключается последним."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import common, texts
from app.bot.keyboards import main_menu
from app.services import settings_store

router = Router(name="fallback")


@router.message(F.text)
async def unknown_text(message: Message, session: AsyncSession) -> None:
    earn_on = await settings_store.get_bool(session, "earn.enabled")
    await message.answer(texts.UNKNOWN, reply_markup=main_menu(earn=earn_on))


@router.message()
async def unknown_content(message: Message, session: AsyncSession) -> None:
    """Фото, стикеры, голосовые: магазин их не обрабатывает."""
    await common.send_menu(message, session, greeting=texts.UNKNOWN)


@router.callback_query()
async def stale_callback(cb: CallbackQuery) -> None:
    """Кнопка из старого сообщения — снимаем «часики», чтобы клиент не ждал."""
    await common.answer_callback(cb)
