"""Прогон админки без браузера.

Поднимает приложение админки в памяти (ASGI-транспорт httpx), заходит логином и
паролем, обходит все разделы и нажимает действия: правка настроек, обновление
наличия, выдача кода, возврат, правка баланса, бан, работа со счетами, удаление
позиции каталога, обход стран маркета. Проверяем то, что руками проверять долго:
нет ли 500-х, доезжают ли редиректы и всплывающие сообщения.

База — своя, data/smoke_admin.db, рабочую data/shop.db не трогаем.
Маркет подменён заглушкой из app/tools/fake_market.py, платёжные сервисы —
из app/tools/fake_pay.py: сети нет.

    python -m app.tools.admin_smoke
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent.parent
SMOKE_DB = BASE_DIR / "data" / "smoke_admin.db"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{SMOKE_DB}"
os.environ.setdefault("BOT_USERNAME", "smoke_shop_bot")
os.environ.setdefault("SUPPORT_BOT_USERNAME", "smoke_support_bot")
os.environ.setdefault("ADMIN_SECRET", "smoke-session-secret-not-for-production")

import httpx  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from app import db  # noqa: E402
from app.admin.main import app as admin_app  # noqa: E402
from app.db import session_scope  # noqa: E402
from app.enums import AdminRole, PaymentProvider, TicketStatus, TxKind  # noqa: E402
from app.integrations.cryptobot import set_cryptobot  # noqa: E402
from app.integrations.lzt import set_lzt  # noqa: E402
from app.integrations.xrocket import set_xrocket  # noqa: E402
from app.models import (  # noqa: E402
    Admin,
    BalanceTx,
    CountryOffer,
    MarketStat,
    Order,
    Payment,
    Ticket,
    User,
)
from app.security import hash_password  # noqa: E402
from app.services import balance, orders as orders_service, payments as pay_service  # noqa: E402
from app.services import market as market_service  # noqa: E402
from app.services import settings_store  # noqa: E402
from app.tools.fake_market import FakeLzt, make_lot  # noqa: E402
from app.tools.fake_pay import FakeCryptoBot, FakeXRocket  # noqa: E402

LOGIN = "smoke"
PASSWORD = "smoke-pass-123"
ADMIN_LOGIN = "smoke-admin"
SUPPORT_LOGIN = "smoke-support"

# Что показывать в отчёте: заголовок страницы и сколько строк в таблице.
TITLE_MARK = "<h1>"


def brief(html: str) -> str:
    """Заголовок страницы и первое уведомление — этого хватает, чтобы понять экран."""
    out = []
    start = html.find(TITLE_MARK)
    if start != -1:
        end = html.find("</h1>", start)
        out.append(html[start + len(TITLE_MARK) : end].strip())
    note = html.find('class="note')
    if note != -1:
        end = html.find("</div>", note)
        text = html[html.find(">", note) + 1 : end]
        out.append(" ".join(text.split())[:160])
    rows = html.count("<tr>") - html.count('class="empty"')
    if rows > 0:
        out.append(f"строк в таблицах: {rows}")
    return " · ".join(out)


class Runner:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.problems: list[str] = []
        self.steps = 0

    async def get(self, url: str, title: str = "", expect: int = 0) -> httpx.Response:
        response = await self.client.get(url)
        self._report("GET", url, response, title, expect)
        return response

    async def post(
        self,
        url: str,
        data: dict[str, Any] | None = None,
        title: str = "",
        expect: int = 0,
    ) -> httpx.Response:
        response = await self.client.post(url, data=data or {})
        # Действия отвечают редиректом; идём по нему, чтобы увидеть уведомление.
        if response.status_code in (302, 303, 307) and "location" in response.headers:
            location = response.headers["location"]
            self._report("POST", url, response, title or location, expect)
            return await self.get(location, f"после {url}")
        self._report("POST", url, response, title, expect)
        return response

    def _report(
        self, method: str, url: str, response: httpx.Response, title: str, expect: int = 0
    ) -> None:
        """expect — код, который в этом шаге и должен прийти (отказ формы, неверный пароль)."""
        self.steps += 1
        code = response.status_code
        if expect:
            if code == expect:
                mark = "ждали"
            else:
                mark = "БЕДА "
                self.problems.append(f"{method} {url} → {code}, ожидали {expect}")
        elif code < 400:
            mark = "ok   "
        else:
            mark = "БЕДА "
            self.problems.append(f"{method} {url} → {code}")
        note = ""
        if code in (200, 400) and "text/html" in response.headers.get("content-type", ""):
            note = brief(response.text)
        elif code in (302, 303, 307):
            note = f"→ {response.headers.get('location', '')}"
        print(f"[{mark}] {code} {method} {url} {('· ' + title) if title else ''}")
        if note:
            print(f"        {note}")


async def seed(market: FakeLzt) -> tuple[int, int, int, int]:
    """Наполнить базу: админ, клиент с балансом, страна и купленный заказ."""
    async with session_scope() as session:
        await settings_store.ensure_defaults(session)
        session.add(
            Admin(
                login=LOGIN,
                password_hash=hash_password(PASSWORD),
                role=AdminRole.OWNER.value,
            )
        )
        session.add_all(
            [
                Admin(
                    login=ADMIN_LOGIN,
                    password_hash=hash_password(PASSWORD),
                    role=AdminRole.ADMIN.value,
                ),
                Admin(
                    login=SUPPORT_LOGIN,
                    password_hash=hash_password(PASSWORD),
                    role=AdminRole.SUPPORT.value,
                ),
            ]
        )
        offer = CountryOffer(
            code="ID",
            title="Индонезия +62",
            lzt_country="ID",
            price=4900,
            buy_limit=2000,
            sort=10,
        )
        user = User(tg_id=1001, username="alice", first_name="Алиса")
        session.add_all([offer, user])
        await session.flush()
        await balance.credit(session, user, 30000, TxKind.TOPUP, comment="прогон")
        offer_id, user_id = offer.id, user.id

    # Заказ делаем настоящим путём — тем же кодом, что и бот.
    async with session_scope() as session:
        user = await session.get(User, user_id)
        offer = await session.get(CountryOffer, offer_id)
        order = await orders_service.buy(session, user, offer, lzt=market, source="smoke")
        order_id = order.id
        ticket = Ticket(
            user_id=user.id,
            order_id=order.id,
            status=TicketStatus.OPEN.value,
            subject="Нужна помощь с заказом",
        )
        session.add(ticket)
        await session.flush()
        ticket_id = ticket.id
        await orders_service.request_replacement(session, order)
    return offer_id, user_id, order_id, ticket_id


async def add_payment(user_id: int, provider: str, amount: int = 50000) -> tuple[int, str]:
    """Счёт выставляем настоящим путём — тем же кодом, что и бот. Отдаём id и внешний id."""
    async with session_scope() as session:
        user = await session.get(User, user_id)
        payment, _ = await pay_service.create(session, user, provider, amount)
        return payment.id, str(payment.external_id)


async def main() -> None:
    SMOKE_DB.unlink(missing_ok=True)
    await db.create_all()

    market = FakeLzt()
    market.lots = [make_lot(9001, 9.0, "+6281100011122"), make_lot(9002, 12.5, "+6281100033344")]
    market.codes = ["55231"]
    set_lzt(market)

    crypto, rocket = FakeCryptoBot(), FakeXRocket()
    set_cryptobot(crypto)
    set_xrocket(rocket)

    offer_id, user_id, order_id, ticket_id = await seed(market)
    print(f"наполнил базу: страна {offer_id}, клиент {user_id}, заказ {order_id}")

    # Три счёта — предел открытых на клиента. Один оплатим у провайдера до опроса,
    # второй — до кнопки «Проверить», третий зачислим вручную.
    poll_id, poll_ext = await add_payment(user_id, PaymentProvider.XROCKET.value)
    check_id, check_ext = await add_payment(user_id, PaymentProvider.CRYPTOBOT.value)
    manual_id, _ = await add_payment(user_id, PaymentProvider.CRYPTOBOT.value, 12000)
    rocket.pay(poll_ext)
    crypto.pay(check_ext)
    print(f"счёта: опрос {poll_id}, проверка {check_id}, вручную {manual_id}\n")

    transport = httpx.ASGITransport(app=admin_app)
    # В prod cookie админки помечена Secure. HTTPS нужен и в ASGI-прогоне,
    # иначе httpx корректно не отправляет cookie после успешного входа и все
    # следующие страницы выглядят как разлогиненные.
    async with httpx.AsyncClient(transport=transport, base_url="https://admin", timeout=30) as client:
        run = Runner(client)

        # --- вход ---
        await run.get("/", "без входа — должен быть редирект")
        # expect=401 — отказ здесь и есть правильный ответ, в «проблемы» он не идёт.
        await run.post(
            "/login", {"login": LOGIN, "password": "неверный"}, "неверный пароль", expect=401
        )
        await run.post("/login", {"login": LOGIN, "password": PASSWORD}, "вход")

        # --- разделы ---
        await run.get("/", "обзор")
        await run.get("/orders", "заказы")
        await run.get("/replacements", "замены")
        await run.get("/orders?stuck=1", "зависшие заказы")
        await run.get(f"/orders/{order_id}", "карточка заказа")
        await run.get("/users", "пользователи")
        await run.get("/users?only=paid&sort=topup", "с пополнениями")
        await run.get(f"/users/{user_id}", "карточка клиента")
        await run.get("/catalog", "каталог")
        await run.get(f"/catalog/{offer_id}", "правка страны")
        await run.get("/catalog/new", "новая страна")
        await run.get("/catalog/market", "страны маркета")
        await run.get("/payments", "платежи")
        await run.get("/promos", "промокоды")
        await run.get("/settings", "настройки")
        await run.get("/logs", "логи")
        await run.get("/logs?section=shop", "логи магазина")
        await run.get("/support", "поддержка")
        await run.get("/reviews", "отзывы")
        await run.get("/broadcasts", "рассылки")
        await run.get("/broadcasts/99999", "рассылки нет")
        await run.get("/orders/99999", "заказа нет")

        await run.post(
            "/promos",
            {
                "code": "SMOKE500",
                "title": "Проверка админки",
                "bonus": "500",
                "max_uses": "2",
                "expires_at": "",
            },
            "создать промокод",
        )
        await run.post("/promos/1/toggle", title="выключить промокод")
        await run.post("/promos/1/toggle", title="включить промокод")

        # --- рассылки: черновик, фиксированная аудитория и отмена очереди ---
        await run.post(
            "/broadcasts",
            {
                "title": "Проверка",
                "text": "<b>Новое поступление</b>",
                "audience": "all",
                "buttons": "Магазин | https://example.com/shop",
            },
            "создать рассылку",
        )
        broadcast_page = await run.get("/broadcasts/1", "карточка рассылки")
        if "<b>Новое поступление</b>" in broadcast_page.text:
            run.problems.append("HTML рассылки исполнился внутри админки вместо экранирования")
        if "&lt;b&gt;Новое поступление&lt;/b&gt;" not in broadcast_page.text:
            run.problems.append("в карточке рассылки не найден экранированный HTML")
        await run.post("/broadcasts/1/start", title="запустить рассылку")
        await run.post("/broadcasts/1/cancel", title="отменить рассылку")

        # --- действия по заказу ---
        await run.post(f"/orders/{order_id}/validate", title="проверить валидность")
        await run.post(f"/replacements/{order_id}/approve", title="подтвердить замену")
        market.invalid_codes.add(9002)
        await run.post(f"/orders/{order_id}/validate", title="пометить аккаунт недействительным")
        await run.post(f"/orders/{order_id}/code", title="выдать код")
        await run.post(f"/orders/{order_id}/reset", title="сбросить сессии")
        await run.post(f"/orders/{order_id}/done", title="завершить заказ")
        await run.post(f"/orders/{order_id}/refund", {"comment": "прогон"}, "возврат")
        await run.post(f"/orders/{order_id}/refund", {"comment": "второй раз"}, "возврат повторно")

        # --- каталог ---
        await run.post("/catalog/refresh", title="обновить наличие всех")
        await run.post(f"/catalog/{offer_id}/refresh", title="обновить наличие одной")
        await run.post(f"/catalog/{offer_id}/toggle", title="скрыть страну")
        await run.post(f"/catalog/{offer_id}/toggle", title="вернуть страну")
        await run.post(
            f"/catalog/{offer_id}",
            {
                "title": "Индонезия +62",
                "code": "ID",
                "price": "59",
                "sort": "10",
                "description": "виртуальный номер",
                "is_active": "1",
                "lzt_country": "ID",
                "buy_limit": "25",
                "spam_filter": "nomatter",
                "password_filter": "nomatter",
                "origin_filter": ["brute", "stealer"],
                "extra_filters": "",
            },
            "правка страны",
        )
        await run.post(
            "/catalog/new",
            {
                "title": "США +1",
                "code": "ID",  # код занят — форма должна вернуться с ошибкой
                "price": "99",
                "sort": "20",
                "is_active": "1",
                "lzt_country": "US",
                "buy_limit": "40",
                "spam_filter": "nomatter",
                "password_filter": "nomatter",
                "origin_filter": "",
                "extra_filters": "",
            },
            "занятый код позиции",
            expect=400,  # форма возвращается с ошибкой над полями — так и задумано
        )
        await run.post(
            "/catalog/new",
            {
                "title": "США +1",
                "code": "US",
                "price": "99",
                "sort": "20",
                "is_active": "1",
                "lzt_country": "US",
                "buy_limit": "40",
                "spam_filter": "nomatter",
                "password_filter": "nomatter",
                "origin_filter": "",
                "extra_filters": "",
            },
            "новая страна",
        )

        # --- страны маркета ---
        await run.post("/catalog/market/ID", title="проверить одну страну")
        await run.post(
            "/catalog/market/scan",
            {"pmax": "30", "spam": "no", "scope": "catalog", "only": "lots"},
            "обход по каталогу",
        )
        await run.post(
            "/catalog/market/scan", {"scope": "catalog"}, "обход, пока идёт прошлый"
        )
        # Обход живёт фоновой задачей: дожидаемся конца, иначе он допишет в базу,
        # когда движок уже закрыт.
        for _ in range(100):
            if not market_service.state().running:
                break
            await asyncio.sleep(0.1)
        await run.get("/catalog/market?only=all", "страны маркета: все")
        await run.get("/catalog/new?code=ID", "форма, заполненная под страну")

        # --- удаление позиции ---
        await run.get(f"/catalog/{offer_id}/delete", "подтверждение удаления")
        await run.post(f"/catalog/{offer_id}/delete", title="удалить позицию")
        await run.post(f"/catalog/{offer_id}/delete", title="удалить второй раз")
        await run.get(f"/orders/{order_id}", "заказ удалённой позиции")

        # --- платежи ---
        await run.get("/payments?status=pending", "ожидающие счёта")
        await run.get("/payments?provider=xrocket", "счёта xRocket")
        await run.get(f"/payments?q={check_ext}", "поиск по внешнему id")
        await run.get("/payments?q=1001", "поиск по tg_id")
        await run.get("/payments?q=alice", "поиск по имени")
        await run.get("/payments?q=нет-такого", "поиск без совпадений")
        await run.post(f"/payments/{check_id}/check", title="проверить оплаченный счёт")
        await run.post(f"/payments/{check_id}/check", title="проверить его же повторно")
        await run.post("/payments/poll", title="сверить ожидающие")
        await run.post("/payments/poll", title="сверить второй раз")
        await run.post(f"/payments/{manual_id}/credit", {"comment": "пришло на кошелёк"}, "зачислить вручную")
        await run.post(f"/payments/{manual_id}/credit", {"comment": "второй раз"}, "зачислить повторно")
        await run.post("/payments/99999/check", title="счёта нет")

        # Четвёртый счёт: до этого шага открытых счетов уже нет, лимит не мешает.
        expire_id, _ = await add_payment(user_id, PaymentProvider.CRYPTOBOT.value, 8000)
        await run.post(f"/payments/{expire_id}/expire", title="снять счёт")
        await run.post(f"/payments/{expire_id}/expire", title="снять его же повторно")
        await run.get("/payments?status=paid", "оплаченные счёта")

        # --- клиент ---
        await run.post(f"/users/{user_id}/balance", {"amount": "150", "comment": "бонус"}, "плюс баланс")
        await run.post(f"/users/{user_id}/balance", {"amount": "-50", "comment": "правка"}, "минус баланс")
        await run.post(f"/users/{user_id}/balance", {"amount": "-999999"}, "списать больше остатка")
        await run.post(f"/users/{user_id}/balance", {"amount": "ну как-то так"}, "мусор в сумме")
        await run.post(
            f"/users/{user_id}/settings",
            {"restrict_buy": "1", "ref_percent": "25", "admin_note": "проверка"},
            "ограничения",
        )
        await run.post(f"/users/{user_id}/settings", {"ref_percent": "200"}, "процент больше 100")
        await run.post(f"/users/{user_id}/ban", {"reason": "прогон", "days": "3"}, "бан на 3 дня")
        await run.post(f"/users/{user_id}/unban", title="снять бан")

        # --- настройки ---
        values = await current_settings()
        values["shop.code_hours"] = "12"
        values["referral.percent"] = "15"
        values["topup.min"] = "100"
        values["support.hours"] = "09:00 — 21:00 МСК"
        await run.post("/settings", values, "сохранить настройки")
        await run.post("/settings", values, "сохранить те же значения")
        bad = dict(values, **{"referral.percent": "300"})
        await run.post("/settings", bad, "процент больше 100")
        bad = dict(values, **{"reviews.channel_url": "t.me/otzyvy"})
        await run.post("/settings", bad, "ссылка без https")

        await run.get("/settings", "настройки после правок")
        await run.post("/logout", title="выход")
        await run.get("/users", "после выхода")

        # --- роль поддержки: только обращения, без финансов и реквизитов ---
        await run.post(
            "/login", {"login": SUPPORT_LOGIN, "password": PASSWORD}, "вход поддержки"
        )
        await run.get("/", "корень поддержки ведёт к обращениям", expect=303)
        support_page = await run.get("/support", "доступный раздел поддержки")
        ticket_page = await run.get(f"/support/{ticket_id}", "карточка обращения")

        leaked = [
            marker
            for marker in (
                '<span>Обзор</span>',
                '<span>Заказы</span>',
                '<span>Пользователи</span>',
                '<span>Платежи</span>',
                '<span>Настройки</span>',
            )
            if marker in support_page.text
        ]
        private = [
            marker
            for marker in (
                "Баланс",
                "Последние заказы клиента",
                'href="/users/',
                'href="/orders/',
            )
            if marker in ticket_page.text
        ]
        if leaked or private:
            run.problems.append(
                "support увидел закрытые элементы: " + ", ".join(leaked + private)
            )

        for path in (
            "/orders",
            "/replacements",
            f"/orders/{order_id}",
            "/users",
            f"/users/{user_id}",
            "/payments",
            "/catalog",
            "/reviews",
            "/logs",
            "/settings",
            "/security/ip",
            "/maintenance",
            "/broadcasts",
            "/promos",
        ):
            await run.get(path, "support: доступ закрыт", expect=403)

        for path, data in (
            (f"/users/{user_id}/balance", {"amount": "999"}),
            (f"/orders/{order_id}/refund", {"comment": "нельзя"}),
            (f"/payments/{manual_id}/credit", {"comment": "нельзя"}),
            ("/settings", {}),
            ("/maintenance", {"confirm": "ОБНУЛИТЬ", "group": "users"}),
        ):
            await run.post(path, data, "support: действие закрыто", expect=403)
        await run.post("/logout", title="выход поддержки")

        # Обычный admin ведёт рабочие разделы, но не системное обслуживание.
        await run.post(
            "/login", {"login": ADMIN_LOGIN, "password": PASSWORD}, "вход администратора"
        )
        await run.get("/", "обзор администратора")
        await run.get("/settings", "настройки администратора")
        await run.get("/maintenance", "обслуживание только владельцу", expect=403)
        await run.get("/security/ip", "безопасность входа только владельцу", expect=403)
        await run.post("/logout", title="выход администратора")

        print(f"\nшагов: {run.steps}")
        if run.problems:
            print("проблемы:")
            for line in run.problems:
                print(f"    {line}")
        else:
            print("ни одного ответа 4xx/5xx там, где его не ждали.")

    await check_db(user_id, order_id)
    set_lzt(None)
    set_cryptobot(None)
    set_xrocket(None)
    await db.dispose()
    print(f"\nвызовов маркета: {', '.join(market.calls)}")
    print(f"вызовов Crypto Bot: {', '.join(crypto.calls)}")
    print(f"вызовов xRocket: {', '.join(rocket.calls)}")
    print(f"База прогона: {SMOKE_DB.name}")


async def current_settings() -> dict[str, str]:
    """Форма настроек отправляет все поля разом — собираем их из текущих значений."""
    async with session_scope() as session:
        raw = await settings_store.all_values(session)
    out: dict[str, str] = {}
    for d in settings_store.DEFAULTS:
        value = raw.get(d.key, d.default)
        if d.kind == "bool":
            if value.strip() in {"1", "true", "yes", "on"}:
                out[d.key] = "1"
        elif d.kind == "money":
            out[d.key] = str(int(value) / 100)
        else:
            out[d.key] = value
    return out


async def check_db(user_id: int, order_id: int) -> None:
    async with session_scope() as session:
        user = await session.get(User, user_id)
        order = await session.get(Order, order_id)
        assert order.replacement_status == orders_service.REPLACEMENT_COMPLETED
        assert order.lzt_item_id == 9002
        assert order.account_valid is False
        offers = (await session.scalars(select(CountryOffer).order_by(CountryOffer.id))).all()
        print("\n=== база после прогона ===")
        print(
            f"    клиент: баланс={user.balance} потрачено={user.total_spent} "
            f"заказов={user.orders_count} процент={user.ref_percent} бан={user.is_banned}"
        )
        print(
            f"    заказ №{order.id}: {order.status} возврат={order.refunded} "
            f"запросов кода={order.code_requests} позиция={order.offer_id} "
            f"(снимок: {order.offer_code} {order.offer_title})"
        )
        for offer in offers:
            print(
                f"    страна {offer.code}: цена={offer.price} лимит={offer.buy_limit} "
                f"активна={offer.is_active} наличие={offer.stock_cached}"
            )
        payments = (await session.scalars(select(Payment).order_by(Payment.id))).all()
        for p in payments:
            print(
                f"    счёт №{p.id}: {p.provider} {p.status} сумма={p.amount} "
                f"зачислено={p.credited} {p.asset}"
            )
        stats = (await session.scalars(select(MarketStat).order_by(MarketStat.code))).all()
        for s in stats:
            print(
                f"    маркет {s.code}: лотов={s.lots} выборка={s.sample} "
                f"от={s.price_min} средняя={s.price_avg} ошибка={s.error}"
            )
        # Сходимость баланса: сумма движений должна равняться остатку клиента.
        moves = int(
            await session.scalar(
                select(func.coalesce(func.sum(BalanceTx.amount), 0)).where(
                    BalanceTx.user_id == user_id
                )
            )
            or 0
        )
        print(f"    сумма движений={moves} остаток={user.balance} "
              f"{'сходится' if moves == user.balance else 'РАСХОДИТСЯ'}")
        print(f"    настройки: срок кода={await settings_store.get(session, 'shop.code_hours')} ч, "
              f"процент={await settings_store.get(session, 'referral.percent')}, "
              f"минимум пополнения={await settings_store.get(session, 'topup.min')}")


if __name__ == "__main__":
    asyncio.run(main())
