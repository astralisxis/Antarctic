"""Маркет-заглушка для прогонов без сети.

Отвечает на те же вызовы, что LztMarket, поэтому app/services подмены не замечают:
покупка, выдача кода и сброс сессий проверяются целиком, без денег и без интернета.
Ставится через app.integrations.lzt.set_lzt().

Используется в app/tools/bot_smoke.py и app/tools/admin_smoke.py.
"""

from __future__ import annotations

from typing import Any

from app.integrations.lzt import LztApiError


def make_lot(item_id: int, price_rub: float, phone: str, origin: str = "brute") -> dict[str, Any]:
    """Лот в том виде, в каком его отдаёт поиск: без данных входа."""
    return {
        "item_id": item_id,
        "price": price_rub,
        "price_currency": "rub",
        "item_origin": origin,
        "item_state": "active",
        "published_date": 1_780_000_000,
        "telegram_country": "ID",
        "telegram_phone": phone,
        "telegram_dc": 2,
        "telegram_spam_block": False,
        "telegram_premium": False,
    }


class FakeLzt:
    """Сценарий правится полями: lots — что лежит на маркете, gone — лоты, которые
    «ушли» между поиском и покупкой, codes — что вернёт login-code, no_money —
    закончились деньги на балансе маркета.
    """

    def __init__(self) -> None:
        self.lots: list[dict[str, Any]] = []
        self.gone: set[int] = set()
        self.sold: set[int] = set()
        self.codes: list[str] = []
        self.invalid_codes: set[int] = set()
        self.no_money = False
        self.calls: list[str] = []

    def _card(self, item_id: int) -> dict[str, Any] | None:
        for lot in self.lots:
            if lot["item_id"] == item_id:
                return lot
        return None

    def _owned(self, item_id: int) -> dict[str, Any]:
        """Карточка купленного лота: данные входа маркет отдаёт только покупателю."""
        card = dict(self._card(item_id) or {"item_id": item_id, "price": 0})
        card["loginData"] = {"login": f"+{item_id}", "password": "smoke-pass"}
        card["telegram_password_value"] = "smoke-2fa"
        return card

    # --- каталог ---
    async def accounts_search_telegram(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("search")
        pmax = kwargs.get("price_max")
        items = [
            lot
            for lot in self.lots
            if lot["item_id"] not in self.sold
            and (pmax is None or float(lot["price"]) <= float(pmax))
        ]
        return {"items": items, "totalItems": len(items)}

    async def item_get(
        self, item_id: int, parse_same_item_ids: bool | None = None
    ) -> dict[str, Any]:
        self.calls.append(f"item:{item_id}")
        if item_id in self.sold:
            return {"item": self._owned(item_id)}
        card = self._card(item_id)
        if card is None:
            raise LztApiError(["Item not found"], 404)
        return {"item": card}

    # --- покупка ---
    async def purchasing_fast_buy(
        self, item_id: int, price: float, balance_id: int | None = None
    ) -> dict[str, Any]:
        self.calls.append(f"fast-buy:{item_id}")
        if self.no_money:
            raise LztApiError(["Not enough money on your balance"], 400)
        if item_id in self.gone or item_id in self.sold:
            raise LztApiError(["Item already sold"], 400)
        self.sold.add(item_id)
        return {"item": self._owned(item_id)}

    # --- Telegram-специфика ---
    async def telegram_confirmation_code(self, item_id: int) -> dict[str, Any]:
        self.calls.append(f"code:{item_id}")
        if item_id in self.invalid_codes:
            raise LztApiError(["Telegram session is invalid"], 400)
        return {
            "codes": [
                {"code": code, "date": 1_780_000_000 + index}
                for index, code in enumerate(self.codes)
            ]
        }

    async def telegram_reset_auth(self, item_id: int) -> dict[str, Any]:
        self.calls.append(f"reset:{item_id}")
        # Именно так живой LZT подтверждает успешный сброс. Поле message на
        # HTTP 200 не является ошибкой.
        return {"message": "Изменения сохранены"}

    async def aclose(self) -> None:
        return None
