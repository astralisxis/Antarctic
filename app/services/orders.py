"""Заказ: подбор лота на LZT, покупка, выдача кода входа, возврат.

Порядок шагов выбран так, чтобы обрыв сети не мог потерять деньги:

  1. заказ пишется в базу и деньги списываются одной транзакцией;
  2. транзакция коммитится ДО первого запроса в маркет;
  3. только потом идут поиск и покупка;
  4. подтверждённая неудача — возврат на баланс и статус failed;
     неопределённый ответ после fast-buy оставляет заказ на перепроверку.

Если связь оборвётся между покупкой на маркете и записью результата, заказ
останется в статусе «покупка» с известным lzt_item_id — админка такие заказы
показывает отдельно и умеет их перепроверить (recheck).

Слой без aiogram и без FastAPI: этими же функциями будет пользоваться мини-апп.
"""

from __future__ import annotations

import datetime as dt
import logging
import secrets
from dataclasses import asdict
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import LogLevel, LogSection, OrderStatus, TicketStatus, TxKind
from app.integrations.lzt import (
    Credentials,
    LztError,
    LztHTTPError,
    LztUncertainError,
    LztMarket,
    credentials_of,
    get_lzt,
    item_of,
    mask_phone,
    newest_login_code,
)
from app.models import CountryOffer, Order, Ticket, User
from app.money import fmt_money, rub_to_kop
from app.services import balance, catalog, settings_store
from app.services import support as support_service
from app.services.events import log_event
from app.timeutil import as_utc

log = logging.getLogger("orders")

# Статусы, в которых заказ уже оплачен клиентом и номер у него на руках.
DELIVERED = (
    OrderStatus.PURCHASED.value,
    OrderStatus.CODE_ISSUED.value,
    OrderStatus.DONE.value,
)
# Статусы, в которых заказ ещё в работе: второй такой же начинать нельзя.
IN_FLIGHT = (OrderStatus.NEW.value, OrderStatus.SEARCHING.value, OrderStatus.BUYING.value)

CODE_HOURS_FALLBACK = 12

REPLACEMENT_PENDING = "pending"
REPLACEMENT_PROCESSING = "processing"
REPLACEMENT_REVIEW = "review"
REPLACEMENT_COMPLETED = "completed"
REPLACEMENT_REJECTED = "rejected"
REPLACEMENT_FAILED = "failed"
REPLACEMENT_STATUSES = {
    REPLACEMENT_PENDING,
    REPLACEMENT_PROCESSING,
    REPLACEMENT_REVIEW,
    REPLACEMENT_COMPLETED,
    REPLACEMENT_REJECTED,
    REPLACEMENT_FAILED,
}

# Сколько заказ считается «в работе». Дольше — это уже зависший заказ: разбирает
# админка (recheck), а клиенту он больше не мешает купить.
STUCK_AFTER = dt.timedelta(minutes=10)
REPLACEMENT_STUCK_AFTER = dt.timedelta(minutes=10)

# По тексту ошибки маркета решаем, пробовать ли следующий лот.
# Лот ушёл из-под нас — берём другой; кончились деньги на маркете — смысла нет.
_GONE_MARKERS = ("already", "sold", "not for sale", "not found", "no longer", "куплен", "продан")
_MONEY_MARKERS = ("not enough", "insufficient", "недостаточно", "too low", "no money")
_INVALID_ACCOUNT_MARKERS = (
    "invalid session",
    "session invalid",
    "session is invalid",
    "session expired",
    "expired session",
    "invalid auth key",
    "auth key is invalid",
    "authorization key is invalid",
    "telegram account invalid",
    "telegram account is invalid",
    "account invalid",
    "not valid account",
    "сессия недейств",
    "сессия не действ",
    "невалидная сесс",
    "невалидной сесс",
    "сессия истек",
)

# Поля карточки лота, которые храним в заказе. Целиком не пишем: там десятки
# полей, а в истории нужны цена, страна, источник и состояние.
_SNAPSHOT_KEYS = (
    "item_id",
    "price",
    "price_currency",
    "item_origin",
    "item_state",
    "published_date",
    "telegram_country",
    "telegram_dc",
    "telegram_username",
    "telegram_last_seen",
    "telegram_spam_block",
    "telegram_premium",
)


class OrderError(Exception):
    """Ошибка с текстом, который можно показать покупателю как есть."""


class ShopClosed(OrderError):
    pass


class OfferUnavailable(OrderError):
    pass


class AlreadyBuying(OrderError):
    pass


class OutOfStock(OrderError):
    pass


class BuyFailed(OrderError):
    pass


class BuyPending(OrderError):
    """Маркет мог купить лот, но ещё не отдал данные входа."""


class CodeWindowClosed(OrderError):
    pass


# --------------------------------------------------------------------------- #
#  Покупка
# --------------------------------------------------------------------------- #
async def precheck(session: AsyncSession, user: User, offer: CountryOffer) -> None:
    """Отказы, которые видны до обращения к маркету.

    Витрина вызывает это до экрана «Подбираем номер»: показывать ожидание, а
    через мгновение отказ — мигание на пустом месте. buy() зовёт эту же функцию,
    поэтому правила у витрины и у покупки одни и те же.
    """
    if not await settings_store.get_bool(session, "shop.enabled"):
        raise ShopClosed(
            await settings_store.get(session, "shop.disabled_text")
            or "Магазин временно закрыт."
        )
    if user.restrict_buy:
        raise OfferUnavailable("Покупки для вашего аккаунта ограничены. Напишите в поддержку.")
    if not offer.is_active:
        raise OfferUnavailable("Эта страна сейчас недоступна.")

    # Зависший заказ не должен запирать клиента навсегда: если процесс умер
    # посреди покупки, через STUCK_AFTER разбираться с ним будет админка,
    # а клиент сможет купить снова.
    fresh_since = dt.datetime.now(dt.UTC) - STUCK_AFTER
    running = await session.scalar(
        select(Order.id)
        .where(
            Order.buyer_user_id == user.id,
            Order.status.in_(IN_FLIGHT),
            Order.created_at >= fresh_since,
        )
        .limit(1)
    )
    if running:
        raise AlreadyBuying("Предыдущий заказ ещё оформляется. Подождите его результат.")

    if user.balance < offer.price:
        raise OrderError(
            f"Не хватает {fmt_money(offer.price - user.balance)}. "
            f"Цена — {fmt_money(offer.price)}, на балансе {fmt_money(user.balance)}."
        )


