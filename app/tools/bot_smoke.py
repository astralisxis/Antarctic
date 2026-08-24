"""Прогон бота без сети.

Telegram может быть недоступен (блокировка, нет прокси), а проверить логику нужно.
Инструмент подменяет транспорт aiogram заглушкой, скармливает диспетчеру настоящие
апдейты и печатает, что бот ответил. LZT Market подменяется тем же приёмом
(set_lzt), поэтому покупка и выдача кода проверяются целиком, без денег и сети.
Платёжные сервисы подменяются так же (set_cryptobot, set_xrocket, заглушки в
app/tools/fake_pay.py): счёт, кнопка «Проверить оплату», фоновый опрос и процент
пригласившему проходят настоящим кодом. Отдельная временная база — рабочую
data/shop.db не трогаем.

Номера и пароли в прогоне выдуманные, поэтому в консоль они идут как есть:
маскировать нечего, а видеть карточку целиком полезно.

    python -m app.tools.bot_smoke
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent.parent
SMOKE_DB = BASE_DIR / "data" / "smoke.db"
# Переменная окружения важнее .env — база подменяется до импорта app.config.
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{SMOKE_DB}"
os.environ.setdefault("BOT_USERNAME", "smoke_shop_bot")
os.environ.setdefault("SUPPORT_BOT_USERNAME", "smoke_support_bot")

from aiogram import Bot  # noqa: E402
from aiogram.client.default import DefaultBotProperties  # noqa: E402
from aiogram.client.session.base import BaseSession  # noqa: E402
from aiogram.enums import ParseMode  # noqa: E402
from aiogram.exceptions import TelegramBadRequest  # noqa: E402
from aiogram.methods import (  # noqa: E402
    AnswerCallbackQuery,
    EditMessageText,
    GetMe,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import (  # noqa: E402
    CallbackQuery,
    Chat,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
    Update,
    User as TgUser,
)
from sqlalchemy import func, select  # noqa: E402

from app.bot import keyboards  # noqa: E402
from app import db  # noqa: E402
from app.bot.handlers import topup  # noqa: E402
from app.bot.main import build_dispatcher  # noqa: E402
from app.db import session_scope  # noqa: E402
from app.enums import TxKind  # noqa: E402
from app.integrations.cryptobot import set_cryptobot  # noqa: E402
from app.integrations.lzt import set_lzt  # noqa: E402
from app.integrations.xrocket import set_xrocket  # noqa: E402
from app.models import CountryOffer, EventLog, Order, Payment, User  # noqa: E402
from app.services import balance, catalog, settings_store, users  # noqa: E402
from app.services import payments as pay_service  # noqa: E402
from app.tools.fake_market import FakeLzt, make_lot  # noqa: E402
from app.tools.fake_pay import FakeCryptoBot, FakeXRocket  # noqa: E402

TOKEN = "123456:SMOKE-TOKEN"
BOT_ID = 123456
NOW = dt.datetime(2026, 8, 19, 12, 0, tzinfo=dt.UTC)

# Теги, которые Telegram принимает в parse_mode=HTML.
HTML_TAGS = frozenset(
    {
        "b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
        "span", "tg-spoiler", "tg-emoji", "a", "code", "pre", "blockquote",
    }
)


def check_html(text: str | None) -> str | None:
    """Повторяет придирчивость Telegram: вернуть текст ошибки или None, если всё цело.

    Нужно, чтобы прогон ловил битую разметку в текстах из админки, а не пропускал её.
    """
    if not text:
        return None
    stack: list[str] = []
    i = 0
    while (i := text.find("<", i)) != -1:
        j = text.find(">", i)
        if j == -1:
            return "can't parse entities: Unmatched '<'"
        raw = text[i + 1 : j].strip()
        closing = raw.startswith("/")
        parts = raw.lstrip("/").split()
        name = parts[0].lower() if parts else ""
        if name not in HTML_TAGS:
            return f'can\'t parse entities: Unsupported start tag "{name}"'
        if closing:
            if not stack or stack[-1] != name:
                return f'can\'t parse entities: Unmatched end tag "{name}"'
            stack.pop()
        else:
            stack.append(name)
        i = j + 1
    if stack:
        return f'can\'t parse entities: Can\'t find end tag corresponding to start tag "{stack[-1]}"'
    return None


class FakeSession(BaseSession):
    """Транспорт-заглушка: ничего не отправляет, всё записывает."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, Any]] = []
        self._msg_id = 1000

    async def close(self) -> None:
        return None

    async def stream_content(self, *args: Any, **kw: Any):  # pragma: no cover
        raise NotImplementedError
        yield b""

    async def make_request(self, bot: Bot, method: TelegramMethod, timeout: int | None = None):
        self.calls.append((type(method).__name__, method))

        if isinstance(method, SendMessage | EditMessageText):
            # parse_mode приходит либо sentinel-объектом Default (значит HTML из
            # DefaultBotProperties), либо явным None — так помечен наш откат на
            # простой текст. Проверяем всё, кроме явного None.
            if method.parse_mode is not None:
                problem = check_html(method.text)
                if problem:
                    raise TelegramBadRequest(method=method, message=f"Bad Request: {problem}")

        if isinstance(method, GetMe):
            return TgUser(id=BOT_ID, is_bot=True, first_name="Магазин", username="smoke_shop_bot")
        if isinstance(method, SendMessage):
            self._msg_id += 1
            return Message(
                message_id=self._msg_id,
                date=NOW,
                chat=Chat(id=method.chat_id, type="private"),
                text=method.text,
            )
        if isinstance(method, EditMessageText):
            return Message(
                message_id=method.message_id or 0,
                date=NOW,
                chat=Chat(id=method.chat_id or 0, type="private"),
                text=method.text,
            )
        if isinstance(method, AnswerCallbackQuery):
            return True
        return True

    # --- разбор записанного ---
    def drain(self) -> list[tuple[str, Any]]:
        calls, self.calls = self.calls, []
        return calls


