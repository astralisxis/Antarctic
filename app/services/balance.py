"""Баланс пользователя. Единственное место, где меняются деньги.

Правило проекта: баланс всегда равен сумме amount в balance_tx. Поэтому поле
`users.balance` и книга операций пишутся здесь вместе, одной транзакцией, и
никто больше не трогает `user.balance` напрямую.

Списание идёт условным UPDATE с проверкой остатка в самом запросе:
    UPDATE users SET balance = balance - :amount WHERE id = :id AND balance >= :amount
База сама решает, кто первый. Если бы мы читали баланс в Python, а потом писали,
два одновременных нажатия «Купить» списали бы дважды с одного остатка.
"""

from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import LogSection, TxKind
from app.models import BalanceTx, User
from app.money import fmt_money
from app.services.events import log_event

log = logging.getLogger("balance")


class NotEnoughMoney(Exception):
    """На балансе меньше, чем нужно списать."""

    def __init__(self, need: int, have: int) -> None:
        self.need = need
        self.have = have
        super().__init__(f"нужно {fmt_money(need)}, на балансе {fmt_money(have)}")


async def debit(
    session: AsyncSession,
    user: User,
    amount: int,
    kind: TxKind | str = TxKind.PURCHASE,
    *,
    order_id: int | None = None,
    admin_id: int | None = None,
    comment: str | None = None,
) -> int:
    """Списать. Возвращает баланс после операции, поднимает NotEnoughMoney при нехватке."""
    if amount <= 0:
        raise ValueError("списание должно быть положительным")

    kind_v = str(getattr(kind, "value", kind))
    values: dict = {"balance": User.balance - amount}
    # «Потрачено» — это про покупки. Ручная правка баланса из админки в эту
    # сумму попадать не должна, иначе статистика по клиенту начинает врать.
    if kind_v == TxKind.PURCHASE.value:
        values["total_spent"] = User.total_spent + amount

    result = await session.execute(
        update(User)
        .where(User.id == user.id, User.balance >= amount)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        # Читаем фактический остаток: сообщение клиенту должно быть точным.
        await session.refresh(user, ["balance"])
        raise NotEnoughMoney(amount, user.balance)

    await session.refresh(user, ["balance", "total_spent"])
    session.add(
        BalanceTx(
            user_id=user.id,
            kind=kind_v,
            amount=-amount,
            balance_after=user.balance,
            order_id=order_id,
            admin_id=admin_id,
            comment=comment,
        )
    )
    await session.flush()
    await log_event(
        LogSection.BALANCE,
        "debit",
        user_id=user.id,
        order_id=order_id,
        admin_id=admin_id,
        message=f"−{fmt_money(amount)} → {fmt_money(user.balance)}"
        + (f" ({comment})" if comment else ""),
        payload={"amount": -amount, "kind": kind_v},
        session=session,
    )
    return user.balance


async def credit(
    session: AsyncSession,
    user: User,
    amount: int,
    kind: TxKind | str = TxKind.TOPUP,
    *,
    order_id: int | None = None,
    payment_id: int | None = None,
    admin_id: int | None = None,
    comment: str | None = None,
) -> int:
    """Зачислить. Возвращает баланс после операции.

    Побочные счётчики ведём здесь же, чтобы статистика не зависела от того,
    вспомнил ли о них вызывающий:
      · пополнение   — total_topup и дата первой оплаты;
      · реферальные  — ref_earned;
      · возврат      — уменьшает total_spent, иначе «потрачено» врёт.
    """
    if amount <= 0:
        raise ValueError("зачисление должно быть положительным")

    kind_v = str(getattr(kind, "value", kind))
    values: dict = {"balance": User.balance + amount}
    if kind_v == TxKind.TOPUP.value:
        values["total_topup"] = User.total_topup + amount
    elif kind_v == TxKind.REFERRAL.value:
        values["ref_earned"] = User.ref_earned + amount
    elif kind_v == TxKind.REFUND.value:
        values["total_spent"] = User.total_spent - amount

    await session.execute(
        update(User)
        .where(User.id == user.id)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    await session.refresh(user, ["balance", "total_topup", "total_spent", "ref_earned"])

    if kind_v == TxKind.TOPUP.value and user.first_paid_at is None:
        user.first_paid_at = dt.datetime.now(dt.UTC)

    session.add(
        BalanceTx(
            user_id=user.id,
            kind=kind_v,
            amount=amount,
            balance_after=user.balance,
            order_id=order_id,
            payment_id=payment_id,
            admin_id=admin_id,
            comment=comment,
        )
    )
    await session.flush()
    await log_event(
        LogSection.BALANCE,
        "credit",
        user_id=user.id,
        order_id=order_id,
        admin_id=admin_id,
        message=f"+{fmt_money(amount)} → {fmt_money(user.balance)}"
        + (f" ({comment})" if comment else ""),
        payload={"amount": amount, "kind": kind_v},
        session=session,
    )
    return user.balance


async def adjust(
    session: AsyncSession,
    user: User,
    amount: int,
    *,
    admin_id: int | None = None,
    comment: str | None = None,
) -> int:
    """Ручная правка из админки: плюс или минус одним вызовом.

    В долг не уводим: списываем не больше остатка. Чтобы обрезание не выглядело
    молчаливым, админка сравнивает запрошенное с фактическим движением и пишет
    в сообщении, что списала до нуля.
    """
    if amount == 0:
        return user.balance
    if amount > 0:
        return await credit(
            session, user, amount, TxKind.ADMIN, admin_id=admin_id, comment=comment
        )
    take = min(-amount, user.balance)
    if take <= 0:
        raise NotEnoughMoney(-amount, user.balance)
    return await debit(session, user, take, TxKind.ADMIN, admin_id=admin_id, comment=comment)
