"""Клиент Crypto Pay API (@CryptoBot).

По документации https://help.send.tg/en/articles/10279948-crypto-pay-api
(проверено на живом API 20.08.2026).

База — https://pay.crypt.bot/api/%method%, тестовая сеть — testnet-pay.crypt.bot
с отдельным токеном (CRYPTOBOT_BASE_URL в .env). Токен приложения уходит в
заголовке Crypto-Pay-API-Token. Ответ всегда в конверте:
    {"ok": true,  "result": ...}
    {"ok": false, "error": {"code": 400, "name": "AMOUNT_TOO_SMALL"}}

Почему именно этот провайдер считает курс. createInvoice умеет счёт сразу в
фиате (currency_type=fiat, fiat=RUB) — покупатель платит любой доступной
криптой, а магазин получает нужные рубли, пересчёт не наш. Плюс getExchangeRates
отдаёт курсы, которые обновляются на их стороне; из них берём рубль→USDT для
xRocket, у которого фиата в API нет вообще.

Задействованные методы:
    getMe             — проверить токен
    createInvoice     — счёт (у нас: currency_type=fiat, fiat=RUB)
    getInvoices       — состояние счетов, до 1000 за запрос
    deleteInvoice     — снять неоплаченный счёт
    getBalance        — остатки приложения
    getExchangeRates  — курсы, обновляются на стороне Crypto Bot
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.config import settings
from app.integrations.pay import PayApiError, PayClient, PayHTTPError, signature_ok

log = logging.getLogger("pay.cryptobot")

# Статусы счёта из документации.
ACTIVE = "active"
PAID = "paid"
EXPIRED = "expired"

# Заголовок с подписью вебхука.
SIGNATURE_HEADER = "crypto-pay-api-signature"

# Ограничения из документации — проверяем до запроса, чтобы не ловить 400.
DESCRIPTION_MAX = 1024
HIDDEN_MESSAGE_MAX = 2048
PAYLOAD_MAX = 4096
EXPIRES_MIN = 1
EXPIRES_MAX = 2678400  # 31 день


def amount_str(value: str | int | float | Decimal) -> str:
    """Сумма уходит строкой: в документации amount — String, не число."""
    if isinstance(value, str):
        return value.strip()
    text = format(Decimal(str(value)), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


class CryptoBot(PayClient):
    provider = "Crypto Bot"
    auth_header = "Crypto-Pay-API-Token"

    def __init__(
        self,
        token: str | None = None,
        *,
        base_url: str | None = None,
        timeout: float | None = None,
        proxy: str | None = None,
        max_retries: int = 3,
    ) -> None:
        super().__init__(
            token if token is not None else settings.cryptobot_token,
            base_url=f"{(base_url or settings.cryptobot_base_url).rstrip('/')}/api",
            timeout=timeout,
            proxy=proxy,
            max_retries=max_retries,
        )

    # ------------------------------------------------------------------ #
    #  Конверт
    # ------------------------------------------------------------------ #
    def unwrap(self, data: Any, response: httpx.Response) -> Any:
        if not isinstance(data, dict):
            raise PayHTTPError(self.provider, response.status_code, data, str(response.url))
        if data.get("ok") is True:
            return data.get("result")

        error = data.get("error")
        if isinstance(error, dict):
            name = str(error.get("name") or error.get("message") or "отказ")
            raise PayApiError(
                self.provider, name, code=error.get("code"), status=response.status_code
            )
        raise PayHTTPError(self.provider, response.status_code, data, str(response.url))

    def check_signature(self, raw_body: bytes, signature: str | None) -> bool:
        """Подпись вебхука: hex HMAC-SHA-256 от сырого тела, ключ — SHA-256 токена."""
        return signature_ok(self.token, raw_body, signature)

    # ------------------------------------------------------------------ #
    #  Приложение
    # ------------------------------------------------------------------ #
    async def get_me(self) -> dict[str, Any]:
        """getMe — сведения о приложении: app_id, name, имя бота-платёжника."""
        data = await self.request("GET", "/getMe")
        return data if isinstance(data, dict) else {}

    async def get_balance(self) -> list[dict[str, Any]]:
        """getBalance — остатки приложения по валютам."""
        data = await self.request("GET", "/getBalance")
        return data if isinstance(data, list) else []

    # ------------------------------------------------------------------ #
    #  Счёта
    # ------------------------------------------------------------------ #
    async def create_invoice(
        self,
        *,
        amount: str | int | float | Decimal,
        currency_type: str = "fiat",
        asset: str | None = None,
        fiat: str | None = "RUB",
        accepted_assets: list[str] | str | None = None,
        description: str | None = None,
        hidden_message: str | None = None,
        payload: str | None = None,
        expires_in: int | None = None,
        allow_comments: bool | None = None,
        allow_anonymous: bool | None = None,
        paid_btn_name: str | None = None,
        paid_btn_url: str | None = None,
    ) -> dict[str, Any]:
        """createInvoice — счёт на оплату.

        currency_type=fiat + fiat=RUB: сумма зафиксирована в рублях, крипту для
        оплаты выбирает покупатель (accepted_assets ограничивает список).
        currency_type=crypto требует asset.
        """
        if currency_type == "crypto" and not asset:
            raise ValueError("для currency_type=crypto нужен asset")
        if currency_type == "fiat" and not fiat:
            raise ValueError("для currency_type=fiat нужен fiat")
        if expires_in is not None and not EXPIRES_MIN <= expires_in <= EXPIRES_MAX:
            raise ValueError(f"expires_in вне {EXPIRES_MIN}…{EXPIRES_MAX} секунд")
        if paid_btn_name and not paid_btn_url:
            raise ValueError("paid_btn_name без paid_btn_url документация не принимает")

        params: dict[str, Any] = {
            "currency_type": currency_type,
            "asset": asset if currency_type == "crypto" else None,
            "fiat": fiat if currency_type == "fiat" else None,
            "accepted_assets": (
                ",".join(accepted_assets)
                if isinstance(accepted_assets, list)
                else accepted_assets
            ),
            "amount": amount_str(amount),
            "description": _cut(description, DESCRIPTION_MAX),
            "hidden_message": _cut(hidden_message, HIDDEN_MESSAGE_MAX),
            "payload": _cut(payload, PAYLOAD_MAX),
            "expires_in": expires_in,
            "allow_comments": allow_comments,
            "allow_anonymous": allow_anonymous,
            "paid_btn_name": paid_btn_name,
            "paid_btn_url": paid_btn_url,
        }
        data = await self.request("POST", "/createInvoice", params=params)
        if not isinstance(data, dict):
            raise PayApiError(self.provider, "createInvoice вернул не объект счёта")
        return data

    async def get_invoices(
        self,
        *,
        invoice_ids: list[int | str] | None = None,
        status: str | None = None,
        asset: str | None = None,
        fiat: str | None = None,
        offset: int | None = None,
        count: int | None = None,
    ) -> list[dict[str, Any]]:
        """getInvoices — счёта приложения. invoice_ids уходит списком через запятую."""
        if count is not None and not 1 <= count <= 1000:
            raise ValueError("count вне 1…1000")
        params: dict[str, Any] = {
            "invoice_ids": ",".join(str(i) for i in invoice_ids) if invoice_ids else None,
            "status": status,
            "asset": asset,
            "fiat": fiat,
            "offset": offset,
            "count": count,
        }
        data = await self.request("GET", "/getInvoices", params=params)
        # Документация обещает объект с items; на всякий случай принимаем и голый список.
        if isinstance(data, dict):
            items = data.get("items")
            return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []
        return [i for i in data if isinstance(i, dict)] if isinstance(data, list) else []

    async def get_invoice(self, invoice_id: int | str) -> dict[str, Any] | None:
        """Один счёт по id. None, если приложение такого не знает."""
        items = await self.get_invoices(invoice_ids=[invoice_id])
        return items[0] if items else None

    async def delete_invoice(self, invoice_id: int | str) -> bool:
        """deleteInvoice — снять неоплаченный счёт."""
        return bool(await self.request("POST", "/deleteInvoice", params={"invoice_id": invoice_id}))

    # ------------------------------------------------------------------ #
    #  Курсы
    # ------------------------------------------------------------------ #
    async def get_exchange_rates(self) -> list[dict[str, Any]]:
        """getExchangeRates — курсы крипты к фиату. Обновляются на стороне Crypto Bot."""
        data = await self.request("GET", "/getExchangeRates")
        return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []

    async def rate(self, source: str, target: str = "RUB") -> Decimal | None:
        """Курс source→target. None, если пары нет или курс помечен невалидным.

        rate приходит строкой («83.93653983») — так и разбираем, через Decimal:
        деньги через float в этом проекте не ходят.
        """
        source, target = source.upper(), target.upper()
        for row in await self.get_exchange_rates():
            if str(row.get("source", "")).upper() != source:
                continue
            if str(row.get("target", "")).upper() != target:
                continue
            if row.get("is_valid") is False:
                log.warning("Crypto Bot: курс %s→%s помечен невалидным", source, target)
                return None
            try:
                value = Decimal(str(row.get("rate")))
            except (InvalidOperation, TypeError):
                return None
            return value if value > 0 else None
        return None


def _cut(text: str | None, limit: int) -> str | None:
    """Обрезать до лимита документации: провайдер на превышении отвечает 400."""
    if text is None:
        return None
    text = text.strip()
    return text[:limit] if text else None


def is_paid(invoice: dict[str, Any]) -> bool:
    return str(invoice.get("status") or "").lower() == PAID


def is_expired(invoice: dict[str, Any]) -> bool:
    return str(invoice.get("status") or "").lower() == EXPIRED


_client: CryptoBot | None = None


def get_cryptobot() -> CryptoBot:
    """Общий клиент процесса. Держит пул соединений httpx."""
    global _client
    if _client is None:
        _client = CryptoBot()
    return _client


def set_cryptobot(client: CryptoBot | None) -> None:
    """Подменить клиент — нужно прогону без сети (app/tools/fake_pay.py)."""
    global _client
    _client = client


async def close_cryptobot() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
