"""Точка входа приложения. Пакет держит и бота, и админку, и мини-апп — общий сервисный слой."""

from __future__ import annotations

import sys

__version__ = "0.1.0"


def _force_utf8_console() -> None:
    """Консоль Windows по умолчанию cp1251 и падает на русском тексте в логах.

    Делаем это при импорте пакета: иначе первая же строка лога с «₽» или
    тонким пробелом валит процесс с UnicodeEncodeError.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


_force_utf8_console()
