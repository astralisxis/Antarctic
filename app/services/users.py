"""Пользователи магазина: регистрация, реферальная привязка, ограничения.

Без импортов aiogram: этими же функциями будет пользоваться мини-апп.
"""

from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.enums import LogSection
from app.models import User
from app.services import settings_store
from app.services.events import log_event

log = logging.getLogger("users")

REF_PREFIX = "r"
# Как часто обновляем «был в сети»: чаще — лишние записи на каждое нажатие.
SEEN_INTERVAL = dt.timedelta(minutes=1)


def ref_payload(user: User) -> str:
    """Полезная нагрузка ссылки. Внутренний id, а не tg_id — его не светим."""
    return f"{REF_PREFIX}{user.id}"


def ref_link(user: User) -> str:
    return f"{settings.bot_link}?start={ref_payload(user)}"


def parse_ref_payload(payload: str | None) -> int | None:
    if not payload:
        return None
    raw = payload.strip()
    if raw.startswith(REF_PREFIX):
        raw = raw[len(REF_PREFIX) :]
    return int(raw) if raw.isdigit() and len(raw) < 12 else None


async def percent_for(session: AsyncSession, user: User) -> int:
    """Процент реферала: персональный, иначе общий из настроек."""
    if user.ref_percent is not None:
        return user.ref_percent
    return await settings_store.get_int(session, "referral.percent", 10)


def ban_until_passed(user: User) -> bool:
    """Срок бана истёк (постоянный бан — banned_until пустой, не истекает)."""
    if not user.is_banned or user.banned_until is None:
        return False
    until = user.banned_until
    if until.tzinfo is None:  # sqlite отдаёт наивное время, писали в UTC
        until = until.replace(tzinfo=dt.UTC)
    return until <= dt.datetime.now(dt.UTC)


async def unban_if_expired(session: AsyncSession, user: User) -> bool:
    if not ban_until_passed(user):
        return False
    user.is_banned = False
    user.ban_reason = None
    user.banned_until = None
    await session.flush()
    await log_event(
        LogSection.USER, "ban_expired", user_id=user.id, message="срок ограничения истёк",
        session=session,
    )
    return True


async def touch(
    session: AsyncSession,
    *,
    tg_id: int,
    username: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    language_code: str | None = None,
    is_premium: bool = False,
    payload: str | None = None,
) -> tuple[User, bool]:
    """Найти или создать пользователя, обновить профиль. Возвращает (user, создан ли).

    Реферал привязывается только при создании: иначе ссылку можно переиграть
    и переписать чужой приход на себя.

    Апдейты aiogram обрабатывает параллельно, поэтому от нового клиента легко
    приходят два сразу — оба видят «пользователя нет» и оба лезут вставлять.
    Вставку держим в savepoint: проигравший откатывает только её и читает
    запись победителя, вместо того чтобы уронить весь апдейт.
    """
    user = await session.scalar(select(User).where(User.tg_id == tg_id))

    if user is None:
        fresh = User(
            tg_id=tg_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
            language_code=language_code,
            is_tg_premium=is_premium,
            start_payload=(payload or None) and payload[:64],
            last_seen_at=dt.datetime.now(dt.UTC),
        )
        try:
            async with session.begin_nested():
                session.add(fresh)
                await session.flush()  # нужен user.id для лога и привязки
        except IntegrityError:
            log.info("гонка на регистрации tg=%s — берём уже созданную запись", tg_id)
            user = await session.scalar(select(User).where(User.tg_id == tg_id))
            if user is None:  # ни вставить, ни найти — это уже не гонка
                raise
        else:
            await log_event(
                LogSection.USER,
                "start_new",
                user_id=fresh.id,
                message=f"новый клиент {fresh.display_name}",
                payload={"tg_id": tg_id, "payload": payload} if payload else {"tg_id": tg_id},
                session=session,
            )
            await _bind_referrer(session, fresh, payload)
            return fresh, True

    # профиль в телеграме мог поменяться
    changed = False
    # Username и фамилия могут быть удалены в Telegram, поэтому None здесь —
    # тоже новое значение, а не повод оставить устаревшие данные.
    for field, value in (
        ("username", username),
        ("first_name", first_name),
        ("last_name", last_name),
        ("language_code", language_code),
    ):
        if field in {"username", "last_name"} and getattr(user, field) != value:
            setattr(user, field, value)
            changed = True
        elif value is not None and getattr(user, field) != value:
            setattr(user, field, value)
            changed = True
    if username:
        # Telegram username уникален, но после смены мог остаться у старой
        # записи в нашей базе. Иначе подарок по @username можно отдать не тому.
        await session.execute(
            update(User)
            .where(User.id != user.id, func.lower(User.username) == username.lower())
            .values(username=None)
        )
    if user.is_tg_premium != is_premium:
        user.is_tg_premium = is_premium
        changed = True

    now = dt.datetime.now(dt.UTC)
    last = user.last_seen_at
    if last is not None and last.tzinfo is None:
        last = last.replace(tzinfo=dt.UTC)
    if last is None or now - last > SEEN_INTERVAL:
        user.last_seen_at = now
        changed = True
    if changed:
        await session.flush()
    return user, False


async def find_recipient(session: AsyncSession, value: str, owner: User) -> User | None:
    """Найти зарегистрированного в боте получателя по Telegram ID или username."""
    raw = (value or "").strip()
    if raw.startswith("@"):
        raw = raw[1:].strip()
    if raw.isdigit() and len(raw) <= 20:
        user = await session.scalar(select(User).where(User.tg_id == int(raw)))
    else:
        username = raw.lower()
        if not username or len(username) > 64:
            return None
        matches = list(
            (
                await session.scalars(
                    select(User)
                    .where(func.lower(User.username) == username)
                    .order_by(User.last_seen_at.desc(), User.id.desc())
                    .limit(2)
                )
            ).all()
        )
        # При старых дубликатах безопаснее попросить Telegram ID, чем угадать.
        user = matches[0] if len(matches) == 1 else None
    if user is None or user.id == owner.id or user.is_banned:
        return None
    return user


async def _bind_referrer(session: AsyncSession, user: User, payload: str | None) -> None:
    referrer_id = parse_ref_payload(payload)
    if referrer_id is None or referrer_id == user.id:
        return
    referrer = await session.get(User, referrer_id)
    if referrer is None or referrer.is_banned:
        log.info("реферальная ссылка %s не привязана: пригласивший недоступен", payload)
        return

    user.referrer_id = referrer.id
    await session.execute(
        update(User)
        .where(User.id == referrer.id)
        .values(referrals_count=User.referrals_count + 1)
    )
    await log_event(
        LogSection.REFERRAL,
        "bound",
        user_id=user.id,
        message=f"пришёл по ссылке {referrer.display_name}",
        payload={"referrer_id": referrer.id},
        session=session,
    )


async def referrals_count(session: AsyncSession, user: User) -> int:
    """Считаем запросом, а не полем: поле — кэш, а тут нужна правда."""
    return await session.scalar(
        select(func.count()).select_from(User).where(User.referrer_id == user.id)
    ) or 0
