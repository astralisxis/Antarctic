"""Сводка для админки: кто зашёл, кто оплатил, что с заказами."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import OrderStatus, PaymentStatus, ReviewStatus, TicketStatus
from app.models import EventLog, Order, Payment, Review, Ticket, User
from app.timeutil import day_start

# Заказы, которые считаем состоявшимися (деньги отработали).
PAID_ORDER_STATUSES = (
    OrderStatus.PURCHASED.value,
    OrderStatus.CODE_ISSUED.value,
    OrderStatus.DONE.value,
)


@dataclass(slots=True)
class Dashboard:
    users_total: int = 0
    users_today: int = 0
    users_week: int = 0
    users_paid: int = 0
    users_bought: int = 0
    users_banned: int = 0

    orders_total: int = 0
    orders_today: int = 0
    orders_paid: int = 0
    orders_failed: int = 0
    orders_by_status: dict[str, int] = field(default_factory=dict)

    revenue_total: int = 0
    revenue_today: int = 0
    cost_total: int = 0
    avg_check: int = 0

    topup_total: int = 0
    topup_today: int = 0
    payments_pending: int = 0

    balance_hold: int = 0

    tickets_open: int = 0
    reviews_pending: int = 0

    recent: list[EventLog] = field(default_factory=list)
    errors_24h: int = 0

    @property
    def margin_total(self) -> int:
        return self.revenue_total - self.cost_total

    @property
    def conv_to_paid(self) -> tuple[int, int]:
        return self.users_paid, self.users_total

    @property
    def conv_to_order(self) -> tuple[int, int]:
        return self.users_bought, self.users_total


async def _count(session: AsyncSession, stmt) -> int:
    return int(await session.scalar(stmt) or 0)


async def collect(session: AsyncSession) -> Dashboard:
    d = Dashboard()
    today = day_start()
    week = day_start(7)

    d.users_total = await _count(session, select(func.count()).select_from(User))
    d.users_today = await _count(
        session, select(func.count()).select_from(User).where(User.created_at >= today)
    )
    d.users_week = await _count(
        session, select(func.count()).select_from(User).where(User.created_at >= week)
    )
    d.users_paid = await _count(
        session, select(func.count()).select_from(User).where(User.total_topup > 0)
    )
    d.users_bought = await _count(
        session, select(func.count()).select_from(User).where(User.orders_count > 0)
    )
    d.users_banned = await _count(
        session, select(func.count()).select_from(User).where(User.is_banned.is_(True))
    )
    d.balance_hold = await _count(session, select(func.coalesce(func.sum(User.balance), 0)))

    by_status = await session.execute(
        select(Order.status, func.count()).group_by(Order.status)
    )
    d.orders_by_status = {row[0]: row[1] for row in by_status}
    d.orders_total = sum(d.orders_by_status.values())
    d.orders_paid = sum(d.orders_by_status.get(s, 0) for s in PAID_ORDER_STATUSES)
    d.orders_failed = d.orders_by_status.get(OrderStatus.FAILED.value, 0)
    d.orders_today = await _count(
        session, select(func.count()).select_from(Order).where(Order.created_at >= today)
    )

    paid_orders = Order.status.in_(PAID_ORDER_STATUSES)
    d.revenue_total = await _count(
        session, select(func.coalesce(func.sum(Order.price), 0)).where(paid_orders)
    )
    d.revenue_today = await _count(
        session,
        select(func.coalesce(func.sum(Order.price), 0)).where(
            paid_orders, Order.created_at >= today
        ),
    )
    d.cost_total = await _count(
        session, select(func.coalesce(func.sum(Order.lzt_cost), 0)).where(paid_orders)
    )
    d.avg_check = int(d.revenue_total / d.orders_paid) if d.orders_paid else 0

    paid_payments = Payment.status == PaymentStatus.PAID.value
    d.topup_total = await _count(
        session, select(func.coalesce(func.sum(Payment.credited), 0)).where(paid_payments)
    )
    d.topup_today = await _count(
        session,
        select(func.coalesce(func.sum(Payment.credited), 0)).where(
            paid_payments, Payment.paid_at >= today
        ),
    )
    d.payments_pending = await _count(
        session,
        select(func.count())
        .select_from(Payment)
        .where(Payment.status == PaymentStatus.PENDING.value),
    )

    d.tickets_open = await _count(
        session,
        select(func.count()).select_from(Ticket).where(Ticket.status == TicketStatus.OPEN.value),
    )
    d.reviews_pending = await _count(
        session,
        select(func.count()).select_from(Review).where(Review.status == ReviewStatus.PENDING.value),
    )

    d.errors_24h = await _count(
        session,
        select(func.count())
        .select_from(EventLog)
        .where(EventLog.level == "error", EventLog.ts >= dt.datetime.now(dt.UTC) - dt.timedelta(days=1)),
    )
    d.recent = list(
        (await session.scalars(select(EventLog).order_by(EventLog.id.desc()).limit(12))).all()
    )
    return d
