"""Обнуление данных: убрать следы тестов, оставив настройки магазина.

Стирается только то, что накопилось в работе — заказы, платежи, отзывы,
обращения, рассылки, журнал. Каталог, настройки, админы и список забаненных
адресов не трогаются никогда: после обнуления магазин должен остаться готовым к
работе, а не собираться заново.

Группы связаны: клиентов нельзя удалить, пока на них ссылаются заказы, поэтому у
каждой группы перечислено, что уедет вместе с ней (`needs`). Страница
подтверждения показывает это до нажатия, а не после.

Идентификаторы после обнуления начнутся с единицы: в sqlite ключ — это rowid, и
пустая таблица снова считает с нуля. Для тестов это удобно, но старые ссылки
«заказ №7» после обнуления будут указывать на другой заказ.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import BASE_DIR, DATA_DIR, MEDIA_DIR
from app.enums import LogLevel, LogSection
from app.models import (
    BalanceTx,
    Broadcast,
    BroadcastDelivery,
    CountryOffer,
    EventLog,
    MarketStat,
    Order,
    Payment,
    ReferralEarning,
    Review,
    Ticket,
    TicketMessage,
    User,
)
from app.services.events import log_event

log = logging.getLogger("maintenance")

CARDS_DIR = DATA_DIR / "reviews"


@dataclass(frozen=True, slots=True)
class Group:
    key: str
    title: str
    hint: str
    # Что нельзя оставить, если стираем эту группу: на неё ссылаются.
    needs: tuple[str, ...] = ()


GROUPS: tuple[Group, ...] = (
    Group("orders", "Заказы", "Покупки, выданные номера и коды входа"),
    Group(
        "payments",
        "Платежи и книга операций",
        "Счёта провайдеров, движения по балансу, реферальные начисления",
    ),
    Group("reviews", "Отзывы", "Записи и нарисованные карточки в data/reviews"),
    Group("support", "Обращения", "Тикеты вместе с перепиской"),
    Group("broadcasts", "Рассылки", "Черновики, ход отправки и загруженные картинки"),
    Group("logs", "Журнал событий", "Всё, что видно в разделе «Логи»"),
    Group("market", "Срезы маркета", "Цифры по странам и запомненное наличие в каталоге"),
    Group(
        "counters",
        "Счётчики и балансы клиентов",
        "Балансы, суммы пополнений и покупок обнулятся, сами клиенты останутся",
    ),
    Group(
        "users",
        "Клиентов целиком",
        "База клиентов очистится — вместе с их заказами, платежами, отзывами и обращениями",
        needs=("orders", "payments", "reviews", "support", "broadcasts"),
    ),
)

BY_KEY: dict[str, Group] = {g.key: g for g in GROUPS}

# Порядок стирания: сначала то, что ссылается, потом то, на что ссылаются.
ORDER: tuple[str, ...] = (
    "support",
    "broadcasts",
    "reviews",
    "payments",
    "orders",
    "logs",
    "market",
    "counters",
    "users",
)


def expand(keys: list[str]) -> list[str]:
    """Добавить к выбору то, без чего он не выполнится. Порядок — как в ORDER."""
    picked = {k for k in keys if k in BY_KEY}
    for key in list(picked):
        picked.update(BY_KEY[key].needs)
    return [k for k in ORDER if k in picked]


# --------------------------------------------------------------------------- #
#  Сколько чего лежит
# --------------------------------------------------------------------------- #
async def counts(session: AsyncSession) -> dict[str, int]:
    """Строки по группам — их показывает страница подтверждения."""

    async def rows(model, *where) -> int:
        return int(await session.scalar(select(func.count()).select_from(model).where(*where)) or 0)

    return {
        "orders": await rows(Order),
        "payments": await rows(Payment) + await rows(BalanceTx) + await rows(ReferralEarning),
        "reviews": await rows(Review),
        "support": await rows(Ticket),
        "broadcasts": await rows(Broadcast),
        "logs": await rows(EventLog),
        "market": await rows(MarketStat),
        # Клиент «с накопленным» — у кого хоть один счётчик не нулевой.
        "counters": await rows(
            User,
            or_(
                User.balance != 0,
                User.total_topup != 0,
                User.total_spent != 0,
                User.ref_earned != 0,
                User.orders_count != 0,
            ),
        ),
        "users": await rows(User),
    }


# --------------------------------------------------------------------------- #
#  Файлы
# --------------------------------------------------------------------------- #
def _inside(path: Path) -> bool:
    """Удаляем только внутри data/ и media/: в базе может лежать что угодно."""
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return any(resolved.is_relative_to(root.resolve()) for root in (DATA_DIR, MEDIA_DIR))


def _drop_files(values: list[str | None]) -> int:
    dropped = 0
    for raw in values:
        value = (raw or "").strip()
        if not value:
            continue
        path = Path(value)
        if not path.is_absolute():
            path = BASE_DIR / path
        if not _inside(path) or not path.exists():
            continue
        try:
            path.unlink()
            dropped += 1
        except OSError as exc:
            log.warning("файл %s не удалился: %s", path.name, exc)
    return dropped


# --------------------------------------------------------------------------- #
#  Стирание по группам
# --------------------------------------------------------------------------- #
async def _wipe(session: AsyncSession, model) -> int:
    result = await session.execute(
        delete(model).execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0)


async def _null(session: AsyncSession, model, **values) -> None:
    await session.execute(
        update(model).values(**values).execution_options(synchronize_session=False)
    )


async def _support(session: AsyncSession, picked: list[str]) -> int:
    await _wipe(session, TicketMessage)
    return await _wipe(session, Ticket)


async def _broadcasts(session: AsyncSession, picked: list[str]) -> int:
    images = list((await session.scalars(select(Broadcast.image_path))).all())
    await _wipe(session, BroadcastDelivery)
    removed = await _wipe(session, Broadcast)
    _drop_files(images)
    return removed


async def _reviews(session: AsyncSession, picked: list[str]) -> int:
    images = list((await session.scalars(select(Review.image_path))).all())
    removed = await _wipe(session, Review)
    _drop_files(images)
    # Карточки от удалённых раньше отзывов тоже лишние — папка должна быть пустой.
    if CARDS_DIR.exists():
        _drop_files([str(p) for p in CARDS_DIR.glob("review_*.png")])
    return removed


async def _payments(session: AsyncSession, picked: list[str]) -> int:
    await _wipe(session, ReferralEarning)
    await _wipe(session, BalanceTx)
    return await _wipe(session, Payment)


async def _orders(session: AsyncSession, picked: list[str]) -> int:
    # Внешние ключи в sqlite выключены (см. app/db.py), «ON DELETE SET NULL» сам
    # не сработает — ссылки обнуляем руками.
    if "payments" not in picked:
        await _null(session, BalanceTx, order_id=None)
    if "reviews" not in picked:
        await _null(session, Review, order_id=None)
    if "support" not in picked:
        await _null(session, Ticket, order_id=None)
    if "logs" not in picked:
        await _null(session, EventLog, order_id=None)
    return await _wipe(session, Order)


async def _logs(session: AsyncSession, picked: list[str]) -> int:
    return await _wipe(session, EventLog)


async def _market(session: AsyncSession, picked: list[str]) -> int:
    removed = await _wipe(session, MarketStat)
    # Запомненное наличие без срезов ни о чём не говорит — проверится обходом.
    await _null(session, CountryOffer, stock_cached=None, stock_checked_at=None)
    return removed


async def _counters(session: AsyncSession, picked: list[str]) -> int:
    result = await session.execute(
        update(User)
        .values(
            balance=0,
            total_topup=0,
            total_spent=0,
            ref_earned=0,
            orders_count=0,
            first_paid_at=None,
            first_order_at=None,
        )
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0)


async def _users(session: AsyncSession, picked: list[str]) -> int:
    if "logs" not in picked:
        await _null(session, EventLog, user_id=None)
    # Приглашённые ссылаются на пригласившего — рвём связь, иначе удаление
    # упрётся в самих себя при включённых внешних ключах.
    await _null(session, User, referrer_id=None, referrals_count=0)
    return await _wipe(session, User)


WIPERS = {
    "support": _support,
    "broadcasts": _broadcasts,
    "reviews": _reviews,
    "payments": _payments,
    "orders": _orders,
    "logs": _logs,
    "market": _market,
    "counters": _counters,
    "users": _users,
}


async def purge(
    session: AsyncSession, keys: list[str], *, admin_id: int | None = None
) -> dict[str, int]:
    """Стереть выбранные группы. Вернёт, сколько строк убрано в каждой."""
    picked = expand(keys)
    done: dict[str, int] = {}
    for key in picked:
        done[key] = await WIPERS[key](session, picked)

    # Журнал пишем после стирания: иначе запись уедет вместе с группой «Логи».
    await log_event(
        LogSection.ADMIN,
        "data_purged",
        level=LogLevel.WARN,
        admin_id=admin_id,
        message=", ".join(f"{BY_KEY[k].title.lower()}: {v}" for k, v in done.items()) or "ничего",
        payload={"groups": picked, "removed": done},
        session=session,
    )
    await session.commit()
    return done
