"""Explicit, user-confirmed cleanup of a purchased Telegram account."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from dataclasses import asdict, dataclass
from typing import Any

from app.enums import LogLevel, LogSection
from app.integrations.telegram_client import TelegramClientError, client_for_auth_key
from app.models import Order
from app.services import orders
from app.services.events import log_event

log = logging.getLogger("telegram.cleanup")
_locks: dict[int, asyncio.Lock] = {}


@dataclass(slots=True)
class CleanupReport:
    dialogs_deleted: int = 0
    groups_left: int = 0
    channels_left: int = 0
    contacts_deleted: int = 0
    owned_communities_skipped: int = 0
    failed: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


def _lock_for(order_id: int) -> asyncio.Lock:
    lock = _locks.get(order_id)
    if lock is None:
        lock = _locks[order_id] = asyncio.Lock()
    return lock


async def _retry_flood_wait(action: Any) -> Any:
    """Retry one Telegram operation after a short server-requested pause."""
    from telethon.errors import FloodWaitError

    try:
        return await action()
    except FloodWaitError as exc:
        seconds = min(max(int(exc.seconds), 1), 30)
        await asyncio.sleep(seconds)
        return await action()


async def _delete_contacts(client: Any, report: CleanupReport) -> None:
    from telethon import functions

    try:
        contacts = list(
            (await _retry_flood_wait(
                lambda: client(functions.contacts.GetContactsRequest(hash=0))
            )).users
        )
    except Exception as exc:
        report.failed += 1
        log.warning("cleanup contacts lookup failed: %s", exc)
        return
    if not contacts:
        return
    try:
        await _retry_flood_wait(
            lambda: client(functions.contacts.DeleteContactsRequest(id=contacts))
        )
        report.contacts_deleted = len(contacts)
    except Exception:
        # A single bad contact must not prevent dialog cleanup.
        for contact in contacts:
            try:
                await _retry_flood_wait(
                    lambda contact=contact: client(
                        functions.contacts.DeleteContactsRequest(id=[contact])
                    )
                )
                report.contacts_deleted += 1
            except Exception:
                report.failed += 1


async def _delete_dialogs(client: Any, report: CleanupReport) -> None:
    try:
        async for dialog in client.iter_dialogs():
            entity = dialog.entity
            try:
                is_group = bool(getattr(dialog, "is_group", False))
                is_channel = bool(getattr(dialog, "is_channel", False))
                if is_group or is_channel:
                    # Deleting an owned community would affect every member.
                    # The normal cleanup leaves only communities where this
                    # account is an ordinary participant.
                    if bool(getattr(entity, "creator", False)):
                        report.owned_communities_skipped += 1
                        continue
                    await _retry_flood_wait(lambda entity=entity: client.delete_dialog(entity))
                    if is_group:
                        report.groups_left += 1
                    else:
                        report.channels_left += 1
                else:
                    # Revoke private history where Telegram allows it, so the
                    # former account holder does not retain the old conversation.
                    await _retry_flood_wait(
                        lambda entity=entity: client.delete_dialog(entity, revoke=True)
                    )
                    report.dialogs_deleted += 1
            except Exception as exc:
                report.failed += 1
                log.warning("cleanup dialog failed: %s", exc)
    except Exception as exc:
        report.failed += 1
        log.warning("cleanup dialogs lookup failed: %s", exc)


async def cleanup_order(session: Any, order: Order) -> CleanupReport:
    """Clean an order's account and persist only the non-sensitive report."""

    if order.status not in orders.DELIVERED:
        raise TelegramClientError("Очистка доступна только по оплаченному заказу.")
    creds = orders.credentials(order)
    if not creds.auth_key or not creds.dc_id:
        raise TelegramClientError("В этом заказе нет необходимых данных подключения.")

    lock = _lock_for(order.id)
    if lock.locked():
        raise TelegramClientError("Очистка этого аккаунта уже выполняется.")

    async with lock:
        report = CleanupReport()
        async with client_for_auth_key(creds.auth_key, creds.dc_id) as client:
            await _delete_dialogs(client, report)
            await _delete_contacts(client, report)

        snapshot = dict(order.lzt_raw or {})
        snapshot["cleanup"] = {
            "at": dt.datetime.now(dt.UTC).isoformat(),
            **report.as_dict(),
        }
        order.lzt_raw = snapshot
        await log_event(
            LogSection.SHOP,
            "account_cleaned",
            level=LogLevel.WARN if report.failed else LogLevel.INFO,
            user_id=order.user_id,
            order_id=order.id,
            message=(
                f"диалоги {report.dialogs_deleted}, группы {report.groups_left}, "
                f"каналы {report.channels_left}, контакты {report.contacts_deleted}, "
                f"ошибки {report.failed}"
            ),
            payload=report.as_dict(),
            session=session,
        )
        await session.commit()
        return report
