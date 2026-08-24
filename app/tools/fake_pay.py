"""Заглушки платёжных сервисов для прогонов без сети.

Отвечают на те же вызовы, что CryptoBot и XRocket, поэтому app/services/payments.py
подмены не замечает: счёт, опрос, зачисление и снятие проверяются целиком, без
денег и без интернета. Ставятся через set_cryptobot() и set_xrocket().

Счёт лежит в том же виде, в каком его отдаёт провайдер: у Crypto Bot ключ
invoice_id и статусы active/paid/expired, у xRocket — id, link и то же поле
status. Проверки app/integrations/*.is_paid читают именно их, поэтому
«оплатить» в прогоне — это pay(invoice_id), а не отдельный флаг.

Используется в app/tools/pay_check.py, app/tools/admin_smoke.py и bot_smoke.py.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.integrations import cryptobot as cb
from app.integrations import xrocket as xr
from app.integrations.pay import PayError


class FakeCryptoBot:
    """Crypto Bot без сети. Курс и минимумы задаются полями.

    down=True — провайдер молчит: так проверяется ветка «сервис не ответил».
    rates — курсы к рублю, их же отдаёт настоящий getExchangeRates.
    """

    provider = "Crypto Bot"

    def __init__(self, token: str = "fake-cryptobot-token") -> None:
        self.token = token
        self.down = False
        self.rates: dict[str, Decimal] = {"USDT": Decimal("83.5"), "TON": Decimal("245.1")}
        self.invoices: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []
        self._next_id = 900_100

    @property
    def ready(self) -> bool:
        return bool(self.token)

    def _guard(self, call: str) -> None:
        self.calls.append(call)
        if self.down:
            raise PayError("Crypto Bot: сеть недоступна (заглушка)")

    # --- приложение ---
    async def get_me(self) -> dict[str, Any]:
        self._guard("getMe")
        return {"app_id": 1, "name": "fake shop", "payment_processing_bot_username": "CryptoBot"}

    async def get_balance(self) -> list[dict[str, Any]]:
        self._guard("getBalance")
        return [{"currency_code": "USDT", "available": "0", "onhold": "0"}]

    # --- счёта ---
    async def create_invoice(self, **kwargs: Any) -> dict[str, Any]:
        self._guard("createInvoice")
        self._next_id += 1
        invoice_id = self._next_id
        invoice = {
            "invoice_id": invoice_id,
            "hash": f"fake{invoice_id}",
            "status": cb.ACTIVE,
            "currency_type": kwargs.get("currency_type", "fiat"),
            "fiat": kwargs.get("fiat"),
            "asset": kwargs.get("asset"),
            "amount": cb.amount_str(kwargs.get("amount", 0)),
            "description": kwargs.get("description"),
            "payload": kwargs.get("payload"),
            "bot_invoice_url": f"https://t.me/CryptoBot?start=fake{invoice_id}",
            "mini_app_invoice_url": f"https://t.me/CryptoBot/app?startapp=fake{invoice_id}",
        }
        self.invoices[str(invoice_id)] = invoice
        return invoice

    async def get_invoices(self, **kwargs: Any) -> list[dict[str, Any]]:
        self._guard("getInvoices")
        ids = kwargs.get("invoice_ids")
        if not ids:
            return list(self.invoices.values())
        return [self.invoices[str(i)] for i in ids if str(i) in self.invoices]

    async def get_invoice(self, invoice_id: int | str) -> dict[str, Any] | None:
        items = await self.get_invoices(invoice_ids=[invoice_id])
        return items[0] if items else None

    async def delete_invoice(self, invoice_id: int | str) -> bool:
        self._guard(f"deleteInvoice:{invoice_id}")
        invoice = self.invoices.get(str(invoice_id))
        if invoice is None:
            return False
        invoice["status"] = cb.EXPIRED
        return True

    # --- курсы ---
    async def get_exchange_rates(self) -> list[dict[str, Any]]:
        self._guard("getExchangeRates")
        return [
            {"source": code, "target": "RUB", "rate": str(value), "is_valid": True}
            for code, value in self.rates.items()
        ]

    async def rate(self, source: str, target: str = "RUB") -> Decimal | None:
        self._guard(f"rate:{source}-{target}")
        if target.upper() != "RUB":
            return None
        return self.rates.get(source.upper())

    # --- сценарий прогона ---
    def pay(self, invoice_id: int | str) -> None:
        """Покупатель оплатил счёт: дальше его увидит опрос."""
        self.invoices[str(invoice_id)]["status"] = cb.PAID

    def expire(self, invoice_id: int | str) -> None:
        self.invoices[str(invoice_id)]["status"] = cb.EXPIRED

    def forget(self, invoice_id: int | str) -> None:
        """Провайдер больше не знает счёт — так бывает после удаления."""
        self.invoices.pop(str(invoice_id), None)

    async def aclose(self) -> None:
        return None


class FakeXRocket:
    """xRocket без сети. Фиат и курс он не отдаёт, поэтому здесь только валюты."""

    provider = "xRocket"

    def __init__(self, token: str = "fake-xrocket-token") -> None:
        self.token = token
        self.down = False
        self.minimums: dict[str, Decimal] = {"USDT": Decimal("0.01"), "TON": Decimal("0.001")}
        self.fee = Decimal("1.5")
        self.invoices: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []
        self._next_id = 500_100

    @property
    def ready(self) -> bool:
        return bool(self.token)

    def _guard(self, call: str) -> None:
        self.calls.append(call)
        if self.down:
            raise PayError("xRocket: сеть недоступна (заглушка)")

    # --- приложение ---
    async def version(self) -> str:
        self._guard("version")
        return "fake"

    async def app_info(self) -> dict[str, Any]:
        self._guard("app/info")
        return {"name": "fake shop", "feePercents": float(self.fee), "balances": []}

    async def fee_percent(self) -> Decimal | None:
        return self.fee

    # --- валюты ---
    async def currencies(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        self._guard("currencies")
        return [
            {"currency": code, "name": code, "minInvoice": str(value), "minTransfer": str(value)}
            for code, value in self.minimums.items()
        ]

    async def currency(self, code: str) -> dict[str, Any] | None:
        for row in await self.currencies():
            if row["currency"] == code.strip().upper():
                return row
        return None

    async def min_invoice(self, code: str) -> Decimal | None:
        return self.minimums.get(code.strip().upper())

    # --- счёта ---
    async def create_invoice(self, **kwargs: Any) -> dict[str, Any]:
        self._guard("tg-invoices")
        self._next_id += 1
        invoice_id = self._next_id
        invoice = {
            "id": invoice_id,
            "status": xr.ACTIVE,
            "amount": float(Decimal(str(kwargs.get("amount", 0)))),
            "currency": str(kwargs.get("currency", "USDT")).upper(),
            "description": kwargs.get("description"),
            "payload": kwargs.get("payload"),
            "paid": False,
            "link": f"https://t.me/xrocket?start=inv_fake{invoice_id}",
        }
        self.invoices[str(invoice_id)] = invoice
        return invoice

    async def get_invoice(self, invoice_id: int | str) -> dict[str, Any] | None:
        self._guard(f"tg-invoices:{invoice_id}")
        return self.invoices.get(str(invoice_id))

    async def delete_invoice(self, invoice_id: int | str) -> bool:
        self._guard(f"delete:{invoice_id}")
        invoice = self.invoices.get(str(invoice_id))
        if invoice is None:
            return False
        invoice["status"] = xr.EXPIRED
        return True

    # --- сценарий прогона ---
    def pay(self, invoice_id: int | str) -> None:
        invoice = self.invoices[str(invoice_id)]
        invoice["status"] = xr.PAID
        invoice["paid"] = True

    def expire(self, invoice_id: int | str) -> None:
        self.invoices[str(invoice_id)]["status"] = xr.EXPIRED

    def forget(self, invoice_id: int | str) -> None:
        self.invoices.pop(str(invoice_id), None)

    async def aclose(self) -> None:
        return None