async def buy(
    session: AsyncSession,
    user: User,
    offer: CountryOffer,
    *,
    lzt: LztMarket | None = None,
    source: str = "bot",
    recipient: User | None = None,
) -> Order:
    """Купить номер по позиции каталога. Возвращает заказ в статусе purchased.

    Все отказы — исключения OrderError с готовым текстом для клиента.
    Деньги на момент любого исключения уже вернулись на баланс.
    """
    client = lzt or get_lzt()

    # Проверяем ещё раз, даже если витрина уже спрашивала: между показом кнопки
    # и нажатием магазин могли закрыть, а деньги — потратить.
    await precheck(session, user, offer)

    # Подарок оплачивает отправитель, но владельцем готового заказа становится
    # выбранный получатель. Получатель уже должен быть зарегистрирован в боте.
    recipient = recipient or user
    if recipient.id == user.id:
        recipient = user
    elif recipient.is_banned:
        raise OrderError("Получатель сейчас недоступен для подарка.")

    # --- заказ и деньги: одна транзакция, коммит до сети ---
    order = Order(
        user_id=recipient.id,
        buyer_user_id=user.id,
        offer_id=offer.id,
        offer_code=offer.code,
        offer_title=offer.title,
        price=offer.price,
        guarantee_hours=max(1, int(offer.guarantee_hours or CODE_HOURS_FALLBACK)),
        status=OrderStatus.NEW.value,
    )
    session.add(order)
    await session.flush()

    try:
        await balance.debit(
            session,
            user,
            offer.price,
            TxKind.PURCHASE,
            order_id=order.id,
            comment=f"заказ №{order.id}, {offer.title}",
        )
    except balance.NotEnoughMoney as exc:
        # Гонка: между проверкой выше и списанием деньги ушли на другой заказ.
        # Откатываем только строку заказа — rollback всей транзакции обнулил бы
        # состояние объектов, с которыми хендлер продолжит работать.
        await session.delete(order)
        await session.flush()
        raise OrderError(
            f"Не хватает {fmt_money(exc.need - exc.have)}. Пополните баланс."
        ) from exc

    order.status = OrderStatus.SEARCHING.value
    await log_event(
        LogSection.SHOP,
        "order_created",
        user_id=user.id,
        order_id=order.id,
        message=f"{offer.title} за {fmt_money(offer.price)} ({source})",
        payload={"offer": offer.code, "limit": offer.buy_limit},
        session=session,
    )
    await session.commit()

    # --- поиск лота ---
    try:
        lots = await catalog.find_lots(offer, lzt=client)
    except LztError as exc:
        await _fail(session, order, user, f"поиск лота не удался: {exc}")
        raise BuyFailed(
            "Маркет не ответил. Деньги вернулись на баланс, попробуйте ещё раз."
        ) from exc

    if not lots:
        offer.stock_cached = 0
        offer.stock_checked_at = dt.datetime.now(dt.UTC)
        await _fail(session, order, user, "под лимит закупки нет лотов", level=LogLevel.INFO)
        raise OutOfStock(
            "Этой страны сейчас нет в наличии. Деньги остались на балансе."
        )

    # --- покупка: дешёвые лоты разбирают за секунды, поэтому кандидатов несколько ---
    last_error = "не удалось купить лот"
    for lot in lots:
        order.status = OrderStatus.BUYING.value
        order.lzt_item_id = lot.item_id
        order.attempts += 1
        await session.commit()

        try:
            answer = await client.purchasing_fast_buy(lot.item_id, lot.price_rub)
        except LztUncertainError as exc:
            # Запрос мог дойти до маркета и списать баланс, хотя ответ потерялся.
            # Следующий лот покупать нельзя; оставляем заказ для recheck.
            order.status = OrderStatus.BUYING.value
            order.error = str(exc)[:500]
            await log_event(
                LogSection.LZT,
                "buy_uncertain",
                level=LogLevel.ERROR,
                user_id=user.id,
                order_id=order.id,
                message=order.error,
                payload={"item_id": lot.item_id},
                session=session,
            )
            await session.commit()
            raise BuyPending(
                "Маркет не подтвердил покупку. Заказ оставлен на перепроверку, повторная закупка не выполняется."
            ) from exc
        except LztError as exc:
            last_error = str(exc)
            stop = _is_money_problem(exc)
            await log_event(
                LogSection.LZT,
                "buy_failed",
                level=LogLevel.WARN,
                user_id=user.id,
                order_id=order.id,
                message=f"лот {lot.item_id} за {fmt_money(lot.price)}: {exc}",
                payload={"item_id": lot.item_id, "fatal": stop},
            )
            if stop:
                break
            continue

        item = item_of(answer)
        creds = credentials_of(item)
        if not creds.filled and order.lzt_item_id:
            # fast-buy иногда отдаёт карточку без данных входа — добираем её отдельно.
            try:
                item = item_of(await client.item_get(order.lzt_item_id))
                creds = credentials_of(item)
            except LztError as exc:
                log.warning("карточка лота %s не дочиталась: %s", lot.item_id, exc)

        if not creds.filled:
            # fast-buy уже мог списать деньги маркета. Не покупаем следующий лот
            # и не возвращаем деньги клиенту: заказ остаётся в BUYING, а
            # перепроверка подтянет карточку, когда API снова станет доступен.
            order.status = OrderStatus.BUYING.value
            order.error = "лот куплен, но маркет не отдал данные входа"
            await log_event(
                LogSection.LZT,
                "buy_pending_credentials",
                level=LogLevel.WARN,
                user_id=user.id,
                order_id=order.id,
                message=order.error,
                payload={"item_id": order.lzt_item_id},
                session=session,
            )
            await session.commit()
            raise BuyPending(
                "Номер куплен, но маркет пока не отдал данные. Поддержка перепроверит заказ."
            )

        await _complete(session, order, lot, item, creds)
        return order

    await _fail(session, order, user, last_error)
    raise BuyFailed(
        "Номер не выдался: лот ушёл раньше. Деньги вернулись на баланс, попробуйте ещё раз."
    )