def show(title: str, session: FakeSession) -> None:
    print(f"\n=== {title} ===")
    for name, method in session.drain():
        if isinstance(method, SendMessage | EditMessageText):
            prefix = "отправил" if isinstance(method, SendMessage) else "переписал"
            print(f"[{prefix}]")
            for line in (method.text or "").splitlines():
                print(f"    {line}")
            markup = method.reply_markup
            if isinstance(markup, ReplyKeyboardMarkup):
                rows = [[b.text for b in row] for row in markup.keyboard]
                print(f"    клавиатура: {rows}")
            elif isinstance(markup, InlineKeyboardMarkup):
                rows = [
                    [f"{b.text}→{b.callback_data or b.url}" for b in row]
                    for row in markup.inline_keyboard
                ]
                print(f"    кнопки: {rows}")
        elif isinstance(method, AnswerCallbackQuery):
            if method.text:
                print(f"[всплывашка] {method.text}")
        else:
            print(f"[{name}]")


def tg_user(tg_id: int, name: str, username: str | None = None) -> TgUser:
    return TgUser(id=tg_id, is_bot=False, first_name=name, username=username)


_update_id = 0


def next_id() -> int:
    """Апдейты нумеруются сами: сценарии вставляются в середину без перенумерации."""
    global _update_id
    _update_id += 1
    return _update_id


def make_message(user: TgUser, text: str) -> Update:
    update_id = next_id()
    return Update(
        update_id=update_id,
        message=Message(
            message_id=update_id,
            date=NOW,
            chat=Chat(id=user.id, type="private"),
            from_user=user,
            text=text,
        ),
    )


def make_callback(user: TgUser, data: str) -> Update:
    update_id = next_id()
    return Update(
        update_id=update_id,
        callback_query=CallbackQuery(
            id=str(update_id),
            from_user=user,
            chat_instance="smoke",
            data=data,
            message=Message(
                message_id=500,
                date=NOW,
                chat=Chat(id=user.id, type="private"),
                from_user=tg_user(BOT_ID, "Магазин", "smoke_shop_bot"),
                text="старый экран",
            ),
        ),
    )


async def last_payment(tg_id: int) -> tuple[int, str]:
    """Свежий счёт клиента: id для кнопок и внешний id — для «оплаты» в заглушке."""
    async with session_scope() as session:
        payment = await session.scalar(
            select(Payment)
            .join(User, User.id == Payment.user_id)
            .where(User.tg_id == tg_id)
            .order_by(Payment.id.desc())
            .limit(1)
        )
        return payment.id, str(payment.external_id)


