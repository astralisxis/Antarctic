"""Проверка связи с Crypto Bot и xRocket. Нужны токены в .env.

    python -m app.tools.pay_check me
    python -m app.tools.pay_check rate --asset USDT
    python -m app.tools.pay_check currencies
    python -m app.tools.pay_check quote --amount 100
    python -m app.tools.pay_check invoice --provider cryptobot --amount 100

Курс берётся только у Crypto Bot: в API xRocket его нет, там сумма счёта уже в
крипте. Поэтому при выключенном Crypto Bot способ xRocket недоступен.

Команда invoice создаёт настоящий счёт у провайдера (денег это не списывает) и
сразу снимает его, если не передать --keep. Записи в базе она не делает: это
проверка токена и прав, а не пополнение.
"""

from __future__ import annotations

import argparse
import asyncio
from decimal import Decimal

from app.db import dispose, session_scope
from app.integrations import cryptobot as cb
from app.integrations import xrocket as xr
from app.integrations.pay import PayError
from app.money import fmt_money, parse_rub
from app.services import payments as pay
from app.services import settings_store


def _amount(value: str) -> int:
    """Сумма из командной строки в копейки. Принимает «100», «99,50», «100 ₽»."""
    amount = parse_rub(value)
    if not amount or amount <= 0:
        raise ValueError(f"«{value}» — не сумма в рублях")
    return amount


async def cmd_me() -> None:
    if cb.get_cryptobot().ready:
        me = await cb.get_cryptobot().get_me()
        print(f"Crypto Bot: приложение {me.get('name')} (app_id {me.get('app_id')})")
        bot = me.get("payment_processing_bot_username")
        if bot:
            print(f"    счёта оплачиваются через @{bot}")
        for row in await cb.get_cryptobot().get_balance():
            print(f"    {row.get('currency_code')}: {row.get('available')}")
    else:
        print("Crypto Bot: CRYPTOBOT_TOKEN не задан")

    if xr.get_xrocket().ready:
        info = await xr.get_xrocket().app_info()
        print(f"xRocket: приложение {info.get('name')}, версия API {await xr.get_xrocket().version()}")
        fee = await xr.get_xrocket().fee_percent()
        # Этот процент xRocket берёт с магазина, покупатель платит ровно сумму счёта.
        print(f"    комиссия приложения: {fee if fee is not None else '—'}%")
        for row in info.get("balances") or []:
            print(f"    {row.get('currency')}: {row.get('balance')}")
    else:
        print("xRocket: XROCKET_TOKEN не задан")


async def cmd_rate(asset: str) -> None:
    """Курс к рублю. Обновляется на стороне Crypto Bot, у нас не кэшируется."""
    value = await cb.get_cryptobot().rate(asset, "RUB")
    if value is None:
        print(f"пары {asset.upper()}→RUB в getExchangeRates нет")
        return
    print(f"1 {asset.upper()} = {value} ₽")
    print(f"100 ₽ = {(Decimal(100) / value).quantize(Decimal('0.000001'))} {asset.upper()}")


async def cmd_currencies() -> None:
    rows = await xr.get_xrocket().currencies()
    if not rows:
        print("xRocket не вернул ни одной валюты")
        return
    print(f"{'валюта':10} {'мин. счёт':>14}  название")
    for row in rows:
        print(f"{str(row.get('currency')):10} {str(row.get('minInvoice')):>14}  {row.get('name')}")


async def cmd_quote(amount_rub: str) -> None:
    """Что увидит покупатель на эту сумму у каждого способа."""
    amount = _amount(amount_rub)
    async with session_scope() as session:
        await settings_store.ensure_defaults(session)
        ways = await pay.methods(session)
        if not ways:
            print("ни один способ оплаты не включён или нет токенов")
            return
        print(f"сумма пополнения: {fmt_money(amount)}\n")
        for method in ways:
            try:
                calc = await pay.quote(session, method.provider, amount)
            except pay.PaymentError as exc:
                print(f"{method.title:12} — недоступно: {exc}")
                continue
            extra = ""
            if calc.rate is not None:
                extra = f" (курс {calc.rate}, наценка {calc.markup}%)"
            print(f"{method.title:12} — к оплате {calc.charge_text}{extra}")


async def cmd_invoice(provider: str, amount_rub: str, keep: bool) -> None:
    amount = _amount(amount_rub)
    async with session_scope() as session:
        await settings_store.ensure_defaults(session)
        calc = await pay.quote(session, provider, amount)

    if provider == "cryptobot":
        invoice = await cb.get_cryptobot().create_invoice(
            amount=calc.charge,
            currency_type="fiat",
            fiat="RUB",
            description="Проверка связи, оплачивать не нужно",
            payload="pay_check",
            expires_in=600,
        )
        external = invoice.get("invoice_id")
        link = invoice.get("mini_app_invoice_url") or invoice.get("bot_invoice_url")
    else:
        invoice = await xr.get_xrocket().create_invoice(
            amount=calc.charge,
            currency=calc.asset,
            num_payments=1,
            description="Проверка связи, оплачивать не нужно",
            payload="pay_check",
            comments_enabled=False,
            expired_in=600,
        )
        external = invoice.get("id")
        link = xr.link_of(invoice)

    print(f"счёт создан: id={external}, к оплате {calc.charge_text}")
    print(f"ссылка: {link}")

    if keep:
        print("счёт оставлен: он сам закроется через 10 минут")
        return
    if provider == "cryptobot":
        removed = await cb.get_cryptobot().delete_invoice(str(external))
    else:
        removed = await xr.get_xrocket().delete_invoice(str(external))
    print("счёт снят" if removed else "снять счёт не удалось — закроется по сроку")


def main() -> None:
    parser = argparse.ArgumentParser(description="Проверка Crypto Bot и xRocket")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("me")
    rate = sub.add_parser("rate")
    rate.add_argument("--asset", default="USDT")
    sub.add_parser("currencies")
    quote = sub.add_parser("quote")
    quote.add_argument("--amount", default="100", help="сумма пополнения в рублях")
    invoice = sub.add_parser("invoice")
    invoice.add_argument("--provider", default="cryptobot", choices=["cryptobot", "xrocket"])
    invoice.add_argument("--amount", default="100", help="сумма пополнения в рублях")
    invoice.add_argument("--keep", action="store_true", help="не снимать счёт после проверки")
    args = parser.parse_args()

    async def run() -> None:
        try:
            if args.cmd == "me":
                await cmd_me()
            elif args.cmd == "rate":
                await cmd_rate(args.asset)
            elif args.cmd == "currencies":
                await cmd_currencies()
            elif args.cmd == "quote":
                await cmd_quote(args.amount)
            elif args.cmd == "invoice":
                await cmd_invoice(args.provider, args.amount, args.keep)
        except (PayError, pay.PaymentError) as exc:
            print("ошибка платёжного сервиса:", exc)
        except ValueError as exc:
            print("сумма не разобрана:", exc)
        finally:
            await cb.close_cryptobot()
            await xr.close_xrocket()
            await dispose()

    asyncio.run(run())


if __name__ == "__main__":
    main()
