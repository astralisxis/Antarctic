"""Отзывы в боте: оценка, текст, отправка в канал.

Отзыв предлагается кнопкой на карточке купленного номера — один раз на покупку.
Оценка ставится тапом (символы ★, не эмодзи), текст приходит следующим
сообщением; можно и без текста. Публикацию делает app/services/reviews.py.
"""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import common, keyboards, texts
from app.bot.handlers.shop import account_screen, tail_id
from app.enums import ReviewStatus
from app.models import Review, User
from app.services import orders, reviews

router = Router(name="reviews")


class ReviewStates(StatesGroup):
    text = State()


def _stars_arg(data: str | None) -> tuple[int | None, int | None]:
    """«rv:s:<заказ>:<оценка>» → (id заказа, оценка)."""
    parts = (data or "").split(":")
    if len(parts) != 4 or not parts[2].isdigit() or not parts[3].isdigit():
        return None, None
    stars = int(parts[3])
    if not 1 <= stars <= reviews.STARS_MAX:
        return None, None
    return int(parts[2]), stars


@router.callback_query(F.data.startswith("rv:new:"))
async def start_review(
    cb: CallbackQuery, session: AsyncSession, user: User, state: FSMContext
) -> None:
    order_id = tail_id(cb.data)
    order = await orders.get_owned(session, order_id, user) if order_id else None
    if order is None:
        await common.answer_callback(cb, "Заказ не найден", show_alert=True)
        return
    if not await reviews.can_leave(session, order):
        await common.answer_callback(cb, texts.REVIEW_EXISTS, show_alert=True)
        return

    await state.clear()
    await common.edit(
        cb,
        texts.review_ask_stars(title=order.offer_title),
        keyboards.review_stars(order.id),
    )


@router.callback_query(F.data.startswith("rv:s:"))
async def pick_stars(
    cb: CallbackQuery, session: AsyncSession, user: User, state: FSMContext
) -> None:
    order_id, stars = _stars_arg(cb.data)
    order = await orders.get_owned(session, order_id, user) if order_id else None
    if order is None or stars is None:
        await common.answer_callback(cb, "Заказ не найден", show_alert=True)
        return
    if not await reviews.can_leave(session, order):
        await common.answer_callback(cb, texts.REVIEW_EXISTS, show_alert=True)
        return

    await state.set_state(ReviewStates.text)
    await state.update_data(order_id=order.id, stars=stars)
    await common.edit(cb, texts.REVIEW_ASK_TEXT, keyboards.review_text(order.id))


@router.callback_query(F.data.startswith("rv:cancel:"))
async def cancel_review(
    cb: CallbackQuery, session: AsyncSession, user: User, state: FSMContext
) -> None:
    await state.clear()
    order_id = tail_id(cb.data)
    order = await orders.get_owned(session, order_id, user) if order_id else None
    if order is None:
        await common.answer_callback(cb, texts.REVIEW_CANCELLED, show_alert=True)
        return
    text, keyboard = await account_screen(session, order)
    await common.edit(cb, f"{texts.REVIEW_CANCELLED}\n\n{text}", keyboard)


@router.callback_query(F.data.startswith("rv:skip:"))
async def skip_text(
    cb: CallbackQuery, session: AsyncSession, user: User, state: FSMContext
) -> None:
    data = await state.get_data()
    order_id = tail_id(cb.data)
    stars = data.get("stars")
    if order_id is None or not isinstance(stars, int):
        await common.answer_callback(cb, "Начните отзыв заново", show_alert=True)
        return

    order = await orders.get_owned(session, order_id, user)
    if order is None:
        await common.answer_callback(cb, "Заказ не найден", show_alert=True)
        return

    await state.clear()
    try:
        review = await reviews.create(session, user, order, stars=stars, text=None)
    except reviews.ReviewError as exc:
        await common.answer_callback(cb, str(exc)[:190], show_alert=True)
        return

    published = await _deliver(session, review, cb.bot)
    await common.edit(
        cb,
        texts.review_done(published=published),
        keyboards.review_done(channel_url=await reviews.channel_url(session)),
    )


@router.message(ReviewStates.text, F.text)
async def enter_text(
    message: Message, session: AsyncSession, user: User, state: FSMContext
) -> None:
    data = await state.get_data()
    order_id = data.get("order_id")
    stars = data.get("stars")
    if not isinstance(order_id, int) or not isinstance(stars, int):
        await state.clear()
        await common.answer(message, texts.REVIEW_CANCELLED)
        return

    order = await orders.get_owned(session, order_id, user)
    if order is None:
        await state.clear()
        await common.answer(message, texts.ERROR)
        return

    await state.clear()
    try:
        review = await reviews.create(
            session, user, order, stars=stars, text=message.text
        )
    except reviews.ReviewError as exc:
        await common.answer(message, texts.esc(exc))
        return

    published = await _deliver(session, review, message.bot)
    await common.answer(
        message,
        texts.review_done(published=published),
        keyboards.review_done(channel_url=await reviews.channel_url(session)),
    )


async def _deliver(session: AsyncSession, review: Review, bot: Bot | None) -> bool:
    """Публикуем сразу, если отзыв не ждёт модерации.

    Не ушло (канал не задан, прокси оборвался) — не беда: тем же отзывом займётся
    фоновая задача publish_loop.
    """
    if review.status != ReviewStatus.PUBLISHED.value:
        return False
    if bot is not None:
        await reviews.publish_one(session, review, bot)
    return True
