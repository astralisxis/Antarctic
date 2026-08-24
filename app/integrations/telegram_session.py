"""Build an in-memory Telethon StringSession from an LZT Auth Key.

LZT calls the session credentials ``loginData.login`` and
``loginData.password``.  They are an Auth Key (hex) and a Telegram DC id,
not a username/password pair.  The encoded session is never written to disk.
"""

from __future__ import annotations

import base64
import ipaddress
import struct
from dataclasses import dataclass
from typing import ClassVar, Mapping


class TelegramSessionError(ValueError):
    """The purchased credentials cannot form a Telegram session."""


@dataclass(frozen=True, slots=True)
class TelegramSessionEncoder:
    """Encode a 256-byte MTProto Auth Key in Telethon StringSession format."""

    auth_key: bytes
    dc_id: int

    VERSION: ClassVar[str] = "1"
    PORT: ClassVar[int] = 443
    DC_IP_MAP: ClassVar[Mapping[int, str]] = {
        1: "149.154.175.53",
        2: "149.154.167.51",
        3: "149.154.175.100",
        4: "149.154.167.91",
        5: "91.108.56.130",
    }

    def to_string(self) -> str:
        if len(self.auth_key) != 256:
            raise TelegramSessionError("Auth Key должен содержать ровно 256 байт (512 hex-символов).")
        ip = self.DC_IP_MAP.get(self.dc_id)
        if not ip:
            raise TelegramSessionError(f"Неизвестный дата-центр Telegram: {self.dc_id}.")
        packed_ip = ipaddress.ip_address(ip).packed
        payload = struct.pack(f">B{len(packed_ip)}sH256s", self.dc_id, packed_ip, self.PORT, self.auth_key)
        return self.VERSION + base64.urlsafe_b64encode(payload).decode("ascii")


def _dc_id(value: str | int | None) -> int:
    try:
        parsed = int(str(value or "").strip())
    except (TypeError, ValueError) as exc:
        raise TelegramSessionError("DC ID должен быть числом от 1 до 5.") from exc
    if parsed not in TelegramSessionEncoder.DC_IP_MAP:
        raise TelegramSessionError(f"Неизвестный дата-центр Telegram: {parsed}.")
    return parsed


def encode_string_session(auth_key: str | bytes, dc_id: str | int | None) -> str:
    """Return a Telethon-compatible StringSession without touching the filesystem."""

    if isinstance(auth_key, bytes):
        raw = auth_key
    else:
        value = (auth_key or "").strip()
        if value.lower().startswith("0x"):
            value = value[2:]
        value = "".join(value.split())
        if not value:
            raise TelegramSessionError("Auth Key отсутствует.")
        try:
            raw = bytes.fromhex(value)
        except ValueError as exc:
            raise TelegramSessionError("Auth Key должен быть hex-строкой.") from exc
    return TelegramSessionEncoder(raw, _dc_id(dc_id)).to_string()

