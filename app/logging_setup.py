"""Логирование в файл и консоль. Один формат для бота, админки и мини-аппа."""

from __future__ import annotations

import logging
import logging.handlers

from app.config import settings

_configured = False

FORMAT = "%(asctime)s %(levelname)-7s %(name)-14s %(message)s"


def setup_logging(component: str = "app") -> None:
    global _configured
    if _configured:
        return

    root = logging.getLogger()
    root.setLevel(settings.log_level.upper())

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(FORMAT, datefmt="%H:%M:%S"))
    root.addHandler(console)

    file_handler = logging.handlers.RotatingFileHandler(
        settings.log_dir / f"{component}.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(FORMAT))
    root.addHandler(file_handler)

    # Шумные библиотеки
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)

    _configured = True
