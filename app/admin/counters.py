"""Счётчики для меню: сколько ждёт внимания."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import PaymentStatus, ReviewStatus, TicketStatus
from app.models import Order, Payment, Review, Ticket
from app.services.orders import IN_FLIGHT, STUCK_AFTER


async def nav_counts(session: AsyncSession) -> dict[str, int]:
    async def count(model, *where) -> int:
        return int(await session.scalar(select(func.count()).select_from(model).where(*where)) or 0)

    return {
        "support": await count(Ticket, Ticket.status == TicketStatus.OPEN.value),
        "reviews": await count(Review, Review.status == ReviewStatus.PENDING.value),
        "payments": await count(Payment, Payment.status == PaymentStatus.PENDING.value),
        # В «Заказах» цифра значит «застряло», а не «всего»: висит слишком долго
        # в промежуточном статусе — значит покупка не доехала.
        "orders": await count(
            Order,
            Order.status.in_(IN_FLIGHT),
            Order.created_at < dt.datetime.now(dt.UTC) - STUCK_AFTER,
        ),
        "replacements": await count(
            Order,
            Order.replacement_requested_at.is_not(None),
            (Order.replacement_status == "pending") | Order.replacement_status.is_(None),
        ),
    }
