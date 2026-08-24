"""End-to-end smoke test for the public site and Telegram Mini App."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from urllib.parse import urlencode


async def main() -> None:
    project = Path(__file__).resolve().parents[2]
    smoke_db = project / "data" / "smoke_web.db"
    for suffix in ("", "-wal", "-shm"):
        path = Path(f"{smoke_db}{suffix}")
        if path.exists():
            path.unlink()

    os.environ["ENV"] = "local"
    os.environ["DEBUG"] = "false"
    os.environ["DB_AUTO_CREATE"] = "true"
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{smoke_db.as_posix()}"
    os.environ["WEB_SECRET"] = "web-smoke-secret"
    os.environ["WEB_BASE_URL"] = "https://testserver"

    import httpx
    from sqlalchemy import select

    from app import db
    from app.config import settings
    from app.integrations.lzt import set_lzt
    from app.models import CountryOffer, PromoCode, User
    from app.services import settings_store
    from app.tools.fake_market import FakeLzt, make_lot
    from app.web.main import app

    await db.create_all()
    async with db.session_scope() as session:
        await settings_store.ensure_defaults(session)
        session.add(
            CountryOffer(
                code="ID",
                title="Индонезия +62",
                lzt_country="ID",
                price=4900,
                buy_limit=2000,
                stock_cached=1,
                guarantee_hours=12,
                is_active=True,
            )
        )

    market = FakeLzt()
    market.lots = [make_lot(99001, 9.0, "+628111111111")]
    market.codes = ["31415"]
    set_lzt(market)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as guest:
        for path in ("/", "/login", "/shop", "/support", "/profile", "/app"):
            response = await guest.get(path)
            assert response.status_code == 200, (path, response.status_code, response.text)
        landing = (await guest.get("/")).text
        assert "Популярные направления" not in landing
        assert "лотов на витрине" in landing
        login_page = (await guest.get("/login")).text
        assert "Войти через Google" in login_page
        assert "Войти через Telegram" in login_page
        assert "Войти как гость" in login_page
        assert "telegram-widget.js" in login_page
        catalog = await guest.get("/api/catalog")
        assert catalog.status_code == 200 and len(catalog.json()["items"]) == 1
        denied = await guest.post("/api/orders", json={"offer_id": 1})
        assert denied.status_code == 401
        assert (await guest.post("/auth/guest")).status_code == 200
        assert (await guest.get("/api/me")).json()["guest"] is True

    tg_user = {
        "id": 990000001,
        "first_name": "Web",
        "last_name": "Smoke",
        "username": "web_smoke",
        "language_code": "ru",
    }
    pairs = {
        "auth_date": str(int(time.time())),
        "query_id": "web-smoke",
        "user": json.dumps(tg_user, separators=(",", ":"), ensure_ascii=False),
    }
    check = "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs))
    secret = hmac.new(b"WebAppData", settings.bot_token.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    init_data = urlencode(pairs)

    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
        login = await client.post("/api/auth/miniapp", json={"init_data": init_data})
        assert login.status_code == 200, login.text
        async with db.session_scope() as session:
            user = await session.scalar(select(User).where(User.tg_id == tg_user["id"]))
            assert user is not None
            user.balance = 20000

        purchase = await client.post("/api/orders", json={"offer_id": 1})
        assert purchase.status_code == 200, purchase.text
        order = purchase.json()["order"]
        assert order["phone"] == "+628111111111"

        code = await client.post(f"/api/orders/{order['id']}/code")
        assert code.status_code == 200 and code.json()["code"] == "31415", code.text

        support = await client.post("/api/support", json={"text": "Нужна помощь по заказу"})
        assert support.status_code == 200, support.text
        thread = await client.get("/api/support")
        assert thread.status_code == 200 and len(thread.json()["messages"]) == 1

        profile = await client.get("/api/profile")
        assert profile.status_code == 200 and len(profile.json()["orders"]) == 1

        leaders = await client.get("/api/leaders")
        assert leaders.status_code == 200 and leaders.json()["items"][0]["orders"] == 1

        async with db.session_scope() as session:
            session.add(PromoCode(code="WEB500", title="Web smoke", bonus=50000, max_uses=1))
        promo = await client.post("/api/promos/redeem", json={"code": "web500"})
        assert promo.status_code == 200 and promo.json()["bonus_text"], promo.text
        duplicate = await client.post("/api/promos/redeem", json={"code": "WEB500"})
        assert duplicate.status_code == 400, duplicate.text
        promo_history = await client.get("/api/promos")
        assert promo_history.status_code == 200 and len(promo_history.json()["items"]) == 1

        topup = await client.get("/api/topup")
        assert topup.status_code == 200 and topup.json()["minimum"] > 0

    set_lzt(None)
    await db.dispose()
    print("web smoke: landing, guest, Mini App auth, purchase, code, support, profile, leaders, promos, topup — ok")
    print(f"database: {smoke_db.name}")


if __name__ == "__main__":
    asyncio.run(main())
