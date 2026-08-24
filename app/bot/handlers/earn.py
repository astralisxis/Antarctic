"""Заработать: инструкции и контакт менеджера. Задания не проверяем — так решено."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import common, keyboards, texts
from app.enums import LogSection
from app.models import User
from app.services import settings_store
from app.services.events import log_event

router = Router(name="earn")

SECTIONS = {
    "comments": ("Комментарии TikTok", "earn.comments.text", None),
    "video": ("Видео TikTok", "earn.video.text", "earn.video.enabled"),
}


@router.message(F.text.in_(keyboards.BTN_ALIASES[keyboards.BTN_EARN]))
async def open_earn(message: Message, session: AsyncSession, user: User) -> None:
    if not await settings_store.get_bool(session, "earn.enabled"):
        await common.answer(message, texts.EARN_OFF)
        return
    video = await settings_store.get_bool(session, "earn.video.enabled")
    await log_event(LogSection.EARN, "open", user_id=user.id, session=session)
    await common.answer(message, texts.EARN_ROOT, keyboards.earn_menu(video=video))


@router.callback_query(F.data == "ea:root")
async def back(cb: CallbackQuery, session: AsyncSession) -> None:
    video = await settings_store.get_bool(session, "earn.video.enabled")
    await common.edit(cb, texts.EARN_ROOT, keyboards.earn_menu(video=video))


@router.callback_query(F.data.startswith("ea:"))
async def section(cb: CallbackQuery, session: AsyncSession, user: User) -> None:
    key = (cb.data or "").split(":", 1)[1]
    if key not in SECTIONS:
        await common.answer_callback(cb)
        return
    title, text_key, flag_key = SECTIONS[key]

    # Кнопку могли скрыть в админке, пока экран висел у клиента открытым.
    if flag_key and not await settings_store.get_bool(session, flag_key):
        await common.answer_callback(cb, "Направление больше недоступно", show_alert=True)
        return

    body = await settings_store.get(session, text_key) or texts.SOON
    manager = (await settings_store.get(session, "earn.manager") or "").strip()
    await log_event(LogSection.EARN, f"open_{key}", user_id=user.id, session=session)
    await common.edit(
        cb, texts.earn_section(title, body), keyboards.earn_back(manager=manager or None)
    )
