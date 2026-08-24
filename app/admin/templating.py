"""Шаблоны админки: окружение Jinja, фильтры, меню."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.admin.notice import pop_flash
from app.config import settings
from app.enums import (
    ORDER_STATUS_TITLES,
    PAYMENT_STATUS_TITLES,
    PROVIDER_TITLES,
    REVIEW_STATUS_TITLES,
    SENDER_TITLES,
    TICKET_STATUS_TITLES,
    TX_KIND_TITLES,
)
from app.money import fmt_int, fmt_money, fmt_pct
from app.services.reviews import mask_name, stars_line
from app.timeutil import now_local, to_local

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@dataclass(frozen=True, slots=True)
class NavItem:
    key: str
    title: str
    url: str
    ready: bool = True


# Порядок — по частоте использования, не по алфавиту.
NAV: tuple[NavItem, ...] = (
    NavItem("dashboard", "Обзор", "/"),
    NavItem("support", "Поддержка", "/support"),
    NavItem("orders", "Заказы", "/orders"),
    NavItem("replacements", "Замены", "/replacements"),
    NavItem("users", "Пользователи", "/users"),
    NavItem("payments", "Платежи", "/payments"),
    NavItem("promos", "Промокоды", "/promos"),
    NavItem("catalog", "Каталог", "/catalog"),
    NavItem("broadcasts", "Рассылки", "/broadcasts"),
    NavItem("reviews", "Отзывы", "/reviews"),
    NavItem("logs", "Логи", "/logs"),
    NavItem("settings", "Настройки", "/settings"),
)

SECTION_TITLES = {
    "system": "Система",
    "user": "Пользователи",
    "shop": "Магазин",
    "lzt": "LZT",
    "payment": "Платежи",
    "balance": "Баланс",
    "referral": "Рефералы",
    "review": "Отзывы",
    "support": "Поддержка",
    "earn": "Заработок",
    "broadcast": "Рассылки",
    "admin": "Админка",
    "webapp": "Мини-апп",
}

LEVEL_TITLES = {"info": "инфо", "warn": "внимание", "error": "ошибка"}


def f_dt(value: dt.datetime | None) -> str:
    local = to_local(value)
    return local.strftime("%d.%m.%Y %H:%M") if local else "—"


def f_time(value: dt.datetime | None) -> str:
    local = to_local(value)
    if not local:
        return "—"
    if local.date() == now_local().date():
        return local.strftime("%H:%M:%S")
    return local.strftime("%d.%m %H:%M")


templates.env.filters["money"] = fmt_money
templates.env.filters["num"] = fmt_int
templates.env.filters["dt"] = f_dt
templates.env.filters["time"] = f_time
templates.env.filters["section"] = lambda v: SECTION_TITLES.get(v, v)
templates.env.filters["level"] = lambda v: LEVEL_TITLES.get(v, v)
templates.env.filters["order_status"] = lambda v: ORDER_STATUS_TITLES.get(v, v)
templates.env.filters["pay_status"] = lambda v: PAYMENT_STATUS_TITLES.get(v, v)
templates.env.filters["provider"] = lambda v: PROVIDER_TITLES.get(v, v)
templates.env.filters["tx_kind"] = lambda v: TX_KIND_TITLES.get(v, v)
templates.env.filters["review_status"] = lambda v: REVIEW_STATUS_TITLES.get(v, v)
templates.env.filters["ticket_status"] = lambda v: TICKET_STATUS_TITLES.get(v, v)
templates.env.filters["sender"] = lambda v: SENDER_TITLES.get(v, v)
# Оценка теми же символами, что в канале: ★ и ☆, эмодзи в панели тоже нет.
templates.env.filters["stars"] = stars_line
# Ник так, как его увидят в канале: первая и последняя буква.
templates.env.filters["mask"] = mask_name
templates.env.globals["pct"] = fmt_pct
templates.env.globals["NAV"] = NAV
templates.env.globals["ENV"] = settings.env


def render(
    request: Request,
    template: str,
    context: dict[str, Any] | None = None,
    *,
    active: str = "",
    status_code: int = 200,
):
    ctx: dict[str, Any] = {
        "active": active,
        "admin": getattr(request.state, "admin", None),
        "flash": pop_flash(request),
    }
    ctx.update(context or {})
    return templates.TemplateResponse(request, template, ctx, status_code=status_code)
