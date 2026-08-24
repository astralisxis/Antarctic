"""Клиент Rocket Pay API (@xRocket).

По спецификации https://pay.xrocket.exchange/api/ (Rocket Pay API 1.3.1,
проверено на живом API 20.08.2026). База — https://pay.xrocket.exchange,
токен приложения в заголовке Rocket-Pay-Key (без него работает только /version).
Ответ в конверте {"success": true, "data": ...}.

Важное следствие из спецификации: фиата у xRocket нет — в /currencies/available
все 63 значения криптовалютные, а метода курсов среди пятнадцати эндпоинтов нет
(проверены и /rates, и /currencies/rates — 404). Поэтому счёт здесь всегда в
крипте, а рубли в неё пересчитывает app/services/payments.py по курсу Crypto Bot.

Ещё одно: /app/info отдаёт feePercents (на 20.08.2026 — 1.5), то есть xRocket
берёт свой процент с приложения. Компенсируется наценкой topup.xrocket_markup.

Задействованные эндпоинты:
    GET    /version              — версия, единственный без ключа
    GET    /app/info             — имя приложения, комиссия, балансы
    GET    /currencies/available — валюты и минимумы (minInvoice)
    POST   /tg-invoices          — создать счёт
    GET    /tg-invoices/{id}     — состояние счёта
    DELETE /tg-invoices/{id}     — снять счёт
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

import httpx

from app.config import settings
from app.integrations.pay import PayApiError, PayClient, PayHTTPError, signature_ok

log = logging.getLogger("pay.xrocket")

# Статусы счёта из спецификации.
ACTIVE = "active"
PAID = "paid"
EXPIRED = "expired"

SIGNATURE_HEADER = "rocket-pay-signature"

# Ограничения из CreateInvoiceDto — проверяем до запроса.
AMOUNT_MAX = Decimal("1000000")
DESCRIPTION_MAX = 1000
HIDDEN_MESSAGE_MAX = 2000
PAYLOAD_MAX = 4000
CALLBACK_MAX = 500
EXPIRED_IN_MAX = 86400  # 0 = бессрочный


class XRocket(PayClient):
    provider = "xRocket"
    auth_header = "Rocket-Pay-Key"

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
            token if token is not None else settings.xrocket_token,
            base_url=base_url or settings.xrocket_base_url,
            timeout=timeout,
            proxy=proxy,
            max_retries=max_retries,
        )
        self._currencies: list[dict[str, Any]] | None = None

    # ------------------------------------------------------------------ #
    #  Конверт
    # ------------------------------------------------------------------ #
    def unwrap(self, data: Any, response: httpx.Response) -> Any:
        if not isinstance(data, dict):
            raise PayHTTPError(self.provider, response.status_code, data, str(response.url))
        if data.get("success") is True:
            return data.get("data")

        message = _message_of(data)
        if message:
            raise PayApiError(
                self.provider,
                message,
                code=data.get("statusCode") or response.status_code,
                status=response.status_code,
            )
        raise PayHTTPError(self.provider, response.status_code, data, str(response.url))

    def check_signature(self, raw_body: bytes, signature: str | None) -> bool:
        """Подпись вебхука: hex HMAC-SHA-256 от тела, ключ — SHA-256 токена."""
        return signature_ok(self.token, raw_body, signature)

    # ------------------------------------------------------------------ #
    #  Приложение
    # ------------------------------------------------------------------ #
    async def version(self) -> str:
        """GET /version — проверка связи без ключа."""
        data = await self.request("GET", "/version", need_token=False)
        if isinstance(data, dict):
            return str(data.get("version") or data)
        return str(data)

    async def app_info(self) -> dict[str, Any]:
        """GET /app/info — имя, feePercents и балансы приложения."""
        data = await self.request("GET", "/app/info")
        return data if isinstance(data, dict) else {}

    async def fee_percent(self) -> Decimal | None:
        """Процент, который xRocket берёт с приложения. None, если не отдали."""
        raw = (await self.app_info()).get("feePercents")
        try:
            return Decimal(str(raw))
        except (TypeError, ArithmeticError):
            return None

    # ------------------------------------------------------------------ #
    #  Валюты
    # ------------------------------------------------------------------ #
    async def currencies(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        """GET /currencies/available — валюты и минимумы.

        Список на процесс кэшируется: он меняется редко, а нужен на каждый счёт
        (проверить minInvoice) — лишний запрос на каждое пополнение не нужен.
        """
        if self._currencies is None or refresh:
            data = await self.request("GET", "/currencies/available")
            rows = data.get("results") if isinstance(data, dict) else data
            self._currencies = [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
        return self._currencies

    async def currency(self, code: str) -> dict[str, Any] | None:
        code = code.strip().upper()
        for row in await self.currencies():
            if str(row.get("currency", "")).upper() == code:
                return row
        return None

    async def min_invoice(self, code: str) -> Decimal | None:
        """Минимальная сумма счёта в этой валюте. None, если валюта неизвестна."""
        row = await self.currency(code)
        if not row:
            return None
        try:
            return Decimal(str(row.get("minInvoice")))
        except (TypeError, ArithmeticError):
            return None

    # ------------------------------------------------------------------ #
    #  Счёта
    # ------------------------------------------------------------------ #
    async def create_invoice(
        self,
        *,
        amount: Decimal | float | int,
        currency: str,
        num_payments: int = 1,
        min_payment: Decimal | float | int | None = None,
        description: str | None = None,
        hidden_message: str | None = None,
        payload: str | None = None,
        callback_url: str | None = None,
        comments_enabled: bool | None = None,
        expired_in: int | None = None,
    ) -> dict[str, Any]:
        """POST /tg-invoices — создать счёт. Возвращает FullInvoiceDto.

        amount уходит числом (в спецификации это number, не строка). На книги
        магазина это не влияет: там копейки, а здесь транспорт до провайдера.
        """
        value = Decimal(str(amount))
        if value <= 0 or value > AMOUNT_MAX:
            raise ValueError(f"amount вне 0…{AMOUNT_MAX}")
        if expired_in is not None and not 0 <= expired_in <= EXPIRED_IN_MAX:
            raise ValueError(f"expiredIn вне 0…{EXPIRED_IN_MAX} секунд")

        payload_body: dict[str, Any] = {
            "amount": float(value),
            "currency": currency.strip().upper(),
            "numPayments": num_payments,
            "minPayment": None if min_payment is None else float(Decimal(str(min_payment))),
            "description": _cut(description, DESCRIPTION_MAX),
            "hiddenMessage": _cut(hidden_message, HIDDEN_MESSAGE_MAX),
            "payload": _cut(payload, PAYLOAD_MAX),
            "callbackUrl": _cut(callback_url, CALLBACK_MAX),
            "commentsEnabled": comments_enabled,
            "expiredIn": expired_in,
        }
        data = await self.request("POST", "/tg-invoices", json=payload_body)
        if not isinstance(data, dict):
            raise PayApiError(self.provider, "/tg-invoices вернул не объект счёта")
        return data

    async def get_invoice(self, invoice_id: int | str) -> dict[str, Any] | None:
        """GET /tg-invoices/{id}. None, если счёта нет."""
        try:
            data = await self.request("GET", f"/tg-invoices/{invoice_id}")
        except PayApiError as exc:
            if exc.status == 404:
                return None
            raise
        return data if isinstance(data, dict) else None

    async def delete_invoice(self, invoice_id: int | str) -> bool:
        """DELETE /tg-invoices/{id} — снять счёт."""
        try:
            await self.request("DELETE", f"/tg-invoices/{invoice_id}")
        except PayApiError as exc:
            if exc.status == 404:
                return False
            raise
        return True


def _message_of(data: dict[str, Any]) -> str:
    """Причина отказа. У xRocket она приходит в трёх видах, принимаем все."""
    parts: list[str] = []
    message = data.get("message")
    if isinstance(message, str) and message:
        parts.append(message)
    elif isinstance(message, list):
        parts.extend(str(m) for m in message if m)
    errors = data.get("errors")
    if isinstance(errors, list):
        for item in errors:
            if isinstance(item, dict):
                text = item.get("error") or item.get("message") or item.get("property")
                if text:
                    parts.append(str(text))
            elif item:
                parts.append(str(item))
    return "; ".join(dict.fromkeys(parts))


def _cut(text: str | None, limit: int) -> str | None:
    if text is None:
        return None
    text = text.strip()
    return text[:limit] if text else None


def is_paid(invoice: dict[str, Any]) -> bool:
    """Оплачен ли счёт. Смотрим и status, и paid — в разных ответах приходит своё."""
    if str(invoice.get("status") or "").lower() == PAID:
        return True
    return bool(invoice.get("paid"))


def is_expired(invoice: dict[str, Any]) -> bool:
    return str(invoice.get("status") or "").lower() == EXPIRED


def link_of(invoice: dict[str, Any]) -> str | None:
    value = invoice.get("link")
    return str(value) if value else None


_client: XRocket | None = None


def get_xrocket() -> XRocket:
    global _client
    if _client is None:
        _client = XRocket()
    return _client


def set_xrocket(client: XRocket | None) -> None:
    """Подменить клиент — нужно прогону без сети (app/tools/fake_pay.py)."""
    global _client
    _client = client


async def close_xrocket() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
