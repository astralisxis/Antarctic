"""Проверка доступа к Telegram и токенов ботов.

Идёт тем же путём, что и рабочий бот: aiogram + BOT_PROXY из .env. Поэтому если
здесь всё зелёное, то и бот поднимется.

    python -m app.tools.tg_check
"""

from __future__ import annotations

import asyncio
import socket
from urllib.parse import urlparse

from aiogram.exceptions import TelegramAPIError, TelegramNetworkError

from app.config import mask_proxy, settings

HOST = "api.telegram.org"
TIMEOUT = 6.0
ATTEMPTS = 4  # дешёвые прокси рвут часть соединений, одна попытка ничего не доказывает


def probe_tcp(host: str, port: int) -> str | None:
    """Дозвониться до host:port. None — дошли, иначе текст с причиной."""
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        return f"DNS не отвечает: {exc}"

    ips = sorted({info[4][0] for info in infos})
    for ip in ips:
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.settimeout(TIMEOUT)
        try:
            sock.connect((ip, port))
            return None
        except OSError:
            continue
        finally:
            sock.close()
    return f"не отвечает ни один адрес ({', '.join(ips)})"


def probe_network() -> str:
    """Куда пойдёт бот: через прокси или напрямую, и живо ли это."""
    if settings.bot_proxy:
        parsed = urlparse(settings.bot_proxy)
        host, port = parsed.hostname, parsed.port
        if not host or not port:
            return f"прокси разобран криво: {mask_proxy(settings.bot_proxy)}"
        problem = probe_tcp(host, port)
        if problem:
            return f"прокси {host}:{port} не отвечает — {problem}"
        return f"прокси {host}:{port} доступен (TCP)"

    problem = probe_tcp(HOST, 443)
    if problem:
        return f"напрямую недоступен — {problem}; нужен BOT_PROXY"
    return "напрямую доступен"


async def check_token(label: str, token: str, configured_username: str) -> None:
    from app.bot.main import build_bot  # локальный импорт: тянет aiogram и настройки

    if not token:
        print(f"{label}: токен не задан в .env")
        return

    bot = build_bot(token)
    me = None
    drops = 0
    last_error = ""
    try:
        for attempt in range(ATTEMPTS):
            try:
                me = await bot.get_me()
                break
            except TelegramNetworkError as exc:
                # Обрыв — это про канал, а не про токен. Пробуем ещё, но считаем.
                drops += 1
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < ATTEMPTS - 1:
                    await asyncio.sleep(1.0)
            except TelegramAPIError as exc:
                print(f"{label}: Telegram отказал — {exc}")
                return
            except Exception as exc:
                print(f"{label}: не дошли до Telegram — {type(exc).__name__}: {exc}")
                return
    finally:
        await bot.session.close()

    if me is None:
        print(f"{label}: не дошли за {ATTEMPTS} попыток — {last_error}")
        return

    note = f"  [связь рвалась {drops} раз(а) из {drops + 1}]" if drops else ""
    print(f"{label}: @{me.username}, id {me.id}, «{me.first_name}»{note}")
    expected = configured_username.lstrip("@").lower()
    if expected and me.username and expected != me.username.lower():
        print(f"    в .env указан {configured_username} — не совпадает, поправьте")
    elif not expected:
        print("    юзернейм в .env не заполнен")


async def run() -> None:
    print(f"прокси: {mask_proxy(settings.bot_proxy)}")
    print(f"сеть: {probe_network()}\n")
    await check_token("основной бот", settings.bot_token, settings.bot_username)
    await check_token("бот поддержки", settings.support_bot_token, settings.support_bot_username)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
