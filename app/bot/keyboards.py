"""Клавиатуры бота.

Навигация двухслойная и это осознанно:
  · нижняя клавиатура — пять разделов, доступны из любого места в один тап
    (приоритет «скорость навигации»: не надо мотать чат в поисках кнопки);
  · внутри раздела — inline-кнопки, сообщение правится на месте,
    чат не забивается.

Схема callback_data: «префикс:действие[:значение]». Префиксы:
    pr  — профиль,  ea — заработать,  sh — магазин,  tp — пополнение,
    rv  — отзывы
"""

from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

BTN_SHOP = "🛍 Магазин"
BTN_TOPUP = "💳 Пополнить баланс"
BTN_PROFILE = "👤 Профиль"
BTN_EARN = "💼 Заработать"
BTN_SUPPORT = "💬 Поддержка"

# Старые подписи уже могли быть отправлены пользователю или сохранены в
# тестовых/внешних клиентах. Принимаем их наряду с новыми.
BTN_ALIASES = {
    BTN_SHOP: frozenset({BTN_SHOP, "Магазин"}),
    BTN_TOPUP: frozenset({BTN_TOPUP, "Пополнить баланс"}),
    BTN_PROFILE: frozenset({BTN_PROFILE, "Профиль"}),
    BTN_EARN: frozenset({BTN_EARN, "Заработать"}),
    BTN_SUPPORT: frozenset({BTN_SUPPORT, "Поддержка"}),
}

MAIN_BUTTONS = frozenset().union(*BTN_ALIASES.values())

BACK = "↩ Назад"


def main_menu(*, earn: bool = True) -> ReplyKeyboardMarkup:
    """Постоянная клавиатура. «Заработать» скрывается переключателем в админке."""
    rows: list[list[KeyboardButton]] = [
        [KeyboardButton(text=BTN_SHOP), KeyboardButton(text=BTN_TOPUP)],
    ]
    second = [KeyboardButton(text=BTN_PROFILE)]
    if earn:
        second.append(KeyboardButton(text=BTN_EARN))
    rows.append(second)
    rows.append([KeyboardButton(text=BTN_SUPPORT)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


# --------------------------------------------------------------------------- #
#  Профиль
# --------------------------------------------------------------------------- #
def profile_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Реферальная система", callback_data="pr:ref")],
            [InlineKeyboardButton(text="📱 Мои аккаунты", callback_data="pr:accounts")],
            # По одной в строке: подписи длинные, в паре телеграм их обрежет.
            [InlineKeyboardButton(text="💳 История пополнений", callback_data="pr:topups")],
            [InlineKeyboardButton(text="🧾 История заказов", callback_data="pr:orders")],
            [InlineKeyboardButton(text="🏆 Лидеры покупателей", callback_data="pr:leaders")],
            [InlineKeyboardButton(text="🎟 Промокоды", callback_data="pr:promos")],
        ]
    )


