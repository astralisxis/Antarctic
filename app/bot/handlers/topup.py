"""Пополнение баланса: сумма → способ → счёт → зачисление.

Порядок «сначала сумма, потом способ» выбран не случайно: лимиты магазина от
способа не зависят, а вот доступный способ от суммы зависит (у xRocket свой
минимум в крипте). Так человек вводит сумму один раз.

Сумма едет в callback_data кнопок способов, а не только в состоянии FSM: память
состояний живёт в процессе, и после перезапуска бота кнопка обязана работать.
Состояние нужно лишь для того, чтобы понять, что следующее сообщение — это сумма.
Своя сумма при этом принимается в любой момент, а не только пока бот её ждёт:
сообщение из одних цифр в магазине номеров ничем другим быть не может.

Вся работа с провайдерами — в app/services/payments.py, здесь только экраны.
"""

from __future__ import annotations

import logging
import re
from contextlib import suppress

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import assets, common, keyboards, texts
from app.enums import PROVIDER_TITLES, LogSection, PaymentStatus
from app.integrations.pay import PayError
from app.models import Payment, User
from app.money import fmt_money, parse_rub, rub_to_kop
from app.services import payments
from app.services.events import log_event

log = logging.getLogger("bot.topup")

router = Router(name="topup")

# Быстрые суммы — кратные минимуму: предсказуемо и без «умного» округления.
QUICK_FACTORS = (1, 2, 5, 10)

# «500», «500 р», «1 500,50 ₽» — сообщение, которое может быть только суммой.
# Значение достаём первой группой: money.parse_rub про «руб» не знает.
AMOUNT_RE = re.compile(
    r"^\s*(\d[\d\s.,]{0,12}?)\s*(?:р|руб\.?|рубл\w*|₽)?\s*$", re.IGNORECASE
)


class TopupStates(StatesGroup):
    amount = State()


# --------------------------------------------------------------------------- #
#  Экран суммы
# --------------------------------------------------------------------------- #
def _quick_amounts(minimum: int, maximum: int) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for factor in QUICK_FACTORS:
        value = minimum * factor
        if value > maximum:
            break
        out.append((value, fmt_money(value)))
    return out


async def _root_screen(
    session: AsyncSession, user: User
) -> tuple[str, InlineKeyboardMarkup | None]:
    low, high = await payments.limits(session)
    text = texts.topup_root(balance=user.balance, minimum=low, maximum=high)
    quick = _quick_amounts(low, high)
    return text, keyboards.topup_amounts(quick) if quick else None


@router.message(F.text.in_(keyboards.BTN_ALIASES[keyboards.BTN_TOPUP]))
async def open_topup(
    message: Message, session: AsyncSession, user: User, state: FSMContext
) -> None:
    await log_event(LogSection.PAYMENT, "open", user_id=user.id, session=session)

    if user.restrict_topup:
        await state.clear()
        await common.answer(message, texts.TOPUP_RESTRICTED)
        return
    if not await payments.methods(session):
        await state.clear()
        await common.answer(message, texts.TOPUP_OFF)
        return

    await state.set_state(TopupStates.amount)
    text, keyboard = await _root_screen(session, user)
    await common.answer_photo(message, assets.TOPUP, text, keyboard)


@router.callback_query(F.data == "tp:root")
async def back_to_root(
    cb: CallbackQuery, session: AsyncSession, user: User, state: FSMContext
) -> None:
    await state.set_state(TopupStates.amount)
    text, keyboard = await _root_screen(session, user)
    await common.edit(cb, text, keyboard)


@router.message(TopupStates.amount, F.text)
async def enter_amount(
    message: Message, session: AsyncSession, user: User, state: FSMContext
) -> None:
    """Сумма из сообщения, когда бот её ждёт.

    Кнопки главного меню и команды сюда не доходят — состояние снимает
    StateResetMiddleware до хендлеров.
    """
    await _take_amount(message, session, user, state, expected=True)


@router.message(StateFilter(None), F.text.regexp(AMOUNT_RE))
async def loose_amount(
    message: Message, session: AsyncSession, user: User, state: FSMContext
) -> None:
    """Сумма, присланная без ожидания: «500» в любой момент — это пополнение.

    Состояние живёт в памяти процесса и снимается после первого же экрана, а
    человек пишет сумму когда захочет: после быстрой кнопки, после счёта, после
    перезапуска бота. Раньше такое сообщение уезжало в «не понял» — то есть своя
    сумма работала только в первые секунды после «Пополнить баланс».

    Фильтр по состоянию обязателен: иначе число перебьёт ввод отзыва.
    """
    await _take_amount(message, session, user, state, expected=False)


