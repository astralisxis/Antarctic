"""Public storefront and Telegram Mini App server.

The web layer deliberately reuses the bot's service layer. It owns only HTTP
sessions, provider authentication, and JSON/HTML presentation.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.sessions import SessionMiddleware

from app import db
from app.bot import texts
from app.config import MEDIA_DIR, settings
from app.enums import OrderStatus, PaymentStatus
from app.models import CountryOffer, Order, Payment, TicketMessage, User
from app.money import fmt_money
from app.services import catalog, leaders, orders, payments, promos, settings_store, support, users
from app.services.events import log_event

log = logging.getLogger("web")

WEB_DIR = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(WEB_DIR / "templates"))


class PurchaseBody(BaseModel):
    offer_id: int


class SupportBody(BaseModel):
    text: str = Field(min_length=1, max_length=3000)


class TelegramInitBody(BaseModel):
    init_data: str = Field(min_length=1, max_length=10000)


class TelegramWidgetBody(BaseModel):
    id: int
    first_name: str | None = None
    last_name: str | None = None
    username: str | None = None
    photo_url: str | None = None
    auth_date: int
    hash: str


class PromoBody(BaseModel):
    code: str = Field(min_length=3, max_length=32)


class TopupBody(BaseModel):
    provider: str = Field(min_length=2, max_length=24)
    amount: int = Field(gt=0)


def _secret() -> str:
    # SessionMiddleware needs an unpredictable signing key in production. The
    # application already enforces ADMIN_SECRET there; use a separate web key.
    return settings.web_secret if settings.web_secret != "change-me-web-secret" else settings.admin_secret


def _telegram_bot_token() -> str:
    if not settings.bot_token:
        raise HTTPException(503, "Telegram-вход временно недоступен.")
    return settings.bot_token


def _check_fresh(timestamp: int) -> None:
    if abs(int(time.time()) - int(timestamp)) > 86400:
        raise HTTPException(401, "Ссылка входа устарела. Откройте её заново.")


def _verify_telegram_widget(payload: dict[str, Any]) -> dict[str, Any]:
    bot_token = _telegram_bot_token()
    received = str(payload.get("hash") or "")
    if not received:
        raise HTTPException(401, "Telegram не передал подпись входа.")
    try:
        auth_date = int(payload.get("auth_date"))
        telegram_id = int(payload.get("id"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(401, "Некорректные данные Telegram.") from exc
    _check_fresh(auth_date)
    pairs = []
    for key, value in payload.items():
        if key != "hash" and value is not None:
            pairs.append(f"{key}={value}")
    check_string = "\n".join(sorted(pairs))
    secret = hashlib.sha256(bot_token.encode()).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        raise HTTPException(401, "Не удалось подтвердить вход через Telegram.")
    return {
        "id": telegram_id,
        "first_name": payload.get("first_name"),
        "last_name": payload.get("last_name"),
        "username": payload.get("username"),
        "language_code": None,
        "is_premium": False,
        "photo_url": payload.get("photo_url"),
    }


def _verify_miniapp(init_data: str) -> dict[str, Any]:
    bot_token = _telegram_bot_token()
    from urllib.parse import parse_qsl

    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received = pairs.pop("hash", "")
    if not received:
        raise HTTPException(401, "Telegram Mini App не передал подпись.")
    try:
        _check_fresh(int(pairs.get("auth_date", "0")))
        tg_user = json.loads(pairs.get("user", "{}"))
        telegram_id = int(tg_user["id"])
    except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise HTTPException(401, "Некорректные данные Telegram Mini App.") from exc
    check_string = "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        raise HTTPException(401, "Не удалось подтвердить Telegram Mini App.")
    return {
        "id": telegram_id,
        "first_name": tg_user.get("first_name"),
        "last_name": tg_user.get("last_name"),
        "username": tg_user.get("username"),
        "language_code": tg_user.get("language_code"),
        "is_premium": bool(tg_user.get("is_premium")),
        "photo_url": tg_user.get("photo_url"),
    }


async def _login_telegram(session: AsyncSession, data: dict[str, Any]) -> User:
    user, _ = await users.touch(
        session,
        tg_id=int(data["id"]),
        username=data.get("username"),
        first_name=data.get("first_name"),
        last_name=data.get("last_name"),
        language_code=data.get("language_code"),
        is_premium=bool(data.get("is_premium")),
    )
    user.auth_provider = "telegram"
    user.auth_subject = str(user.tg_id)
    if data.get("photo_url"):
        user.avatar_url = str(data["photo_url"])[:500]
    await session.commit()
    return user


def _google_synthetic_tg_id(subject: str) -> int:
    number = int(hashlib.sha256(subject.encode()).hexdigest()[:15], 16)
    return -(number or 1)


async def _login_google(session: AsyncSession, profile: dict[str, Any]) -> User:
    subject = str(profile.get("sub") or "").strip()
    if not subject:
        raise HTTPException(401, "Google не передал идентификатор пользователя.")
    user = await session.scalar(
        select(User).where(User.auth_provider == "google", User.auth_subject == subject)
    )
    if user is None:
        user = User(
            tg_id=_google_synthetic_tg_id(subject),
            auth_provider="google",
            auth_subject=subject,
            email=str(profile.get("email") or "")[:255] or None,
            first_name=str(profile.get("given_name") or profile.get("name") or "Google")[:128],
            last_name=str(profile.get("family_name") or "")[:128] or None,
            username=None,
            avatar_url=str(profile.get("picture") or "")[:500] or None,
            last_seen_at=None,
        )
        session.add(user)
        await session.flush()
        await log_event(
            "user",
            "web_google_login",
            user_id=user.id,
            message=f"Google {user.email or subject}",
            session=session,
        )
    else:
        user.email = str(profile.get("email") or user.email or "")[:255] or None
        user.first_name = str(profile.get("given_name") or user.first_name or "Google")[:128]
        user.last_name = str(profile.get("family_name") or user.last_name or "")[:128] or None
        user.avatar_url = str(profile.get("picture") or user.avatar_url or "")[:500] or None
    await session.commit()
    return user


async def current_user(request: Request, session: AsyncSession) -> User | None:
    user_id = request.session.get("user_id")
    if not isinstance(user_id, int):
        return None
    return await session.get(User, user_id)


async def require_user(request: Request, session: AsyncSession) -> User:
    user = await current_user(request, session)
    if user is None:
        raise HTTPException(401, "Войдите, чтобы продолжить.")
    if await users.unban_if_expired(session, user):
        await session.commit()
    return user


def _user_payload(user: User | None, *, guest: bool = False) -> dict[str, Any]:
    if user is None:
        return {"authenticated": False, "guest": guest}
    return {
        "authenticated": True,
        "guest": False,
        "id": user.id,
        "name": user.display_name,
        "first_name": user.first_name,
        "username": user.username,
        "email": user.email,
        "avatar_url": user.avatar_url,
        "provider": user.auth_provider or "telegram",
        "balance": user.balance,
        "balance_text": fmt_money(user.balance),
        "orders_count": user.orders_count,
        "is_banned": user.is_banned,
    }


def _offer_payload(offer: CountryOffer, *, sold: int = 0) -> dict[str, Any]:
    title = re.sub(r"[\U0001F1E6-\U0001F1FF\uFE0F]", "", offer.title).strip()
    return {
        "id": offer.id,
        "code": offer.code,
        "title": title,
        "price": offer.price,
        "price_text": fmt_money(offer.price),
        "stock": offer.stock_cached,
        "stock_text": catalog.stock_label(offer),
        "description": offer.description or "",
        "guarantee_hours": offer.guarantee_hours or 12,
        "sold": sold,
        "created_at": offer.created_at.isoformat() if offer.created_at else None,
    }


def _order_payload(order: Order) -> dict[str, Any]:
    creds = orders.credentials(order)
    return {
        "id": order.id,
        "title": order.offer_title,
        "phone": creds.phone,
        "tg_password": creds.tg_password,
        "status": order.status,
        "status_text": {
            "purchased": "Активен",
            "code_issued": "Код выдан",
            "done": "Завершён",
            "refunded": "Возврат",
            "failed": "Не выдан",
        }.get(order.status, order.status),
        "price": order.price,
        "price_text": fmt_money(order.price),
        "code": order.login_code,
        "code_until": orders.code_until(order, order.guarantee_hours or 12).isoformat()
        if orders.code_until(order, order.guarantee_hours or 12)
        else None,
        "account_valid": order.account_valid,
        "replacement_status": order.replacement_status,
        "can_replace": orders.replacement_open(order),
        "can_code": orders.code_open(order, order.guarantee_hours or 12),
    }


async def _orders_for_user(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    rows = await orders.for_user(session, user, limit=50)
    return [_order_payload(row) for row in rows]


app = FastAPI(title="Antarctic Shop", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=_secret(),
    session_cookie="web_session",
    max_age=30 * 24 * 60 * 60,
    same_site="lax",
    https_only=(
        settings.env in {"prod", "production"}
        or urlparse(settings.web_base_url).scheme.lower() == "https"
    ),
)
app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="web-static")
app.mount("/media", StaticFiles(directory=str(MEDIA_DIR)), name="media")


@app.on_event("startup")
async def startup() -> None:
    if settings.db_auto_create:
        await db.create_all()
    async with db.session_scope() as session:
        await settings_store.ensure_defaults(session)
    log.info("web app on http://%s:%s", settings.web_host, settings.web_port)


@app.on_event("shutdown")
async def shutdown() -> None:
    await db.dispose()


@app.get("/", response_class=HTMLResponse)
async def landing(request: Request, session: AsyncSession = Depends(db.get_session)):
    buyers = int(
        await session.scalar(select(func.count()).select_from(User).where(User.orders_count > 0)) or 0
    )
    sold = int(await session.scalar(select(func.sum(User.orders_count))) or 0)
    all_offers = list(
        (
            await session.scalars(
                select(CountryOffer)
                .where(CountryOffer.is_active.is_(True))
                .order_by(CountryOffer.sort, CountryOffer.title)
            )
        ).all()
    )
    return TEMPLATES.TemplateResponse(
        request,
        "landing.html",
        {
            "stats": {"buyers": buyers, "sold": sold, "offers_count": len(all_offers)},
        },
    )


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return TEMPLATES.TemplateResponse(
        request,
        "login.html",
        {
            "bot_username": settings.bot_username.lstrip("@"),
            "google_enabled": bool(settings.google_client_id and settings.google_client_secret),
            "error": request.query_params.get("error"),
        },
    )


@app.get("/shop", response_class=HTMLResponse)
@app.get("/support", response_class=HTMLResponse)
@app.get("/profile", response_class=HTMLResponse)
@app.get("/topup", response_class=HTMLResponse)
@app.get("/referrals", response_class=HTMLResponse)
@app.get("/leaders", response_class=HTMLResponse)
@app.get("/promos", response_class=HTMLResponse)
@app.get("/app", response_class=HTMLResponse)
async def app_shell(request: Request):
    mode = "miniapp" if request.url.path == "/app" else "site"
    return TEMPLATES.TemplateResponse(request, "app.html", {"mode": mode})


@app.post("/auth/guest")
async def guest_login(request: Request):
    request.session.clear()
    request.session["guest"] = True
    return {"ok": True, "redirect": "/shop"}


@app.post("/auth/logout")
async def logout(request: Request):
    request.session.clear()
    return {"ok": True, "redirect": "/"}


@app.post("/auth/telegram/widget")
async def telegram_widget(payload: TelegramWidgetBody, request: Request, session: AsyncSession = Depends(db.get_session)):
    data = _verify_telegram_widget(payload.model_dump())
    user = await _login_telegram(session, data)
    request.session.clear()
    request.session["user_id"] = user.id
    request.session["auth_provider"] = "telegram"
    return {"ok": True, "redirect": "/shop"}


@app.post("/api/auth/miniapp")
async def miniapp_login(payload: TelegramInitBody, request: Request, session: AsyncSession = Depends(db.get_session)):
    data = _verify_miniapp(payload.init_data)
    user = await _login_telegram(session, data)
    request.session.clear()
    request.session["user_id"] = user.id
    request.session["auth_provider"] = "telegram"
    return {"ok": True, "user": _user_payload(user)}


@app.get("/auth/google")
async def google_login(request: Request):
    if not settings.google_client_id or not settings.google_client_secret:
        return RedirectResponse("/login?error=google_disabled", status_code=303)
    redirect_uri = settings.google_redirect_uri or f"{settings.web_base_url}/auth/google/callback"
    state = secrets.token_urlsafe(24)
    request.session["google_state"] = state
    query = urlencode(
        {
            "client_id": settings.google_client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "access_type": "online",
        }
    )
    return RedirectResponse(f"https://accounts.google.com/o/oauth2/v2/auth?{query}", status_code=303)


@app.get("/auth/google/callback")
async def google_callback(request: Request, session: AsyncSession = Depends(db.get_session)):
    if request.query_params.get("state") != request.session.pop("google_state", None):
        return RedirectResponse("/login?error=google_state", status_code=303)
    code = request.query_params.get("code")
    if not code or not settings.google_client_id or not settings.google_client_secret:
        return RedirectResponse("/login?error=google_failed", status_code=303)
    redirect_uri = settings.google_redirect_uri or f"{settings.web_base_url}/auth/google/callback"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            token = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code": code,
                    "client_id": settings.google_client_id,
                    "client_secret": settings.google_client_secret,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
            token.raise_for_status()
            access_token = token.json()["access_token"]
            profile_response = await client.get(
                "https://openidconnect.googleapis.com/v1/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            profile_response.raise_for_status()
            profile = profile_response.json()
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        log.warning("Google login failed: %s", exc)
        return RedirectResponse("/login?error=google_failed", status_code=303)
    user = await _login_google(session, profile)
    request.session.clear()
    request.session["user_id"] = user.id
    request.session["auth_provider"] = "google"
    return RedirectResponse("/shop", status_code=303)


@app.get("/api/me")
async def me(request: Request, session: AsyncSession = Depends(db.get_session)):
    user = await current_user(request, session)
    return _user_payload(user, guest=bool(request.session.get("guest")))


@app.get("/api/catalog")
async def api_catalog(session: AsyncSession = Depends(db.get_session)):
    rows = list(
        (
            await session.scalars(
                select(CountryOffer)
                .where(CountryOffer.is_active.is_(True))
                .order_by(CountryOffer.sort, CountryOffer.title)
            )
        ).all()
    )
    sold = dict(
        (
            await session.execute(
                select(Order.offer_id, func.count())
                .where(Order.status.in_(orders.DELIVERED))
                .group_by(Order.offer_id)
            )
        ).all()
    )
    return {"items": [_offer_payload(row, sold=int(sold.get(row.id, 0))) for row in rows]}


@app.get("/api/catalog/{offer_id}")
async def api_offer(offer_id: int, session: AsyncSession = Depends(db.get_session)):
    offer = await session.get(CountryOffer, offer_id)
    if offer is None or not offer.is_active:
        raise HTTPException(404, "Страна не найдена.")
    return _offer_payload(offer)


@app.get("/api/profile")
async def api_profile(request: Request, session: AsyncSession = Depends(db.get_session)):
    user = await require_user(request, session)
    return {
        "user": _user_payload(user),
        "orders": await _orders_for_user(session, user),
        "referral": {
            "link": users.ref_link(user),
            "invited": await users.referrals_count(session, user),
            "earned": user.ref_earned,
            "earned_text": fmt_money(user.ref_earned),
            "percent": await users.percent_for(session, user),
        },
        "leader_position": await leaders.position(session, user),
    }


@app.get("/api/leaders")
async def api_leaders(request: Request, session: AsyncSession = Depends(db.get_session)):
    user = await current_user(request, session)
    rows = await leaders.top(session, limit=25)
    return {
        "items": [
            {
                "position": index,
                "name": row.display_name,
                "orders": row.orders_count,
                "spent_text": fmt_money(row.total_spent),
                "is_me": bool(user and row.id == user.id),
            }
            for index, row in enumerate(rows, 1)
        ],
        "my_position": await leaders.position(session, user) if user else None,
    }


@app.get("/api/promos")
async def api_promos(request: Request, session: AsyncSession = Depends(db.get_session)):
    user = await require_user(request, session)
    rows = await promos.redemptions_for(session, user)
    return {
        "items": [
            {
                "code": promo.code,
                "title": promo.title,
                "bonus_text": fmt_money(redemption.bonus),
                "created_at": redemption.created_at.isoformat() if redemption.created_at else None,
            }
            for redemption, promo in rows
        ]
    }


@app.post("/api/promos/redeem")
async def api_redeem_promo(
    body: PromoBody, request: Request, session: AsyncSession = Depends(db.get_session)
):
    user = await require_user(request, session)
    try:
        promo = await promos.redeem(session, user, body.code)
    except promos.PromoError as exc:
        raise HTTPException(400, str(exc)) from exc
    await session.refresh(user, ["balance"])
    return {
        "ok": True,
        "bonus_text": fmt_money(promo.bonus),
        "balance": user.balance,
        "balance_text": fmt_money(user.balance),
    }


@app.get("/api/topup")
async def api_topup(request: Request, session: AsyncSession = Depends(db.get_session)):
    user = await require_user(request, session)
    low, high = await payments.limits(session)
    ways = await payments.methods(session)
    invoices = await payments.open_invoices(session, user)
    return {
        "minimum": low,
        "maximum": high,
        "minimum_text": fmt_money(low),
        "maximum_text": fmt_money(high),
        "methods": [
            {"provider": method.provider, "title": method.title, "hint": method.hint}
            for method in ways
        ],
        "invoices": [
            {
                "id": invoice.id,
                "provider": invoice.provider,
                "amount_text": fmt_money(invoice.amount),
                "url": invoice.invoice_url,
                "status": invoice.status,
            }
            for invoice in invoices
        ],
    }


@app.post("/api/topup/invoices")
async def api_create_topup(
    body: TopupBody, request: Request, session: AsyncSession = Depends(db.get_session)
):
    user = await require_user(request, session)
    try:
        payment, quote = await payments.create(session, user, body.provider, body.amount)
        await session.commit()
    except payments.PaymentError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "id": payment.id,
        "url": payment.invoice_url,
        "amount_text": fmt_money(payment.amount),
        "charge_text": quote.charge_text,
        "provider": payment.provider,
    }


async def _owned_payment(session: AsyncSession, payment_id: int, user: User) -> Payment:
    payment = await session.get(Payment, payment_id)
    if payment is None or payment.user_id != user.id:
        raise HTTPException(404, "Счёт не найден.")
    return payment


@app.post("/api/topup/invoices/{payment_id}/check")
async def api_check_topup(
    payment_id: int, request: Request, session: AsyncSession = Depends(db.get_session)
):
    user = await require_user(request, session)
    payment = await _owned_payment(session, payment_id, user)
    if payment.status == PaymentStatus.PENDING.value:
        try:
            await payments.refresh(session, payment)
        except Exception as exc:
            log.warning("web payment check failed: %s", exc)
            raise HTTPException(502, "Платёжный сервис не ответил. Попробуйте ещё раз.") from exc
    await session.refresh(user, ["balance"])
    return {"status": payment.status, "balance": user.balance, "balance_text": fmt_money(user.balance)}


@app.post("/api/topup/invoices/{payment_id}/cancel")
async def api_cancel_topup(
    payment_id: int, request: Request, session: AsyncSession = Depends(db.get_session)
):
    user = await require_user(request, session)
    payment = await _owned_payment(session, payment_id, user)
    if payment.status != PaymentStatus.PENDING.value:
        return {"ok": True, "status": payment.status}
    if not await payments.cancel(session, payment):
        raise HTTPException(502, "Не удалось подтвердить отмену счёта.")
    return {"ok": True, "status": payment.status}


@app.get("/api/orders")
async def api_orders(request: Request, session: AsyncSession = Depends(db.get_session)):
    user = await require_user(request, session)
    return {"items": await _orders_for_user(session, user)}


@app.post("/api/orders")
async def api_buy(body: PurchaseBody, request: Request, session: AsyncSession = Depends(db.get_session)):
    user = await require_user(request, session)
    offer = await session.get(CountryOffer, body.offer_id)
    if offer is None or not offer.is_active:
        raise HTTPException(404, "Страна не найдена.")
    try:
        order = await orders.buy(session, user, offer, source="web")
    except orders.OrderError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"order": _order_payload(order)}


@app.post("/api/orders/{order_id}/code")
async def api_code(order_id: int, request: Request, session: AsyncSession = Depends(db.get_session)):
    user = await require_user(request, session)
    order = await orders.get_owned(session, order_id, user)
    if order is None:
        raise HTTPException(404, "Заказ не найден.")
    try:
        code = await orders.issue_code(session, order)
    except orders.OrderError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"code": code, "order": _order_payload(order)}


@app.post("/api/orders/{order_id}/replacement")
async def api_replacement(order_id: int, request: Request, session: AsyncSession = Depends(db.get_session)):
    user = await require_user(request, session)
    order = await orders.get_owned(session, order_id, user)
    if order is None:
        raise HTTPException(404, "Заказ не найден.")
    try:
        await orders.request_replacement(session, order)
    except orders.OrderError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "order": _order_payload(order)}


@app.get("/api/support")
async def api_support(request: Request, session: AsyncSession = Depends(db.get_session)):
    result: dict[str, Any] = {
        "hours": await support.hours(session),
        "logged_in": False,
        "messages": [],
    }
    user = await current_user(request, session)
    if user is None:
        return result
    ticket = await support.active(session, user)
    if ticket is None:
        return {**result, "logged_in": True, "ticket": None}
    messages = await support.thread(session, ticket)
    if ticket.unread_user:
        ticket.unread_user = 0
        await session.commit()
    return {
        **result,
        "logged_in": True,
        "ticket": {"id": ticket.id, "status": ticket.status},
        "messages": [
            {
                "sender": row.sender,
                "text": row.text or "",
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in messages
        ],
    }


@app.post("/api/support")
async def api_support_message(
    body: SupportBody, request: Request, session: AsyncSession = Depends(db.get_session)
):
    user = await require_user(request, session)
    try:
        ticket, _ = await support.open_ticket(session, user, subject=body.text[:160])
        await support.add_user_message(session, ticket, text=body.text)
    except support.SupportError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/favicon.ico")
async def favicon() -> Response:
    # A blank response keeps browsers from logging a noisy 404 on every page.
    return Response(status_code=204)


def main() -> None:
    import uvicorn

    uvicorn.run(
        "app.web.main:app",
        host=settings.web_host,
        port=settings.web_port,
        reload=settings.debug,
        log_config=None,
    )


if __name__ == "__main__":
    main()
