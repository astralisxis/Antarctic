"""Настройки: тексты бота, проценты, переключатели разделов.

Набор ключей задан в app/services/settings_store.py — там же и типы полей.
Форма собирается из него, поэтому новый ключ появляется в админке сам.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from app.admin.counters import nav_counts
from app.admin.deps import DbSession, StaffAdmin
from app.admin.notice import flash
from app.admin.templating import render
from app.enums import LogSection
from app.money import parse_rub, rub_input
from app.services import admin_guard, settings_store
from app.services.events import log_event

router = APIRouter()

TRUE = "1"
FALSE = "0"
# Telegram accepts at most 4096 characters in a text message.  Dynamic
# screens add their own headings and hints, so leave room for that wrapper.
TEXT_SETTING_LIMIT = 3500


def _groups() -> dict[str, list[settings_store.SettingDef]]:
    out: dict[str, list[settings_store.SettingDef]] = {}
    for d in settings_store.DEFAULTS:
        out.setdefault(d.group, []).append(d)
    return out


def _shown(values: dict[str, str]) -> dict[str, Any]:
    """Значения в том виде, в каком их правит человек: деньги — рублями."""
    out: dict[str, Any] = {}
    for d in settings_store.DEFAULTS:
        raw = values.get(d.key, d.default)
        if d.kind == "bool":
            out[d.key] = raw.strip() in {"1", "true", "yes", "on"}
        elif d.kind == "money":
            out[d.key] = rub_input(int(raw) if raw.strip().lstrip("-").isdigit() else 0)
        else:
            out[d.key] = raw
    return out


@router.get("/settings")
async def index(request: Request, session: DbSession, admin: StaffAdmin):
    values = _shown(await settings_store.all_values(session))
    return render(
        request,
        "settings.html",
        {
            "groups": _groups(),
            "v": values,
            "ip_stat": await admin_guard.counts(session),
            "counts": await nav_counts(session),
        },
        active="settings",
    )


@router.post("/settings")
async def save(request: Request, session: DbSession, admin: StaffAdmin):
    form = await request.form()
    to_write: dict[str, str] = {}
    errors: list[str] = []

    for d in settings_store.DEFAULTS:
        if d.kind == "bool":
            to_write[d.key] = TRUE if form.get(d.key) else FALSE
            continue

        raw = str(form.get(d.key) or "").strip()

        if d.kind == "money":
            kop = parse_rub(raw)
            if kop is None:
                errors.append(f"«{d.title}»: сумма не разобралась.")
                continue
            to_write[d.key] = str(kop)
        elif d.kind == "int":
            try:
                number = int(raw or 0)
            except ValueError:
                errors.append(f"«{d.title}»: нужно целое число.")
                continue
            if number < 0:
                errors.append(f"«{d.title}»: число не может быть отрицательным.")
                continue
            if d.key.endswith("percent") and number > 100:
                errors.append(f"«{d.title}»: процент больше 100 не бывает.")
                continue
            to_write[d.key] = str(number)
        elif d.kind == "url":
            if raw and not raw.startswith(("http://", "https://")):
                errors.append(f"«{d.title}»: ссылка должна начинаться с https://.")
                continue
            to_write[d.key] = raw
        else:
            if d.kind in {"text", "string"} and len(raw) > TEXT_SETTING_LIMIT:
                errors.append(
                    f"«{d.title}»: текст слишком длинный — максимум {TEXT_SETTING_LIMIT} символов."
                )
                continue
            to_write[d.key] = raw

    if errors:
        # Ничего не пишем: половина сохранённых настроек хуже, чем ни одной.
        flash(request, " ".join(errors), ok=False)
        return RedirectResponse("/settings", status_code=303)

    changed = await settings_store.set_many(session, to_write, admin_id=admin.id)
    if changed:
        await log_event(
            LogSection.ADMIN,
            "settings_changed",
            admin_id=admin.id,
            message=", ".join(changed[:20]),
            payload={"keys": changed},
        )
        flash(request, f"Сохранено. Изменено полей: {len(changed)}.")
    else:
        flash(request, "Ничего не изменилось.")
    return RedirectResponse("/settings", status_code=303)