async def _take_amount(
    message: Message,
    session: AsyncSession,
    user: User,
    state: FSMContext,
    *,
    expected: bool,
) -> None:
    """Разобрать сумму и показать способы оплаты."""
    if user.restrict_topup:
        await state.clear()
        await common.answer(message, texts.TOPUP_RESTRICTED)
        return

    low, high = await payments.limits(session)
    raw = (message.text or "").strip()
    match = AMOUNT_RE.match(raw)
    amount = parse_rub(match.group(1) if match else raw)
    if amount is None or amount < low or amount > high:
        # Ждём исправленную сумму: следующее число разберёт этот же хендлер.
        await state.set_state(TopupStates.amount)
        note = texts.topup_bad_amount(minimum=low, maximum=high)
        if expected:
            await common.answer(message, note)
            return
        # Сумму прислали сами, границ человек мог не знать — показываем весь экран.
        text, keyboard = await _root_screen(session, user)
        await common.answer(message, f"{note}\n\n{text}", keyboard)
        return

    await state.clear()
    text, keyboard = await _methods_screen(session, amount)
    await common.answer(message, text, keyboard)


@router.callback_query(F.data.startswith("tp:a:"))
async def quick_amount(cb: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    amount = _tail_int(cb.data)
    low, high = await payments.limits(session)
    if amount is None or amount < low or amount > high:
        await common.answer_callback(cb, "Сумма больше не подходит", show_alert=True)
        return
    await state.clear()
    text, keyboard = await _methods_screen(session, amount)
    await common.edit(cb, text, keyboard)


# --------------------------------------------------------------------------- #
#  Экран способов
# --------------------------------------------------------------------------- #
async def _methods_screen(
    session: AsyncSession, amount: int
) -> tuple[str, InlineKeyboardMarkup | None]:
    ways = await payments.methods(session)
    if not ways:
        return texts.TOPUP_OFF, None
    hints = [f"{m.title} — {m.hint}" for m in ways if m.hint]
    return (
        texts.topup_methods(amount=amount, hints=hints),
        keyboards.topup_methods([(m.provider, m.title) for m in ways], amount),
    )


@router.callback_query(F.data.startswith("tp:p:"))
async def pick_method(
    cb: CallbackQuery, session: AsyncSession, user: User, state: FSMContext
) -> None:
    provider, amount = _method_args(cb.data)
    if provider is None or amount is None:
        await common.answer_callback(cb)
        return
    await state.clear()

    # Счёт выставляется через сеть — секунда-две. Показываем, что нажатие принято.
    await common.edit(cb, texts.TOPUP_CREATING)

    try:
        payment, calc = await payments.create(session, user, provider, amount)
    except payments.PaymentError as exc:
        text, keyboard = await _methods_screen(session, amount)
        await common.edit(cb, f"{texts.esc(exc)}\n\n{text}", keyboard, answer=False)
        return

    text, keyboard = await _invoice_screen(session, payment, calc)
    await common.edit(cb, text, keyboard, answer=False)


async def _invoice_screen(
    session: AsyncSession, payment: Payment, calc: payments.Quote
) -> tuple[str, InlineKeyboardMarkup | None]:
    rate = None
    if calc.rate is not None:
        rate = f"1 {calc.asset} ≈ {fmt_money(rub_to_kop(calc.rate))}"
        if calc.markup:
            rate += f", наценка {calc.markup}%"
    text = texts.topup_invoice(
        amount=payment.amount,
        method=PROVIDER_TITLES.get(payment.provider, payment.provider),
        charge=calc.charge_text,
        minutes=await payments.invoice_ttl(session) // 60,
        rate=rate,
    )
    if not payment.invoice_url:
        return text, None
    return text, keyboards.topup_invoice(payment.id, payment.invoice_url)


# --------------------------------------------------------------------------- #
#  Счёт: проверка и отмена
# --------------------------------------------------------------------------- #
@router.callback_query(F.data.startswith("tp:c:"))
async def check_invoice(cb: CallbackQuery, session: AsyncSession, user: User) -> None:
    payment = await _owned(session, cb.data, user)
    if payment is None:
        await common.answer_callback(cb, "Счёт не найден", show_alert=True)
        return

    if payment.status == PaymentStatus.PAID.value:
        # Опрос успел раньше кнопки — просто показываем итог.
        await session.refresh(user, ["balance"])
        await common.edit(
            cb,
            texts.topup_paid(amount=payment.credited or payment.amount, balance=user.balance),
            keyboards.topup_done(),
        )
        return
    if payment.status != PaymentStatus.PENDING.value:
        await common.edit(cb, texts.TOPUP_STALE)
        return

    try:
        credited = await payments.refresh(session, payment)
    except PayError as exc:
        log.warning("проверка счёта %s не удалась: %s", payment.id, exc)
        await common.answer_callback(cb, "Платёжный сервис не ответил. Попробуйте ещё раз.", show_alert=True)
        return

    if credited is not None:
        await common.edit(
            cb,
            texts.topup_paid(amount=credited.amount, balance=credited.balance),
            keyboards.topup_done(),
        )
        # Пригласившему сообщаем сами: фоновая задача этот счёт уже не увидит.
        if cb.bot is not None:
            await announce(cb.bot, credited, to_payer=False)
        return

    if payment.status == PaymentStatus.EXPIRED.value:
        await common.edit(cb, texts.TOPUP_STALE)
        return
    await common.answer_callback(cb, texts.TOPUP_WAIT, show_alert=True)


@router.callback_query(F.data.startswith("tp:x:"))
async def cancel_invoice(
    cb: CallbackQuery, session: AsyncSession, user: User, state: FSMContext
) -> None:
    payment = await _owned(session, cb.data, user)
    if payment is None:
        await common.answer_callback(cb, "Счёт не найден", show_alert=True)
        return
    if payment.status != PaymentStatus.PENDING.value:
        await common.edit(cb, texts.TOPUP_STALE)
        return

    if not await payments.cancel(session, payment):
        # Не говорим, что счёт отменён, пока провайдер этого не подтвердил:
        # он мог принять оплату прямо перед сетевым сбоем. Pending-счёт
        # останется доступен для фоновой проверки оплаты.
        await common.answer_callback(
            cb,
            "Не удалось подтвердить отмену у платёжного сервиса. "
            "Счёт оставлен активным, попробуйте проверить оплату позже.",
            show_alert=True,
        )
        return
    await state.set_state(TopupStates.amount)
    text, keyboard = await _root_screen(session, user)
    await common.edit(cb, f"{texts.TOPUP_CANCELLED}\n\n{text}", keyboard)


# --------------------------------------------------------------------------- #
#  Уведомления об оплате
# --------------------------------------------------------------------------- #
async def announce(bot: Bot, item: payments.Credited, *, to_payer: bool = True) -> None:
    """Сообщить об оплате плательщику и пригласившему.

    Вызывается из фоновой задачи (там нужны оба сообщения) и из кнопки
    «Проверить оплату» (там плательщик уже видит экран, нужен только реферал).
    Заблокировавший бота клиент — не повод ронять зачисление, поэтому ошибки
    отправки только пишем в лог.
    """
    if to_payer:
        try:
            await bot.send_message(
                item.tg_id,
                texts.topup_paid(amount=item.amount, balance=item.balance),
                reply_markup=keyboards.topup_done(),
            )
        except Exception:
            log.info("не удалось сообщить об оплате tg=%s", item.tg_id)

    if item.ref_tg_id and item.ref_amount:
        with suppress(Exception):
            await bot.send_message(
                item.ref_tg_id,
                texts.topup_referral(amount=item.ref_amount, balance=item.ref_balance),
            )


# --------------------------------------------------------------------------- #
#  Разбор callback_data
# --------------------------------------------------------------------------- #
def _tail_int(data: str | None) -> int | None:
    if not data:
        return None
    tail = data.rsplit(":", 1)[-1]
    return int(tail) if tail.isdigit() and len(tail) < 12 else None


def _method_args(data: str | None) -> tuple[str | None, int | None]:
    """«tp:p:<провайдер>:<копейки>» → (провайдер, сумма)."""
    parts = (data or "").split(":")
    if len(parts) != 4 or not parts[3].isdigit() or len(parts[3]) > 11:
        return None, None
    return parts[2], int(parts[3])


async def _owned(session: AsyncSession, data: str | None, user: User) -> Payment | None:
    """Счёт из callback_data, только свой: id в кнопке можно подделать."""
    payment_id = _tail_int(data)
    if payment_id is None:
        return None
    payment = await session.get(Payment, payment_id)
    if payment is None or payment.user_id != user.id:
        return None
    return payment
