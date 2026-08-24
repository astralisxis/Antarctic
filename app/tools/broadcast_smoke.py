"""Прогон очереди рассылок без Telegram и без рабочей базы.

    python -m app.tools.broadcast_smoke
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent.parent
SMOKE_DB = BASE_DIR / "data" / "smoke_broadcast.db"
SMOKE_IMAGE = BASE_DIR / "data" / "smoke_broadcast.jpg"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{SMOKE_DB}"

from PIL import Image  # noqa: E402
from aiogram.exceptions import TelegramBadRequest  # noqa: E402
from aiogram.methods import SendMessage  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from app import db  # noqa: E402
from app.db import session_scope  # noqa: E402
from app.enums import BroadcastStatus, DeliveryStatus  # noqa: E402
from app.models import Admin, Broadcast, BroadcastDelivery, User  # noqa: E402
from app.security import hash_password  # noqa: E402
from app.services import broadcasts  # noqa: E402


class FakeBot:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    async def send_message(self, chat_id: int, text: str, **kw: Any):
        self.calls.append(("text", chat_id))
        return SimpleNamespace(message_id=len(self.calls))

    async def send_photo(self, *, chat_id: int, photo: Any, **kw: Any):
        self.calls.append(("photo", chat_id))
        return SimpleNamespace(
            message_id=len(self.calls),
            photo=[SimpleNamespace(file_id="cached-smoke-photo")],
        )


class InvalidHtmlBot(FakeBot):
    """Первый HTML-вызов отклоняется, повтор должен уйти обычным текстом."""

    def __init__(self) -> None:
        super().__init__()
        self.attempts: list[str | None] = []

    async def send_message(self, chat_id: int, text: str, **kw: Any):
        self.attempts.append(kw.get("parse_mode"))
        if len(self.attempts) == 1:
            raise TelegramBadRequest(
                method=SendMessage(chat_id=chat_id, text=text),
                message="can't parse entities",
            )
        return await super().send_message(chat_id, text, **kw)


async def main() -> None:
    SMOKE_DB.unlink(missing_ok=True)
    SMOKE_IMAGE.unlink(missing_ok=True)
    await db.create_all()

    try:
        async with session_scope() as session:
            session.add(
                Admin(login="smoke", password_hash=hash_password("smoke"), role="owner")
            )
            session.add_all(
                [
                    User(tg_id=1001, username="all", total_topup=1000),
                    User(tg_id=1002, username="buyer", orders_count=1),
                    User(tg_id=1003, username="premium", is_tg_premium=True),
                    User(tg_id=1004, username="banned", is_banned=True, total_topup=1000),
                    User(
                        tg_id=-1005,
                        username=None,
                        auth_provider="google",
                        auth_subject="google-smoke-user",
                        email="google@example.test",
                        total_topup=1000,
                        orders_count=1,
                        is_tg_premium=True,
                    ),
                ]
            )

        async with session_scope() as session:
            counts = await broadcasts.audience_counts(session)
            assert counts == {"all": 3, "paid": 1, "buyers": 1, "premium": 1}, counts
            first = await broadcasts.create(
                session,
                admin_id=1,
                title="Всем",
                text='<b>Поступление</b> <a href="https://example.com">открыть</a>',
                audience="all",
                buttons=broadcasts.parse_buttons("Магазин | https://example.com/shop"),
            )
            first_id = first.id

        async with session_scope() as session:
            first = await session.get(Broadcast, first_id)
            assert await broadcasts.start(session, first, admin_id=1) == 3
        async with session_scope() as session:
            first = await session.get(Broadcast, first_id)
            try:
                await broadcasts.start(session, first, admin_id=1)
            except broadcasts.BroadcastError:
                pass
            else:
                raise AssertionError("повторный запуск должен быть отклонён")

        bot = FakeBot()
        # Два воркера одновременно должны забронировать разные строки.
        assert await asyncio.gather(
            broadcasts.deliver_next(bot), broadcasts.deliver_next(bot)
        ) == [True, True]
        while await broadcasts.deliver_next(bot):
            pass

        async with session_scope() as session:
            first = await session.get(Broadcast, first_id)
            assert first.status == BroadcastStatus.DONE.value
            assert (first.sent, first.failed, first.blocked) == (3, 0, 0)
            statuses = list(
                (
                    await session.scalars(
                        select(BroadcastDelivery.status).where(
                            BroadcastDelivery.broadcast_id == first_id
                        )
                    )
                ).all()
            )
            assert statuses == [DeliveryStatus.SENT.value] * 3, statuses

        Image.new("RGB", (20, 20), "white").save(SMOKE_IMAGE, format="JPEG")
        async with session_scope() as session:
            second = await broadcasts.create(
                session,
                admin_id=1,
                title="Premium",
                text="Только Premium",
                audience="premium",
            )
            second.image_path = str(SMOKE_IMAGE.relative_to(BASE_DIR))
            await session.commit()
            second_id = second.id
        async with session_scope() as session:
            second = await session.get(Broadcast, second_id)
            assert await broadcasts.start(session, second, admin_id=1) == 1
        assert await broadcasts.deliver_next(bot) is True

        async with session_scope() as session:
            second = await session.get(Broadcast, second_id)
            assert second.status == BroadcastStatus.DONE.value
            assert second.sent == 1
            assert second.image_file_id == "cached-smoke-photo"

            cancelled = await broadcasts.create(
                session,
                admin_id=1,
                title="Отмена",
                text="Не отправлять",
                audience="paid",
            )
            assert await broadcasts.start(session, cancelled, admin_id=1) == 1
            await broadcasts.cancel(session, cancelled, admin_id=1)
            cancelled_id = cancelled.id
        assert await broadcasts.deliver_next(bot) is False

        async with session_scope() as session:
            cancelled = await session.get(Broadcast, cancelled_id)
            assert cancelled.status == BroadcastStatus.CANCELLED.value
            queued = int(
                await session.scalar(
                    select(func.count())
                    .select_from(BroadcastDelivery)
                    .where(
                        BroadcastDelivery.broadcast_id == cancelled_id,
                        BroadcastDelivery.status == DeliveryStatus.QUEUED.value,
                    )
                )
                or 0
            )
            assert queued == 1

        # Отмена во время claim не должна оставлять строку в sending: после
        # перезапуска такой кампании уже не будет активного воркера.
        async with session_scope() as session:
            claimed_cancel = await broadcasts.create(
                session,
                admin_id=1,
                title="Отмена после claim",
                text="Не отправлять",
                audience="paid",
            )
            assert await broadcasts.start(session, claimed_cancel, admin_id=1) == 1
            claimed_cancel_id = claimed_cancel.id
            delivery = await session.scalar(
                select(BroadcastDelivery).where(
                    BroadcastDelivery.broadcast_id == claimed_cancel_id
                )
            )
            assert delivery is not None
            delivery.status = DeliveryStatus.SENDING.value
            await broadcasts.cancel(session, claimed_cancel, admin_id=1)
        async with session_scope() as session:
            claimed_cancel = await session.get(Broadcast, claimed_cancel_id)
            delivery = await session.scalar(
                select(BroadcastDelivery).where(
                    BroadcastDelivery.broadcast_id == claimed_cancel_id
                )
            )
            assert claimed_cancel is not None
            assert claimed_cancel.status == BroadcastStatus.CANCELLED.value
            assert delivery is not None
            assert delivery.status == DeliveryStatus.QUEUED.value

        assert bot.calls == [
            ("text", 1001),
            ("text", 1002),
            ("text", 1003),
            ("photo", 1003),
        ], bot.calls

        # Бан после фиксации аудитории: строка остаётся в снимке, но Telegram не
        # вызывается, доставка закрывается как blocked, а рассылка завершается.
        async with session_scope() as session:
            late_ban = await broadcasts.create(
                session,
                admin_id=1,
                title="Поздний бан",
                text="Не отправлять забаненному",
                audience="buyers",
            )
            assert await broadcasts.start(session, late_ban, admin_id=1) == 1
            late_ban_id = late_ban.id
            buyer = await session.scalar(select(User).where(User.tg_id == 1002))
            assert buyer is not None
            buyer.is_banned = True
            await session.commit()
        calls_before_ban = list(bot.calls)
        assert await broadcasts.deliver_next(bot) is True
        assert bot.calls == calls_before_ban
        async with session_scope() as session:
            late_ban = await session.get(Broadcast, late_ban_id)
            assert late_ban is not None
            assert late_ban.status == BroadcastStatus.DONE.value
            assert (late_ban.sent, late_ban.failed, late_ban.blocked) == (0, 0, 1)

        # Битый Telegram HTML не должен останавливать очередь: повторяем то же
        # сообщение без parse_mode и учитываем доставку ровно один раз.
        async with session_scope() as session:
            invalid = await broadcasts.create(
                session,
                admin_id=1,
                title="HTML fallback",
                text="<b>незакрытый тег",
                audience="premium",
            )
            assert await broadcasts.start(session, invalid, admin_id=1) == 1
            invalid_id = invalid.id
        html_bot = InvalidHtmlBot()
        assert await broadcasts.deliver_next(html_bot) is True
        assert html_bot.attempts == ["HTML", None], html_bot.attempts
        assert html_bot.calls == [("text", 1003)], html_bot.calls
        async with session_scope() as session:
            invalid = await session.get(Broadcast, invalid_id)
            assert invalid is not None
            assert invalid.status == BroadcastStatus.DONE.value
            assert (invalid.sent, invalid.failed, invalid.blocked) == (1, 0, 0)

        print(
            "рассылки: аудитории, конкурентная очередь/счётчики, двойной запуск, "
            "текст, HTML fallback, картинка, поздний бан и отмена — ok"
        )
    finally:
        await db.dispose()
        SMOKE_IMAGE.unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(main())