async def _complete(
    session: AsyncSession,
    order: Order,
    lot: catalog.Lot,
    item: dict[str, Any],
    creds: Credentials,
) -> None:
    # Keep the persistence boundary defensive: a new caller must not be able
    # to mark an order purchased before LZT has returned usable credentials.
    if not creds.filled:
        raise BuyPending(
            "Маркет не отдал данные входа. Заказ оставлен на перепроверку."
        )
    order.status = OrderStatus.PURCHASED.value
    order.lzt_cost = lot.price
    order.phone = creds.phone or lot.phone
    order.lzt_raw = _snapshot(item, creds)
    order.completed_at = dt.datetime.now(dt.UTC)
    order.error = None

    recipient = await session.get(User, order.user_id)
    if recipient is None:
        raise OrderError("Получатель заказа не найден.")
    recipient.orders_count += 1
    if recipient.first_order_at is None:
        recipient.first_order_at = order.completed_at

    await log_event(
        LogSection.SHOP,
        "order_purchased",
        user_id=recipient.id,
        order_id=order.id,
        message=(
            f"{order.offer_title}: {mask_phone(order.phone)}, "
            f"закуп {fmt_money(order.lzt_cost)}, маржа {fmt_money(order.margin)}"
        ),
        payload={"item_id": order.lzt_item_id, "cost": order.lzt_cost},
        session=session,
    )
    await session.commit()


