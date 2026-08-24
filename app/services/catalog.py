"""Каталог: поиск лота на LZT под позицию магазина и кэш наличия.

Витрину бот рисует из `stock_cached`, а не из живого поиска: экран открывается
чаще всего, и он не должен ждать маркет. Кэш обновляет фоновая задача бота
(stock_loop) и кнопка «Обновить наличие» в админке.

Цену покупателю задаёт админ (`CountryOffer.price`), лот подбирается сам в
пределах `buy_limit`. Если под лимит ничего не попало — позиция «нет в наличии».
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import session_scope
from app.enums import LogLevel, LogSection
from app.integrations.lzt import LztError, LztMarket, get_lzt
from app.models import CountryOffer
from app.money import fmt_money, rub_to_kop
from app.services.events import log_event

log = logging.getLogger("catalog")

# Как часто фоновая задача обновляет наличие. Чаще смысла нет: цены и лоты на
# маркете живут минутами, а лимиты запросов там общие на токен.
STOCK_INTERVAL = 180.0
# Пауза между странами внутри одного обхода — маркет не любит частых запросов.
STOCK_PAUSE = 0.7
# Сколько лотов берём в запас: дешёвые разбирают за секунды, и первый может уйти
# из-под нас между поиском и покупкой.
CANDIDATES = 3


@dataclass(frozen=True, slots=True)
class Lot:
    """Лот маркета в наших единицах: цена — копейки."""

    item_id: int
    price: int
    country: str | None = None
    origin: str | None = None
    phone: str | None = None
    raw: dict[str, Any] | None = None

    @property
    def price_rub(self) -> float:
        """Цена в рублях — в таком виде её ждёт fast-buy."""
        return self.price / 100


def pmax_rub(offer: CountryOffer) -> float:
    """Потолок закупки в рублях: в документации маркета pmax — float.

    Отдаём точное значение из админки, копейки не теряем. Настоящая защита
    лимита всё равно на нашей стороне (_search отбрасывает лоты дороже
    buy_limit) — маркет мог бы округлить границу в свою пользу.
    """
    return max(offer.buy_limit, 0) / 100


def search_kwargs(offer: CountryOffer) -> dict[str, Any]:
    """Параметры поиска для позиции. Отдельной функцией — их же показываем в админке."""
    return {
        "countries": [offer.lzt_country],
        "price_max": pmax_rub(offer),
        "spam": offer.spam_filter or None,
        "password": offer.password_filter or None,
        "origin": list(offer.origin_filter) if offer.origin_filter else None,
        "order_by": "price_to_up",
        "extra": dict(offer.extra_filters) if offer.extra_filters else None,
    }


def _to_lot(item: dict[str, Any]) -> Lot | None:
    item_id = item.get("item_id") or item.get("id")
    price = item.get("price")
    if not item_id or price is None:
        return None
    return Lot(
        item_id=int(item_id),
        price=rub_to_kop(price),
        country=item.get("telegram_country") or item.get("country"),
        origin=item.get("item_origin"),
        phone=item.get("telegram_phone"),
        raw=item,
    )


async def _search(offer: CountryOffer, *, lzt: LztMarket | None = None) -> tuple[list[Lot], int]:
    """Один запрос к маркету: (лоты в пределах лимита, всего по фильтру)."""
    client = lzt or get_lzt()
    data = await client.accounts_search_telegram(**search_kwargs(offer))
    items = data.get("items") or []
    lots = [lot for lot in (_to_lot(i) for i in items) if lot and lot.price <= offer.buy_limit]
    total = data.get("totalItems")
    # totalItems маркет отдаёт не всегда; тогда честнее показать то, что видим.
    return lots, int(total) if isinstance(total, int) else len(lots)


async def find_lots(
    offer: CountryOffer, *, limit: int = CANDIDATES, lzt: LztMarket | None = None
) -> list[Lot]:
    """Кандидаты на покупку, от дешёвых к дорогим."""
    lots, _ = await _search(offer, lzt=lzt)
    return lots[:limit]


async def refresh_stock(
    session: AsyncSession, offer: CountryOffer, *, lzt: LztMarket | None = None
) -> int | None:
    """Обновить кэш наличия одной позиции. None — маркет не ответил.

    При ошибке кэш не обнуляем: «нет в наличии» из-за оборванного запроса —
    это ложь витрине. Оставляем прошлое значение и пишем предупреждение.
    """
    try:
        lots, total = await _search(offer, lzt=lzt)
    except LztError as exc:
        log.warning("наличие %s не обновилось: %s", offer.code, exc)
        await log_event(
            LogSection.LZT,
            "stock_failed",
            level=LogLevel.WARN,
            message=f"{offer.code}: {exc}",
        )
        return None

    offer.stock_cached = total if total else len(lots)
    offer.stock_checked_at = dt.datetime.now(dt.UTC)
    await session.flush()
    return offer.stock_cached


async def refresh_all(session: AsyncSession, *, lzt: LztMarket | None = None) -> dict[str, int | None]:
    """Обойти активные позиции. Возвращает {код страны: наличие}."""
    offers = list(
        (
            await session.scalars(
                select(CountryOffer)
                .where(CountryOffer.is_active.is_(True))
                .order_by(CountryOffer.sort, CountryOffer.title)
            )
        ).all()
    )
    out: dict[str, int | None] = {}
    for index, offer in enumerate(offers, 1):
        out[offer.code] = await refresh_stock(session, offer, lzt=lzt)
        if index < len(offers):
            await asyncio.sleep(STOCK_PAUSE)
    return out


async def refresh_each(*, lzt: LztMarket | None = None) -> dict[str, int | None]:
    """То же, но своей короткой сессией на позицию. Для фоновой задачи.

    Писать в sqlite может только один, а между запросами к маркету стоит пауза:
    одна транзакция на весь обход держала бы базу закрытой дольше busy_timeout, и
    админка с ботом валились бы с «database is locked».
    """
    async with session_scope() as session:
        ids = list(
            (
                await session.scalars(
                    select(CountryOffer.id)
                    .where(CountryOffer.is_active.is_(True))
                    .order_by(CountryOffer.sort, CountryOffer.title)
                )
            ).all()
        )

    out: dict[str, int | None] = {}
    for index, offer_id in enumerate(ids, 1):
        async with session_scope() as session:
            offer = await session.get(CountryOffer, offer_id)
            if offer is not None:  # позицию могли удалить, пока шёл обход
                out[offer.code] = await refresh_stock(session, offer, lzt=lzt)
        if index < len(ids):
            await asyncio.sleep(STOCK_PAUSE)
    return out


async def stock_loop(interval: float = STOCK_INTERVAL) -> None:
    """Фоновая задача бота. Падать не имеет права: витрина живёт с этого кэша."""
    if not settings.lzt_token:
        log.warning("LZT_TOKEN не задан — наличие обновляться не будет")
        return
    log.info("обновление наличия каждые %.0f с", interval)
    while True:
        try:
            result = await refresh_each()
            if result:
                log.info(
                    "наличие: %s",
                    ", ".join(f"{code}={value if value is not None else '?'}" for code, value in result.items()),
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("обход наличия сорвался, повторим через %.0f с", interval)
        await asyncio.sleep(interval)


def stock_label(offer: CountryOffer) -> str:
    """Подпись наличия для витрины и админки."""
    if not offer.is_active:
        return "скрыт"
    if offer.stock_cached is None:
        return "наличие не проверено"
    if offer.stock_cached <= 0:
        return "нет в наличии"
    return f"в наличии: {offer.stock_cached}"


def offer_label(offer: CountryOffer) -> str:
    """Строка кнопки витрины: страна, цена и наличие одним взглядом."""
    if offer.stock_cached is not None and offer.stock_cached <= 0:
        return f"{offer.title} · нет в наличии"
    return f"{offer.title} · {fmt_money(offer.price)}"
