"""Профиль: баланс, рефералы, истории."""

from __future__ import annotations

from urllib.parse import quote

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import assets, common, keyboards, texts
from app.enums import ORDER_STATUS_TITLES, PAYMENT_STATUS_TITLES, PROVIDER_TITLES, LogSection
from app.models import Payment, User
from app.money import fmt_money
from app.services import orders as orders_service
from app.services import leaders as leaders_service
from app.services import promos as promos_service
from app.services import settings_store, users
from app.services.events import log_event

router = Router(name="profile")

# Сколько строк показываем в историях. Больше — уже не читают, а сообщение
# телеграма не бесконечное.
PAGE = 20


class ProfileStates(StatesGroup):
    promo = State()


def _screen(user: User) -> str:
    return texts.profile(
        tg_id=user.tg_id,
        balance=user.balance,
        total_topup=user.total_topup,
        orders_count=user.orders_count,
        ref_earned=user.ref_earned,
    )


@router.message(F.text.in_(keyboards.BTN_ALIASES[keyboards.BTN_PROFILE]))
async def open_profile(message: Message, session: AsyncSession, user: User) -> None:
    await log_event(LogSection.USER, "open_profile", user_id=user.id, session=session)
    await common.answer_photo(message, assets.PROFILE, _screen(user), keyboards.profile_menu())


@router.callback_query(F.data == "pr:root")
async def back_to_profile(cb: CallbackQuery, user: User) -> None:
    await common.edit(cb, _screen(user), keyboards.profile_menu(), photo=assets.PROFILE)


@router.callback_query(F.data == "pr:ref")
async def referral(cb: CallbackQuery, session: AsyncSession, user: User) -> None:
    percent = await users.percent_for(session, user)
    invited = await users.referrals_count(session, user)
    note = await settings_store.get(session, "referral.text") or ""
    link = users.ref_link(user)
    await log_event(LogSection.REFERRAL, "open", user_id=user.id, session=session)
    await common.edit(
        cb,
        texts.referral(
            link=link, percent=percent, invited=invited, earned=user.ref_earned, note=note
        ),
        keyboards.profile_back(share_link=_share_url(link)),
    )


def _share_url(link: str) -> str:
    """Кнопка «Поделиться» — стандартный шаринг телеграма, без своего текста."""
    return f"https://t.me/share/url?url={quote(link, safe='')}"


@router.callback_query(F.data == "pr:accounts")
async def accounts(cb: CallbackQuery, session: AsyncSession, user: User) -> None:
    await log_event(LogSection.USER, "open_accounts", user_id=user.id, session=session)
    items = await orders_service.for_user(session, user, delivered_only=True, limit=PAGE)
    if not items:
        await common.edit(
            cb, texts.ACCOUNTS_EMPTY, keyboards.profile_back(), photo=assets.PROFILE
        )
        return

    rows = [
        " · ".join(
            [
                f"№{o.id}",
                texts.esc(o.offer_title),
                texts.code(o.phone) if o.phone else texts.DASH,
            ]
        )
        for o in items
    ]
    rows.append("")
    rows.append("Выберите номер, чтобы открыть данные входа.")
    buttons = [(o.id, _account_label(o.offer_title, o.phone, o.id)) for o in items]
    await common.edit(
        cb,
        texts.accounts_list(rows),
        keyboards.accounts_list(buttons),
        photo=assets.PROFILE,
    )


def _account_label(title: str, phone: str | None, order_id: int) -> str:
    """Подпись кнопки: страна плюс хвост номера — так они различимы между собой."""
    tail = phone[-4:] if phone and len(phone) > 4 else None
    return f"{title} · …{tail}" if tail else f"{title} · №{order_id}"


