"""Short-lived Telethon clients authenticated by a purchased Auth Key."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from app.integrations.telegram_session import TelegramSessionError, encode_string_session

# Telegram Desktop's public MTProto application identity.  It identifies the
# client implementation; the account authorization still comes exclusively
# from the Auth Key embedded in the StringSession.
TELEGRAM_API_ID = 2040
TELEGRAM_API_HASH = "b18441a1ff607e10a989891a5462e627"


class TelegramClientError(RuntimeError):
    """Telethon is unavailable or the Auth Key cannot authorize the client."""


@asynccontextmanager
async def client_for_auth_key(
    auth_key: str,
    dc_id: str | int | None,
) -> AsyncIterator[object]:
    """Connect a memory-only Telethon client and always disconnect it."""

    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
    except ImportError as exc:
        raise TelegramClientError(
            "Модуль Telethon не установлен. Установите зависимости проекта и повторите."
        ) from exc

    try:
        encoded = encode_string_session(auth_key, dc_id)
    except TelegramSessionError as exc:
        raise TelegramClientError("Данные подключения недействительны.") from exc

    client = TelegramClient(
        StringSession(encoded),
        TELEGRAM_API_ID,
        TELEGRAM_API_HASH,
        device_model="Numbers Shop",
        system_version="Windows 10",
        app_version="1.0",
        lang_code="ru",
        system_lang_code="ru-RU",
    )
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise TelegramClientError("Данные подключения недействительны или уже отозваны Telegram.")
        yield client
    except TelegramClientError:
        raise
    except Exception as exc:
        # Raw Telethon errors may contain DC addresses, proxy details, or
        # internal protocol data. Keep those in logs, never in a user message.
        import logging

        logging.getLogger("telegram_client").exception("Telethon client failed")
        raise TelegramClientError("Не удалось войти в аккаунт Telegram. Напишите в поддержку.") from exc
    finally:
        await client.disconnect()
