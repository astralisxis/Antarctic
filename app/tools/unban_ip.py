"""Снять бан с адреса, когда в панель уже не войти.

    python -m app.tools.unban_ip 203.0.113.7
    python -m app.tools.unban_ip --all
    python -m app.tools.unban_ip --list

Бан за перебор пароля снимается в панели («Настройки» → «Блокировки по адресу»),
но забанить можно и себя: десять раз не вспомнить свой пароль — и форма входа
закрыта. Тогда остаётся эта команда на сервере.
"""

from __future__ import annotations

import argparse
import asyncio

from app.db import dispose, session_scope
from app.services import admin_guard


async def run(ip: str | None, everyone: bool, show: bool) -> None:
    async with session_scope() as session:
        rows = await admin_guard.rows(session)

        if show or not (ip or everyone):
            if not rows:
                print("список пуст: пароль никто не перебирал")
            for row in rows:
                state = "бан" if row.banned else f"{row.fails} из {admin_guard.LIMIT}"
                print(f"{row.ip:<40} {state:<14} логины: {row.logins or '—'}")
            await dispose()
            return

        targets = [row.ip for row in rows] if everyone else [str(ip)]
        for target in targets:
            ok = await admin_guard.unban(session, target)
            print(f"снято: {target}" if ok else f"такого адреса в списке нет: {target}")
    await dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Снять бан входа по IP")
    parser.add_argument("ip", nargs="?", help="адрес; без аргументов — покажет список")
    parser.add_argument("--all", action="store_true", help="снять все баны сразу")
    parser.add_argument("--list", action="store_true", help="только показать список")
    args = parser.parse_args()
    asyncio.run(run((args.ip or "").strip() or None, args.all, args.list))


if __name__ == "__main__":
    main()