@router.callback_query(F.data == "pr:topups")
async def topups(cb: CallbackQuery, session: AsyncSession, user: User) -> None:
    await log_event(LogSection.USER, "open_topups", user_id=user.id, session=session)
    items = list(
        (
            await session.scalars(
                select(Payment)
                .where(Payment.user_id == user.id)
                .order_by(Payment.id.desc())
                .limit(PAGE)
            )
        ).all()
    )
    if not items:
        await common.edit(cb, texts.TOPUPS_EMPTY, keyboards.profile_back())
        return

    rows = [
        " · ".join(
            [
                texts.when(p.paid_at or p.created_at),
                texts.esc(PROVIDER_TITLES.get(p.provider, p.provider)),
                fmt_money(p.credited or p.amount),
                texts.esc(PAYMENT_STATUS_TITLES.get(p.status, p.status)),
            ]
        )
        for p in items
    ]
    if len(items) == PAGE:
        rows += ["", f"Показаны последние {PAGE}."]
    await common.edit(cb, texts.topups_list(rows), keyboards.profile_back())


@router.callback_query(F.data == "pr:orders")
async def orders_history(cb: CallbackQuery, session: AsyncSession, user: User) -> None:
    await log_event(LogSection.USER, "open_orders", user_id=user.id, session=session)
    items = await orders_service.for_user(session, user, limit=PAGE)
    if not items:
        await common.edit(cb, texts.ORDERS_EMPTY, keyboards.profile_back())
        return

    rows = [
        " · ".join(
            [
                f"№{o.id}",
                texts.when(o.created_at),
                texts.esc(o.offer_title),
                fmt_money(o.price),
                texts.esc(ORDER_STATUS_TITLES.get(o.status, o.status)),
            ]
        )
        for o in items
    ]
    if len(items) == PAGE:
        rows += ["", f"Показаны последние {PAGE}."]
    buttons = [
        (
            o.id,
            " · ".join(
                [
                    f"№{o.id}",
                    texts.esc(o.offer_title),
                    texts.when(o.created_at),
                ]
            ),
            orders_service.replacement_open(o),
        )
        for o in items
    ]
    await common.edit(cb, texts.orders_list(rows), keyboards.orders_list(buttons))


@router.callback_query(F.data == "pr:leaders")
async def leaders(cb: CallbackQuery, session: AsyncSession, user: User) -> None:
    items = await leaders_service.top(session, limit=15)
    rows = [
        f"{index}. {texts.esc(item.display_name)} · {item.orders_count} покупок · "
        f"{fmt_money(item.total_spent)}"
        for index, item in enumerate(items, 1)
    ]
    await common.edit(
        cb,
        texts.leaders(rows, await leaders_service.position(session, user)),
        keyboards.profile_back(),
    )


async def _promo_screen(session: AsyncSession, user: User) -> str:
    rows = await promos_service.redemptions_for(session, user, limit=10)
    history = [
        f"{texts.code(promo.code)} · +{fmt_money(redemption.bonus)} · "
        f"{texts.when(redemption.created_at)}"
        for redemption, promo in rows
    ]
    return texts.promos_history(history)


@router.callback_query(F.data == "pr:promos")
async def promos(
    cb: CallbackQuery, session: AsyncSession, user: User, state: FSMContext
) -> None:
    await state.set_state(ProfileStates.promo)
    await common.edit(cb, await _promo_screen(session, user), keyboards.profile_back())


@router.message(ProfileStates.promo, F.text)
async def redeem_promo(
    message: Message, session: AsyncSession, user: User, state: FSMContext
) -> None:
    try:
        promo = await promos_service.redeem(session, user, message.text or "")
    except promos_service.PromoError as exc:
        await common.answer(message, f"ℹ️ {texts.esc(exc)}", keyboards.profile_back())
        return
    await state.clear()
    await common.answer(
        message,
        "\n".join(
            [
                texts.bold("🎟 Промокод активирован"),
                "",
                f"Начислено: {texts.bold(fmt_money(promo.bonus))}",
                f"Баланс: {texts.bold(fmt_money(user.balance))}",
            ]
        ),
        keyboards.profile_back(),
    )
