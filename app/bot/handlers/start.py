"""Старт и главное меню."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import common
from app.enums import LogSection
from app.models import User
from app.services import orders
from app.bot import texts
from app.services.events import log_event

router = Router(name="start")


@router.message(CommandStart())
async def cmd_start(message: Message, session: AsyncSession, user: User, is_new_user: bool) -> None:
    # Регистрация и привязка реферала уже прошли в мидлвари: она видит /start
    # раньше хендлера и обрабатывает аргумент ссылки.
    if not is_new_user:
        await log_event(
            LogSection.USER, "start", user_id=user.id, message="повторный вход", session=session
        )
    payload = _payload(message)
    if payload.startswith("gift_"):
        try:
            order = await orders.accept_transfer(session, payload[5:], user)
        except orders.OrderError as exc:
            await message.answer(texts.esc(str(exc)))
        else:
            await message.answer(texts.gift_received(order.offer_title))
    await common.send_menu(message, session)


def _payload(message: Message) -> str:
    parts = (message.text or "").split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


@router.message(Command("menu"))
async def cmd_menu(message: Message, session: AsyncSession) -> None:
    await common.send_menu(message, session)


@router.message(Command("help"))
async def cmd_help(message: Message, session: AsyncSession) -> None:
    await common.send_menu(message, session)
