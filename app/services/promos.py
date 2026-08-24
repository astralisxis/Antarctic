"""Промокоды: проверка, атомарная активация и списки для интерфейсов."""

from __future__ import annotations

import datetime as dt
import re

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import LogSection, TxKind
from app.models import PromoCode, PromoRedemption, User
from app.money import fmt_money
from app.services import balance
from app.services.events import log_event

CODE_RE = re.compile(r"^[A-Z0-9_-]{3,32}$")


class PromoError(Exception):
    pass


def normalize(raw: str) -> str:
    return re.sub(r"\s+", "", raw or "").upper()


def available(promo: PromoCode, *, now: dt.datetime | None = None) -> bool:
    now = now or dt.datetime.now(dt.UTC)
    expires = promo.expires_at
    if expires is not None and expires.tzinfo is None:
        expires = expires.replace(tzinfo=dt.UTC)
    return bool(
        promo.is_active
        and (expires is None or expires > now)
        and (promo.max_uses is None or promo.uses_count < promo.max_uses)
    )


async def redeem(session: AsyncSession, user: User, raw_code: str) -> PromoCode:
    code = normalize(raw_code)
    if not CODE_RE.fullmatch(code):
        raise PromoError("Проверьте код: от 3 до 32 латинских букв, цифр, _ или -.")

    promo = await session.scalar(select(PromoCode).where(PromoCode.code == code))
    if promo is None or not available(promo):
        raise PromoError("Промокод не найден или срок его действия закончился.")
    used = await session.scalar(
        select(PromoRedemption.id).where(
            PromoRedemption.promo_id == promo.id,
            PromoRedemption.user_id == user.id,
        )
    )
    if used:
        raise PromoError("Вы уже активировали этот промокод.")

    where = [PromoCode.id == promo.id, PromoCode.is_active.is_(True)]
    if promo.max_uses is not None:
        where.append(PromoCode.uses_count < promo.max_uses)
    if promo.expires_at is not None:
        where.append(PromoCode.expires_at > dt.datetime.now(dt.UTC))
    result = await session.execute(
        update(PromoCode)
        .where(*where)
        .values(uses_count=PromoCode.uses_count + 1)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise PromoError("Лимит активаций этого промокода уже закончился.")

    redemption = PromoRedemption(promo_id=promo.id, user_id=user.id, bonus=promo.bonus)
    session.add(redemption)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise PromoError("Вы уже активировали этот промокод.") from exc

    await balance.credit(
        session,
        user,
        promo.bonus,
        TxKind.PROMO,
        comment=f"Промокод {promo.code}",
    )
    await session.refresh(promo)
    await log_event(
        LogSection.BALANCE,
        "promo_redeemed",
        user_id=user.id,
        message=f"{promo.code}: +{fmt_money(promo.bonus)}",
        payload={"promo_id": promo.id, "bonus": promo.bonus},
        session=session,
    )
    await session.commit()
    return promo


async def redemptions_for(session: AsyncSession, user: User, *, limit: int = 20):
    return list(
        (
            await session.execute(
                select(PromoRedemption, PromoCode)
                .join(PromoCode, PromoCode.id == PromoRedemption.promo_id)
                .where(PromoRedemption.user_id == user.id)
                .order_by(PromoRedemption.id.desc())
                .limit(limit)
            )
        ).all()
    )


async def stats(session: AsyncSession) -> tuple[int, int]:
    count = int(await session.scalar(select(func.count()).select_from(PromoCode)) or 0)
    active = int(
        await session.scalar(
            select(func.count()).select_from(PromoCode).where(PromoCode.is_active.is_(True))
        )
        or 0
    )
    return count, active
