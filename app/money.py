"""Деньги: копейки внутри, рубли на экране."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

NBSP = " "  # тонкий пробел для разрядов


def rub_to_kop(value: float | int | str | Decimal) -> int:
    """Рубли (в том числе дробные, как их отдаёт LZT) → копейки."""
    return int(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * 100)


def kop_to_rub(kop: int) -> Decimal:
    return (Decimal(kop) / 100).quantize(Decimal("0.01"))


def fmt_money(kop: int | None, sign: bool = False) -> str:
    """1234567 → «12 345,67 ₽». sign=True добавляет + для положительных."""
    if kop is None:
        return "—"
    value = kop_to_rub(abs(kop))
    whole, frac = f"{value:.2f}".split(".")
    groups = []
    while len(whole) > 3:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    groups.insert(0, whole)
    body = NBSP.join(groups)
    tail = "" if frac == "00" else f",{frac}"
    prefix = "−" if kop < 0 else ("+" if sign and kop > 0 else "")
    return f"{prefix}{body}{tail}{NBSP}₽"


def fmt_int(value: int | None) -> str:
    if value is None:
        return "—"
    s = str(abs(value))
    groups = []
    while len(s) > 3:
        groups.insert(0, s[-3:])
        s = s[:-3]
    groups.insert(0, s)
    return ("−" if value < 0 else "") + NBSP.join(groups)


def fmt_pct(part: int, whole: int) -> str:
    """Доля в процентах без десятых, когда нечего показывать."""
    if not whole:
        return "—"
    pct = part / whole * 100
    return f"{pct:.0f}%" if pct >= 10 or pct == 0 else f"{pct:.1f}%"


def parse_rub(value: str | int | float | None) -> int | None:
    """Ввод из формы админки → копейки. None, если это не число.

    Принимаем то, что человек реально печатает: «49», «49,50», «1 200.5», «49 ₽».
    Пробелы любые, включая неразрывные из скопированной строки.
    """
    if value is None:
        return None
    if isinstance(value, int | float):
        return rub_to_kop(value)

    text = str(value).strip().replace(",", ".").replace("₽", "")
    text = "".join(ch for ch in text if not ch.isspace())
    if not text:
        return None
    try:
        return rub_to_kop(Decimal(text))
    except (InvalidOperation, ValueError, ArithmeticError):
        return None


def rub_input(kop: int | None) -> str:
    """Копейки → значение для поля ввода: «49» или «49.50», без знаков валюты."""
    if kop is None:
        return ""
    value = kop_to_rub(kop)
    return f"{value:.2f}".rstrip("0").rstrip(".") or "0"
