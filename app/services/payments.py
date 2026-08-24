"""Пополнение баланса: счёт у провайдера, проверка оплаты, зачисление.

Без импортов aiogram и FastAPI — этим кодом пользуются и бот, и админка.

Как считается сумма
    Crypto Bot умеет счёт сразу в рублях (currency_type=fiat, fiat=RUB): сумма
    зафиксирована в рублях, крипту выбирает покупатель, пересчёт — не наша
    забота. У xRocket фиата в API нет вообще (в /currencies/available только
    крипта, метода курсов среди эндпоинтов нет), поэтому его счёт выставляется в
    крипте, а рубли в неё пересчитываются по курсу Crypto Bot — он обновляется
    на их стороне. Криптосумма округляется ВВЕРХ: магазин не должен получить
    меньше, чем зачислил.

    Из-за этого xRocket требует и токен Crypto Bot — иначе курс взять негде, и
    метод не показывается.

Что зачисляем
    Всегда ту сумму в рублях, которую человек ввёл: её он видел, когда создавал
    счёт. Курс, криптосумма и ответ провайдера ложатся в payments.raw для сверки.

Как узнаём об оплате
    Опросом (poll_loop): getInvoices у Crypto Bot одним запросом на все счёта,
    GET /tg-invoices/{id} у xRocket. Вебхуки провайдеры умеют, но им нужен
    публичный https-адрес, которого у магазина пока нет; проверка подписи для
    них уже написана — check_signature в обоих клиентах.

Зачисление идемпотентно: статус переводится условным UPDATE
    UPDATE payments SET status='paid' WHERE id=:id AND status='pending'
Кто первый (опрос или кнопка «Проверить оплату»), тот и зачисляет; второй видит
rowcount 0 и не делает ничего.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import ROUND_UP, Decimal
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import session_scope
from app.enums import (
    LogLevel,
    LogSection,
    PaymentProvider,
    PaymentStatus,
    PROVIDER_TITLES,
    TxKind,
)
from app.integrations import cryptobot as cb
from app.integrations import xrocket as xr
from app.integrations.pay import PayError
from app.models import Payment, ReferralEarning, User
from app.money import fmt_money, kop_to_rub
from app.services import balance, settings_store, users
from app.services.events import log_event

log = logging.getLogger("payments")

# Как часто фоновая задача спрашивает провайдеров о неоплаченных счетах.
POLL_INTERVAL = 25.0
# Пауза между счетами xRocket внутри одного обхода: у него нет пакетного запроса.
POLL_PAUSE = 0.2
# Сколько неоплаченных счетов держим на человека. Больше — это уже мусор в базе
# и в переписке, а оплатить всё равно можно только один.
OPEN_LIMIT = 3
# Точность криптосуммы. Шесть знаков хватает всем валютам xRocket
# (минимальный minInvoice у USDT — 0.0001).
CRYPTO_STEP = Decimal("0.000001")


class PaymentError(Exception):
    """Причина, которую можно показать покупателю как есть."""


@dataclass(frozen=True, slots=True)
class Method:
    """Способ оплаты для экрана выбора."""

    provider: str
    title: str
    hint: str = ""


@dataclass(frozen=True, slots=True)
class Quote:
    """Во что превращается сумма пополнения у конкретного провайдера."""

    provider: str
    amount: int  # копейки — столько зачислим
    asset: str  # RUB у Crypto Bot, USDT у xRocket
    charge: Decimal  # сколько платит покупатель в asset
    rate: Decimal | None = None  # курс asset→RUB, если пересчитывали
    markup: int = 0  # наценка к курсу, %

    @property
    def fiat(self) -> bool:
        return self.asset == "RUB"

    @property
    def charge_text(self) -> str:
        """Сумма к оплате для экрана: рубли — деньгами, крипта — без лишних нулей."""
        if self.fiat:
            return fmt_money(self.amount)
        text = format(self.charge, "f").rstrip("0").rstrip(".")
        return f"{text or '0'} {self.asset}"


@dataclass(frozen=True, slots=True)
class Credited:
    """Итог зачисления — всё, что нужно для уведомлений, без обращения к базе."""

    payment_id: int
    provider: str
    tg_id: int
    amount: int
    balance: int
    ref_tg_id: int | None = None
    ref_amount: int = 0
    ref_balance: int = 0


# --------------------------------------------------------------------------- #
#  Доступность и лимиты
# --------------------------------------------------------------------------- #
async def limits(session: AsyncSession) -> tuple[int, int]:
    """Минимум и максимум пополнения в копейках."""
    low = await settings_store.get_int(session, "topup.min", 5000)
    high = await settings_store.get_int(session, "topup.max", 10_000_000)
    if high < low:
        high = low
    return low, high


async def invoice_ttl(session: AsyncSession) -> int:
    """Срок жизни счёта в секундах. Ограничен сутками — предел xRocket."""
    minutes = await settings_store.get_int(session, "topup.invoice_minutes", 30)
    minutes = min(max(minutes, 1), 1440)
    return minutes * 60


async def methods(session: AsyncSession) -> list[Method]:
    """Способы оплаты, которые сейчас реально работают.

    Выключенный в настройках или без токена в .env способ не показываем: кнопка,
    которая отвечает ошибкой, хуже отсутствующей кнопки.
    """
    out: list[Method] = []

    if await settings_store.get_bool(session, "topup.cryptobot_enabled") and cb.get_cryptobot().ready:
        out.append(
            Method(
                PaymentProvider.CRYPTOBOT.value,
                PROVIDER_TITLES[PaymentProvider.CRYPTOBOT],
                "счёт в рублях, оплата любой криптой",
            )
        )

    if await settings_store.get_bool(session, "topup.xrocket_enabled") and xr.get_xrocket().ready:
        # Курс для пересчёта рублей берётся у Crypto Bot: у xRocket его в API нет.
        if cb.get_cryptobot().ready:
            asset = await xrocket_asset(session)
            out.append(
                Method(
                    PaymentProvider.XROCKET.value,
                    PROVIDER_TITLES[PaymentProvider.XROCKET],
                    f"счёт в {asset} по курсу на момент оплаты",
                )
            )
        else:
            log.warning("xRocket включён, но без CRYPTOBOT_TOKEN курс брать негде — скрыт")

    if (
        await settings_store.get_bool(session, "topup.platega_enabled")
        and settings.platega_merchant_id
        and settings.platega_secret
    ):
        out.append(
            Method(
                PaymentProvider.PLATEGA.value,
                PROVIDER_TITLES[PaymentProvider.PLATEGA],
                "карта и СБП",
            )
        )

    return out


async def xrocket_asset(session: AsyncSession) -> str:
    value = (await settings_store.get(session, "topup.xrocket_asset") or "USDT").strip().upper()
    return value or "USDT"


# --------------------------------------------------------------------------- #
#  Расчёт суммы
# --------------------------------------------------------------------------- #
async def quote(session: AsyncSession, provider: str, amount: int) -> Quote:
    """Посчитать, сколько и в чём заплатит покупатель."""
    if provider == PaymentProvider.CRYPTOBOT.value:
        # Счёт в рублях: пересчитывать нечего.
        return Quote(provider, amount, "RUB", kop_to_rub(amount))

    if provider == PaymentProvider.XROCKET.value:
        asset = await xrocket_asset(session)
        markup = max(await settings_store.get_int(session, "topup.xrocket_markup", 0), 0)
        try:
            rate = await cb.get_cryptobot().rate(asset, "RUB")
        except PayError as exc:
            raise PaymentError("Курс сейчас недоступен, попробуйте через минуту.") from exc
        if rate is None:
            raise PaymentError(f"Курс {asset} к рублю сейчас недоступен, выберите другой способ.")

        # Округляем ВВЕРХ: иначе на копейках магазин получит меньше зачисленного.
        charge = (kop_to_rub(amount) * (100 + markup) / (100 * rate)).quantize(
            CRYPTO_STEP, rounding=ROUND_UP
        )

        try:
            minimum = await xr.get_xrocket().min_invoice(asset)
        except PayError:
            minimum = None  # не ответили — пусть решает сам провайдер
        if minimum is not None and charge < minimum:
            need = (minimum * 100 * rate / (100 + markup)).quantize(
                Decimal("1"), rounding=ROUND_UP
            )
            raise PaymentError(
                f"Через xRocket минимум {minimum} {asset} — это примерно {need} ₽. "
                "Увеличьте сумму или выберите Crypto Bot."
            )
        return Quote(provider, amount, asset, charge, rate=rate, markup=markup)

    if provider == PaymentProvider.PLATEGA.value:
        raise PaymentError("Карта и СБП подключаются, пока доступна оплата криптой.")

    raise PaymentError("Неизвестный способ оплаты.")


# --------------------------------------------------------------------------- #
#  Создание счёта
# --------------------------------------------------------------------------- #
async def create(
    session: AsyncSession,
    user: User,
    provider: str,
    amount: int,
    *,
    ttl: int | None = None,
) -> tuple[Payment, Quote]:
    """Выставить счёт. Поднимает PaymentError с текстом для покупателя."""
    if user.restrict_topup:
        raise PaymentError("Пополнение для вашего аккаунта ограничено. Напишите в поддержку.")

    low, high = await limits(session)
    if amount < low:
        raise PaymentError(f"Минимальная сумма пополнения — {fmt_money(low)}.")
    if amount > high:
        raise PaymentError(f"Максимальная сумма пополнения — {fmt_money(high)}.")

    available = {m.provider for m in await methods(session)}
    if provider not in available:
        raise PaymentError("Этот способ оплаты сейчас недоступен.")

    open_count = await session.scalar(
        select(func.count())
        .select_from(Payment)
        .where(Payment.user_id == user.id, Payment.status == PaymentStatus.PENDING.value)
    )
    if (open_count or 0) >= OPEN_LIMIT:
        raise PaymentError(
            f"У вас уже {open_count} неоплаченных счёта. Оплатите или отмените их."
        )

    calc = await quote(session, provider, amount)
    seconds = ttl if ttl is not None else await invoice_ttl(session)
    now = dt.datetime.now(dt.UTC)

    payment = Payment(
        user_id=user.id,
        provider=provider,
        amount=amount,
        status=PaymentStatus.PENDING.value,
        asset=calc.asset,
        expires_at=now + dt.timedelta(seconds=seconds),
        raw={"quote": _quote_raw(calc)},
    )
    session.add(payment)
    await session.flush()  # нужен id: он уходит в payload счёта

    try:
        invoice = await _create_invoice(payment, calc, seconds=seconds)
    except (PayError, ValueError) as exc:
        # Счёт не создался — запись не оставляем «ожидающей», иначе её будет
        # опрашивать фоновая задача, а покупатель увидит мёртвый счёт в истории.
        payment.status = PaymentStatus.FAILED.value
        payment.raw = {"quote": _quote_raw(calc), "error": str(exc)}
        await session.flush()
        await log_event(
            LogSection.PAYMENT,
            "invoice_failed",
            level=LogLevel.ERROR,
            user_id=user.id,
            message=f"{PROVIDER_TITLES.get(provider, provider)}: {exc}",
            payload={"payment_id": payment.id, "amount": amount},
            session=session,
        )
        log.warning("счёт %s не создан: %s", provider, exc)
        raise PaymentError("Платёжный сервис не ответил. Попробуйте ещё раз через минуту.") from exc

    payment.external_id = str(invoice["external_id"])
    payment.invoice_url = invoice["url"]
    payment.raw = {"quote": _quote_raw(calc), "invoice": invoice["raw"]}
    await session.flush()

    await log_event(
        LogSection.PAYMENT,
        "invoice_created",
        user_id=user.id,
        message=f"{PROVIDER_TITLES.get(provider, provider)} · {fmt_money(amount)} · {calc.charge_text}",
        payload={
            "payment_id": payment.id,
            "provider": provider,
            "amount": amount,
            "asset": calc.asset,
            "charge": str(calc.charge),
            "rate": str(calc.rate) if calc.rate is not None else None,
            "external_id": payment.external_id,
        },
        session=session,
    )
    return payment, calc


async def _create_invoice(payment: Payment, calc: Quote, *, seconds: int) -> dict[str, Any]:
    """Обратиться к провайдеру. Возвращает {external_id, url, raw}."""
    tag = f"topup:{payment.id}"

    if calc.provider == PaymentProvider.CRYPTOBOT.value:
        invoice = await cb.get_cryptobot().create_invoice(
            amount=calc.charge,
            currency_type="fiat",
            fiat="RUB",
            description=f"Пополнение баланса на {fmt_money(payment.amount)}",
            payload=tag,
            expires_in=min(seconds, cb.EXPIRES_MAX),
            hidden_message="Оплата получена, баланс пополнится в течение минуты.",
        )
        url = invoice.get("mini_app_invoice_url") or invoice.get("bot_invoice_url")
        if not invoice.get("invoice_id") or not url:
            raise PayError("Crypto Bot: в ответе нет счёта")
        return {"external_id": invoice["invoice_id"], "url": str(url), "raw": invoice}

    if calc.provider == PaymentProvider.XROCKET.value:
        invoice = await xr.get_xrocket().create_invoice(
            amount=calc.charge,
            currency=calc.asset,
            num_payments=1,
            description=f"Пополнение баланса на {fmt_money(payment.amount)}",
            hidden_message="Оплата получена, баланс пополнится в течение минуты.",
            payload=tag,
            comments_enabled=False,
            expired_in=min(seconds, xr.EXPIRED_IN_MAX),
        )
        url = xr.link_of(invoice)
        if invoice.get("id") is None or not url:
            raise PayError("xRocket: в ответе нет счёта")
        return {"external_id": invoice["id"], "url": url, "raw": invoice}

    raise PaymentError("Этот способ оплаты пока не подключён.")


def _quote_raw(calc: Quote) -> dict[str, Any]:
    """Расчёт в payments.raw — по нему потом сверяют, почему сумма именно такая."""
    return {
        "asset": calc.asset,
        "charge": str(calc.charge),
        "rate": str(calc.rate) if calc.rate is not None else None,
        "markup": calc.markup,
        "amount_kop": calc.amount,
    }


# --------------------------------------------------------------------------- #
#  Проверка и зачисление
# --------------------------------------------------------------------------- #
async def refresh(session: AsyncSession, payment: Payment) -> Credited | None:
    """Спросить провайдера про один счёт. Возвращает итог, если зачислили."""
    if payment.status != PaymentStatus.PENDING.value:
        return None
    if not payment.external_id:
        return await _expire_if_due(session, payment)

    invoice: dict[str, Any] | None
    if payment.provider == PaymentProvider.CRYPTOBOT.value:
        invoice = await cb.get_cryptobot().get_invoice(payment.external_id)
    elif payment.provider == PaymentProvider.XROCKET.value:
        invoice = await xr.get_xrocket().get_invoice(payment.external_id)
    else:
        return await _expire_if_due(session, payment)

    return await apply(session, payment, invoice)


async def apply(
    session: AsyncSession, payment: Payment, invoice: dict[str, Any] | None
) -> Credited | None:
    """Сверить наш счёт с тем, что рассказал провайдер."""
    if invoice is None:
        # Провайдер счёта не знает: удалён или не его. Ждём срока и закрываем.
        return await _expire_if_due(session, payment)

    if payment.provider == PaymentProvider.CRYPTOBOT.value:
        paid, expired = cb.is_paid(invoice), cb.is_expired(invoice)
    else:
        paid, expired = xr.is_paid(invoice), xr.is_expired(invoice)

    if paid:
        return await mark_paid(session, payment, invoice=invoice)
    if expired:
        await mark_expired(session, payment, invoice=invoice)
        return None
    return await _expire_if_due(session, payment)


async def mark_paid(
    session: AsyncSession,
    payment: Payment,
    *,
    invoice: dict[str, Any] | None = None,
    admin_id: int | None = None,
    comment: str | None = None,
) -> Credited | None:
    """Зачислить оплату. None, если счёт уже был закрыт кем-то другим.

    Статус переводим условным UPDATE: зачисление должно случиться ровно один
    раз, даже если опрос и кнопка «Проверить оплату» пришли одновременно.
    """
    now = dt.datetime.now(dt.UTC)
    raw = dict(payment.raw or {})
    if invoice is not None:
        raw["paid_invoice"] = invoice

    result = await session.execute(
        update(Payment)
        .where(Payment.id == payment.id, Payment.status == PaymentStatus.PENDING.value)
        .values(
            status=PaymentStatus.PAID.value,
            credited=payment.amount,
            paid_at=now,
            raw=raw,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        log.info("счёт %s уже закрыт, второй раз не зачисляем", payment.id)
        return None
    await session.refresh(payment, ["status", "credited", "paid_at", "raw"])

    user = await session.get(User, payment.user_id)
    if user is None:  # пользователя удалили — зачислять некому
        log.error("счёт %s оплачен, но пользователь %s не найден", payment.id, payment.user_id)
        return None

    new_balance = await balance.credit(
        session,
        user,
        payment.amount,
        TxKind.TOPUP,
        payment_id=payment.id,
        admin_id=admin_id,
        comment=comment or PROVIDER_TITLES.get(payment.provider, payment.provider),
    )
    await log_event(
        LogSection.PAYMENT,
        "paid",
        user_id=user.id,
        admin_id=admin_id,
        message=f"{PROVIDER_TITLES.get(payment.provider, payment.provider)} · {fmt_money(payment.amount)}",
        payload={"payment_id": payment.id, "external_id": payment.external_id},
        session=session,
    )

    ref = await _accrue_referral(session, payment, user)
    return Credited(
        payment_id=payment.id,
        provider=payment.provider,
        tg_id=user.tg_id,
        amount=payment.amount,
        balance=new_balance,
        ref_tg_id=ref[0] if ref else None,
        ref_amount=ref[1] if ref else 0,
        ref_balance=ref[2] if ref else 0,
    )


async def _accrue_referral(
    session: AsyncSession, payment: Payment, payer: User
) -> tuple[int, int, int] | None:
    """Процент пригласившему. Возвращает (tg_id, начислено, его баланс) или None.

    Процент считается от суммы пополнения и округляется вниз — в пользу магазина.
    Повторного начисления не будет: на один счёт заводится одна запись, и здесь
    же проверяется, что её ещё нет.
    """
    if payer.referrer_id is None:
        return None
    referrer = await session.get(User, payer.referrer_id)
    if referrer is None or referrer.is_banned:
        return None

    percent = await users.percent_for(session, referrer)
    if percent <= 0:
        return None
    amount = payment.amount * percent // 100
    if amount <= 0:
        return None

    already = await session.scalar(
        select(func.count())
        .select_from(ReferralEarning)
        .where(ReferralEarning.payment_id == payment.id)
    )
    if already:
        return None

    session.add(
        ReferralEarning(
            user_id=referrer.id,
            from_user_id=payer.id,
            payment_id=payment.id,
            amount=amount,
            percent=percent,
        )
    )
    await balance.credit(
        session,
        referrer,
        amount,
        TxKind.REFERRAL,
        payment_id=payment.id,
        comment=f"{percent}% с пополнения {payer.display_name}",
    )
    await log_event(
        LogSection.REFERRAL,
        "accrued",
        user_id=referrer.id,
        message=f"+{fmt_money(amount)} ({percent}% с пополнения {payer.display_name})",
        payload={"payment_id": payment.id, "from_user_id": payer.id, "percent": percent},
        session=session,
    )
    return referrer.tg_id, amount, referrer.balance


async def mark_expired(
    session: AsyncSession,
    payment: Payment,
    *,
    invoice: dict[str, Any] | None = None,
    admin_id: int | None = None,
) -> bool:
    """Закрыть неоплаченный счёт. Оплаченный не трогаем."""
    raw = dict(payment.raw or {})
    if invoice is not None:
        raw["last_invoice"] = invoice
    result = await session.execute(
        update(Payment)
        .where(Payment.id == payment.id, Payment.status == PaymentStatus.PENDING.value)
        .values(status=PaymentStatus.EXPIRED.value, raw=raw)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        return False
    await session.refresh(payment, ["status", "raw"])
    await log_event(
        LogSection.PAYMENT,
        "expired",
        user_id=payment.user_id,
        admin_id=admin_id,
        message=f"{PROVIDER_TITLES.get(payment.provider, payment.provider)} · {fmt_money(payment.amount)}",
        payload={"payment_id": payment.id},
        session=session,
    )
    return True


async def cancel(
    session: AsyncSession, payment: Payment, *, admin_id: int | None = None
) -> bool:
    """Снять счёт: локально закрываем только после подтверждения провайдера.

    Неопределённый ответ нельзя трактовать как успешное удаление: покупатель мог
    оплатить счёт прямо перед сетевым сбоем. Оставляем его ``pending``, чтобы
    фоновый опрос мог увидеть оплату и зачислить деньги.
    """
    if payment.status != PaymentStatus.PENDING.value:
        return False
    if payment.external_id:
        try:
            if payment.provider == PaymentProvider.CRYPTOBOT.value:
                deleted = await cb.get_cryptobot().delete_invoice(payment.external_id)
            elif payment.provider == PaymentProvider.XROCKET.value:
                deleted = await xr.get_xrocket().delete_invoice(payment.external_id)
            else:
                deleted = False
        except PayError as exc:
            log.warning("счёт %s не снят у провайдера: %s", payment.id, exc)
            return False
        if not deleted:
            log.warning("провайдер не подтвердил снятие счёта %s", payment.id)
            return False
    return await mark_expired(session, payment, admin_id=admin_id)


async def _expire_if_due(session: AsyncSession, payment: Payment) -> None:
    """Закрыть счёт, если его срок прошёл. Ничего не возвращает — зачисления нет."""
    if payment.expires_at is None:
        return None
    due = payment.expires_at
    if due.tzinfo is None:  # sqlite отдаёт наивное время, писали в UTC
        due = due.replace(tzinfo=dt.UTC)
    if due <= dt.datetime.now(dt.UTC):
        await mark_expired(session, payment)
    return None


# --------------------------------------------------------------------------- #
#  Опрос
# --------------------------------------------------------------------------- #
async def pending(session: AsyncSession, *, limit: int = 200) -> list[Payment]:
    """Счёта, которые ждут оплаты и уже созданы у провайдера."""
    rows = await session.scalars(
        select(Payment)
        .where(
            Payment.status == PaymentStatus.PENDING.value,
            Payment.external_id.is_not(None),
        )
        .order_by(Payment.id)
        .limit(limit)
    )
    return list(rows.all())


async def poll_pending(session: AsyncSession, *, limit: int = 200) -> list[Credited]:
    """Один обход неоплаченных счетов. Возвращает то, что зачислили."""
    rows = await pending(session, limit=limit)
    if not rows:
        return []
    out: list[Credited] = []

    # Crypto Bot отдаёт до 1000 счетов одним запросом — спрашиваем пакетом.
    batch = [p for p in rows if p.provider == PaymentProvider.CRYPTOBOT.value]
    if batch and cb.get_cryptobot().ready:
        try:
            invoices = await cb.get_cryptobot().get_invoices(
                invoice_ids=[str(p.external_id) for p in batch]
            )
        except PayError as exc:
            log.warning("Crypto Bot не ответил на опрос: %s", exc)
        else:
            by_id = {str(i.get("invoice_id")): i for i in invoices}
            for payment in batch:
                credited = await apply(session, payment, by_id.get(str(payment.external_id)))
                if credited:
                    out.append(credited)

    singles = [p for p in rows if p.provider == PaymentProvider.XROCKET.value]
    if singles and xr.get_xrocket().ready:
        for index, payment in enumerate(singles, 1):
            try:
                credited = await refresh(session, payment)
            except PayError as exc:
                log.warning("xRocket не ответил про счёт %s: %s", payment.id, exc)
                continue
            if credited:
                out.append(credited)
            if index < len(singles):
                await asyncio.sleep(POLL_PAUSE)

    # Остальные провайдеры (пока это только ручные счёта) просто закрываем по сроку.
    for payment in rows:
        if payment.provider in {
            PaymentProvider.CRYPTOBOT.value,
            PaymentProvider.XROCKET.value,
        }:
            continue
        await _expire_if_due(session, payment)

    return out


async def poll_loop(
    notify: Callable[[Credited], Awaitable[None]] | None = None,
    interval: float = POLL_INTERVAL,
) -> None:
    """Фоновая задача бота. Падать не имеет права: на ней зачисление денег."""
    if not (settings.cryptobot_token or settings.xrocket_token):
        log.warning("токенов платёжных сервисов нет — опрос счетов не запускаем")
        return
    log.info("опрос неоплаченных счетов каждые %.0f с", interval)
    while True:
        try:
            async with session_scope() as session:
                credited = await poll_pending(session)
            for item in credited:
                log.info(
                    "зачислено %s пользователю %s (счёт %s)",
                    fmt_money(item.amount),
                    item.tg_id,
                    item.payment_id,
                )
                if notify is not None:
                    try:
                        await notify(item)
                    except Exception:  # уведомление не должно ломать обход
                        log.exception("не удалось уведомить об оплате %s", item.payment_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("обход счетов сорвался, повторим через %.0f с", interval)
        await asyncio.sleep(interval)


# --------------------------------------------------------------------------- #
#  Чтение
# --------------------------------------------------------------------------- #
async def history(session: AsyncSession, user: User, *, limit: int = 10) -> list[Payment]:
    """История пополнений для профиля: сначала свежие."""
    rows = await session.scalars(
        select(Payment)
        .where(Payment.user_id == user.id)
        .order_by(Payment.id.desc())
        .limit(limit)
    )
    return list(rows.all())


async def open_invoices(session: AsyncSession, user: User) -> list[Payment]:
    rows = await session.scalars(
        select(Payment)
        .where(Payment.user_id == user.id, Payment.status == PaymentStatus.PENDING.value)
        .order_by(Payment.id.desc())
    )
    return list(rows.all())
