"""Магазин: витрина, карточка страны, покупка, выдача кода входа.

Экраны правятся на месте — один и тот же message переписывается, чат не
забивается. Вся работа с деньгами и маркетом живёт в app/services/orders.py,
здесь только показ и разбор нажатий.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import assets, common, keyboards, texts
from app.enums import LogSection
from app.models import CountryOffer, Order, User
from app.money import fmt_money
from app.services import catalog, orders, reviews, settings_store, users
from app.services import account_cleanup
from app.services.events import log_event

router = Router(name="shop")
log = logging.getLogger("bot.shop")


class GiftPurchaseStates(StatesGroup):
    recipient = State()
    confirm = State()


# --------------------------------------------------------------------------- #
#  Витрина
# --------------------------------------------------------------------------- #
async def _offers(session: AsyncSession) -> list[CountryOffer]:
    return list(
        (
            await session.scalars(
                select(CountryOffer)
                .where(CountryOffer.is_active.is_(True))
                .order_by(CountryOffer.sort, CountryOffer.title)
            )
        ).all()
    )


async def _shop_screen(
    session: AsyncSession, user: User
) -> tuple[str, InlineKeyboardMarkup | None]:
    offers = await _offers(session)
    if not offers:
        return texts.SHOP_EMPTY, None
    rows = [(o.id, catalog.offer_label(o)) for o in offers]
    return texts.shop_root(user.balance), keyboards.shop_countries(rows)


@router.message(F.text.in_(keyboards.BTN_ALIASES[keyboards.BTN_SHOP]))
async def open_shop(message: Message, session: AsyncSession, user: User) -> None:
    await log_event(LogSection.SHOP, "open", user_id=user.id, session=session)

    if not await settings_store.get_bool(session, "shop.enabled"):
        text = await settings_store.get(session, "shop.disabled_text") or texts.SOON
        await common.answer_photo(message, assets.SHOP, text)
        return

    if user.restrict_buy:
        await common.answer_photo(
            message,
            assets.SHOP,
            "Покупки для вашего аккаунта ограничены. Напишите в поддержку.",
        )
        return

    text, keyboard = await _shop_screen(session, user)
    await common.answer_photo(message, assets.COUNTRIES, text, keyboard)


@router.callback_query(F.data == "sh:root")
async def back_to_shop(cb: CallbackQuery, session: AsyncSession, user: User) -> None:
    text, keyboard = await _shop_screen(session, user)
    await common.edit(cb, text, keyboard, photo=assets.COUNTRIES)


# --------------------------------------------------------------------------- #
#  Карточка страны
# --------------------------------------------------------------------------- #
def tail_id(data: str | None) -> int | None:
    """Последний сегмент callback_data. Подделанный id просто не найдётся в базе."""
    if not data:
        return None
    tail = data.rsplit(":", 1)[-1]
    return int(tail) if tail.isdigit() and len(tail) < 12 else None


def _offer_screen(offer: CountryOffer, user: User) -> tuple[str, InlineKeyboardMarkup]:
    in_stock = offer.stock_cached is None or offer.stock_cached > 0
    can_buy = in_stock and user.balance >= offer.price
    text = texts.offer_card(
        title=offer.title,
        price=offer.price,
        balance=user.balance,
        in_stock=in_stock,
        description=offer.description,
        guarantee_hours=max(0, offer.guarantee_hours or 12),
    )
    label = f"Купить за {fmt_money(offer.price)}" if can_buy else None
    gift_label = "Подарить при покупке" if can_buy else None
    return text, keyboards.offer_card(offer.id, buy_label=label, gift_label=gift_label)


@router.callback_query(F.data.startswith("sh:c:"))
async def pick_country(cb: CallbackQuery, session: AsyncSession, user: User) -> None:
    offer_id = tail_id(cb.data)
    offer = await session.get(CountryOffer, offer_id) if offer_id else None
    if offer is None or not offer.is_active:
        await common.answer_callback(cb, "Эта страна больше недоступна", show_alert=True)
        return
    text, keyboard = _offer_screen(offer, user)
    await common.edit(cb, text, keyboard, photo=assets.SHOP)


# --------------------------------------------------------------------------- #
#  Покупка
# --------------------------------------------------------------------------- #
async def account_screen(
    session: AsyncSession, order: Order, *, fresh: bool = False
) -> tuple[str, InlineKeyboardMarkup]:
    """Карточка купленного номера.

    Кода входа в тексте нет: он уходит отдельным сообщением, чтобы не пропадать
    при следующей правке карточки. Срок самостоятельной выдачи — shop.code_hours,
    по умолчанию 12 часов от покупки.
    """
    creds = orders.credentials(order)
    hours = order.guarantee_hours or await orders.code_hours(session)
    is_open = orders.code_open(order, hours)
    until = orders.code_until(order, hours)
    text = texts.account_card(
        title=order.offer_title,
        phone=creds.phone,
        tg_password=creds.tg_password,
        code_at=texts.when(order.code_issued_at) if order.login_code else None,
        code_until=texts.when(until) if until else None,
        code_open=is_open,
        hint=await settings_store.get(session, "shop.code_wait_hint"),
        fresh=fresh,
        replacement_status=order.replacement_status,
        replacement_error=order.replacement_error,
        account_valid=order.account_valid,
    )
    keyboard = keyboards.account_card(
        order.id,
        can_code=is_open,
        can_cleanup=bool(creds.auth_key and creds.dc_id),
        can_review=await reviews.can_leave(session, order),
        can_replace=orders.replacement_open(order),
    )
    return text, keyboard


@router.callback_query(F.data.startswith("sh:buy:"))
async def buy(cb: CallbackQuery, session: AsyncSession, user: User) -> None:
    offer_id = tail_id(cb.data)
    offer = await session.get(CountryOffer, offer_id) if offer_id else None
    if offer is None:
        await common.answer_callback(cb, "Эта страна больше недоступна", show_alert=True)
        return

    # Отказы, которые видны без маркета, показываем сразу: экран ожидания перед
    # мгновенным «нет» только мигает.
    try:
        await orders.precheck(session, user, offer)
    except orders.OrderError as exc:
        text, keyboard = _offer_screen(offer, user)
        await common.edit(cb, f"{texts.esc(exc)}\n\n{text}", keyboard)
        return

    # Промежуточный экран: покупка идёт секунды, и клиент должен видеть, что
    # нажатие принято. Отвечаем на callback здесь, дальше правим без ответа.
    await common.edit(cb, texts.BUYING)

    try:
        order = await orders.buy(session, user, offer)
    except orders.OrderError as exc:
        # Позиция могла обнулиться по наличию — перечитываем карточку.
        await session.refresh(offer)
        text, keyboard = _offer_screen(offer, user)
        await common.edit(cb, f"{texts.esc(exc)}\n\n{text}", keyboard, answer=False)
        return

    text, keyboard = await account_screen(session, order, fresh=True)
    await common.edit(cb, text, keyboard, answer=False)


@router.callback_query(F.data.startswith("sh:giftbuy:"))
async def start_gift_purchase(
    cb: CallbackQuery, session: AsyncSession, user: User, state: FSMContext
) -> None:
    offer_id = tail_id(cb.data)
    offer = await session.get(CountryOffer, offer_id) if offer_id else None
    if offer is None:
        await common.answer_callback(cb, "Эта страна больше недоступна", show_alert=True)
        return
    try:
        await orders.precheck(session, user, offer)
    except orders.OrderError as exc:
        await common.answer_callback(cb, str(exc)[:190], show_alert=True)
        return
    await state.set_state(GiftPurchaseStates.recipient)
    await state.update_data(offer_id=offer.id)
    await common.edit(
        cb,
        texts.gift_purchase_prompt(offer.title),
        keyboards.gift_recipient_back(offer.id),
    )


@router.message(GiftPurchaseStates.recipient, F.text)
async def gift_recipient(
    message: Message, session: AsyncSession, user: User, state: FSMContext
) -> None:
    data = await state.get_data()
    offer_id = data.get("offer_id")
    offer = await session.get(CountryOffer, offer_id) if isinstance(offer_id, int) else None
    if offer is None:
        await state.clear()
        await common.answer(message, texts.ERROR)
        return

    recipient = await users.find_recipient(session, message.text or "", user)
    if recipient is None:
        await common.answer(
            message,
            "Не нашёл получателя. Проверьте Telegram ID или @username. "
            "Получатель должен запустить этого бота хотя бы один раз.",
        )
        return

    await state.set_state(GiftPurchaseStates.confirm)
    await state.update_data(recipient_id=recipient.id)
    await common.answer(
        message,
        texts.gift_purchase_confirm(
            offer.title,
            offer.price,
            recipient.display_name,
            recipient.tg_id,
        ),
        keyboards.gift_purchase_confirm(offer.id),
    )


@router.callback_query(F.data.startswith("sh:giftcancel:"))
async def cancel_gift_purchase(
    cb: CallbackQuery, session: AsyncSession, user: User, state: FSMContext
) -> None:
    await state.clear()
    offer_id = tail_id(cb.data)
    offer = await session.get(CountryOffer, offer_id) if offer_id else None
    if offer is None:
        await common.answer_callback(cb, "Товар больше недоступен", show_alert=True)
        return
    text, keyboard = _offer_screen(offer, user)
    await common.edit(cb, text, keyboard)


@router.callback_query(F.data.startswith("sh:giftconfirm:"))
async def confirm_gift_purchase(
    cb: CallbackQuery, session: AsyncSession, user: User, state: FSMContext
) -> None:
    if await state.get_state() != GiftPurchaseStates.confirm.state:
        await common.answer_callback(cb, "Подтверждение устарело. Начните покупку заново.", show_alert=True)
        return
    data = await state.get_data()
    recipient_id = data.get("recipient_id")
    stored_offer_id = data.get("offer_id")
    offer_id = tail_id(cb.data)
    if stored_offer_id != offer_id:
        await state.clear()
        await common.answer_callback(cb, "Подтверждение устарело. Начните покупку заново.", show_alert=True)
        return
    offer = await session.get(CountryOffer, offer_id) if offer_id else None
    recipient = (
        await session.get(User, recipient_id)
        if isinstance(recipient_id, int)
        else None
    )
    if offer is None or recipient is None or recipient.is_banned:
        await state.clear()
        await common.answer_callback(cb, "Получатель или товар больше недоступны", show_alert=True)
        return

    await state.clear()
    await common.edit(cb, texts.BUYING)
    try:
        order = await orders.buy(session, user, offer, recipient=recipient, source="gift")
    except orders.OrderError as exc:
        await common.edit(cb, texts.esc(str(exc)), answer=False)
        return

    await common.edit(
        cb,
        texts.gift_purchase_done(order.offer_title, recipient.display_name),
        answer=False,
    )
    try:
        await cb.bot.send_message(
            recipient.tg_id,
            "🎁 Вам подарили аккаунт. Откройте раздел «Мои аккаунты», чтобы получить код.",
        )
    except Exception:
        log.info("не удалось уведомить получателя %s о подарке", recipient.tg_id)


# --------------------------------------------------------------------------- #
#  Купленный номер
# --------------------------------------------------------------------------- #
@router.callback_query(F.data.startswith("sh:acc:"))
async def open_account(cb: CallbackQuery, session: AsyncSession, user: User) -> None:
    order_id = tail_id(cb.data)
    order = await orders.get_owned(session, order_id, user) if order_id else None
    if order is None:
        await common.answer_callback(cb, "Заказ не найден", show_alert=True)
        return
    text, keyboard = await account_screen(session, order)
    await common.edit(cb, text, keyboard)


@router.callback_query(F.data.startswith("sh:code:"))
async def get_code(cb: CallbackQuery, session: AsyncSession, user: User) -> None:
    """Код входа — отдельным сообщением.

    Правкой карточки код показывать нельзя: следующее нажатие любой кнопки
    затирает его, а код нужен под рукой, пока человек вводит его в Telegram.
    """
    order_id = tail_id(cb.data)
    order = await orders.get_owned(session, order_id, user) if order_id else None
    if order is None:
        await common.answer_callback(cb, "Заказ не найден", show_alert=True)
        return

    try:
        value = await orders.issue_code(session, order)
    except orders.OrderError as exc:
        # Всплывашка, а не правка экрана: карточка с номером должна остаться.
        await common.answer_callback(cb, str(exc)[:190], show_alert=True)
        text, keyboard = await account_screen(session, order)
        await common.edit(cb, text, keyboard, answer=False)
        return

    await common.answer_callback(cb, "Код пришёл ниже")
    creds = orders.credentials(order)
    if isinstance(cb.message, Message):
        await common.answer(
            cb.message,
            texts.login_code(
                title=order.offer_title,
                phone=creds.phone,
                value=value,
                hint=await settings_store.get(session, "shop.code_wait_hint"),
            ),
        )
    # Карточку обновляем следом: в ней меняются время выдачи и остаток срока.
    text, keyboard = await account_screen(session, order)
    await common.edit(cb, text, keyboard, answer=False)


@router.callback_query(F.data.startswith("sh:reset:"))
async def reset_sessions(cb: CallbackQuery, session: AsyncSession, user: User) -> None:
    order_id = tail_id(cb.data)
    order = await orders.get_owned(session, order_id, user) if order_id else None
    if order is None:
        await common.answer_callback(cb, "Заказ не найден", show_alert=True)
        return

    try:
        await orders.reset_auth(session, order)
    except orders.OrderError as exc:
        await common.answer_callback(cb, str(exc)[:190], show_alert=True)
        return
    await common.answer_callback(cb, texts.RESET_DONE, show_alert=True)


@router.callback_query(F.data.startswith("sh:replace:"))
async def replace_account(cb: CallbackQuery, session: AsyncSession, user: User) -> None:
    order_id = tail_id(cb.data)
    order = await orders.get_owned(session, order_id, user) if order_id else None
    if order is None:
        await common.answer_callback(cb, "Заказ не найден", show_alert=True)
        return
    if cb.data and ":yes:" in cb.data:
        try:
            await orders.request_replacement(session, order)
        except orders.OrderError as exc:
            await common.answer_callback(cb, str(exc)[:190], show_alert=True)
            return
        await common.edit(cb, texts.replacement_sent(), keyboards.account_back(order.id))
        return
    if not orders.replacement_open(order):
        await common.answer_callback(cb, "Срок гарантии на замену вышел", show_alert=True)
        return
    until = orders.guarantee_until(order)
    await common.edit(
        cb,
        texts.replacement_prompt(
            order.guarantee_hours or 12,
            texts.when(until) if until else None,
        ),
        keyboards.replacement_confirm(order.id),
    )


@router.callback_query(F.data.startswith("sh:clean:"))
async def cleanup_prompt(cb: CallbackQuery, session: AsyncSession, user: User) -> None:
    """Show a destructive-action confirmation; no network work happens yet."""
    order_id = tail_id(cb.data)
    order = await orders.get_owned(session, order_id, user) if order_id else None
    if order is None:
        await common.answer_callback(cb, "Заказ не найден", show_alert=True)
        return
    creds = orders.credentials(order)
    if not creds.auth_key or not creds.dc_id:
        await common.answer_callback(cb, "Для этого заказа очистка недоступна: нет данных подключения.", show_alert=True)
        return
    if cb.data and ":yes:" in cb.data:
        await common.answer_callback(cb, "Очистка запущена")
        await common.edit(
            cb,
            texts.CLEANUP_RUNNING,
            keyboards.account_back(order.id),
            answer=False,
            remove_photo=True,
        )
        try:
            report = await account_cleanup.cleanup_order(session, order)
        except (account_cleanup.TelegramClientError, orders.OrderError) as exc:
            text, keyboard = await account_screen(session, order)
            await common.edit(cb, f"ℹ️ {texts.esc(str(exc))}\n\n{text}", keyboard, answer=False)
            return
        except Exception:
            log.exception("account cleanup failed for order %s", order.id)
            text, keyboard = await account_screen(session, order)
            await common.edit(
                cb,
                f"ℹ️ Не удалось завершить очистку. Напишите в поддержку.\n\n{text}",
                keyboard,
                answer=False,
            )
            return
        await common.edit(
            cb,
            texts.cleanup_report(report.as_dict()),
            keyboards.account_back(order.id),
            answer=False,
            remove_photo=True,
        )
        return
    await common.edit(
        cb,
        texts.cleanup_confirm(),
        keyboards.cleanup_confirm(order.id),
        remove_photo=True,
    )