def profile_back(*, share_link: str | None = None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if share_link:
        rows.append([InlineKeyboardButton(text="↗ Поделиться ссылкой", url=share_link)])
    rows.append([InlineKeyboardButton(text=BACK, callback_data="pr:root")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# --------------------------------------------------------------------------- #
#  Заработать
# --------------------------------------------------------------------------- #
def earn_menu(*, video: bool) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="💬 Комментарии TikTok", callback_data="ea:comments")]]
    if video:
        rows.append([InlineKeyboardButton(text="🎥 Видео TikTok", callback_data="ea:video")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def earn_back(*, manager: str | None = None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if manager:
        rows.append(
            [
                InlineKeyboardButton(
                    text="✉ Написать менеджеру", url=f"https://t.me/{manager.lstrip('@')}"
                )
            ]
        )
    rows.append([InlineKeyboardButton(text=BACK, callback_data="ea:root")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# --------------------------------------------------------------------------- #
#  Магазин
# --------------------------------------------------------------------------- #
def shop_countries(offers: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    """offers: (id позиции, подпись). По одной стране в строке — подписи длинные."""
    rows = [
        [InlineKeyboardButton(text=f"🌍 {label}", callback_data=f"sh:c:{offer_id}")]
        for offer_id, label in offers
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def offer_card(
    offer_id: int,
    *,
    buy_label: str | None = None,
    gift_label: str | None = None,
) -> InlineKeyboardMarkup:
    """Карточка страны. Кнопка покупки с ценой в подписи — цена видна в момент нажатия."""
    rows: list[list[InlineKeyboardButton]] = []
    if buy_label:
        rows.append([InlineKeyboardButton(text=f"🛒 {buy_label}", callback_data=f"sh:buy:{offer_id}")])
    if gift_label:
        rows.append(
            [InlineKeyboardButton(text=f"🎁 {gift_label}", callback_data=f"sh:giftbuy:{offer_id}")]
        )
    rows.append([InlineKeyboardButton(text=BACK, callback_data="sh:root")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def gift_recipient_back(offer_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=BACK, callback_data=f"sh:giftcancel:{offer_id}")]
        ]
    )


def gift_purchase_confirm(offer_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Купить и подарить",
                    callback_data=f"sh:giftconfirm:{offer_id}",
                )
            ],
            [InlineKeyboardButton(text="Отмена", callback_data=f"sh:giftcancel:{offer_id}")],
        ]
    )


def account_card(
    order_id: int,
    *,
    can_code: bool = True,
    can_cleanup: bool = False,
    can_review: bool = False,
    can_replace: bool = False,
) -> InlineKeyboardMarkup:
    """Карточка купленного номера.

    Порядок кнопок — код и подтверждённая очистка выше технических данных;
    отзыв предлагается последним, чтобы не мешал входу.
    """
    rows: list[list[InlineKeyboardButton]] = []
    if can_code:
        rows.append(
            [InlineKeyboardButton(text="🔐 Получить код", callback_data=f"sh:code:{order_id}")]
        )
    if can_cleanup:
        rows.append(
            [InlineKeyboardButton(text="🧹 Очистить аккаунт", callback_data=f"sh:clean:{order_id}")]
        )
    rows.append(
        [InlineKeyboardButton(text="♻ Сбросить чужие сессии", callback_data=f"sh:reset:{order_id}")]
    )
    if can_review:
        rows.append(
            [InlineKeyboardButton(text="⭐ Оставить отзыв", callback_data=f"rv:new:{order_id}")]
        )
    if can_replace:
        rows.append(
            [InlineKeyboardButton(text="♻ Заменить аккаунт", callback_data=f"sh:replace:{order_id}")]
        )
    rows.append(
        [
            InlineKeyboardButton(text="📱 Мои аккаунты", callback_data="pr:accounts"),
            InlineKeyboardButton(text="🛍 В магазин", callback_data="sh:root"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def account_back(order_id: int) -> InlineKeyboardMarkup:
    """Возврат к карточке номера."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=BACK, callback_data=f"sh:acc:{order_id}")]
        ]
    )


def cleanup_confirm(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Да, очистить",
                    callback_data=f"sh:clean:yes:{order_id}",
                )
            ],
            [InlineKeyboardButton(text=BACK, callback_data=f"sh:acc:{order_id}")],
        ]
    )


def accounts_list(items: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    """items: (id заказа, подпись)."""
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"sh:acc:{order_id}")]
        for order_id, label in items
    ]
    rows.append([InlineKeyboardButton(text=BACK, callback_data="pr:root")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def orders_list(items: list[tuple[int, str, bool]]) -> InlineKeyboardMarkup:
    """История заказов: открытие заказа и доступная замена рядом."""
    rows: list[list[InlineKeyboardButton]] = []
    for order_id, label, can_replace in items:
        row = [InlineKeyboardButton(text=label, callback_data=f"sh:acc:{order_id}")]
        if can_replace:
            row.append(
                InlineKeyboardButton(text="♻ Заменить", callback_data=f"sh:replace:{order_id}")
            )
        rows.append(row)
    rows.append([InlineKeyboardButton(text=BACK, callback_data="pr:root")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def replacement_confirm(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Отправить заявку", callback_data=f"sh:replace:yes:{order_id}")],
            [InlineKeyboardButton(text=BACK, callback_data=f"sh:acc:{order_id}")],
        ]
    )


# --------------------------------------------------------------------------- #
#  Пополнение
# --------------------------------------------------------------------------- #
def topup_amounts(values: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    """Быстрые суммы: (копейки, подпись). По две в строке — подписи короткие."""
    rows: list[list[InlineKeyboardButton]] = []
    for index in range(0, len(values), 2):
        rows.append(
            [
                InlineKeyboardButton(text=label, callback_data=f"tp:a:{kop}")
                for kop, label in values[index : index + 2]
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def topup_methods(items: list[tuple[str, str]], amount: int) -> InlineKeyboardMarkup:
    """items: (провайдер, подпись). Сумма едет в callback_data, а не в состоянии:
    после перезапуска бота кнопка должна работать, память состояний этого не умеет."""
    rows = [
        [InlineKeyboardButton(text=f"💳 {title}", callback_data=f"tp:p:{provider}:{amount}")]
        for provider, title in items
    ]
    rows.append([InlineKeyboardButton(text=BACK, callback_data="tp:root")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def topup_invoice(payment_id: int, url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💸 Оплатить", url=url)],
            [InlineKeyboardButton(text="🔄 Проверить оплату", callback_data=f"tp:c:{payment_id}")],
            [InlineKeyboardButton(text="✕ Отменить счёт", callback_data=f"tp:x:{payment_id}")],
        ]
    )


def topup_done() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🛍 В магазин", callback_data="sh:root"),
                InlineKeyboardButton(text="👤 Профиль", callback_data="pr:root"),
            ]
        ]
    )


# --------------------------------------------------------------------------- #
#  Отзывы
# --------------------------------------------------------------------------- #
def review_stars(order_id: int) -> InlineKeyboardMarkup:
    """Оценка одним тапом: компактные звёзды не занимают лишнюю строку."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{value}★", callback_data=f"rv:s:{order_id}:{value}"
                )
                for value in range(1, 6)
            ],
            [InlineKeyboardButton(text="Отмена", callback_data=f"sh:acc:{order_id}")],
        ]
    )


def review_text(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📝 Без текста", callback_data=f"rv:skip:{order_id}")],
            [InlineKeyboardButton(text="Отмена", callback_data=f"rv:cancel:{order_id}")],
        ]
    )


def review_done(*, channel_url: str | None = None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if channel_url:
        rows.append([InlineKeyboardButton(text="📣 Канал отзывов", url=channel_url)])
    rows.append(
        [
            InlineKeyboardButton(text="📱 Мои аккаунты", callback_data="pr:accounts"),
            InlineKeyboardButton(text="🛍 В магазин", callback_data="sh:root"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


# --------------------------------------------------------------------------- #
#  Поддержка
# --------------------------------------------------------------------------- #
def support_link(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="💬 Написать в поддержку", url=url)]]
    )
