"""Настройки, которые правятся из админки без деплоя: тексты, проценты, переключатели."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Setting


@dataclass(frozen=True, slots=True)
class SettingDef:
    key: str
    default: str
    title: str
    group: str
    kind: str = "string"  # string | text | bool | int | money | url
    hint: str = ""


# Значение money хранится в копейках, в админке показывается рублями.
DEFAULTS: tuple[SettingDef, ...] = (
    # магазин
    SettingDef("shop.enabled", "1", "Магазин работает", "Магазин", "bool"),
    SettingDef(
        "shop.disabled_text",
        "Магазин временно закрыт. Откроемся в ближайшее время.",
        "Текст при закрытом магазине",
        "Магазин",
        "text",
    ),
    SettingDef(
        "shop.code_wait_hint",
        "Код приходит в течение минуты после запроса. Если кода нет — запросите повторно.",
        "Подсказка при выдаче кода",
        "Магазин",
        "text",
    ),
    SettingDef(
        "shop.code_hours",
        "12",
        "Код входа доступен, часов после покупки",
        "Магазин",
        "int",
        hint="0 — без срока; после срока код выдаёт только поддержка",
    ),
    # бот
    SettingDef(
        "bot.welcome",
        "Магазин номеров. Выберите раздел ниже.",
        "Приветствие",
        "Бот",
        "text",
    ),
    # пополнение
    SettingDef("topup.min", "5000", "Минимальное пополнение", "Пополнение", "money"),
    SettingDef("topup.max", "10000000", "Максимальное пополнение", "Пополнение", "money"),
    SettingDef("topup.cryptobot_enabled", "1", "Crypto Bot", "Пополнение", "bool"),
    SettingDef("topup.xrocket_enabled", "1", "xRocket", "Пополнение", "bool"),
    SettingDef("topup.platega_enabled", "1", "Карта / СБП (Platega)", "Пополнение", "bool"),
    SettingDef(
        "topup.invoice_minutes",
        "30",
        "Счёт действует, минут",
        "Пополнение",
        "int",
        hint="1—1440: дольше суток xRocket счёт не держит",
    ),
    SettingDef(
        "topup.xrocket_asset",
        "USDT",
        "Валюта счёта xRocket",
        "Пополнение",
        "string",
        hint="рублей у xRocket в API нет, счёт выставляется в крипте по курсу Crypto Bot",
    ),
    SettingDef(
        "topup.xrocket_markup",
        "3",
        "Наценка к курсу xRocket, %",
        "Пополнение",
        "int",
        hint="xRocket удерживает с магазина 1,5% — наценка их перекрывает",
    ),
    # рефералы
    SettingDef("referral.percent", "10", "Процент с пополнений реферала", "Рефералы", "int"),
    SettingDef(
        "referral.text",
        "Приводите людей по своей ссылке и получайте процент с каждого их пополнения.",
        "Текст раздела",
        "Рефералы",
        "text",
    ),
    # заработать
    SettingDef("earn.enabled", "1", "Раздел «Заработать» виден", "Заработать", "bool"),
    SettingDef(
        "earn.comments.text",
        "Комментарии в TikTok.\n\nУсловия и оплата — у менеджера.",
        "Текст: комментарии TikTok",
        "Заработать",
        "text",
    ),
    SettingDef(
        "earn.video.enabled", "1", "Кнопка «Видео TikTok» видна", "Заработать", "bool"
    ),
    SettingDef(
        "earn.video.text",
        "Видео в TikTok.\n\nУсловия и оплата — у менеджера.",
        "Текст: видео TikTok",
        "Заработать",
        "text",
    ),
    SettingDef("earn.manager", "", "Менеджер (@username)", "Заработать", "string"),
    # поддержка
    SettingDef(
        "support.hours", "10:00 — 22:00 МСК, ежедневно", "Часы работы", "Поддержка", "string"
    ),
    SettingDef(
        "support.auto_reply",
        "Обращение принято. Поддержка работает {hours} — ответим в это время.",
        "Автоответ",
        "Поддержка",
        "text",
        hint="{hours} подставляется из часов работы",
    ),
    # отзывы
    SettingDef("reviews.enabled", "1", "Предлагать отзыв после покупки", "Отзывы", "bool"),
    SettingDef(
        "reviews.channel_chat",
        "",
        "Канал отзывов для публикации",
        "Отзывы",
        "string",
        hint="@username канала или его числовой id; бот должен быть там админом",
    ),
    SettingDef("reviews.channel_url", "", "Ссылка на канал отзывов", "Отзывы", "url"),
    SettingDef(
        "reviews.auto_publish", "1", "Публиковать без модерации", "Отзывы", "bool"
    ),
    SettingDef(
        "reviews.card_image",
        "1",
        "Публиковать карточкой-картинкой",
        "Отзывы",
        "bool",
        hint="выключено — уйдёт аватарка с подписью текстом, как раньше",
    ),
    SettingDef(
        "reviews.card_bg",
        "media/review_card_bg.jpg",
        "Фон карточки (файл)",
        "Отзывы",
        "string",
        hint="путь от корня проекта; загрузить картинку удобнее на странице «Отзывы»",
    ),
)

BY_KEY: dict[str, SettingDef] = {d.key: d for d in DEFAULTS}


async def ensure_defaults(session: AsyncSession) -> int:
    """Досоздать отсутствующие ключи. Существующие значения не трогаем."""
    existing = set((await session.scalars(select(Setting.key))).all())
    added = 0
    for d in DEFAULTS:
        if d.key not in existing:
            session.add(Setting(key=d.key, value=d.default))
            added += 1
    if added:
        await session.commit()
    return added


async def get(session: AsyncSession, key: str) -> str | None:
    value = await session.scalar(select(Setting.value).where(Setting.key == key))
    if value is None:
        d = BY_KEY.get(key)
        return d.default if d else None
    return value


async def get_bool(session: AsyncSession, key: str) -> bool:
    return (await get(session, key) or "0").strip() in {"1", "true", "yes", "on"}


async def get_int(session: AsyncSession, key: str, fallback: int = 0) -> int:
    try:
        return int((await get(session, key) or "").strip())
    except ValueError:
        return fallback


async def set_value(
    session: AsyncSession, key: str, value: str, admin_id: int | None = None
) -> None:
    row = await session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=value, updated_by=admin_id))
    else:
        row.value = value
        row.updated_by = admin_id
    await session.commit()


async def set_many(
    session: AsyncSession, values: dict[str, str], admin_id: int | None = None
) -> list[str]:
    """Записать сразу несколько ключей одной транзакцией. Возвращает изменённые.

    Форма настроек присылает все поля разом, а в лог должно попасть только то,
    что действительно поменялось.
    """
    rows = {
        row.key: row
        for row in (await session.scalars(select(Setting).where(Setting.key.in_(values)))).all()
    }
    changed: list[str] = []
    for key, value in values.items():
        row = rows.get(key)
        if row is None:
            session.add(Setting(key=key, value=value, updated_by=admin_id))
            changed.append(key)
        elif (row.value or "") != value:
            row.value = value
            row.updated_by = admin_id
            changed.append(key)
    if changed:
        await session.commit()
    return changed


async def all_values(session: AsyncSession) -> dict[str, str]:
    rows = (await session.scalars(select(Setting))).all()
    values = {d.key: d.default for d in DEFAULTS}
    values.update({r.key: (r.value or "") for r in rows})
    return values
