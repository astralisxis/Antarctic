"""Рейтинг покупателей без отдельного кэша или фонового пересчёта."""

from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User


async def top(session: AsyncSession, *, limit: int = 20) -> list[User]:
    return list(
        (
            await session.scalars(
                select(User)
                .where(User.orders_count > 0)
                .order_by(User.orders_count.desc(), User.total_spent.desc(), User.id.asc())
                .limit(limit)
            )
        ).all()
    )


async def position(session: AsyncSession, user: User) -> int | None:
    if user.orders_count <= 0:
        return None
    ahead = int(
        await session.scalar(
            select(func.count()).select_from(User).where(
                or_(
                    User.orders_count > user.orders_count,
                    (User.orders_count == user.orders_count) & (User.total_spent > user.total_spent),
                    (User.orders_count == user.orders_count)
                    & (User.total_spent == user.total_spent)
                    & (User.id < user.id),
                )
            )
        )
        or 0
    )
    return ahead + 1
