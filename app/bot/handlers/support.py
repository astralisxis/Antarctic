"""Поддержка: часы работы и переход в отдельный бот поддержки.

Сама переписка идёт в боте поддержки (app/bot/support_main.py) и в админке —
здесь только часы работы и кнопка-ссылка, чтобы человек не искал бота руками.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import assets, common, keyboards, texts
from app.config import settings
from app.enums import LogSection
from app.models import User
from app.services import settings_store
from app.services.events import log_event

router = Router(name="support")


@router.message(F.text.in_(keyboards.BTN_ALIASES[keyboards.BTN_SUPPORT]))
async def open_support(message: Message, session: AsyncSession, user: User) -> None:
    hours = await settings_store.get(session, "support.hours") or texts.DASH
    username = settings.support_bot_username.lstrip("@")
    await log_event(LogSection.SUPPORT, "open", user_id=user.id, session=session)
    await common.answer_photo(
        message,
        assets.SUPPORT,
        texts.support(hours=hours, has_bot=bool(username)),
        keyboards.support_link(settings.support_bot_link) if username else None,
    )
