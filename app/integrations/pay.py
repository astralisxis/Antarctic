"""Общая часть платёжных клиентов: транспорт, повторы, подпись вебхука.

Crypto Bot и xRocket устроены почти одинаково: один хост, токен приложения в
заголовке, конверт вокруг полезной нагрузки и HMAC-SHA-256 подписи вебхука с
ключом SHA-256 от токена. Различия — имя заголовка, форма конверта и имена
полей — живут в cryptobot.py и xrocket.py; здесь только то, что у них общее.

Прокси по умолчанию нет: pay.crypt.bot и pay.xrocket.exchange с сервера
отвечают напрямую, в отличие от api.telegram.org. Если понадобится — PAY_PROXY.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from typing import Any

import httpx

from app.config import settings

log = logging.getLogger("pay")

# Коды, на которых имеет смысл повторить запрос.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class PayError(Exception):
    """Провайдер недоступен или ответил не тем."""


class PayHTTPError(PayError):
    """Ответ без разбираемого тела: чужая страница, 502 от балансировщика."""

    def __init__(self, provider: str, status: int, body: Any, url: str) -> None:
        self.provider = provider
        self.status = status
        self.body = body
        self.url = url
        super().__init__(f"{provider} {status} на {url}: {body}")


class PayApiError(PayError):
    """Провайдер разобрал запрос и отказал: параметры, лимиты, права токена."""

    def __init__(
        self, provider: str, message: str, *, code: Any = None, status: int = 400
    ) -> None:
        self.provider = provider
        self.code = code
        self.status = status
        super().__init__(f"{provider}: {message}" + (f" [{code}]" if code else ""))


def clean(params: dict[str, Any] | None) -> dict[str, Any] | None:
    """Query-параметры: убрать None и пустые, bool — в true/false, как ждут оба API."""
    if not params:
        return None
    out: dict[str, Any] = {}
    for key, value in params.items():
        if value is None or value == "" or value == []:
            continue
        out[key] = "true" if value is True else ("false" if value is False else value)
    return out or None


def body(data: dict[str, Any] | None) -> dict[str, Any] | None:
    """JSON-тело: выкинуть только None. Типы не трогаем — bool должен уехать bool."""
    if not data:
        return None
    out = {key: value for key, value in data.items() if value is not None}
    return out or None


def signature_ok(token: str, body: bytes, signature: str | None) -> bool:
    """Проверить подпись вебхука. Схема у Crypto Bot и xRocket одна и та же.

    Ключ — SHA-256 от токена приложения, подпись — hex HMAC-SHA-256 от тела
    запроса. Считать надо по сырым байтам: пересобранный из объекта JSON даст
    другие пробелы и другую подпись.
    """
    if not token or not signature:
        return False
    secret = hashlib.sha256(token.encode()).digest()
    expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip())


class PayClient:
    """Транспорт: httpx, повторы, разбор конверта в наследнике."""

    provider = "pay"  # для сообщений об ошибках
    auth_header = ""  # имя заголовка с токеном приложения

    def __init__(
        self,
        token: str | None = None,
        *,
        base_url: str,
        timeout: float | None = None,
        proxy: str | None = None,
        max_retries: int = 3,
    ) -> None:
        self.token = (token or "").strip()
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        headers = {"Accept": "application/json", "User-Agent": "numbers-shop/0.1"}
        if self.auth_header and self.token:
            headers[self.auth_header] = self.token
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=settings.pay_timeout if timeout is None else timeout,
            proxy=settings.pay_proxy if proxy is None else proxy,
            headers=headers,
        )

    @property
    def ready(self) -> bool:
        """Есть ли токен. Без него метод оплаты не показываем."""
        return bool(self.token)

    def unwrap(self, data: Any, response: httpx.Response) -> Any:
        """Достать полезную нагрузку из конверта провайдера или поднять ошибку."""
        raise NotImplementedError

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        need_token: bool = True,
    ) -> Any:
        if need_token and not self.token:
            raise PayError(f"{self.provider}: токен не задан в .env")

        delay = 1.0
        last_exc: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                response = await self._client.request(
                    method, path, params=clean(params), json=body(json)
                )
            except httpx.TransportError as exc:  # сеть/таймаут
                last_exc = exc
                log.warning(
                    "%s сеть: %s %s — %s (попытка %s)", self.provider, method, path, exc, attempt
                )
                if attempt == self.max_retries:
                    raise PayError(f"{self.provider}: сеть недоступна: {exc}") from exc
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)
                continue

            if response.status_code in RETRY_STATUSES and attempt < self.max_retries:
                wait = delay
                retry_after = response.headers.get("Retry-After")
                if retry_after:
                    try:
                        wait = float(retry_after)
                    except ValueError:
                        pass
                log.warning(
                    "%s %s на %s %s — ждём %.1fс (попытка %s)",
                    self.provider,
                    response.status_code,
                    method,
                    path,
                    wait,
                    attempt,
                )
                await asyncio.sleep(wait)
                delay = min(delay * 2, 30)
                continue

            try:
                data = response.json()
            except ValueError:
                data = None
            return self.unwrap(data, response)

        raise PayError(f"{self.provider}: запрос не удался: {last_exc}")

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> PayClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()