async def _fail(
    session: AsyncSession,
    order: Order,
    payer: User,
    reason: str,
    *,
    level: LogLevel = LogLevel.WARN,
) -> None:
    """Пометить заказ сбойным и вернуть деньги. Возврат идёт до коммита статуса."""
    now = dt.datetime.now(dt.UTC)
    result = await session.execute(
        update(Order)
        .where(Order.id == order.id, Order.refunded.is_(False))
        .values(
            status=OrderStatus.FAILED.value,
            error=reason[:500],
            completed_at=now,
            refunded=True,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 1:
        await balance.credit(
            session,
            payer,
            order.price,
            TxKind.REFUND,
            order_id=order.id,
            comment=f"возврат по заказу №{order.id}",
        )
        order.refunded = True
    else:
        # Другой worker уже вернул заказ. Не создаём вторую проводку.
        await session.refresh(order)
        return
    order.status = OrderStatus.FAILED.value
    order.error = reason[:500]
    order.completed_at = now
    await log_event(
        LogSection.SHOP,
        "order_failed",
        level=level,
        user_id=payer.id,
        order_id=order.id,
        message=reason[:400],
        session=session,
    )
    await session.commit()


def _snapshot(item: dict[str, Any], creds: Credentials) -> dict[str, Any]:
    """Что храним в заказе: срез карточки плюс данные входа — это и есть товар."""
    data = {key: item[key] for key in _SNAPSHOT_KEYS if key in item}
    data["creds"] = {k: v for k, v in asdict(creds).items() if v}
    return data


def _is_money_problem(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _MONEY_MARKERS)


def _is_gone(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _GONE_MARKERS)


def _is_invalid_account_error(exc: Exception) -> bool:
    """Отличить мёртвую сессию от временного сбоя маркета."""
    if isinstance(exc, LztUncertainError):
        return False
    if isinstance(exc, LztHTTPError) and exc.status >= 500:
        return False
    text = str(exc).lower()
    return any(marker in text for marker in _INVALID_ACCOUNT_MARKERS)


# --------------------------------------------------------------------------- #
#  Данные входа и код
# --------------------------------------------------------------------------- #
def credentials(order: Order) -> Credentials:
    """Данные входа из снимка заказа.

    Старые заказы лежат в базе с прежними именами полей (login/password), под
    которыми маркет отдаёт ключ авторизации и номер дата-центра. Читаем и их,
    иначе карточка купленного до переименования номера опустеет.
    """
    raw = order.lzt_raw or {}
    saved = raw.get("creds") if isinstance(raw, dict) else None
    if isinstance(saved, dict):
        return Credentials(
            phone=saved.get("phone") or order.phone,
            auth_key=saved.get("auth_key") or saved.get("login"),
            dc_id=saved.get("dc_id") or saved.get("password"),
            tg_password=saved.get("tg_password"),
        )
    return Credentials(phone=order.phone)


async def code_hours(session: AsyncSession) -> int:
    """Сколько часов после покупки клиент берёт код сам. 0 — без ограничения."""
    return await settings_store.get_int(session, "shop.code_hours", CODE_HOURS_FALLBACK)


def guarantee_until(order: Order) -> dt.datetime | None:
    hours = int(order.guarantee_hours or CODE_HOURS_FALLBACK)
    start = as_utc(order.completed_at) or as_utc(order.created_at)
    if start is None or hours <= 0:
        return None
    return start + dt.timedelta(hours=hours)


def replacement_open(order: Order) -> bool:
    """Замена доступна только выданному заказу в пределах гарантии."""
    if order.status not in DELIVERED or order.replacement_requested_at is not None:
        return False
    until = guarantee_until(order)
    return until is None or dt.datetime.now(dt.UTC) <= until


def transfer_open(order: Order) -> bool:
    return (
        order.status in DELIVERED
        and order.transferred_at is None
        and order.replacement_requested_at is None
    )


async def create_transfer(session: AsyncSession, order: Order) -> str:
    if not transfer_open(order):
        raise OrderError("Этот аккаунт уже передан или недоступен для передачи.")
    expires = as_utc(order.transfer_expires_at)
    if order.transfer_token and (expires is None or dt.datetime.now(dt.UTC) <= expires):
        return order.transfer_token
    previous_token = order.transfer_token
    token = secrets.token_urlsafe(24)
    expires_at = dt.datetime.now(dt.UTC) + dt.timedelta(hours=24)
    token_condition = (
        Order.transfer_token.is_(None)
        if previous_token is None
        else Order.transfer_token == previous_token
    )
    result = await session.execute(
        update(Order)
        .where(
            Order.id == order.id,
            Order.user_id == order.user_id,
            Order.status.in_(DELIVERED),
            Order.transferred_at.is_(None),
            Order.replacement_requested_at.is_(None),
            token_condition,
        )
        .values(transfer_token=token, transfer_expires_at=expires_at)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await session.refresh(order)
        winner_expires = as_utc(order.transfer_expires_at)
        if (
            transfer_open(order)
            and order.transfer_token
            and (winner_expires is None or dt.datetime.now(dt.UTC) <= winner_expires)
        ):
            return order.transfer_token
        raise OrderError("Этот аккаунт уже передан или недоступен для передачи.")
    order.transfer_token = token
    order.transfer_expires_at = expires_at
    await log_event(
        LogSection.SHOP,
        "transfer_created",
        user_id=order.user_id,
        order_id=order.id,
        message="создана одноразовая ссылка передачи",
        session=session,
    )
    await session.commit()
    return token


async def accept_transfer(
    session: AsyncSession, token: str, recipient: User
) -> Order:
    token = (token or "").strip()
    if not token or len(token) > 64:
        raise OrderError("Ссылка передачи недействительна.")
    order = await session.scalar(select(Order).where(Order.transfer_token == token))
    if (
        order is None
        or order.status not in DELIVERED
        or order.transferred_at is not None
        or not order.transfer_token
    ):
        raise OrderError("Ссылка передачи недействительна или уже использована.")
    expires = as_utc(order.transfer_expires_at)
    if expires is not None and dt.datetime.now(dt.UTC) > expires:
        order.transfer_token = None
        order.transfer_expires_at = None
        await session.commit()
        raise OrderError("Срок действия ссылки передачи истёк.")
    if order.user_id == recipient.id:
        raise OrderError("Нельзя передать аккаунт самому себе.")

    old_user = await session.get(User, order.user_id)
    if old_user is None:
        raise OrderError("Владелец заказа не найден.")
    now = dt.datetime.now(dt.UTC)
    result = await session.execute(
        update(Order)
        .where(
            Order.id == order.id,
            Order.user_id == old_user.id,
            Order.transfer_token == token,
            Order.transferred_at.is_(None),
        )
        .values(
            user_id=recipient.id,
            transferred_at=now,
            transfer_token=None,
            transfer_expires_at=None,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await session.refresh(order)
        raise OrderError("Ссылка передачи уже использована.")
    if old_user.orders_count > 0:
        old_user.orders_count -= 1
    recipient.orders_count += 1
    await log_event(
        LogSection.SHOP,
        "order_transferred",
        user_id=recipient.id,
        order_id=order.id,
        message=f"заказ передан от пользователя {old_user.id}",
        payload={"from_user_id": old_user.id, "to_user_id": recipient.id},
        session=session,
    )
    await session.commit()
    await session.refresh(order)
    return order


async def request_replacement(session: AsyncSession, order: Order) -> None:
    """Создать одну заявку на гарантийную замену."""
    if not replacement_open(order):
        raise OrderError("Срок гарантии на замену уже вышел или заявка уже отправлена.")
    user = await session.get(User, order.user_id)
    if user is None:
        raise OrderError("Покупатель заказа не найден.")
    if user.restrict_support:
        raise OrderError("Обращения для вашего аккаунта ограничены.")
    now = dt.datetime.now(dt.UTC)
    result = await session.execute(
        update(Order)
        .where(
            Order.id == order.id,
            Order.user_id == user.id,
            Order.replacement_requested_at.is_(None),
        )
        .values(replacement_requested_at=now)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await session.refresh(order)
        raise OrderError("Заявка на замену уже отправлена.")
    order.replacement_requested_at = now
    order.replacement_status = REPLACEMENT_PENDING
    order.replacement_decided_at = None
    order.replacement_error = None
    ticket = Ticket(
        user_id=user.id,
        status=TicketStatus.OPEN.value,
        subject=f"Замена аккаунта по заказу №{order.id}",
        order_id=order.id,
    )
    session.add(ticket)
    await session.flush()
    await support_service.add_user_message(
        session,
        ticket,
        text=(
            f"Прошу заменить аккаунт по заказу №{order.id}. "
            f"Страна: {order.offer_title}; номер: {mask_phone(order.phone)}."
        ),
    )
    await log_event(
        LogSection.SUPPORT,
        "replacement_requested",
        user_id=user.id,
        order_id=order.id,
        message=f"заявка на замену по гарантии {order.guarantee_hours} ч",
        session=session,
    )
    await session.commit()


async def _replacement_notice(
    session: AsyncSession, order: Order, text: str
) -> None:
    """Положить результат в очередь поддержки, которую доставляет её бот."""
    ticket = await session.scalar(
        select(Ticket)
        .where(Ticket.order_id == order.id)
        .order_by(Ticket.id.desc())
        .limit(1)
    )
    if ticket is not None:
        await support_service.add_system_message(session, ticket, text=text, delivered=False)


async def _replacement_failed(
    session: AsyncSession, order: Order, reason: str, *, admin_id: int | None = None
) -> str:
    now = dt.datetime.now(dt.UTC)
    order.replacement_status = REPLACEMENT_FAILED
    order.replacement_decided_at = now
    order.replacement_error = reason[:500]
    await log_event(
        LogSection.SHOP,
        "replacement_failed",
        level=LogLevel.WARN,
        user_id=order.user_id,
        order_id=order.id,
        admin_id=admin_id,
        message=reason[:400],
        session=session,
    )
    await session.commit()
    await _replacement_notice(
        session,
        order,
        f"Замена по заказу №{order.id} пока не выполнена: подходящий аккаунт не удалось выдать автоматически. Заявка осталась у администратора.",
    )
    return reason


async def _replacement_review(
    session: AsyncSession, order: Order, reason: str, *, admin_id: int | None = None
) -> str:
    """Покупка могла состояться: не разрешаем повтор до ручной перепроверки."""
    order.replacement_status = REPLACEMENT_REVIEW
    order.replacement_decided_at = dt.datetime.now(dt.UTC)
    order.replacement_error = reason[:500]
    await log_event(
        LogSection.LZT,
        "replacement_needs_review",
        level=LogLevel.ERROR,
        user_id=order.user_id,
        order_id=order.id,
        admin_id=admin_id,
        message=reason[:400],
        payload={"item_id": order.replacement_lzt_item_id},
        session=session,
    )
    await session.commit()
    await _replacement_notice(
        session,
        order,
        f"Замена по заказу №{order.id} подтверждена, но покупка требует дополнительной перепроверки администратором.",
    )
    return "Результат покупки неясен. Заявка оставлена на перепроверку, повторная закупка заблокирована."


async def _complete_replacement(
    session: AsyncSession,
    order: Order,
    lot: catalog.Lot,
    item: dict[str, Any],
    creds: Credentials,
    *,
    admin_id: int | None = None,
) -> str:
    if not creds.filled:
        raise OrderError("Маркет не отдал данные нового аккаунта.")
    previous_item_id = order.lzt_item_id
    previous_phone = order.phone
    snapshot = _snapshot(item, creds)
    snapshot["replacement_previous"] = {
        "item_id": previous_item_id,
        "phone": previous_phone,
    }
    order.status = OrderStatus.PURCHASED.value
    order.lzt_item_id = lot.item_id
    order.lzt_cost = lot.price
    order.phone = creds.phone or lot.phone
    order.lzt_raw = snapshot
    order.replacement_previous_item_id = previous_item_id
    order.replacement_lzt_item_id = lot.item_id
    order.replacement_lzt_cost = lot.price
    order.login_code = None
    order.code_requested_at = None
    order.code_issued_at = None
    order.code_requests = 0
    order.completed_at = dt.datetime.now(dt.UTC)
    order.error = None
    order.account_valid = None
    order.account_checked_at = None
    order.account_invalid_reason = None
    order.replacement_status = REPLACEMENT_COMPLETED
    order.replacement_decided_at = dt.datetime.now(dt.UTC)
    order.replacement_error = None
    await log_event(
        LogSection.SHOP,
        "replacement_completed",
        user_id=order.user_id,
        order_id=order.id,
        admin_id=admin_id,
        message=f"выдан новый лот {lot.item_id}",
        payload={
            "previous_item_id": previous_item_id,
            "item_id": lot.item_id,
            "replacement_cost": lot.price,
        },
        session=session,
    )
    await session.commit()
    await _replacement_notice(
        session,
        order,
        f"✅ Замена по заказу №{order.id} выполнена. Новый аккаунт уже доступен в разделе «Мои аккаунты».",
    )
    return "Замена выполнена: новый аккаунт записан в заказ."


async def _approve_replacement_impl(
    session: AsyncSession,
    order: Order,
    *,
    lzt: LztMarket | None = None,
    admin_id: int | None = None,
) -> str:
    """Подтвердить замену и купить эквивалентный лот без списания клиенту."""
    if order.replacement_requested_at is None:
        raise OrderError("По этому заказу заявки на замену нет.")
    if order.status not in DELIVERED or order.refunded:
        raise OrderError("Заменять можно только действующий оплаченный заказ.")
    result = await session.execute(
        update(Order)
        .where(
            Order.id == order.id,
            Order.status.in_(DELIVERED),
            Order.refunded.is_(False),
            Order.replacement_status.in_((REPLACEMENT_PENDING, REPLACEMENT_FAILED))
            | Order.replacement_status.is_(None),
        )
        .values(
            replacement_status=REPLACEMENT_PROCESSING,
            replacement_decided_at=dt.datetime.now(dt.UTC),
            replacement_error=None,
            replacement_lzt_item_id=None,
            replacement_lzt_cost=None,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await session.refresh(order)
        if order.replacement_status == REPLACEMENT_COMPLETED:
            return "Замена по этому заказу уже выполнена."
        if order.replacement_status == REPLACEMENT_PROCESSING:
            raise OrderError("Замена уже обрабатывается.")
        raise OrderError("Заявка уже обработана.")
    order.replacement_status = REPLACEMENT_PROCESSING
    order.replacement_decided_at = dt.datetime.now(dt.UTC)
    await session.commit()

    offer = await session.get(CountryOffer, order.offer_id) if order.offer_id else None
    if offer is None:
        return await _replacement_failed(
            session, order, "позиция страны удалена из каталога", admin_id=admin_id
        )

    client = lzt or get_lzt()
    try:
        lots = await catalog.find_lots(offer, lzt=client)
    except LztError as exc:
        return await _replacement_failed(
            session, order, f"маркет не ответил: {exc}", admin_id=admin_id
        )
    if not lots:
        return await _replacement_failed(
            session, order, "эквивалентного лота нет в наличии", admin_id=admin_id
        )

    last_error = "эквивалентный лот не куплен"
    for lot in lots:
        order.attempts += 1
        order.replacement_lzt_item_id = lot.item_id
        order.replacement_lzt_cost = lot.price
        await session.commit()
        try:
            answer = await client.purchasing_fast_buy(lot.item_id, lot.price_rub)
        except LztUncertainError as exc:
            return await _replacement_review(session, order, str(exc), admin_id=admin_id)
        except LztError as exc:
            last_error = str(exc)
            if _is_gone(exc):
                continue
            return await _replacement_failed(session, order, last_error, admin_id=admin_id)

        item = item_of(answer)
        creds = credentials_of(item)
        if not creds.filled:
            try:
                item = item_of(await client.item_get(lot.item_id))
                creds = credentials_of(item)
            except LztError as exc:
                return await _replacement_review(session, order, str(exc), admin_id=admin_id)
        if not creds.filled:
            return await _replacement_review(
                session, order, "маркет купил лот, но не отдал данные входа", admin_id=admin_id
            )
        return await _complete_replacement(
            session, order, lot, item, creds, admin_id=admin_id
        )

    return await _replacement_failed(session, order, last_error, admin_id=admin_id)


async def approve_replacement(
    session: AsyncSession,
    order: Order,
    *,
    lzt: LztMarket | None = None,
    admin_id: int | None = None,
) -> str:
    """Approve a replacement and turn unexpected failures into a retryable state."""
    try:
        return await _approve_replacement_impl(session, order, lzt=lzt, admin_id=admin_id)
    except OrderError:
        raise
    except Exception as exc:
        log.exception("replacement processing failed for order %s", order.id)
        await session.rollback()
        await session.refresh(order)
        # Once an item id is recorded, the market may already have charged us;
        # never make that case retryable as a second purchase.
        if order.replacement_lzt_item_id:
            return await _replacement_review(
                session,
                order,
                "покупка завершилась с технической ошибкой; требуется перепроверка",
                admin_id=admin_id,
            )
        return await _replacement_failed(
            session, order, "внутренняя ошибка обработки замены", admin_id=admin_id
        )


async def retry_processing_replacement(
    session: AsyncSession,
    order: Order,
    *,
    lzt: LztMarket | None = None,
    admin_id: int | None = None,
) -> str:
    """Recover a process killed before a replacement lot was selected."""
    if order.replacement_status != REPLACEMENT_PROCESSING:
        raise OrderError("Эта замена не зависла в состоянии покупки.")
    if order.replacement_lzt_item_id:
        raise OrderError("У этой замены уже есть item ID. Используйте перепроверку.")
    started_at = as_utc(order.replacement_decided_at)
    if started_at is not None and dt.datetime.now(dt.UTC) - started_at < REPLACEMENT_STUCK_AFTER:
        raise OrderError("Замена ещё обрабатывается. Перезапуск станет доступен через 10 минут.")
    result = await session.execute(
        update(Order)
        .where(
            Order.id == order.id,
            Order.status.in_(DELIVERED),
            Order.refunded.is_(False),
            Order.replacement_status == REPLACEMENT_PROCESSING,
            Order.replacement_lzt_item_id.is_(None),
        )
        .values(replacement_status=REPLACEMENT_FAILED, replacement_error="обработка перезапущена администратором")
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await session.refresh(order)
        raise OrderError("Состояние замены уже изменилось.")
    await session.commit()
    await session.refresh(order)
    return await approve_replacement(session, order, lzt=lzt, admin_id=admin_id)


async def recheck_replacement(
    session: AsyncSession,
    order: Order,
    *,
    lzt: LztMarket | None = None,
    admin_id: int | None = None,
) -> str:
    """Завершить неопределённую закупку, не покупая второй лот."""
    if order.status not in DELIVERED or order.refunded:
        raise OrderError("Перепроверять можно только действующий оплаченный заказ.")
    if order.replacement_status not in (REPLACEMENT_REVIEW, REPLACEMENT_PROCESSING) or not order.replacement_lzt_item_id:
        raise OrderError("Эта замена не ожидает перепроверки.")
    previous_status = order.replacement_status
    previous_started_at = as_utc(order.replacement_decided_at)
    if (
        previous_status == REPLACEMENT_PROCESSING
        and previous_started_at is not None
        and dt.datetime.now(dt.UTC) - previous_started_at < REPLACEMENT_STUCK_AFTER
    ):
        raise OrderError("Замена ещё обрабатывается. Перепроверка станет доступна через 10 минут.")
    claim = update(Order).where(
        Order.id == order.id,
        Order.status.in_(DELIVERED),
        Order.refunded.is_(False),
        Order.replacement_status == previous_status,
        Order.replacement_lzt_item_id == order.replacement_lzt_item_id,
    )
    if previous_started_at is None:
        claim = claim.where(Order.replacement_decided_at.is_(None))
    else:
        claim = claim.where(Order.replacement_decided_at == order.replacement_decided_at)
    claimed_at = dt.datetime.now(dt.UTC)
    result = await session.execute(
        claim.values(
            replacement_status=REPLACEMENT_PROCESSING,
            replacement_decided_at=claimed_at,
        ).execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await session.refresh(order)
        raise OrderError("Состояние замены уже изменилось.")
    order.replacement_status = REPLACEMENT_PROCESSING
    order.replacement_decided_at = claimed_at
    await session.commit()
    client = lzt or get_lzt()
    try:
        item = item_of(await client.item_get(order.replacement_lzt_item_id))
    except LztError as exc:
        order.replacement_status = REPLACEMENT_REVIEW
        order.replacement_decided_at = dt.datetime.now(dt.UTC)
        order.replacement_error = str(exc)[:500]
        await session.commit()
        raise OrderError(f"Маркет не ответил: {exc}") from exc
    creds = credentials_of(item)
    if not creds.filled:
        order.replacement_status = REPLACEMENT_REVIEW
        order.replacement_decided_at = dt.datetime.now(dt.UTC)
        order.replacement_error = "маркет пока не отдал данные входа"
        await session.commit()
        raise OrderError("Маркет пока не отдал данные входа. Повторите перепроверку позже.")
    lot = catalog.Lot(
        item_id=order.replacement_lzt_item_id,
        price=order.replacement_lzt_cost or _price_of(item) or 0,
        phone=creds.phone,
        raw=item,
    )
    return await _complete_replacement(session, order, lot, item, creds, admin_id=admin_id)


async def reject_replacement(
    session: AsyncSession,
    order: Order,
    *,
    reason: str | None = None,
    admin_id: int | None = None,
) -> str:
    """Отклонить заявку без изменения товара и баланса."""
    if order.replacement_requested_at is None:
        raise OrderError("По этому заказу заявки на замену нет.")
    result = await session.execute(
        update(Order)
        .where(
            Order.id == order.id,
            Order.replacement_status.in_((REPLACEMENT_PENDING,))
            | Order.replacement_status.is_(None),
        )
        .values(
            replacement_status=REPLACEMENT_REJECTED,
            replacement_decided_at=dt.datetime.now(dt.UTC),
            replacement_error=(reason or "заявка отклонена администратором")[:500],
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await session.refresh(order)
        raise OrderError("Заявка уже обработана.")
    order.replacement_status = REPLACEMENT_REJECTED
    order.replacement_decided_at = dt.datetime.now(dt.UTC)
    order.replacement_error = (reason or "заявка отклонена администратором")[:500]
    await log_event(
        LogSection.SHOP,
        "replacement_rejected",
        level=LogLevel.INFO,
        user_id=order.user_id,
        order_id=order.id,
        admin_id=admin_id,
        message=order.replacement_error,
        session=session,
    )
    await session.commit()
    await _replacement_notice(
        session,
        order,
        f"Заявка на замену по заказу №{order.id} отклонена. Причина: {order.replacement_error}",
    )
    return "Заявка отклонена."


def code_until(order: Order, hours: int) -> dt.datetime | None:
    """Момент, после которого кнопка кода закрывается. None — срока нет.

    Считаем от покупки, а не от первого запроса: клиент платит и с этой секунды
    знает, сколько у него времени.
    """
    if not hours:
        return None
    start = as_utc(order.completed_at) or as_utc(order.created_at)
    if start is None:
        return None
    return start + dt.timedelta(hours=hours)


def code_open(order: Order, hours: int) -> bool:
    until = code_until(order, hours)
    return until is None or dt.datetime.now(dt.UTC) <= until


async def issue_code(
    session: AsyncSession,
    order: Order,
    *,
    lzt: LztMarket | None = None,
    hours: int | None = None,
    admin_id: int | None = None,
) -> str:
    """Запросить код входа у маркета. Возвращает код или поднимает OrderError.

    hours=0 снимает срок — так код выдаёт админка, у неё своих ограничений нет.
    """
    if order.status not in DELIVERED:
        raise OrderError("Код доступен только по оплаченному заказу.")
    if not order.lzt_item_id:
        raise OrderError("По этому заказу код запросить нельзя. Напишите в поддержку.")

    window = hours if hours is not None else (order.guarantee_hours or await code_hours(session))
    if not code_open(order, window):
        raise CodeWindowClosed(
            f"Код входа выдаётся {window} ч после покупки — срок вышел. "
            "Дальше поможет поддержка."
        )

    client = lzt or get_lzt()
    try:
        answer = await client.telegram_confirmation_code(order.lzt_item_id)
    except LztError as exc:
        if _is_invalid_account_error(exc):
            order.account_valid = False
            order.account_checked_at = dt.datetime.now(dt.UTC)
            order.account_invalid_reason = str(exc)[:500]
            await session.commit()
            await log_event(
                LogSection.SHOP,
                "account_invalid",
                level=LogLevel.WARN,
                user_id=order.user_id,
                order_id=order.id,
                admin_id=admin_id,
                message=str(exc)[:400],
                session=session,
            )
            raise OrderError(
                "Маркет сообщил, что аккаунт недействителен. Напишите в поддержку."
            ) from exc
        await log_event(
            LogSection.LZT,
            "code_failed",
            level=LogLevel.WARN,
            user_id=order.user_id,
            order_id=order.id,
            admin_id=admin_id,
            message=str(exc)[:400],
        )
        if _is_gone(exc):
            raise OrderError(
                "Код по этому номеру больше не выдаётся. Напишите в поддержку."
            ) from exc
        raise OrderError("Маркет не отдал код. Попробуйте ещё раз через минуту.") from exc

    order.code_requests += 1
    order.code_requested_at = dt.datetime.now(dt.UTC)
    order.account_valid = True
    order.account_checked_at = order.code_requested_at
    order.account_invalid_reason = None

    code = newest_login_code(answer)
    if not code:
        await session.commit()
        raise OrderError(
            "Кода ещё нет. Запросите вход в Telegram и нажмите «Получить код» снова."
        )

    order.login_code = code
    order.code_issued_at = order.code_requested_at
    if order.status == OrderStatus.PURCHASED.value:
        order.status = OrderStatus.CODE_ISSUED.value
    await log_event(
        LogSection.SHOP,
        "code_issued",
        user_id=order.user_id,
        order_id=order.id,
        admin_id=admin_id,
        message=f"{mask_phone(order.phone)}, запрос {order.code_requests}",
        session=session,
    )
    await session.commit()
    return code


async def check_validity(
    session: AsyncSession,
    order: Order,
    *,
    lzt: LztMarket | None = None,
    admin_id: int | None = None,
) -> bool:
    """Проверить аккаунт запросом кода, не сохраняя новый код как выдачу."""
    if order.status not in DELIVERED or not order.lzt_item_id:
        raise OrderError("Проверять можно только оплаченный заказ с лотом LZT.")
    client = lzt or get_lzt()
    try:
        answer = await client.telegram_confirmation_code(order.lzt_item_id)
    except LztError as exc:
        if _is_invalid_account_error(exc):
            order.account_valid = False
            order.account_checked_at = dt.datetime.now(dt.UTC)
            order.account_invalid_reason = str(exc)[:500]
            result = False
        else:
            raise OrderError(f"Временная ошибка проверки: {exc}") from exc
    else:
        order.account_valid = True
        order.account_checked_at = dt.datetime.now(dt.UTC)
        order.account_invalid_reason = None
        result = True
        # Проверка получает код только для диагностики: не выдаём его клиенту.
        _ = newest_login_code(answer)
    await log_event(
        LogSection.SHOP,
        "account_validity_checked",
        level=LogLevel.INFO if result else LogLevel.WARN,
        user_id=order.user_id,
        order_id=order.id,
        admin_id=admin_id,
        message="аккаунт действителен" if result else order.account_invalid_reason or "аккаунт недействителен",
        session=session,
    )
    await session.commit()
    return result


async def reset_auth(
    session: AsyncSession,
    order: Order,
    *,
    lzt: LztMarket | None = None,
    admin_id: int | None = None,
) -> None:
    """Сбросить чужие сессии номера: продавец мог остаться в аккаунте."""
    if order.status not in DELIVERED or not order.lzt_item_id:
        raise OrderError("Сброс сессий доступен только по оплаченному заказу.")

    client = lzt or get_lzt()
    try:
        await client.telegram_reset_auth(order.lzt_item_id)
    except LztError as exc:
        await log_event(
            LogSection.LZT,
            "reset_failed",
            level=LogLevel.WARN,
            user_id=order.user_id,
            order_id=order.id,
            admin_id=admin_id,
            message=str(exc)[:400],
        )
        raise OrderError("Не получилось сбросить сессии. Попробуйте позже.") from exc

    await log_event(
        LogSection.SHOP,
        "auth_reset",
        user_id=order.user_id,
        order_id=order.id,
        admin_id=admin_id,
        message=mask_phone(order.phone),
        session=session,
    )
    await session.commit()


# --------------------------------------------------------------------------- #
#  Возврат и разбор зависших заказов
# --------------------------------------------------------------------------- #
async def refund(
    session: AsyncSession,
    order: Order,
    *,
    admin_id: int | None = None,
    comment: str | None = None,
) -> None:
    """Ручной возврат из админки."""
    owner = await session.get(User, order.user_id)
    payer = await session.get(User, order.buyer_user_id or order.user_id)
    if owner is None or payer is None:
        raise OrderError("Клиент заказа не найден.")

    was_delivered = order.status in DELIVERED
    now = dt.datetime.now(dt.UTC)
    result = await session.execute(
        update(Order)
        .where(
            Order.id == order.id,
            Order.refunded.is_(False),
            or_(
                Order.replacement_status.is_(None),
                ~Order.replacement_status.in_((REPLACEMENT_PROCESSING, REPLACEMENT_REVIEW)),
            ),
        )
        .values(
            refunded=True,
            status=OrderStatus.REFUNDED.value,
            completed_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await session.refresh(order)
        if not order.refunded and order.replacement_status in (
            REPLACEMENT_PROCESSING,
            REPLACEMENT_REVIEW,
        ):
            raise OrderError("Сначала завершите или разберите гарантийную замену.")
        raise OrderError("По этому заказу деньги уже возвращены.")

    # Захват заказа выполнен в той же транзакции, что и проводка. Если credit
    # или commit упадут, session_scope откатит и захват, и баланс целиком.
    await balance.credit(
        session,
        payer,
        order.price,
        TxKind.REFUND,
        order_id=order.id,
        admin_id=admin_id,
        comment=comment or f"возврат по заказу №{order.id}",
    )
    order.refunded = True
    order.status = OrderStatus.REFUNDED.value
    order.completed_at = now
    # Покупка отменена — она не должна оставаться в счётчике покупок клиента.
    if was_delivered and owner.orders_count > 0:
        owner.orders_count -= 1
    await log_event(
        LogSection.SHOP,
        "order_refunded",
        user_id=payer.id,
        order_id=order.id,
        admin_id=admin_id,
        message=f"{fmt_money(order.price)} вернулись, {comment or 'без комментария'}",
        session=session,
    )
    await session.commit()


async def recheck(
    session: AsyncSession,
    order: Order,
    *,
    lzt: LztMarket | None = None,
    admin_id: int | None = None,
) -> str:
    """Разобрать заказ, зависший в статусе «поиск» или «покупка».

    Такое бывает, если связь оборвалась после fast-buy: деньги списаны, лот
    возможно куплен, а результат записать не успели. Данные входа маркет отдаёт
    только покупателю — если они видны, лот наш, и заказ можно закрыть.
    Если реквизиты уже видны, закрываем заказ как купленный. Если маркет пока
    не отдаёт их, заказ остаётся на перепроверке: возврат в этот момент мог бы
    привести к потере уже купленного лота.
    """
    if order.status not in IN_FLIGHT:
        raise OrderError("Этот заказ не в работе, перепроверять нечего.")
    owner = await session.get(User, order.user_id)
    payer = await session.get(User, order.buyer_user_id or order.user_id)
    if owner is None or payer is None:
        raise OrderError("Клиент заказа не найден.")

    if not order.lzt_item_id:
        await _fail(session, order, payer, "лот не был выбран", level=LogLevel.INFO)
        return "Лот не выбирался — заказ закрыт, деньги вернулись."

    client = lzt or get_lzt()
    try:
        item = item_of(await client.item_get(order.lzt_item_id))
    except LztError as exc:
        raise OrderError(f"Маркет не ответил: {exc}") from exc

    creds = credentials_of(item)
    if creds.filled:
        lot = catalog.Lot(
            item_id=order.lzt_item_id,
            price=order.lzt_cost or _price_of(item) or order.price,
            phone=creds.phone,
            raw=item,
        )
        await _complete(session, order, lot, item, creds)
        return f"Лот наш: заказ закрыт, номер {mask_phone(order.phone)} выдан клиенту."

    # Отсутствие реквизитов не доказывает, что покупка не состоялась: API мог
    # вернуть урезанную карточку или ответить до синхронизации заказа. Оставляем
    # его в работе для повторной перепроверки, чтобы не потерять купленный лот.
    order.status = OrderStatus.BUYING.value
    order.error = "маркет пока не отдал данные входа"
    await log_event(
        LogSection.LZT,
        "recheck_pending_credentials",
        level=LogLevel.WARN,
        user_id=payer.id,
        order_id=order.id,
        message=order.error,
        payload={"item_id": order.lzt_item_id},
        session=session,
    )
    await session.commit()
    return "Маркет пока не отдал данные входа — заказ оставлен на перепроверку."


def _price_of(item: dict[str, Any]) -> int | None:
    price = item.get("price")
    return rub_to_kop(price) if price is not None else None


# --------------------------------------------------------------------------- #
#  Выборки
# --------------------------------------------------------------------------- #
async def for_user(
    session: AsyncSession, user: User, *, delivered_only: bool = False, limit: int = 20
) -> list[Order]:
    stmt = select(Order).where(Order.user_id == user.id)
    if delivered_only:
        stmt = stmt.where(Order.status.in_(DELIVERED))
    stmt = stmt.order_by(Order.id.desc()).limit(limit)
    return list((await session.scalars(stmt)).all())


async def get_owned(session: AsyncSession, order_id: int, user: User) -> Order | None:
    """Заказ клиента по id. Чужой заказ не отдаём: id в callback_data подделывается."""
    order = await session.get(Order, order_id)
    if order is None or order.user_id != user.id:
        return None
    return order
