"""Короткое сообщение после действия.

Действия админки отвечают редиректом (POST → Redirect → GET): обновление
страницы не должно повторять списание или покупку. Текст результата живёт
между запросами в сессии-куке и показывается один раз.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request

FLASH_KEY = "flash"


def flash(request: Request, text: str, *, ok: bool = True) -> None:
    """ok=False — это проблема: показываем полным цветом текста, без красного."""
    request.session[FLASH_KEY] = {"text": text[:400], "ok": ok}


def pop_flash(request: Request) -> dict[str, Any] | None:
    session = getattr(request, "session", None)
    if not session:
        return None
    value = session.pop(FLASH_KEY, None)
    return value if isinstance(value, dict) else None
