"""Схема базы.

Деньги везде — целые копейки (int). Никаких float на балансах и ценах:
рубль дробится ровно, float — нет.
Цены LZT приходят рублями с дробью, при записи умножаем на 100 и округляем.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.enums import (
    AdminRole,
    BroadcastStatus,
    DeliveryStatus,
    LogLevel,
    LogSection,
    OrderStatus,
    PaymentProvider,
    PaymentStatus,
    ReviewStatus,
    TicketStatus,
    TxKind,
    YesNo,
)

TS = DateTime(timezone=True)


def created_col() -> Mapped[dt.datetime]:
    return mapped_column(TS, server_default=func.now(), nullable=False, index=True)


# --------------------------------------------------------------------------- #
#  Пользователи
# --------------------------------------------------------------------------- #
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)

    # Web authentication providers. Telegram users keep their tg_id; Google
    # users receive a deterministic synthetic tg_id and are keyed by subject.
    auth_provider: Mapped[str | None] = mapped_column(String(16), index=True)
    auth_subject: Mapped[str | None] = mapped_column(String(255), index=True)
    email: Mapped[str | None] = mapped_column(String(255))
    avatar_url: Mapped[str | None] = mapped_column(String(500))

    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))
    last_name: Mapped[str | None] = mapped_column(String(128))
    language_code: Mapped[str | None] = mapped_column(String(8))
    is_tg_premium: Mapped[bool] = mapped_column(Boolean, default=False)

    # деньги, копейки
    balance: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_topup: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_spent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ref_earned: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    orders_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # реферальная система
    referrer_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    ref_percent: Mapped[int | None] = mapped_column(SmallInteger)  # переопределение общего
    referrals_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    start_payload: Mapped[str | None] = mapped_column(String(64))

    # ограничения
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    ban_reason: Mapped[str | None] = mapped_column(String(255))
    banned_until: Mapped[dt.datetime | None] = mapped_column(TS)
    restrict_buy: Mapped[bool] = mapped_column(Boolean, default=False)
    restrict_topup: Mapped[bool] = mapped_column(Boolean, default=False)
    restrict_support: Mapped[bool] = mapped_column(Boolean, default=False)

    admin_note: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[dt.datetime] = created_col()
    last_seen_at: Mapped[dt.datetime | None] = mapped_column(TS)
    first_paid_at: Mapped[dt.datetime | None] = mapped_column(TS)
    first_order_at: Mapped[dt.datetime | None] = mapped_column(TS)

    referrer: Mapped[User | None] = relationship(remote_side=[id], back_populates="referrals")
    referrals: Mapped[list[User]] = relationship(back_populates="referrer")

    @property
    def display_name(self) -> str:
        if self.username:
            return f"@{self.username}"
        name = " ".join(filter(None, [self.first_name, self.last_name])).strip()
        return name or f"id{self.tg_id}"


# --------------------------------------------------------------------------- #
#  Каталог: страна = товар
# --------------------------------------------------------------------------- #
class CountryOffer(Base):
    """Позиция магазина.

    Покупатель видит `price`. Бот ищет на LZT лот дешевле `buy_limit`
    с фильтрами ниже; если такого нет — позиция показывается как «нет в наличии».
    """

    __tablename__ = "country_offers"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(8), unique=True)  # RU, KZ, UA
    title: Mapped[str] = mapped_column(String(64))  # «Россия»
    # Точное значение для country[] в GET /telegram. Правится в админке,
    # список допустимых значений тянется из GET /telegram/params.
    lzt_country: Mapped[str] = mapped_column(String(64))

    price: Mapped[int] = mapped_column(Integer, nullable=False)  # копейки, для покупателя
    buy_limit: Mapped[int] = mapped_column(Integer, nullable=False)  # копейки, максимум закупки

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    sort: Mapped[int] = mapped_column(Integer, default=100)

    # фильтры поиска лота
    spam_filter: Mapped[str] = mapped_column(String(16), default=YesNo.NO.value)
    password_filter: Mapped[str] = mapped_column(String(16), default=YesNo.NOMATTER.value)
    origin_filter: Mapped[list | None] = mapped_column(JSON)  # origin[] на LZT
    extra_filters: Mapped[dict | None] = mapped_column(JSON)  # любые доп. query-параметры

    # кэш наличия, чтобы витрина не ходила в LZT на каждый рендер
    stock_cached: Mapped[int | None] = mapped_column(Integer)
    stock_checked_at: Mapped[dt.datetime | None] = mapped_column(TS)

    description: Mapped[str | None] = mapped_column(String(255))
    # Срок гарантии и самостоятельной выдачи кода для новых заказов.
    guarantee_hours: Mapped[int] = mapped_column(Integer, default=12, nullable=False)
    created_at: Mapped[dt.datetime] = created_col()
    updated_at: Mapped[dt.datetime | None] = mapped_column(TS, onupdate=func.now())


class MarketStat(Base):
    """Срез маркета по одной стране: сколько лотов и по какой цене.

    Заполняется обходом из админки (app/services/market.py) — перечня стран у
    маркета нет, наличие узнаётся только поиском. Нужен затем, чтобы страну
    выбирали по цифрам, а не наугад: видно, есть ли лоты и от какой цены.

    Цены — копейки. price_avg — средняя по выборке самых дешёвых лотов первой
    страницы (sample), а не по всему маркету: постранично обходить весь маркет
    ради средней слишком дорого. В админке это подписано.
    """

    __tablename__ = "market_stats"

    code: Mapped[str] = mapped_column(String(8), primary_key=True)

    lots: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # totalItems
    sample: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # лотов в выборке
    price_min: Mapped[int | None] = mapped_column(Integer)
    price_avg: Mapped[int | None] = mapped_column(Integer)
    price_max: Mapped[int | None] = mapped_column(Integer)

    # с какими фильтрами считали — иначе цифры не с чем сравнивать
    pmax: Mapped[int | None] = mapped_column(Integer)
    spam: Mapped[str] = mapped_column(String(16), default=YesNo.NO.value)

    error: Mapped[str | None] = mapped_column(String(255))
    checked_at: Mapped[dt.datetime | None] = mapped_column(TS)


# --------------------------------------------------------------------------- #
#  Заказы
# --------------------------------------------------------------------------- #
class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Получатель и владелец товара. Для обычной покупки совпадает с покупателем.
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # Кто оплатил заказ. Нужен отдельно для подарков и корректного возврата денег.
    buyer_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    offer_id: Mapped[int | None] = mapped_column(ForeignKey("country_offers.id", ondelete="SET NULL"))

    # снимок позиции на момент заказа — каталог потом меняется, история не должна врать
    offer_code: Mapped[str] = mapped_column(String(8))
    offer_title: Mapped[str] = mapped_column(String(64))
    price: Mapped[int] = mapped_column(Integer, nullable=False)  # списано с покупателя
    # Снимок гарантии на момент покупки, чтобы правка каталога не меняла старые заказы.
    guarantee_hours: Mapped[int] = mapped_column(Integer, default=12, nullable=False)

    status: Mapped[str] = mapped_column(String(24), default=OrderStatus.NEW.value, index=True)

    # сторона LZT
    lzt_item_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    lzt_cost: Mapped[int | None] = mapped_column(Integer)  # копейки, сколько отдали маркету
    lzt_raw: Mapped[dict | None] = mapped_column(JSON)  # карточка лота как пришла

    phone: Mapped[str | None] = mapped_column(String(32))
    login_code: Mapped[str | None] = mapped_column(String(32))
    code_requests: Mapped[int] = mapped_column(Integer, default=0)
    code_requested_at: Mapped[dt.datetime | None] = mapped_column(TS)
    code_issued_at: Mapped[dt.datetime | None] = mapped_column(TS)

    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(String(500))
    refunded: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[dt.datetime] = created_col()
    completed_at: Mapped[dt.datetime | None] = mapped_column(TS)
    replacement_requested_at: Mapped[dt.datetime | None] = mapped_column(TS)
    # Состояние гарантийной замены: pending/processing/completed/rejected/failed.
    replacement_status: Mapped[str | None] = mapped_column(String(16), index=True)
    replacement_decided_at: Mapped[dt.datetime | None] = mapped_column(TS)
    replacement_error: Mapped[str | None] = mapped_column(String(500))
    replacement_lzt_item_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    replacement_lzt_cost: Mapped[int | None] = mapped_column(Integer)
    replacement_previous_item_id: Mapped[int | None] = mapped_column(BigInteger)
    # None означает, что проверка ещё не выполнялась.
    account_valid: Mapped[bool | None] = mapped_column(Boolean, index=True)
    account_checked_at: Mapped[dt.datetime | None] = mapped_column(TS)
    account_invalid_reason: Mapped[str | None] = mapped_column(String(500))
    transferred_at: Mapped[dt.datetime | None] = mapped_column(TS)
    transfer_token: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    transfer_expires_at: Mapped[dt.datetime | None] = mapped_column(TS)

    user: Mapped[User] = relationship(foreign_keys=[user_id])
    buyer: Mapped[User | None] = relationship(foreign_keys=[buyer_user_id])

    __table_args__ = (Index("ix_orders_user_status", "user_id", "status"),)

    @property
    def margin(self) -> int | None:
        if self.lzt_cost is None:
            return None
        return self.price - self.lzt_cost


# --------------------------------------------------------------------------- #
#  Деньги
# --------------------------------------------------------------------------- #
class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)

    provider: Mapped[str] = mapped_column(String(16), index=True)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)  # копейки, рубли
    credited: Mapped[int] = mapped_column(Integer, default=0)  # реально зачислено
    status: Mapped[str] = mapped_column(String(16), default=PaymentStatus.PENDING.value, index=True)

    external_id: Mapped[str | None] = mapped_column(String(128), index=True)
    invoice_url: Mapped[str | None] = mapped_column(String(500))
    asset: Mapped[str | None] = mapped_column(String(16))  # USDT / TON / RUB
    raw: Mapped[dict | None] = mapped_column(JSON)

    created_at: Mapped[dt.datetime] = created_col()
    paid_at: Mapped[dt.datetime | None] = mapped_column(TS)
    expires_at: Mapped[dt.datetime | None] = mapped_column(TS)

    user: Mapped[User] = relationship()

    # один и тот же счёт провайдера не должен зачислиться дважды
    __table_args__ = (
        UniqueConstraint("provider", "external_id", name="uq_payment_provider_external"),
    )


class BalanceTx(Base):
    """Книга операций. Баланс пользователя всегда = сумма его amount."""

    __tablename__ = "balance_tx"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(24), index=True)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)  # со знаком
    balance_after: Mapped[int] = mapped_column(Integer, nullable=False)

    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"))
    payment_id: Mapped[int | None] = mapped_column(ForeignKey("payments.id", ondelete="SET NULL"))
    admin_id: Mapped[int | None] = mapped_column(ForeignKey("admins.id", ondelete="SET NULL"))
    comment: Mapped[str | None] = mapped_column(String(255))

    created_at: Mapped[dt.datetime] = created_col()

    __table_args__ = (
        # Для заказа допускается ровно одна покупка и одна проводка возврата.
        # При NULL (пополнения и ручные правки) ограничение не мешает истории.
        UniqueConstraint("order_id", "kind", name="uq_balance_tx_order_kind"),
    )


class ReferralEarning(Base):
    __tablename__ = "referral_earnings"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )  # кому начислили
    from_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    payment_id: Mapped[int | None] = mapped_column(ForeignKey("payments.id", ondelete="SET NULL"))
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    percent: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    created_at: Mapped[dt.datetime] = created_col()


class PromoCode(Base):
    """Промокод с фиксированным начислением на баланс."""

    __tablename__ = "promo_codes"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    title: Mapped[str | None] = mapped_column(String(120))
    bonus: Mapped[int] = mapped_column(Integer, nullable=False)
    max_uses: Mapped[int | None] = mapped_column(Integer)
    uses_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    expires_at: Mapped[dt.datetime | None] = mapped_column(TS, index=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("admins.id", ondelete="SET NULL"))
    created_at: Mapped[dt.datetime] = created_col()


class PromoRedemption(Base):
    __tablename__ = "promo_redemptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    promo_id: Mapped[int] = mapped_column(ForeignKey("promo_codes.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    bonus: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[dt.datetime] = created_col()

    promo: Mapped[PromoCode] = relationship()
    user: Mapped[User] = relationship()

    __table_args__ = (
        UniqueConstraint("promo_id", "user_id", name="uq_promo_redemption_user"),
    )


# --------------------------------------------------------------------------- #
#  Отзывы
# --------------------------------------------------------------------------- #
class Review(Base):
    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Тестовый отзыв из админки клиента и заказа не имеет: ник и товар админ
    # пишет руками, чтобы посмотреть карточку в канале, не покупая номер.
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    order_id: Mapped[int | None] = mapped_column(
        ForeignKey("orders.id", ondelete="SET NULL"), unique=True
    )
    stars: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    # Оценка обязательна, текст — нет: «без текста» это нормальный отзыв.
    text: Mapped[str | None] = mapped_column(Text)
    author_name: Mapped[str | None] = mapped_column(String(64))
    product_title: Mapped[str | None] = mapped_column(String(120))

    status: Mapped[str] = mapped_column(String(16), default=ReviewStatus.PENDING.value, index=True)
    image_path: Mapped[str | None] = mapped_column(String(255))
    channel_message_id: Mapped[int | None] = mapped_column(BigInteger)
    reject_reason: Mapped[str | None] = mapped_column(String(255))
    moderated_by: Mapped[int | None] = mapped_column(ForeignKey("admins.id", ondelete="SET NULL"))

    created_at: Mapped[dt.datetime] = created_col()
    published_at: Mapped[dt.datetime | None] = mapped_column(TS)

    user: Mapped[User | None] = relationship()


# --------------------------------------------------------------------------- #
#  Поддержка
# --------------------------------------------------------------------------- #
class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(16), default=TicketStatus.OPEN.value, index=True)
    subject: Mapped[str | None] = mapped_column(String(160))
    assigned_admin_id: Mapped[int | None] = mapped_column(
        ForeignKey("admins.id", ondelete="SET NULL")
    )
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"))

    unread_admin: Mapped[int] = mapped_column(Integer, default=0)
    unread_user: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[dt.datetime] = created_col()
    last_message_at: Mapped[dt.datetime | None] = mapped_column(TS, index=True)
    closed_at: Mapped[dt.datetime | None] = mapped_column(TS)

    user: Mapped[User] = relationship()
    messages: Mapped[list[TicketMessage]] = relationship(
        back_populates="ticket", cascade="all, delete-orphan", order_by="TicketMessage.id"
    )


class TicketMessage(Base):
    __tablename__ = "ticket_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"), index=True)
    sender: Mapped[str] = mapped_column(String(8))
    admin_id: Mapped[int | None] = mapped_column(ForeignKey("admins.id", ondelete="SET NULL"))

    text: Mapped[str | None] = mapped_column(Text)
    media_type: Mapped[str | None] = mapped_column(String(16))  # photo / document / voice
    media_file_id: Mapped[str | None] = mapped_column(String(255))
    tg_message_id: Mapped[int | None] = mapped_column(BigInteger)

    created_at: Mapped[dt.datetime] = created_col()

    ticket: Mapped[Ticket] = relationship(back_populates="messages")


# --------------------------------------------------------------------------- #
#  Рассылки
# --------------------------------------------------------------------------- #
class Broadcast(Base):
    __tablename__ = "broadcasts"

    id: Mapped[int] = mapped_column(primary_key=True)
    admin_id: Mapped[int | None] = mapped_column(ForeignKey("admins.id", ondelete="SET NULL"))
    title: Mapped[str | None] = mapped_column(String(120))

    # HTML телеграма: <b>, <a href>, <tg-emoji emoji-id> для премиум-эмодзи
    text: Mapped[str] = mapped_column(Text)
    parse_mode: Mapped[str] = mapped_column(String(16), default="HTML")
    image_file_id: Mapped[str | None] = mapped_column(String(255))
    image_path: Mapped[str | None] = mapped_column(String(255))
    buttons: Mapped[list | None] = mapped_column(JSON)  # [{"text": ..., "url": ...}]

    audience: Mapped[str] = mapped_column(String(24), default="all")
    audience_filter: Mapped[dict | None] = mapped_column(JSON)

    status: Mapped[str] = mapped_column(String(16), default=BroadcastStatus.DRAFT.value, index=True)
    total: Mapped[int] = mapped_column(Integer, default=0)
    sent: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    blocked: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(String(500))

    created_at: Mapped[dt.datetime] = created_col()
    started_at: Mapped[dt.datetime | None] = mapped_column(TS)
    finished_at: Mapped[dt.datetime | None] = mapped_column(TS)


class BroadcastDelivery(Base):
    """По одной строке на получателя — чтобы рассылку можно было продолжить после падения."""

    __tablename__ = "broadcast_deliveries"

    id: Mapped[int] = mapped_column(primary_key=True)
    broadcast_id: Mapped[int] = mapped_column(
        ForeignKey("broadcasts.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(16), default=DeliveryStatus.QUEUED.value, index=True)
    error: Mapped[str | None] = mapped_column(String(255))
    sent_at: Mapped[dt.datetime | None] = mapped_column(TS)

    __table_args__ = (
        UniqueConstraint("broadcast_id", "user_id", name="uq_delivery_broadcast_user"),
    )


# --------------------------------------------------------------------------- #
#  Логи и админы
# --------------------------------------------------------------------------- #
class EventLog(Base):
    """Единый журнал. Раздел (section) — то, по чему сортируем в админке."""

    __tablename__ = "event_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[dt.datetime] = created_col()
    section: Mapped[str] = mapped_column(String(24), index=True)
    level: Mapped[str] = mapped_column(String(8), default=LogLevel.INFO.value, index=True)
    event: Mapped[str] = mapped_column(String(64), index=True)

    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    admin_id: Mapped[int | None] = mapped_column(ForeignKey("admins.id", ondelete="SET NULL"))
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"))

    message: Mapped[str | None] = mapped_column(String(500))
    payload: Mapped[dict | None] = mapped_column(JSON)
    ip: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        Index("ix_event_logs_section_ts", "section", "ts"),
        Index("ix_event_logs_level_ts", "level", "ts"),
    )


class Admin(Base):
    __tablename__ = "admins"

    id: Mapped[int] = mapped_column(primary_key=True)
    login: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default=AdminRole.ADMIN.value)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    tg_id: Mapped[int | None] = mapped_column(BigInteger)  # для уведомлений о тикетах

    created_at: Mapped[dt.datetime] = created_col()
    last_login_at: Mapped[dt.datetime | None] = mapped_column(TS)


class AdminIpBan(Base):
    """Забаненный за перебор пароля адрес. Снимается только в панели.

    Лежит в базе, а не в памяти процесса: перезапуск админки не должен открывать
    подбор заново, а список забаненных нужен на экране.
    """

    __tablename__ = "admin_ip_bans"

    id: Mapped[int] = mapped_column(primary_key=True)
    ip: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    fails: Mapped[int] = mapped_column(Integer, default=0)  # неудачных попыток подряд
    logins: Mapped[str | None] = mapped_column(String(255))  # какие логины перебирали
    banned: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    first_fail_at: Mapped[dt.datetime | None] = mapped_column(TS)
    last_fail_at: Mapped[dt.datetime | None] = mapped_column(TS)
    banned_at: Mapped[dt.datetime | None] = mapped_column(TS)
    unbanned_by: Mapped[int | None] = mapped_column(ForeignKey("admins.id", ondelete="SET NULL"))


class Setting(Base):
    """Тексты и переключатели, которые правятся из админки без деплоя."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[dt.datetime | None] = mapped_column(TS, onupdate=func.now())
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("admins.id", ondelete="SET NULL"))


__all__ = [
    "Admin",
    "AdminIpBan",
    "BalanceTx",
    "Broadcast",
    "BroadcastDelivery",
    "CountryOffer",
    "EventLog",
    "MarketStat",
    "Order",
    "Payment",
    "PromoCode",
    "PromoRedemption",
    "ReferralEarning",
    "Review",
    "Setting",
    "Ticket",
    "TicketMessage",
    "User",
    # реэкспорт для удобства импорта в сервисах
    "OrderStatus",
    "PaymentProvider",
    "PaymentStatus",
    "TxKind",
    "LogSection",
    "LogLevel",
]
