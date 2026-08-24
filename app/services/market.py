"""Срез маркета по странам: сколько лотов и по какой цене.

Зачем: перечня стран у LZT нет (GET /telegram/params его не отдаёт), наличие
узнаётся только поиском. Чтобы страну в каталог выбирали по цифрам, а не наугад,
здесь обход кандидатов из app/countries.py с записью результата в market_stats.

Что считается средней. Маркет отдаёт лоты страницами, отсортированные по цене
от дешёвых. Мы берём первую страницу (SAMPLE_LIMIT лотов) и считаем по ней
минимум, среднее и максимум. Это средняя по самым дешёвым лотам, а не по всему
маркету: чтобы получить настоящую, пришлось бы вытянуть все страницы по каждой
стране. В админке подписано именно так, и рядом показано, сколько лотов попало
в выборку из общего числа.

Обход всех стран — это по одному запросу на страну с паузой, около минуты.
Поэтому он живёт фоновой задачей, а страница показывает ход и последний
результат из базы.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.countries import COUNTRIES, name_for
from app.db import session_scope
from app.enums import LogLevel, LogSection, YesNo
from app.integrations.lzt import LztError, LztMarket, get_lzt
from app.models import CountryOffer, MarketStat
from app.money import rub_input, rub_to_kop
from app.services.events import log_event

log = logging.getLogger("market")

# Сколько лотов берём в выборку: первая страница маркета и есть выборка.
SAMPLE_LIMIT = 20
# Пауза между странами — та же причина, что и в наличии: маркет не любит частых
# запросов, а обход тут длинный.
SCAN_PAUSE = 0.7


# --------------------------------------------------------------------------- #
#  Один запрос
# --------------------------------------------------------------------------- #
def _prices(items: list[dict[str, Any]]) -> list[int]:
    """Цены лотов в копейках. Лот без цены пропускаем, а не считаем нулём."""
    out: list[int] = []
    for item in items[:SAMPLE_LIMIT]:
        price = item.get("price")
        if price is None:
            continue
        try:
            out.append(rub_to_kop(price))
        except (TypeError, ValueError):
            continue
    return out


async def _row(session: AsyncSession, code: str) -> MarketStat:
    row = await session.get(MarketStat, code)
    if row is None:
        row = MarketStat(code=code)
        session.add(row)
    return row


async def check(
    session: AsyncSession,
    code: str,
    *,
    pmax: int | None = None,
    spam: str = YesNo.NO.value,
    lzt: LztMarket | None = None,
) -> MarketStat:
    """Спросить маркет про одну страну и записать срез. Ошибку тоже записываем."""
    code = code.strip().upper()
    row = await _row(session, code)
    row.pmax = pmax
    row.spam = spam
    row.checked_at = dt.datetime.now(dt.UTC)

    client = lzt or get_lzt()
    try:
        data = await client.accounts_search_telegram(
            countries=[code],
            price_max=(pmax / 100) if pmax else None,
            spam=spam,
            order_by="price_to_up",
        )
    except LztError as exc:
        # Прошлые цифры не стираем: пустая строка вместо «было 40 лотов» врёт.
        row.error = str(exc)[:255]
        await session.flush()
        return row

    items = [i for i in (data.get("items") or []) if isinstance(i, dict)]
    prices = _prices(items)
    total = data.get("totalItems")

    row.error = None
    row.lots = int(total) if isinstance(total, int) else len(items)
    row.sample = len(prices)
    row.price_min = min(prices) if prices else None
    row.price_max = max(prices) if prices else None
    row.price_avg = round(sum(prices) / len(prices)) if prices else None
    await session.flush()
    return row


# --------------------------------------------------------------------------- #
#  Обход
# --------------------------------------------------------------------------- #
@dataclass
class Scan:
    """Ход обхода. Живёт в памяти процесса: это не данные, а состояние кнопки."""

    total: int = 0
    done: int = 0
    code: str = ""
    pmax: int | None = None
    spam: str = YesNo.NO.value
    started_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None
    error: str = ""
    codes: list[str] = field(default_factory=list)

    @property
    def running(self) -> bool:
        return self.started_at is not None and self.finished_at is None

    @property
    def percent(self) -> int:
        return round(self.done * 100 / self.total) if self.total else 0

    @property
    def pmax_rub(self) -> str:
        """Потолок для поля формы: пусто, если его не задавали."""
        return rub_input(self.pmax) if self.pmax else ""


_scan = Scan()
_task: asyncio.Task[None] | None = None


def state() -> Scan:
    return _scan


def codes_all() -> list[str]:
    return list(COUNTRIES)


def start(codes: list[str], *, pmax: int | None = None, spam: str = YesNo.NO.value) -> bool:
    """Запустить обход фоном. False — уже идёт другой.

    Обход занимает около минуты на весь список, держать на нём http-запрос
    админки нельзя: браузер будет ждать молча и не поймёт, что происходит.
    """
    global _task, _scan
    if _scan.running:
        return False
    _scan = Scan(
        total=len(codes),
        pmax=pmax,
        spam=spam,
        started_at=dt.datetime.now(dt.UTC),
        codes=list(codes),
    )
    _task = asyncio.create_task(_run(codes, pmax, spam), name="market-scan")
    return True


async def _run(codes: list[str], pmax: int | None, spam: str) -> None:
    """Фоновая часть обхода: своя короткая сессия на страну, падать нельзя.

    Сессия именно на страну, а не на весь обход. Писать в sqlite может только
    один, а обход всех стран идёт около двух минут: одна транзакция на него
    держала бы базу закрытой всё это время, и бот с остальной админкой валились
    бы с «database is locked» — busy_timeout столько не ждёт. Короткая
    транзакция на страну занимает базу на миллисекунды.
    """
    try:
        for index, code in enumerate(codes, 1):
            _scan.code = code
            async with session_scope() as session:
                await check(session, code, pmax=pmax, spam=spam)
            _scan.done = index
            if index < len(codes):
                await asyncio.sleep(SCAN_PAUSE)
        await log_event(
            LogSection.LZT,
            "market_scan",
            message=f"стран: {len(codes)}, потолок: {pmax if pmax else 'без'}, спам: {spam}",
        )
    except asyncio.CancelledError:
        _scan.error = "обход прерван"
        raise
    except Exception as exc:  # noqa: BLE001 — состояние кнопки важнее типа ошибки
        log.exception("обход маркета сорвался")
        _scan.error = str(exc)[:255]
        await log_event(
            LogSection.LZT,
            "market_scan_failed",
            level=LogLevel.ERROR,
            message=str(exc)[:400],
        )
    finally:
        _scan.finished_at = dt.datetime.now(dt.UTC)


# --------------------------------------------------------------------------- #
#  Чтение
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class Row:
    """Строка таблицы стран: срез маркета плюс то, что уже есть в каталоге."""

    code: str
    name: str
    stat: MarketStat | None
    offer: CountryOffer | None

    @property
    def lots(self) -> int:
        return self.stat.lots if self.stat else 0

    @property
    def checked(self) -> bool:
        return self.stat is not None and self.stat.checked_at is not None

    @property
    def margin(self) -> int | None:
        """Разница между ценой витрины и самым дешёвым лотом — сразу видно смысл."""
        if self.offer is None or self.stat is None or self.stat.price_min is None:
            return None
        return self.offer.price - self.stat.price_min


async def rows(session: AsyncSession, *, only_lots: bool = False) -> list[Row]:
    """Таблица стран: сначала те, где лотов больше. Непроверенные — в конце."""
    stats = {
        s.code: s for s in (await session.scalars(select(MarketStat))).all()
    }
    offers = {
        o.lzt_country.upper(): o for o in (await session.scalars(select(CountryOffer))).all()
    }
    codes = sorted(set(COUNTRIES) | set(stats) | set(offers))

    out = [Row(code=c, name=name_for(c), stat=stats.get(c), offer=offers.get(c)) for c in codes]
    if only_lots:
        out = [r for r in out if r.lots > 0]
    out.sort(key=lambda r: (-r.lots, not r.checked, r.name))
    return out


async def get(session: AsyncSession, code: str) -> MarketStat | None:
    return await session.get(MarketStat, code.strip().upper())


def suggest_limit(stat: MarketStat | None) -> int | None:
    """Подсказка для лимита закупки: самый дешёвый лот, округлённый до рубля вверх.

    Это именно подсказка в форму, а не рекомендация: с лимитом ровно по минимуму
    позиция станет «нет в наличии», как только этот лот уйдёт. Запас админ
    добавляет сам, глядя на среднюю по выборке.
    """
    if stat is None or stat.price_min is None:
        return None
    return ((stat.price_min + 99) // 100) * 100