async def main() -> None:
    SMOKE_DB.unlink(missing_ok=True)
    await db.create_all()
    async with session_scope() as session:
        await settings_store.ensure_defaults(session)

    fake = FakeSession()
    bot = Bot(
        token=TOKEN,
        session=fake,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )
    dp = build_dispatcher()

    alice = tg_user(1001, "Алиса", "alice")
    bob = tg_user(1002, "Боб", "bob")

    market = FakeLzt()
    set_lzt(market)

    async def feed(update: Update, title: str) -> None:
        await dp.feed_update(bot, update)
        show(title, fake)

    await feed(make_message(alice, "/start"), "первый вход")

    # Реферальная ссылка: у Алисы внутренний id 1 — Боб приходит по её ссылке
    await feed(make_message(bob, "/start r1"), "вход по реферальной ссылке")

    await feed(make_message(alice, keyboards.BTN_PROFILE), "профиль новой кнопкой")
    await feed(make_message(alice, "Профиль"), "профиль")
    await feed(make_callback(alice, "pr:ref"), "реферальная система")
    await feed(make_callback(alice, "pr:accounts"), "мои аккаунты")
    await feed(make_callback(alice, "pr:root"), "назад в профиль")

    await feed(make_message(alice, "Заработать"), "заработать")
    await feed(make_callback(alice, "ea:comments"), "комментарии tiktok")
    await feed(make_callback(alice, "ea:root"), "назад в заработать")

    await feed(make_message(alice, "Поддержка"), "поддержка")
    await feed(make_message(alice, "Пополнить баланс"), "пополнение")
    await feed(make_message(alice, "Магазин"), "магазин с пустым каталогом")

    # Добавим страну в каталог и посмотрим витрину
    async with session_scope() as session:
        session.add(
            CountryOffer(
                code="ID",
                title="Индонезия +62",
                lzt_country="ID",
                price=4900,
                buy_limit=2000,
                sort=10,
            )
        )
    await feed(make_message(alice, "Магазин"), "магазин с товаром, наличие не проверено")
    await feed(make_callback(alice, "sh:c:1"), "карточка страны без денег")

    # --- покупка: маркет подменён, деньги начислены вручную ---
    market.lots = [
        make_lot(9001, 9.0, "+6281100011122"),
        make_lot(9002, 12.5, "+6281100033344", origin="stealer"),
        make_lot(9003, 41.0, "+6281100055566"),  # дороже лимита закупки, в подбор не попадёт
    ]
    market.codes = ["55231"]
    async with session_scope() as session:
        user = await session.scalar(select(User).where(User.tg_id == alice.id))
        await balance.credit(session, user, 20000, TxKind.TOPUP, comment="прогон")
        offer = await session.get(CountryOffer, 1)
        stock = await catalog.refresh_stock(session, offer)
    print(f"\n=== наличие после обхода: {stock} (лотов под лимит) ===")

    await feed(make_message(alice, "Магазин"), "витрина с наличием")
    await feed(make_callback(alice, "sh:c:1"), "карточка страны с деньгами")
    await feed(make_callback(alice, "sh:buy:1"), "покупка")
    await feed(make_callback(alice, "sh:code:1"), "код входа")

    market.codes = []
    await feed(make_callback(alice, "sh:code:1"), "код ещё не пришёл")
    await feed(make_callback(alice, "sh:clean:1"), "подтверждение очистки аккаунта")
    await feed(make_callback(alice, "sh:clean:yes:1"), "очистка без Telethon в smoke")
    await feed(make_callback(alice, "sh:reset:1"), "сброс чужих сессий")
    await feed(make_callback(alice, "pr:accounts"), "мои аккаунты после покупки")

    # --- отзыв по покупке ---
    await feed(make_callback(alice, "rv:new:1"), "отзыв: оценка")
    await feed(make_callback(alice, "rv:s:1:5"), "отзыв: текст")
    await feed(make_message(alice, "Номер пришёл за минуту."), "отзыв отправлен")
    await feed(make_callback(alice, "rv:new:1"), "второй отзыв по тому же заказу")

    # Лот ушёл из-под нас между поиском и покупкой: деньги должны вернуться
    market.gone.add(9002)
    await feed(make_callback(alice, "sh:c:1"), "карточка перед второй покупкой")
    await feed(make_callback(alice, "sh:buy:1"), "лот ушёл раньше")

    # Чужой заказ по подделанному id
    await feed(make_callback(bob, "sh:acc:1"), "чужой заказ по подделанному id")

    # Под лимит не осталось ничего
    market.lots = []
    await feed(make_callback(alice, "sh:buy:1"), "покупка при пустом маркете")

    await feed(make_message(alice, "привет"), "непонятный текст")

    # --- пополнение баланса ---
    # До этой строки токенов не было, и способов оплаты бот не показывал.
    crypto, rocket = FakeCryptoBot(), FakeXRocket()
    set_cryptobot(crypto)
    set_xrocket(rocket)

    # Платит Боб: он пришёл по ссылке Алисы, значит должен уйти и процент ей.
    await feed(make_message(bob, "Пополнить баланс"), "пополнение: экран суммы")
    await feed(make_message(bob, "три рубля"), "сумма не разобралась")
    await feed(make_message(bob, "30"), "сумма ниже минимума")
    await feed(make_message(bob, "300"), "способы оплаты")
    await feed(make_callback(bob, "tp:p:xrocket:30000"), "счёт xRocket")

    bob_payment, bob_external = await last_payment(bob.id)
    await feed(make_callback(bob, f"tp:c:{bob_payment}"), "проверка до оплаты")
    rocket.pay(bob_external)
    await feed(make_callback(bob, f"tp:c:{bob_payment}"), "проверка после оплаты")
    await feed(make_callback(bob, f"tp:c:{bob_payment}"), "проверка оплаченного счёта")

    # Чужой счёт по подделанному id — Алиса нажимает кнопку Боба.
    await feed(make_callback(alice, f"tp:c:{bob_payment}"), "чужой счёт по подделанному id")

    # Быстрая сумма, отмена счёта и молчащий провайдер.
    await feed(make_message(alice, "Пополнить баланс"), "пополнение Алисы")
    await feed(make_callback(alice, "tp:a:10000"), "быстрая сумма")
    # Своя сумма после кнопки: ожидания ввода уже нет, а число всё равно сумма.
    await feed(make_message(alice, "700 р"), "своя сумма без ожидания")
    await feed(make_callback(alice, "tp:p:cryptobot:10000"), "счёт Crypto Bot")
    alice_payment, _ = await last_payment(alice.id)
    await feed(make_callback(alice, f"tp:x:{alice_payment}"), "отмена счёта")
    # Если сервис не подтвердил отмену, бот не должен показывать ложный успех
    # и терять возможность найти оплату фоновым опросом.
    await feed(make_callback(alice, "tp:p:cryptobot:10000"), "счёт перед неудачной отменой")
    active_payment, _ = await last_payment(alice.id)
    crypto.down = True
    await feed(make_callback(alice, f"tp:x:{active_payment}"), "отмена при недоступном провайдере")
    crypto.down = True
    await feed(make_callback(alice, "tp:p:cryptobot:10000"), "провайдер не ответил")
    crypto.down = False

    # Оплата, которую нашёл фоновый опрос: клиенту пишет он сам, экрана нет.
    await feed(make_callback(alice, "tp:p:cryptobot:50000"), "счёт под опрос")
    _, alice_external = await last_payment(alice.id)
    crypto.pay(alice_external)
    async with session_scope() as session:
        credited = await pay_service.poll_pending(session)
    for item in credited:
        await topup.announce(bot, item)
    show(f"фоновый опрос: зачислено счетов {len(credited)}", fake)

    await feed(make_callback(alice, "pr:topups"), "история пополнений")
    await feed(make_callback(alice, "pr:orders"), "история заказов")
    # Подарок оформляется до покупки: отправитель выбирает получателя по ID
    # или @username,
    # подтверждает списание, а готовый заказ сразу появляется у Боба.
    market.lots = [make_lot(9010, 9.0, "+6281100099999")]
    market.sold.clear()
    async with session_scope() as session:
        alice_user = await session.scalar(select(User).where(User.tg_id == alice.id))
        await balance.credit(session, alice_user, 20000, TxKind.TOPUP, comment="подарок")
        bob_user = await session.scalar(select(User).where(User.tg_id == bob.id))
        recipient_by_id = await users.find_recipient(session, str(bob.id), alice_user)
        recipient_by_username = await users.find_recipient(session, "@BoB", alice_user)
        assert recipient_by_id is not None and recipient_by_username is not None
        assert recipient_by_id.id == recipient_by_username.id == bob_user.id
        sender_balance_before = alice_user.balance
        recipient_balance_before = bob_user.balance
    await feed(make_callback(alice, "sh:c:1"), "карточка для подарка")
    await feed(make_callback(alice, "sh:giftbuy:1"), "подарок: выбор получателя")
    await feed(make_message(alice, "@BoB"), "подарок: @username получателя")
    await feed(make_callback(alice, "sh:giftconfirm:1"), "подарок: подтверждение")
    async with session_scope() as session:
        gift_order = await session.scalar(select(Order).order_by(Order.id.desc()).limit(1))
        alice_user = await session.scalar(select(User).where(User.tg_id == alice.id))
        bob_user = await session.scalar(select(User).where(User.tg_id == bob.id))
        assert gift_order.user_id == bob_user.id
        assert gift_order.buyer_user_id == alice_user.id
        assert alice_user.balance == sender_balance_before - gift_order.price
        assert bob_user.balance == recipient_balance_before
        gift_order_id = gift_order.id
    await feed(make_callback(bob, f"sh:acc:{gift_order_id}"), "подарок: заказ у получателя")
    await feed(make_callback(bob, f"sh:replace:{gift_order_id}"), "замена: подтверждение")
    await feed(make_callback(bob, f"sh:replace:yes:{gift_order_id}"), "замена: заявка в поддержку")
    await feed(make_callback(bob, "pr:ref"), "реферальная система у приглашённого")

    # Ограничение доступа
    async with session_scope() as session:
        user = await session.scalar(select(User).where(User.tg_id == alice.id))
        user.is_banned = True
        user.ban_reason = "тест"
    await feed(make_message(alice, "Профиль"), "заблокированный клиент")

    # Истёкший бан снимается сам
    async with session_scope() as session:
        user = await session.scalar(select(User).where(User.tg_id == alice.id))
        user.banned_until = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=5)
    await feed(make_message(alice, "Профиль"), "истёкший бан снят")

    # Переключатели из админки
    async with session_scope() as session:
        await settings_store.set_value(session, "earn.video.enabled", "0")
        await settings_store.set_value(session, "shop.enabled", "0")
    await feed(make_message(alice, "Заработать"), "видео tiktok скрыто")
    await feed(make_callback(alice, "ea:video"), "скрытое направление по старой кнопке")
    await feed(make_message(alice, "Магазин"), "магазин выключен")
    await feed(make_callback(alice, "sh:buy:1"), "покупка при выключенном магазине")

    async with session_scope() as session:
        await settings_store.set_value(session, "earn.enabled", "0")
    await feed(make_message(alice, "Заработать"), "раздел заработка выключен")
    await feed(make_message(alice, "непонятно что"), "клавиатура без заработка")

    # Битая разметка в тексте из админки не должна оставлять клиента без ответа
    async with session_scope() as session:
        await settings_store.set_value(session, "bot.welcome", "Скидка <b>10% на <всё")
    await feed(make_message(alice, "/start"), "битый html в приветствии")

    # Ограничения на конкретного клиента
    async with session_scope() as session:
        user = await session.scalar(select(User).where(User.tg_id == alice.id))
        user.restrict_topup = True
        await settings_store.set_value(session, "shop.enabled", "1")
        user.restrict_buy = True
    await feed(make_message(alice, "Пополнить баланс"), "пополнение запрещено клиенту")
    await feed(make_message(alice, "Магазин"), "покупки запрещены клиенту")

    # Итоги в базе
    async with session_scope() as session:
        print("\n=== база ===")
        rows = (
            await session.execute(
                select(
                    User.id,
                    User.tg_id,
                    User.referrer_id,
                    User.referrals_count,
                    User.balance,
                    User.total_spent,
                    User.orders_count,
                    User.is_banned,
                )
            )
        ).all()
        for row in rows:
            print(
                f"    id={row[0]} tg={row[1]} пригласил={row[2]} рефералов={row[3]} "
                f"баланс={row[4]} потрачено={row[5]} заказов={row[6]} бан={row[7]}"
            )
        orders_rows = (
            await session.execute(
                select(
                    Order.id,
                    Order.status,
                    Order.lzt_item_id,
                    Order.price,
                    Order.lzt_cost,
                    Order.refunded,
                    Order.code_requests,
                ).order_by(Order.id)
            )
        ).all()
        for row in orders_rows:
            print(
                f"    заказ №{row[0]} {row[1]} лот={row[2]} цена={row[3]} закуп={row[4]} "
                f"возврат={row[5]} запросов кода={row[6]}"
            )
        payments_rows = (
            await session.execute(
                select(
                    Payment.id,
                    Payment.user_id,
                    Payment.provider,
                    Payment.status,
                    Payment.amount,
                    Payment.credited,
                    Payment.asset,
                ).order_by(Payment.id)
            )
        ).all()
        for row in payments_rows:
            print(
                f"    счёт №{row[0]} клиент={row[1]} {row[2]} {row[3]} сумма={row[4]} "
                f"зачислено={row[5]} {row[6]}"
            )
        sections = (
            await session.execute(
                select(EventLog.section, func.count())
                .group_by(EventLog.section)
                .order_by(func.count().desc())
            )
        ).all()
        print("    события:", ", ".join(f"{s}={c}" for s, c in sections))

    set_lzt(None)
    set_cryptobot(None)
    set_xrocket(None)
    await db.dispose()
    print(f"\nпрогнал апдейтов: {_update_id}. Вызовов маркета: {len(market.calls)}.")
    print(f"маркет: {', '.join(market.calls)}")
    print(f"Crypto Bot: {', '.join(crypto.calls)}")
    print(f"xRocket: {', '.join(rocket.calls)}")
    print(f"База прогона: {SMOKE_DB.name}")


if __name__ == "__main__":
    asyncio.run(main())
