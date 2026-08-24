"""Проверка связи с LZT Market. Нужен LZT_TOKEN в .env.

    python -m app.tools.lzt_check me
    python -m app.tools.lzt_check balances
    python -m app.tools.lzt_check countries --pmax 60
    python -m app.tools.lzt_check search --country ID --pmax 30
    python -m app.tools.lzt_check item 12345678

Коды стран — ISO из двух букв (RU, ID, US), список кандидатов в app/countries.py.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from app.config import settings
from app.countries import COUNTRIES, name_for
from app.integrations.lzt import LztError, balances_of, close_lzt, get_lzt
from app.money import fmt_money


def _dump(data: Any, limit: int = 4000) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2)
    print(text[:limit] + ("\n… обрезано" if len(text) > limit else ""))


async def cmd_me() -> None:
    data = await get_lzt().profile_get()
    user = data.get("user") or data
    print("аккаунт:", user.get("username") or user.get("user_id"))
    for key in ("balance", "hold", "user_id", "currency"):
        if key in user:
            print(f"  {key}: {user[key]}")
    # balance_id нужен для fast-buy при нескольких балансах
    if "balances" in data:
        print("балансы:")
        _dump(data["balances"], 1200)


async def cmd_balances() -> None:
    """Кошельки аккаунта и их balance_id.

    Покупка списывает с того баланса, чей id уходит в fast-buy: деньги на
    «Балансе на Маркете» не помогут, если лоты оплачиваются с «Баланса для
    покупки аккаунтов». Нужный id вписывается в LZT_BALANCE_ID в .env.
    """
    rows = balances_of(await get_lzt().balances())
    if not rows:
        print("маркет не вернул ни одного баланса")
        return
    current = settings.lzt_balance_id
    print(f"{'balance_id':>12}  {'сумма':>10}  назначение")
    for b in rows:
        mark = " ← выбран в .env" if current is not None and str(current) == str(b.balance_id) else ""
        target = "покупка аккаунтов" if b.for_accounts else b.title
        print(f"{str(b.balance_id):>12}  {fmt_money(b.amount):>10}  {target}{mark}")
    if current is None:
        print("\nLZT_BALANCE_ID не задан: покупка пойдёт с баланса по умолчанию.")
        best = next((b for b in rows if b.for_accounts), None)
        if best:
            print(f"Чтобы платить целевыми деньгами, впишите LZT_BALANCE_ID={best.balance_id}")


async def cmd_countries(pmax: int | None, spam: str, codes: list[str] | None) -> None:
    """Наличие по странам.

    Перечня стран в /telegram/params нет, поэтому проходим поиском по кандидатам
    из app/countries.py и печатаем, сколько лотов и от какой цены. Значения из
    колонки «код» вписываются в поле lzt_country позиции каталога.
    """
    lzt = get_lzt()
    targets = [c.strip().upper() for c in codes] if codes else list(COUNTRIES)
    rows: list[tuple[str, str, int | None, Any]] = []

    for index, code in enumerate(targets, 1):
        try:
            data = await lzt.accounts_search_telegram(
                countries=[code], price_max=pmax, spam=spam, order_by="price_to_up"
            )
            items = data.get("items") or []
            rows.append((code, name_for(code), data.get("totalItems"), items[0].get("price") if items else None))
        except LztError as exc:
            rows.append((code, name_for(code), None, f"ошибка: {exc}"))
        if index < len(targets):
            await asyncio.sleep(0.7)  # маркет не любит частых запросов

    rows.sort(key=lambda r: -(r[2] or 0))
    limit = f" до {pmax} ₽" if pmax else ""
    print(f"наличие{limit}, спам-блок: {spam}\n")
    print(f"{'код':4} {'страна':22} {'лотов':>7}  от цены")
    for code, name, total, price in rows:
        if not total:
            continue
        print(f"{code:4} {name:22} {total:>7}  {price if price is not None else '—'}")
    empty = [f"{code} ({name})" for code, name, total, _ in rows if not total]
    if empty:
        print(f"\nбез лотов по этому фильтру: {', '.join(empty)}")


async def cmd_search(country: str | None, pmax: int | None, spam: str) -> None:
    data = await get_lzt().accounts_search_telegram(
        countries=[country] if country else None,
        price_max=pmax,
        spam=spam,
        order_by="price_to_up",
    )
    items = data.get("items", [])
    print(f"найдено на странице: {len(items)} (всего по фильтру: {data.get('totalItems', '?')})")
    for item in items[:10]:
        print(
            f"  id={item.get('item_id')} "
            f"цена={item.get('price')} {item.get('price_currency', '')} "
            f"страна={item.get('telegram_country')} "
            f"источник={item.get('item_origin')} "
            f"состояние={item.get('item_state')}"
        )
    if items:
        print("\nполя первого лота:")
        print(", ".join(sorted(items[0].keys())))


async def cmd_item(item_id: int) -> None:
    _dump(await get_lzt().item_get(item_id))


def main() -> None:
    parser = argparse.ArgumentParser(description="Проверка LZT Market")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("me")
    sub.add_parser("balances")
    countries = sub.add_parser("countries")
    countries.add_argument("--pmax", type=int, help="потолок цены в рублях")
    countries.add_argument("--spam", default="no", choices=["yes", "no", "nomatter"])
    countries.add_argument("--code", action="append", help="проверить только эти коды")
    search = sub.add_parser("search")
    search.add_argument("--country", help="ISO-код страны, например ID")
    search.add_argument("--pmax", type=int)
    search.add_argument("--spam", default="no", choices=["yes", "no", "nomatter"])
    item = sub.add_parser("item")
    item.add_argument("item_id", type=int)
    args = parser.parse_args()

    async def run() -> None:
        try:
            if args.cmd == "me":
                await cmd_me()
            elif args.cmd == "balances":
                await cmd_balances()
            elif args.cmd == "countries":
                await cmd_countries(args.pmax, args.spam, args.code)
            elif args.cmd == "search":
                await cmd_search(args.country, args.pmax, args.spam)
            elif args.cmd == "item":
                await cmd_item(args.item_id)
        except LztError as exc:
            print("ошибка LZT:", exc)
        finally:
            await close_lzt()

    asyncio.run(run())


if __name__ == "__main__":
    main()
