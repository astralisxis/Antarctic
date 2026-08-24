"""Клиент LZT Market.

Основано на OpenAPI-схеме маркета (Lolzteam Public API: Market 1.1.85) из
репозитория lolzteam-py: codegen/market_openapi.json. База — https://prod-api.lzt.market,
авторизация Bearer-токеном (https://lolz.live/account/api).

Имена методов повторяют секции lolzteam-py (accounts_list / purchasing / telegram / payments),
чтобы при необходимости пересесть на саму библиотеку без переписывания вызовов.
Свой клиент, а не библиотека, по двум причинам: пакет не опубликован в PyPI под
именем lolzteam, а его секции генерируются кодогеном из OpenAPI (в репозитории
файлов sections/_market_generated.py нет) — ставить магазин на это нельзя.

Проверено на живом API 19.08.2026: country[] принимает ISO-код страны из двух букв
в ВЕРХНЕМ регистре (RU, ID, US). В нижнем регистре маркет молча отдаёт ноль лотов,
поэтому коды нормализуются в клиенте. Перечня стран в /telegram/params нет —
наличие проверяется поиском, см. app/tools/lzt_check.py countries.

Задействованные эндпоинты:
    GET  /me                                — профиль и балансы
    GET  /telegram                          — поиск номеров (country[], pmin, pmax, spam, ...)
    GET  /telegram/params                   — параметры категории (списка стран в них нет)
    GET  /{item_id}                         — карточка лота
    POST /{item_id}/check-account           — проверка валидности
    POST /{item_id}/fast-buy                — проверить и купить
    POST /{item_id}/confirm-buy             — купить без проверки
    GET  /{item_id}/telegram-login-code     — код входа из Telegram
    POST /{item_id}/telegram-reset-authorizations — сбросить чужие сессии
    GET  /user/orders                       — свои покупки
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import settings
from app.money import rub_to_kop

log = logging.getLogger("lzt")

# Коды, на которых имеет смысл повторить запрос (retry_behaviour из README библиотеки).
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
# GET и остальные безопасные/идемпотентные методы можно повторить после
# неопределённого ответа. POST-покупки нельзя отправлять повторно: запрос мог
# уже списать баланс маркета, а ответ потерялся по дороге.
RETRYABLE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})
RESET_SUCCESS_MESSAGES = frozenset(
    {
        "изменения сохранены",
        "changes saved",
    }
)


class LztError(Exception):
    """Базовая ошибка маркета."""


class LztHTTPError(LztError):
    def __init__(self, status: int, body: Any, url: str) -> None:
        self.status = status
        self.body = body
        self.url = url
        super().__init__(f"LZT {status} на {url}: {body}")


class LztApiError(LztError):
    """Маркет ответил 200/4xx с полем errors."""

    def __init__(self, errors: list[str], status: int = 400) -> None:
        self.errors = errors
        self.status = status
        super().__init__("; ".join(errors) or "неизвестная ошибка маркета")


class LztRateLimit(LztHTTPError):
    pass


class LztRetryExhausted(LztError):
    """fast-buy вернул retry_request больше лимита раз."""


class LztUncertainError(LztError):
    """Неизвестно, применил ли маркет неидемпотентный POST.

    Вызывающий код обязан остановить цепочку покупки и перепроверить лот, а не
    отправлять следующий POST: предыдущий запрос мог уже списать баланс.
    """


def _extract_errors(data: Any, *, include_message: bool = False) -> list[str]:
    """Extract API errors without treating a successful status message as one.

    LZT uses ``message`` for both error details on non-2xx responses and normal
    confirmations such as «Изменения сохранены».  On a successful HTTP status
    only the explicit ``errors``/``error`` fields are failures.
    """
    if not isinstance(data, dict):
        return []
    keys = ("errors", "error", "message") if include_message else ("errors", "error")
    for key in keys:
        value = data.get(key)
        if isinstance(value, list) and value:
            return [str(v) for v in value]
        if isinstance(value, str) and value:
            return [value]
    return []


def _norm_countries(codes: list[str] | None) -> list[str] | None:
    """ISO-коды в верхний регистр: в нижнем маркет отдаёт пустой результат без ошибки."""
    if not codes:
        return None
    return [code.strip().upper() for code in codes if code and code.strip()]


def _clean_params(params: dict[str, Any] | None) -> dict[str, Any] | None:
    """Убрать None; списки оставить как есть — httpx развернёт их в повторяющиеся ключи."""
    if not params:
        return None
    out: dict[str, Any] = {}
    for key, value in params.items():
        if value is None or value == [] or value == "":
            continue
        if isinstance(value, bool):
            out[key] = "true" if value else "false"
        else:
            out[key] = value
    return out or None


class LztMarket:
    def __init__(
        self,
        token: str | None = None,
        *,
        base_url: str | None = None,
        timeout: float | None = None,
        proxy: str | None = None,
        max_retries: int = 5,
        retry_request_limit: int | None = None,
    ) -> None:
        self.token = token if token is not None else settings.lzt_token
        self.base_url = (base_url or settings.lzt_base_url).rstrip("/")
        self.max_retries = max_retries
        self.retry_request_limit = (
            retry_request_limit
            if retry_request_limit is not None
            else settings.lzt_retry_request_limit
        )
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout or settings.lzt_timeout,
            proxy=proxy or settings.lzt_proxy,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                "User-Agent": "numbers-shop/0.1",
            },
        )

    # ------------------------------------------------------------------ #
    #  Транспорт
    # ------------------------------------------------------------------ #
    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.token:
            raise LztError("LZT_TOKEN не задан в .env")

        method = method.upper()
        retryable = method in RETRYABLE_METHODS
        delay = 1.0
        last_exc: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                response = await self._client.request(
                    method, path, params=_clean_params(params), json=json
                )
            except httpx.TransportError as exc:  # сеть/таймаут
                last_exc = exc
                log.warning("LZT сеть: %s %s — %s (попытка %s)", method, path, exc, attempt)
                if not retryable or attempt == self.max_retries:
                    error = (
                        LztUncertainError(f"неопределённый результат {method} {path}: {exc}")
                        if not retryable
                        else LztError(f"сеть недоступна: {exc}")
                    )
                    raise error from exc
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)
                continue

            if not retryable and response.status_code in RETRY_STATUSES:
                raise LztUncertainError(
                    f"неопределённый результат {method} {path}: HTTP {response.status_code}"
                )

            if retryable and response.status_code in RETRY_STATUSES and attempt < self.max_retries:
                wait = delay
                retry_after = response.headers.get("Retry-After")
                if retry_after:
                    try:
                        wait = float(retry_after)
                    except ValueError:
                        pass
                log.warning(
                    "LZT %s на %s %s — ждём %.1fс (попытка %s)",
                    response.status_code,
                    method,
                    path,
                    wait,
                    attempt,
                )
                await asyncio.sleep(wait)
                delay = min(delay * 2, 60)
                continue

            try:
                data = response.json()
            except ValueError:
                data = {"raw": response.text[:500]}

            if response.status_code == 429:
                raise LztRateLimit(429, data, str(response.url))
            if response.status_code >= 400:
                errors = _extract_errors(data, include_message=True)
                if errors:
                    raise LztApiError(errors, response.status_code)
                raise LztHTTPError(response.status_code, data, str(response.url))

            errors = _extract_errors(data)
            if errors:
                raise LztApiError(errors, response.status_code)

            return data if isinstance(data, dict) else {"data": data}

        raise LztError(f"запрос не удался: {last_exc}")

    async def _request_with_retry_flag(
        self, method: str, path: str, *, json: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Для методов, которые могут вернуть retry_request — по докам до 100 повторов."""
        for attempt in range(1, self.retry_request_limit + 1):
            try:
                return await self.request(method, path, json=json)
            except LztApiError as exc:
                if not any("retry_request" in e for e in exc.errors):
                    raise
                log.info("LZT retry_request на %s (попытка %s)", path, attempt)
                await asyncio.sleep(0.7)
        raise LztRetryExhausted(f"{path}: retry_request {self.retry_request_limit} раз подряд")

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> LztMarket:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ #
    #  Профиль и балансы
    # ------------------------------------------------------------------ #
    async def profile_get(self) -> dict[str, Any]:
        """GET /me — данные аккаунта и балансы (в том числе balance_id для закупки)."""
        return await self.request("GET", "/me")

    async def balances(self) -> dict[str, Any]:
        """GET /balance/exchange — список балансов (Documentation/Market.md, Payments → Balance).

        Балансов у аккаунта несколько, и покупка списывает с того, чей balance_id
        передан в fast-buy. «Баланс на Маркете» (balance_id «balance») и «Баланс для
        покупки аккаунтов» (числовой id) — разные кошельки: деньги на первом не
        помогут купить лот, если платить нужно со второго.
        """
        return await self.request("GET", "/balance/exchange")

    async def list_orders(self, **params: Any) -> dict[str, Any]:
        """GET /user/orders — купленные лоты."""
        return await self.request("GET", "/user/orders", params=params)

    async def payments_history(self, **params: Any) -> dict[str, Any]:
        """GET /user/payments — история операций на маркете."""
        return await self.request("GET", "/user/payments", params=params)

    # ------------------------------------------------------------------ #
    #  Каталог
    # ------------------------------------------------------------------ #
    async def category_params(self, category: str = "telegram") -> dict[str, Any]:
        """GET /{category}/params — параметры категории (перечня стран в ответе нет)."""
        return await self.request("GET", f"/{category}/params")

    async def accounts_search_telegram(
        self,
        *,
        countries: list[str] | None = None,
        not_countries: list[str] | None = None,
        price_min: float | None = None,
        price_max: float | None = None,
        spam: str | None = None,
        password: str | None = None,
        origin: list[str] | None = None,
        not_origin: list[str] | None = None,
        sold_before: bool | None = None,
        never_sold: bool | None = None,
        order_by: str = "price_to_up",
        page: int = 1,
        currency: str = "rub",
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """GET /telegram — поиск номеров.

        Параметры по документации LOLZTEAM (Documentation/Market.md, раздел
        Telegram → Get): page, title, pmin, pmax (float), origin, not_origin,
        order_by, sb, nsb плюс любые доп. параметры категории.

        countries/not_countries — ISO-коды из двух букв, регистр приводится сам;
        в документации это часть «доп. параметров», список допустимых значений
        отдаёт GET /telegram/params.
        spam/password — yes|no|nomatter.
        """
        params: dict[str, Any] = {
            "country[]": _norm_countries(countries),
            "not_country[]": _norm_countries(not_countries),
            "pmin": price_min,
            "pmax": price_max,
            "spam": spam,
            "password": password,
            "origin[]": origin,
            "not_origin[]": not_origin,
            "sb": sold_before,
            "nsb": never_sold,
            "order_by": order_by,
            "page": page,
            "currency": currency,
        }
        if extra:
            params.update(extra)
        return await self.request("GET", "/telegram", params=params)

    async def item_get(self, item_id: int, parse_same_item_ids: bool | None = None) -> dict[str, Any]:
        """GET /{item_id} — карточка лота."""
        return await self.request(
            "GET", f"/{item_id}", params={"parse_same_item_ids": parse_same_item_ids}
        )

    # ------------------------------------------------------------------ #
    #  Покупка
    # ------------------------------------------------------------------ #
    async def purchasing_check(self, item_id: int) -> dict[str, Any]:
        """POST /{item_id}/check-account — проверка валидности перед покупкой."""
        return await self._request_with_retry_flag("POST", f"/{item_id}/check-account")

    async def purchasing_fast_buy(
        self, item_id: int, price: float, balance_id: int | str | None = None
    ) -> dict[str, Any]:
        """POST /{item_id}/fast-buy — проверить и купить. price в рублях, как в карточке."""
        body: dict[str, Any] = {"price": price}
        bid = balance_id if balance_id is not None else settings.lzt_balance_id
        if bid is not None:
            body["balance_id"] = bid
        return await self._request_with_retry_flag("POST", f"/{item_id}/fast-buy", json=body)

    async def purchasing_confirm(
        self, item_id: int, price: int, balance_id: int | str | None = None
    ) -> dict[str, Any]:
        """POST /{item_id}/confirm-buy — покупка без проверки валидности."""
        body: dict[str, Any] = {"price": price}
        bid = balance_id if balance_id is not None else settings.lzt_balance_id
        if bid is not None:
            body["balance_id"] = bid
        return await self.request("POST", f"/{item_id}/confirm-buy", json=body)

    # ------------------------------------------------------------------ #
    #  Telegram-специфика
    # ------------------------------------------------------------------ #
    async def telegram_confirmation_code(self, item_id: int) -> dict[str, Any]:
        """GET /{item_id}/telegram-login-code — код входа."""
        return await self.request("GET", f"/{item_id}/telegram-login-code")

    async def telegram_reset_auth(self, item_id: int) -> dict[str, Any]:
        """POST /{item_id}/telegram-reset-authorizations — сбросить прочие сессии."""
        try:
            return await self.request("POST", f"/{item_id}/telegram-reset-authorizations")
        except LztApiError as exc:
            # The live API has returned the successful confirmation
            # «Изменения сохранены» through an error-shaped field.  The action
            # is already complete and must not be shown to the buyer as failed.
            normalized = {str(value).strip().casefold().rstrip(".!") for value in exc.errors}
            if normalized and normalized <= RESET_SUCCESS_MESSAGES:
                return {"message": "; ".join(exc.errors), "status": "ok"}
            raise


# --------------------------------------------------------------------------- #
#  Разбор ответов
# --------------------------------------------------------------------------- #
# Карточка лота приходит по-разному: GET /{item_id}, fast-buy и confirm-buy
# кладут её в ключ item, поиск отдаёт список в items. Здесь — единственное место,
# которое знает эти имена, чтобы сервисы не разбирали JSON руками.


@dataclass(frozen=True, slots=True)
class Credentials:
    """Данные входа купленного номера.

    phone       — сам номер (telegram_phone), им и входят: код приходит в Telegram;
    auth_key    — ключ авторизации в hex, loginData.login. Это не логин: маркет
                  кладёт в это поле 256 байт ключа сессии (проверено на живом
                  заказе 20.08.2026). Нужен для поднятия memory-only сессии
                  Telethon или стороннего клиента;
    dc_id       — номер дата-центра, loginData.password. Тоже не пароль, а
                  дополнение к ключу;
    tg_password — облачный пароль (двухфакторка), telegram_password_value.
    """

    phone: str | None = None
    auth_key: str | None = None
    dc_id: str | None = None
    tg_password: str | None = None

    @property
    def filled(self) -> bool:
        return any((self.phone, self.auth_key, self.dc_id, self.tg_password))

    @property
    def has_session(self) -> bool:
        """Есть ли чем поднять сессию сторонним клиентом."""
        return bool(self.auth_key)


@dataclass(frozen=True, slots=True)
class Balance:
    """Один кошелёк аккаунта на маркете.

    balance_id — то, что уходит в fast-buy. У «Баланса на Маркете» он строковый
    («balance»), у остальных числовой, поэтому тип свободный.
    amount — копейки, как везде в проекте.
    """

    balance_id: int | str
    title: str
    amount: int
    kind: str | None = None

    @property
    def for_accounts(self) -> bool:
        """Целевой баланс «для покупки аккаунтов»: тратится только на лоты."""
        return self.kind == "account"


def balances_of(data: dict[str, Any]) -> list[Balance]:
    """Разобрать GET /balance/exchange.

    Ответ — два словаря from и to, где ключ равен balance_id, а значение —
    карточка кошелька. Один и тот же кошелёк встречается в обоих, поэтому
    собираем по balance_id; порядок — от большей суммы, так первым идёт тот,
    которым реально можно купить.
    """
    found: dict[str, Balance] = {}
    for side in ("from", "to"):
        block = data.get(side)
        if not isinstance(block, dict):
            continue
        for key, card in block.items():
            if not isinstance(card, dict):
                continue
            raw_id = card.get("balance_id", key)
            amount_raw = card.get("convertedBalance")
            if amount_raw is None:
                amount_raw = card.get("balance")
            try:
                amount = rub_to_kop(float(amount_raw))
            except (TypeError, ValueError):
                amount = 0
            found[str(raw_id)] = Balance(
                balance_id=raw_id,
                title=_as_str(card.get("title")) or str(raw_id),
                amount=amount,
                kind=_as_str(card.get("type")),
            )
    return sorted(found.values(), key=lambda b: b.amount, reverse=True)


def item_of(data: dict[str, Any]) -> dict[str, Any]:
    item = data.get("item")
    return item if isinstance(item, dict) else data


def credentials_of(item: dict[str, Any]) -> Credentials:
    """Достать данные входа из карточки лота.

    Полей telegram_phone и loginData нет в OpenAPI-схеме маркета — имена
    установлены по живым ответам /user/orders и /{item_id} (19–20.08.2026).
    Поэтому читаем мягко: чего нет, то None, а не исключение.

    loginData.login/password названы маркетом неудачно: внутри лежат ключ
    авторизации (hex) и номер дата-центра, а не пара логин-пароль. Даём им
    честные имена здесь, чтобы дальше по коду и на экране никто не путал.
    """
    login_data = item.get("loginData")
    auth_key = dc_id = None
    if isinstance(login_data, dict):
        auth_key = login_data.get("login") or login_data.get("username")
        dc_id = login_data.get("password")
    return Credentials(
        phone=_as_str(item.get("telegram_phone") or item.get("phone")),
        auth_key=_as_str(auth_key),
        dc_id=_as_str(dc_id or item.get("telegram_dc")),
        tg_password=_as_str(item.get("telegram_password_value")),
    )


def login_codes_of(data: dict[str, Any]) -> list[tuple[int, str]]:
    """Коды входа из ответа telegram-login-code, свежий первым.

    В OpenAPI-схеме codes описан объектом {code, date}, а живой маркет отдаёт
    список таких объектов. Принимаем оба вида: схема и практика расходятся,
    ломаться на этом магазин не должен.
    """
    raw = data.get("codes")
    entries: list[dict[str, Any]] = []
    if isinstance(raw, dict):
        entries = [raw]
    elif isinstance(raw, list):
        entries = [e for e in raw if isinstance(e, dict)]

    out: list[tuple[int, str]] = []
    for entry in entries:
        code = _as_str(entry.get("code"))
        if not code:
            continue
        date = entry.get("date")
        out.append((int(date) if isinstance(date, int | float) else 0, code))
    out.sort(key=lambda pair: pair[0], reverse=True)
    return out


def newest_login_code(data: dict[str, Any]) -> str | None:
    codes = login_codes_of(data)
    return codes[0][1] if codes else None


def mask_phone(phone: str | None) -> str:
    """Номер для логов и консоли: видно страну и хвост, середина скрыта."""
    if not phone:
        return "—"
    digits = phone.strip()
    if len(digits) <= 6:
        return digits[:2] + "…"
    return f"{digits[:4]}…{digits[-2:]}"


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


_client: LztMarket | None = None


def get_lzt() -> LztMarket:
    """Общий клиент процесса. Держит пул соединений httpx."""
    global _client
    if _client is None:
        _client = LztMarket()
    return _client


def set_lzt(client: LztMarket | None) -> None:
    """Подменить клиент. Нужно прогону без сети (app/tools/bot_smoke.py)."""
    global _client
    _client = client


async def close_lzt() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
