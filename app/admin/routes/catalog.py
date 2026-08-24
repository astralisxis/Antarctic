"""Каталог: страны, цены, лимит закупки, фильтры лотов, наличие.

Цену покупателю задаёт админ, лот бот подбирает сам в пределах лимита закупки.
Поэтому в списке видно и то и другое: цена, лимит и минимальная маржа.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select, update

from app.admin.counters import nav_counts
from app.admin.deps import DbSession, StaffAdmin
from app.admin.notice import flash
from app.admin.templating import render
from app.countries import COUNTRIES, name_for, title_for
from app.enums import LOT_ORIGIN_TITLES, LogSection, YesNo
from app.models import CountryOffer, Order
from app.money import fmt_money, parse_rub, rub_input
from app.services import catalog as catalog_service
from app.services import market
from app.services.events import log_event
from app.services.orders import DELIVERED

router = APIRouter()

YESNO = [y.value for y in YesNo]
YESNO_TITLES = {"yes": "только со спамом", "no": "без спама", "nomatter": "не важно"}
PASS_TITLES = {"yes": "с паролем", "no": "без пароля", "nomatter": "не важно"}


# --------------------------------------------------------------------------- #
#  Список
# --------------------------------------------------------------------------- #
@router.get("/catalog")
async def index(request: Request, session: DbSession, admin: StaffAdmin):
    offers = list(
        (
            await session.scalars(
                select(CountryOffer).order_by(CountryOffer.sort, CountryOffer.title)
            )
        ).all()
    )
    # Сколько продано по каждой стране — одним запросом, а не по строке.
    sold = dict(
        (
            await session.execute(
                select(Order.offer_id, func.count())
                .where(Order.status.in_(DELIVERED))
                .group_by(Order.offer_id)
            )
        ).all()
    )
    rows = [
        {
            "o": o,
            "stock": catalog_service.stock_label(o),
            "sold": sold.get(o.id, 0),
            "margin": o.price - o.buy_limit,
        }
        for o in offers
    ]
    return render(
        request,
        "catalog.html",
        {"rows": rows, "counts": await nav_counts(session)},
        active="catalog",
    )


# --------------------------------------------------------------------------- #
#  Форма
# --------------------------------------------------------------------------- #
def _blank() -> dict[str, Any]:
    return {
        "id": None,
        "code": "",
        "title": "",
        "lzt_country": "",
        "price": "",
        "buy_limit": "",
        "sort": "100",
        "is_active": True,
        "spam_filter": YesNo.NO.value,
        "password_filter": YesNo.NOMATTER.value,
        "origin_filter": [],
        "extra_filters": "",
        "description": "",
        "guarantee_hours": "12",
    }


def _from_offer(offer: CountryOffer) -> dict[str, Any]:
    return {
        "id": offer.id,
        "code": offer.code,
        "title": offer.title,
        "lzt_country": offer.lzt_country,
        "price": rub_input(offer.price),
        "buy_limit": rub_input(offer.buy_limit),
        "sort": str(offer.sort),
        "is_active": offer.is_active,
        "spam_filter": offer.spam_filter,
        "password_filter": offer.password_filter,
        "origin_filter": list(offer.origin_filter or []),
        "extra_filters": json.dumps(offer.extra_filters, ensure_ascii=False)
        if offer.extra_filters
        else "",
        "description": offer.description or "",
        "guarantee_hours": str(offer.guarantee_hours or 12),
    }


def _from_form(form: Any) -> dict[str, Any]:
    draft = _blank()
    for key in draft:
        if key in ("id", "is_active", "origin_filter"):
            continue
        draft[key] = str(form.get(key) or "").strip()
    draft["is_active"] = bool(form.get("is_active"))
    # Источники — галочки, значений может быть несколько.
    draft["origin_filter"] = _origins(form.getlist("origin_filter"))
    return draft


def _origins(values: Any) -> list[str]:
    """Только внятные значения origin[] и без повторов — порядок сохраняем."""
    picked: list[str] = []
    for raw in values:
        value = str(raw or "").strip().lower()
        if value and value.replace("_", "").isalnum() and value not in picked:
            picked.append(value)
    return picked[:12]


def _origin_options(picked: list[str]) -> list[dict[str, Any]]:
    """Галочки источников: перечень маркета плюс всё непонятное, что уже сохранено."""
    options = [
        {"value": value, "title": title, "on": value in picked}
        for value, title in LOT_ORIGIN_TITLES.items()
    ]
    options += [
        {"value": value, "title": value, "on": True}
        for value in picked
        if value not in LOT_ORIGIN_TITLES
    ]
    return options


def _validate(draft: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """Разобранные значения или текст ошибки. Ошибку показываем над формой."""
    code = draft["code"].upper()
    if not code or len(code) > 8 or not code.isalnum():
        return None, "Код страны — до восьми букв или цифр, например RU."
    if not draft["title"] or len(draft["title"]) > 64:
        return None, "Название нужно, не длиннее 64 символов."
    if not draft["lzt_country"] or len(draft["lzt_country"]) > 64:
        return None, "Код страны для LZT нужен: его значения отдаёт GET /telegram/params."

    price = parse_rub(draft["price"])
    if not price or price <= 0:
        return None, "Цена для покупателя должна быть больше нуля."
    limit = parse_rub(draft["buy_limit"])
    if not limit or limit <= 0:
        return None, "Лимит закупки должен быть больше нуля."

    try:
        sort = int(draft["sort"] or 100)
    except ValueError:
        return None, "Порядок — целое число."

    try:
        guarantee_hours = int(draft["guarantee_hours"] or 12)
    except ValueError:
        return None, "Гарантия — целое число часов."
    if not 1 <= guarantee_hours <= 720:
        return None, "Гарантия должна быть от 1 до 720 часов."

    if draft["spam_filter"] not in YESNO or draft["password_filter"] not in YESNO:
        return None, "Фильтры принимают только yes, no или nomatter."

    origin = _origins(draft["origin_filter"])

    extra: dict[str, Any] | None = None
    if draft["extra_filters"]:
        try:
            parsed = json.loads(draft["extra_filters"])
        except ValueError:
            return None, "Доп. параметры — это JSON-объект, например {\"telegram_dc\": 2}."
        if not isinstance(parsed, dict):
            return None, "Доп. параметры должны быть объектом, а не списком или числом."
        extra = parsed

    return {
        "code": code,
        "title": draft["title"],
        "lzt_country": draft["lzt_country"].upper(),
        "price": price,
        "buy_limit": limit,
        "sort": sort,
        "is_active": draft["is_active"],
        "spam_filter": draft["spam_filter"],
        "password_filter": draft["password_filter"],
        "origin_filter": origin or None,
        "extra_filters": extra,
        "description": draft["description"][:255] or None,
        "guarantee_hours": guarantee_hours,
    }, ""


def _form_page(request: Request, draft: dict[str, Any], counts: dict[str, int], error: str = ""):
    return render(
        request,
        "catalog_form.html",
        {
            "d": draft,
            "error": error,
            "counts": counts,
            "yesno": YESNO,
            "spam_titles": YESNO_TITLES,
            "pass_titles": PASS_TITLES,
            "origins": _origin_options(draft["origin_filter"]),
            # Подсказка для полей кода: перечня стран у маркета нет, берём свой.
            "countries": COUNTRIES,
        },
        active="catalog",
        status_code=400 if error else 200,
    )


@router.get("/catalog/new")
async def new_form(
    request: Request, session: DbSession, admin: StaffAdmin, code: str = "", limit: str = ""
):
    """Пустая форма или заполненная под страну — так со страницы стран приходят сюда."""
    draft = _blank()
    code = code.strip().upper()[:8]
    if code:
        draft["code"] = code
        draft["title"] = title_for(code)
        draft["lzt_country"] = code
        stat = await market.get(session, code)
        suggested = parse_rub(limit) or market.suggest_limit(stat)
        if suggested:
            draft["buy_limit"] = rub_input(suggested)
    return _form_page(request, draft, await nav_counts(session))


@router.post("/catalog/new")
async def create(request: Request, session: DbSession, admin: StaffAdmin):
    draft = _from_form(await request.form())
    values, error = _validate(draft)
    if values is None:
        return _form_page(request, draft, await nav_counts(session), error)

    taken = await session.scalar(
        select(CountryOffer.id).where(CountryOffer.code == values["code"])
    )
    if taken:
        return _form_page(
            request, draft, await nav_counts(session), f"Код {values['code']} уже занят."
        )

    offer = CountryOffer(**values)
    session.add(offer)
    await session.flush()
    await log_event(
        LogSection.ADMIN,
        "offer_created",
        admin_id=admin.id,
        message=f"{offer.code} {offer.title}",
        session=session,
    )
    await session.commit()
    flash(request, f"Позиция {offer.title} создана. Наличие проверится в ближайшем обходе.")
    return RedirectResponse("/catalog", status_code=303)


# --------------------------------------------------------------------------- #
#  Действия
# --------------------------------------------------------------------------- #
# Путь с буквальным «refresh» объявлен до /catalog/{offer_id}: маршруты
# разбираются по порядку, иначе «refresh» уедет в offer_id и вернётся 422.
@router.post("/catalog/refresh")
async def refresh_all(request: Request, session: DbSession, admin: StaffAdmin):
    result = await catalog_service.refresh_all(session)
    await session.commit()
    if not result:
        flash(request, "Активных позиций нет — обновлять нечего.", ok=False)
    else:
        parts = [f"{code}: {value if value is not None else 'нет ответа'}" for code, value in result.items()]
        flash(request, "Наличие обновлено. " + ", ".join(parts))
    await log_event(
        LogSection.ADMIN, "stock_refresh", admin_id=admin.id, message=f"позиций: {len(result)}"
    )
    return RedirectResponse("/catalog", status_code=303)


# --------------------------------------------------------------------------- #
#  Страны маркета
# --------------------------------------------------------------------------- #
# «market» — тоже буквальный путь, и он тоже объявлен до /catalog/{offer_id}.
def _market_back(only: str) -> RedirectResponse:
    """Назад к тому же виду таблицы: «только с лотами» после действия не сбрасываем."""
    return RedirectResponse(
        "/catalog/market" + ("?only=all" if only == "all" else ""), status_code=303
    )


@router.get("/catalog/market")
async def market_page(
    request: Request, session: DbSession, admin: StaffAdmin, only: str = "lots"
):
    """Что вообще есть на маркете: лоты и цены по странам.

    Цифры — из базы, их пишет обход. Живьём страницу не считаем: один запрос на
    страну, на весь список это около минуты.
    """
    rows = await market.rows(session, only_lots=only == "lots")
    scan = market.state()
    return render(
        request,
        "catalog_market.html",
        {
            "rows": rows,
            "scan": scan,
            "only": only,
            "sample_limit": market.SAMPLE_LIMIT,
            "total_countries": len(market.codes_all()),
            "counts": await nav_counts(session),
        },
        active="catalog",
    )


@router.post("/catalog/market/scan")
async def market_scan(request: Request, session: DbSession, admin: StaffAdmin):
    """Запустить обход фоном. Синхронно нельзя: минута ожидания в браузере."""
    form = await request.form()
    only = str(form.get("only") or "")
    pmax = parse_rub(str(form.get("pmax") or "")) or None
    spam = str(form.get("spam") or YesNo.NO.value)
    if spam not in YESNO:
        spam = YesNo.NO.value

    codes = market.codes_all()
    if str(form.get("scope") or "") == "catalog":
        codes = [
            (c or "").upper()
            for c in (
                await session.scalars(
                    select(CountryOffer.lzt_country).order_by(CountryOffer.sort)
                )
            ).all()
            if c
        ]
    if not codes:
        flash(request, "Обходить нечего: в каталоге нет ни одной позиции.", ok=False)
        return _market_back(only)

    if not market.start(codes, pmax=pmax, spam=spam):
        flash(request, "Обход уже идёт — дождитесь конца.", ok=False)
        return _market_back(only)

    await log_event(
        LogSection.ADMIN,
        "market_scan_started",
        admin_id=admin.id,
        message=f"стран: {len(codes)}, потолок: {pmax if pmax else 'без'}, спам: {spam}",
    )
    seconds = round(len(codes) * (market.SCAN_PAUSE + 0.6))
    flash(
        request,
        f"Обход запущен: {len(codes)} стран, это примерно {seconds} с. "
        "Обновите страницу, чтобы увидеть цифры.",
    )
    return _market_back(only)


@router.post("/catalog/market/{code}")
async def market_check_one(
    request: Request, session: DbSession, admin: StaffAdmin, code: str
):
    """Одна страна — быстрый запрос, ждать можно прямо в браузере."""
    form = await request.form()
    stat = await market.check(session, code)
    await session.commit()
    if stat.error:
        flash(request, f"{name_for(code)}: маркет не ответил — {stat.error}", ok=False)
    elif not stat.lots:
        flash(request, f"{name_for(code)}: лотов по этому фильтру нет.")
    else:
        flash(
            request,
            f"{name_for(code)}: лотов {stat.lots}, "
            f"от {fmt_money(stat.price_min)}, средняя по выборке {fmt_money(stat.price_avg)}.",
        )
    return _market_back(str(form.get("only") or ""))


@router.get("/catalog/{offer_id}")
async def edit_form(request: Request, session: DbSession, admin: StaffAdmin, offer_id: int):
    offer = await session.get(CountryOffer, offer_id)
    if offer is None:
        return RedirectResponse("/catalog", status_code=303)
    return _form_page(request, _from_offer(offer), await nav_counts(session))


@router.post("/catalog/{offer_id}")
async def save(request: Request, session: DbSession, admin: StaffAdmin, offer_id: int):
    """Сохранить правку. Имя не update: под этим именем в модуле живёт sqlalchemy."""
    offer = await session.get(CountryOffer, offer_id)
    if offer is None:
        return RedirectResponse("/catalog", status_code=303)

    draft = _from_form(await request.form())
    draft["id"] = offer.id
    values, error = _validate(draft)
    if values is None:
        return _form_page(request, draft, await nav_counts(session), error)

    taken = await session.scalar(
        select(CountryOffer.id).where(
            CountryOffer.code == values["code"], CountryOffer.id != offer.id
        )
    )
    if taken:
        return _form_page(
            request, draft, await nav_counts(session), f"Код {values['code']} уже занят."
        )

    changed = [k for k, v in values.items() if getattr(offer, k) != v]
    for key, value in values.items():
        setattr(offer, key, value)
    # Фильтры поменялись — прошлое наличие уже не про этот запрос.
    if {"lzt_country", "buy_limit", "spam_filter", "password_filter", "origin_filter",
            "extra_filters"} & set(changed):
        offer.stock_cached = None
        offer.stock_checked_at = None

    await log_event(
        LogSection.ADMIN,
        "offer_updated",
        admin_id=admin.id,
        message=f"{offer.code}: {', '.join(changed) or 'без изменений'}",
        session=session,
    )
    await session.commit()
    flash(request, "Сохранено.")
    return RedirectResponse("/catalog", status_code=303)


# --------------------------------------------------------------------------- #
#  Действия
# --------------------------------------------------------------------------- #
@router.post("/catalog/{offer_id}/refresh")
async def refresh_one(request: Request, session: DbSession, admin: StaffAdmin, offer_id: int):
    offer = await session.get(CountryOffer, offer_id)
    if offer is None:
        return RedirectResponse("/catalog", status_code=303)
    value = await catalog_service.refresh_stock(session, offer)
    await session.commit()
    if value is None:
        flash(request, f"{offer.title}: маркет не ответил, прошлое наличие оставили.", ok=False)
    else:
        flash(request, f"{offer.title}: {catalog_service.stock_label(offer)}.")
    return RedirectResponse("/catalog", status_code=303)


@router.post("/catalog/{offer_id}/toggle")
async def toggle(request: Request, session: DbSession, admin: StaffAdmin, offer_id: int):
    offer = await session.get(CountryOffer, offer_id)
    if offer is None:
        return RedirectResponse("/catalog", status_code=303)
    offer.is_active = not offer.is_active
    await log_event(
        LogSection.ADMIN,
        "offer_toggled",
        admin_id=admin.id,
        message=f"{offer.code}: {'включена' if offer.is_active else 'скрыта'}",
        session=session,
    )
    await session.commit()
    flash(request, f"{offer.title}: {'в витрине' if offer.is_active else 'скрыта из витрины'}.")
    return RedirectResponse("/catalog", status_code=303)


# --------------------------------------------------------------------------- #
#  Удаление
# --------------------------------------------------------------------------- #
@router.get("/catalog/{offer_id}/delete")
async def delete_form(request: Request, session: DbSession, admin: StaffAdmin, offer_id: int):
    """Страница подтверждения: показываем, что именно исчезнет."""
    offer = await session.get(CountryOffer, offer_id)
    if offer is None:
        return RedirectResponse("/catalog", status_code=303)

    sold = int(
        await session.scalar(
            select(func.count()).select_from(Order).where(Order.offer_id == offer.id)
        )
        or 0
    )
    return render(
        request,
        "catalog_delete.html",
        {"o": offer, "sold": sold, "counts": await nav_counts(session)},
        active="catalog",
    )


@router.post("/catalog/{offer_id}/delete")
async def delete(request: Request, session: DbSession, admin: StaffAdmin, offer_id: int):
    """Убрать позицию из каталога. История заказов остаётся.

    Заказы ссылаются на позицию, но название и код у них свои — в момент покупки
    сохраняется срез. Поэтому ссылку просто обнуляем: в sqlite внешние ключи не
    включены (см. app/db.py), и на «ON DELETE SET NULL» полагаться нельзя.
    """
    offer = await session.get(CountryOffer, offer_id)
    if offer is None:
        return RedirectResponse("/catalog", status_code=303)

    title, code = offer.title, offer.code
    detached = await session.execute(
        update(Order)
        .where(Order.offer_id == offer.id)
        .values(offer_id=None)
        .execution_options(synchronize_session=False)
    )
    await session.delete(offer)
    await log_event(
        LogSection.ADMIN,
        "offer_deleted",
        admin_id=admin.id,
        message=f"{code} {title}",
        payload={"orders_detached": detached.rowcount},
        session=session,
    )
    await session.commit()
    flash(
        request,
        f"Позиция {title} удалена."
        + (f" Заказов осталось в истории: {detached.rowcount}." if detached.rowcount else ""),
    )
    return RedirectResponse("/catalog", status_code=303)
