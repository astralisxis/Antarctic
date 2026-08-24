"""Тексты бота.

Здесь только то, что не правится из админки: структура экранов и служебные строки.
Приветствие, тексты «Заработать», часы поддержки и прочее живут в настройках
(app/services/settings_store.py) — их админ меняет без деплоя.

Тон: спокойно и по делу. Эмодзи используются как короткие визуальные метки,
а не как украшение каждой строки. Без восклицаний и поздравлений.
Разметка — HTML телеграма.
"""

from __future__ import annotations

import datetime as dt
from html import escape

from app.money import fmt_money
from app.timeutil import to_local

DASH = "—"


def esc(value: object) -> str:
    """Экранировать данные пользователя: имя может содержать < или &."""
    return escape(str(value), quote=False)


def bold(value: object) -> str:
    return f"<b>{esc(value)}</b>"


def code(value: object) -> str:
    return f"<code>{esc(value)}</code>"


def when(value: dt.datetime | None) -> str:
    """Дата для списков: «20.08 14:03» в поясе проекта."""
    local = to_local(value)
    return local.strftime("%d.%m %H:%M") if local else DASH


# --------------------------------------------------------------------------- #
#  Общее
# --------------------------------------------------------------------------- #
SOON = "🧩 Раздел скоро откроется."

ERROR = "⚠️ Не получилось выполнить. Попробуйте ещё раз или напишите в поддержку."

UNKNOWN = "ℹ️ Выберите нужный раздел на клавиатуре ниже."


def banned(reason: str | None, until: str | None) -> str:
    lines = ["⛔ Доступ к магазину ограничен."]
    if reason:
        lines.append(f"Причина: {esc(reason)}")
    lines.append(f"Ограничение снимется: {esc(until)}" if until else "Ограничение постоянное.")
    lines.append("💬 Вопросы — через поддержку.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
#  Профиль
# --------------------------------------------------------------------------- #
def profile(
    *,
    tg_id: int,
    balance: int,
    total_topup: int,
    orders_count: int,
    ref_earned: int,
) -> str:
    return "\n".join(
        [
            bold("👤 Профиль"),
            "",
            f"Ваш ID: {code(tg_id)}",
            f"Баланс: {bold(fmt_money(balance))}",
            f"Пополнено всего: {fmt_money(total_topup)}",
            f"Покупок: {orders_count}",
            f"Заработано на рефералах: {fmt_money(ref_earned)}",
        ]
    )


def referral(*, link: str, percent: int, invited: int, earned: int, note: str) -> str:
    blocks = [bold("🔗 Реферальная система")]
    if note:
        blocks.append(note)  # текст из админки, разметку не трогаем
    blocks.append(f"Ваша ссылка:\n{code(link)}")
    blocks.append(
        "\n".join(
            [
                f"Процент с пополнений: {bold(f'{percent}%')}",
                f"Приглашено: {invited}",
                f"Начислено: {fmt_money(earned)}",
            ]
        )
    )
    return "\n\n".join(blocks)


ACCOUNTS_EMPTY = "\n".join(
    [
        bold("📱 Мои аккаунты"),
        "",
        "Купленных номеров пока нет. Они появятся здесь сразу после покупки,",
        "вместе с кнопкой запроса кода входа.",
    ]
)

TOPUPS_EMPTY = "\n".join([bold("💳 История пополнений"), "", "Пополнений пока не было."])

ORDERS_EMPTY = "\n".join([bold("🧾 История заказов"), "", "Заказов пока не было."])


def leaders(rows: list[str], position: int | None) -> str:
    lines = [bold("🏆 Лидеры покупателей"), "", "Рейтинг по числу успешных покупок."]
    lines += ["", *rows] if rows else ["", "В рейтинге пока никого нет."]
    if position:
        lines += ["", f"Ваше место: {bold(position)}"]
    return "\n".join(lines)


def promos_history(rows: list[str]) -> str:
    lines = [
        bold("🎟 Промокоды"),
        "",
        "Введите промокод сообщением. Бонус сразу появится на балансе.",
    ]
    if rows:
        lines += ["", bold("Активированные"), *rows]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
#  Магазин
# --------------------------------------------------------------------------- #
SHOP_EMPTY = "\n".join(
    [
        bold("🛍 Магазин"),
        "",
        "Сейчас нет стран в наличии. Загляните позже.",
    ]
)


def shop_root(balance: int) -> str:
    return "\n".join(
        [
            bold("🛍 Магазин"),
            "",
            "Выберите страну номера.",
            f"На балансе: {fmt_money(balance)}",
        ]
    )


def offer_card(
    *,
    title: str,
    price: int,
    balance: int,
    in_stock: bool,
    description: str | None = None,
    guarantee_hours: int = 12,
) -> str:
    """Карточка лота: описание, правила и отдельная рекомендация."""
    lines = [bold(f"🌍 {title}"), "", f"Цена: {bold(fmt_money(price))}"]
    if description:
        lines.extend(["", bold("Описание товара"), esc(description)])
    lines.extend(
        [
            "",
            bold("Правила покупки"),
            "• Автоматическая выдача товара после успешной оплаты.",
            f"• Гарантия на замену: {guarantee_hours} ч.",
            f"• Код входа можно получать в течение {guarantee_hours} ч. после покупки.",
            "• Перед покупкой проверьте, что сможете принять код и войти в Telegram.",
            "",
            bold("Рекомендация"),
            "Желательно заходить через VPN или прокси страны купленного номера.",
            "",
            "────────────────",
            "Отзывы: <a href=\"https://t.me/reviews_antarctic\">@reviews_antarctic</a>",
            "Канал: <a href=\"https://t.me/antarcticXshop\">@antarcticXshop</a>",
        ]
    )
    lines.append("")
    if not in_stock:
        lines.append("⏳ Сейчас нет в наличии. Загляните позже.")
    elif balance < price:
        lines.append(f"На балансе {fmt_money(balance)} — не хватает {fmt_money(price - balance)}.")
        lines.append("💳 Пополните баланс кнопкой ниже.")
    else:
        lines.append(f"На балансе: {fmt_money(balance)}")
    return "\n".join(lines)


def gift_purchase_prompt(title: str) -> str:
    return "\n".join(
        [
            bold("🎁 Подарок при покупке"),
            "",
            f"Товар: {esc(title)}",
            "Введите Telegram ID получателя или его username в формате @username.",
            "Получатель должен хотя бы один раз запустить этого бота.",
            "",
            "Отправитель оплачивает заказ, а аккаунт сразу появится у получателя.",
        ]
    )


def gift_purchase_confirm(title: str, price: int, recipient: str, tg_id: int) -> str:
    return "\n".join(
        [
            bold("🎁 Проверьте подарок"),
            "",
            f"Товар: {esc(title)}",
            f"Цена: {bold(fmt_money(price))}",
            f"Получатель: {esc(recipient)}",
            f"Telegram ID: {code(tg_id)}",
            "",
            "После подтверждения деньги спишутся, а заказ будет выдан получателю.",
        ]
    )


def gift_purchase_done(title: str, recipient: str) -> str:
    return "\n".join(
        [
            bold("✅ Подарок оформлен"),
            "",
            f"Товар: {esc(title)}",
            f"Получатель: {esc(recipient)}",
            "Заказ автоматически добавлен в его раздел «Мои аккаунты».",
        ]
    )


BUYING = "⏳ Подбираем номер. Это занимает несколько секунд."


def account_card(
    *,
    title: str,
    phone: str | None,
    tg_password: str | None = None,
    code_at: str | None = None,
    code_until: str | None = None,
    code_open: bool = True,
    hint: str | None = None,
    fresh: bool = False,
    replacement_status: str | None = None,
    replacement_error: str | None = None,
    account_valid: bool | None = None,
) -> str:
    """Карточка купленного номера.

    Кода входа здесь нет: он приходит отдельным сообщением, чтобы его не стирала
    следующая правка карточки и чтобы он остался в истории чата.

    Логина и пароля у номера тоже нет — вход в Telegram идёт по номеру и коду.
    """
    lines = [bold(f"📱 {title}")]
    if fresh:
        lines.append("✅ Номер куплен.")
    lines.append("")
    lines.append(f"Номер: {code(phone) if phone else DASH}")
    if tg_password:
        lines.append(f"Облачный пароль: {code(tg_password)}")

    if account_valid is True:
        lines.append("Проверка: аккаунт действителен.")
    elif account_valid is False:
        lines.append("Проверка: аккаунт недействителен — запросите замену по гарантии.")

    replacement_titles = {
        "pending": "Заявка на замену ожидает решения администратора.",
        "processing": "Администратор подтвердил замену, идёт покупка нового аккаунта.",
        "review": "Покупка замены ожидает перепроверки администратором.",
        "completed": "✅ Замена выполнена: здесь уже показан новый аккаунт.",
        "rejected": "Заявка на замену отклонена.",
        "failed": "Замену пока не удалось купить.",
    }
    replacement_text = replacement_titles.get(replacement_status or "")
    if replacement_text:
        lines += ["", replacement_text]
        if replacement_error and replacement_status == "rejected":
            lines.append(f"Причина: {esc(replacement_error)}")

    lines.append("")
    if code_open:
        lines.append("Код входа придёт отдельным сообщением — нажмите кнопку ниже.")
        if code_until:
            lines.append(f"Код доступен до {esc(code_until)}.")
    else:
        lines.append("Срок самостоятельной выдачи кода вышел — дальше поможет поддержка.")
    if code_at:
        lines.append(f"Последний код выдан {esc(code_at)}.")
    if hint and code_open:
        lines.append("")
        lines.append(hint)  # текст из админки, разметку не трогаем
    return "\n".join(lines)


def login_code(
    *, title: str, phone: str | None, value: str, hint: str | None = None
) -> str:
    """Отдельное сообщение с кодом входа: код тапом копируется, карточка остаётся."""
    lines = [
        bold("🔐 Код входа"),
        "",
        f"{esc(title)}, {code(phone) if phone else DASH}",
        f"Код: {code(value)}",
    ]
    if hint:
        lines += ["", hint]  # текст из админки, разметку не трогаем
    return "\n".join(lines)


def accounts_list(rows: list[str]) -> str:
    return "\n".join([bold("📱 Мои аккаунты"), "", *rows])


def orders_list(rows: list[str]) -> str:
    return "\n".join([bold("🧾 История заказов"), "", *rows])


def topups_list(rows: list[str]) -> str:
    return "\n".join([bold("💳 История пополнений"), "", *rows])


RESET_DONE = "✅ Чужие сессии сброшены. В аккаунте остались только вы."


def cleanup_confirm() -> str:
    return "\n".join(
        [
            bold("🧹 Очистить аккаунт"),
            "",
            "Будут удалены личные диалоги и контакты, а из чужих групп и каналов аккаунт выйдет.",
            "",
            "Созданные этим аккаунтом группы и каналы останутся без изменений.",
            "Действие нельзя отменить. Продолжить?",
        ]
    )


CLEANUP_RUNNING = "⏳ Подключаюсь к аккаунту и очищаю данные. Это может занять несколько минут."


def cleanup_report(report: dict[str, int]) -> str:
    lines = [bold("✅ Очистка завершена"), ""]
    lines.extend(
        [
            f"Личные диалоги удалены: {report.get('dialogs_deleted', 0)}",
            f"Из групп вышли: {report.get('groups_left', 0)}",
            f"Из каналов вышли: {report.get('channels_left', 0)}",
            f"Контакты удалены: {report.get('contacts_deleted', 0)}",
        ]
    )
    skipped = report.get("owned_communities_skipped", 0)
    failed = report.get("failed", 0)
    if skipped:
        lines.append(f"Созданные аккаунтом сообщества пропущены: {skipped}")
    if failed:
        lines.append(f"Не обработано из-за ошибок: {failed}")
    return "\n".join(lines)


def replacement_prompt(hours: int, until: str | None) -> str:
    lines = [bold("♻ Заменить аккаунт"), "", f"Гарантия действует {hours} ч."]
    if until:
        lines.append(f"До: {esc(until)}.")
    lines.extend(["", "Заявка будет передана в поддержку. Старый аккаунт останется доступен до решения.", "Продолжить?"])
    return "\n".join(lines)


def replacement_sent() -> str:
    return "✅ Заявка на замену отправлена. Администратор проверит её и подтвердит либо отклонит."


def gift_received(title: str) -> str:
    return f"✅ Аккаунт «{esc(title)}» передан вам. Откройте раздел «Мои аккаунты», чтобы получить код."


# --------------------------------------------------------------------------- #
#  Пополнение
# --------------------------------------------------------------------------- #
TOPUP_OFF = "💳 Способы оплаты сейчас отключены. Напишите в поддержку."

TOPUP_RESTRICTED = "⛔ Пополнение для вашего аккаунта ограничено. Напишите в поддержку."

TOPUP_WAIT = "⏳ Оплата пока не пришла. Если вы уже заплатили, подождите минуту и нажмите ещё раз."

TOPUP_CANCELLED = "↩ Счёт отменён."

TOPUP_STALE = "ℹ️ Этот счёт больше не активен. Создайте новый."

TOPUP_CREATING = "⏳ Выставляем счёт."


def topup_root(*, balance: int, minimum: int, maximum: int) -> str:
    return "\n".join(
        [
            bold("💳 Пополнение баланса"),
            "",
            f"Баланс: {bold(fmt_money(balance))}",
            f"Сумма: от {fmt_money(minimum)} до {fmt_money(maximum)}",
            "",
            "Введите сумму в рублях или выберите ниже.",
        ]
    )


def topup_bad_amount(*, minimum: int, maximum: int) -> str:
    return "\n".join(
        [
            "ℹ️ Укажите сумму в рублях, например 500.",
            f"От {fmt_money(minimum)} до {fmt_money(maximum)}.",
        ]
    )


def topup_methods(*, amount: int, hints: list[str] | None = None) -> str:
    lines = [bold("💳 Пополнение баланса"), "", f"Сумма: {bold(fmt_money(amount))}", ""]
    lines.append("Выберите способ оплаты.")
    if hints:
        lines.append("")
        lines += [esc(h) for h in hints]
    return "\n".join(lines)


def topup_invoice(
    *,
    amount: int,
    method: str,
    charge: str,
    minutes: int,
    rate: str | None = None,
) -> str:
    """Экран счёта: сколько платить, чем и до какого момента он живёт."""
    lines = [
        bold("🧾 Счёт на оплату"),
        "",
        f"К зачислению: {bold(fmt_money(amount))}",
        f"К оплате: {bold(charge)}",
        f"Способ: {esc(method)}",
    ]
    if rate:
        lines.append(f"Курс: {esc(rate)}")
    lines += [
        "",
        f"Счёт действует {minutes} мин. Оплатите кнопкой ниже,",
        "затем нажмите «Проверить оплату».",
    ]
    return "\n".join(lines)


def topup_paid(*, amount: int, balance: int) -> str:
    return "\n".join(
        [
            bold("✅ Баланс пополнен"),
            "",
            f"Зачислено: {bold(fmt_money(amount))}",
            f"Баланс: {bold(fmt_money(balance))}",
        ]
    )


def topup_referral(*, amount: int, balance: int) -> str:
    return "\n".join(
        [
            f"🔗 Начислено с пополнения приглашённого: {bold(fmt_money(amount))}",
            f"Баланс: {fmt_money(balance)}",
        ]
    )


# --------------------------------------------------------------------------- #
#  Заработать
# --------------------------------------------------------------------------- #
EARN_ROOT = "\n".join([bold("💼 Заработать"), "", "Выберите направление."])

EARN_OFF = "⏸ Раздел временно недоступен."


def earn_section(title: str, body: str) -> str:
    """body — текст из админки, разметку не трогаем: там могут быть ссылки."""
    return "\n".join([bold(title), "", body])


# --------------------------------------------------------------------------- #
#  Отзывы
# --------------------------------------------------------------------------- #
REVIEW_OFF = "⭐ Отзывы сейчас не собираем."

REVIEW_EXISTS = "⭐ По этому номеру отзыв уже есть. Спасибо."

REVIEW_CANCELLED = "↩ Отзыв не отправлен."

REVIEW_ASK_TEXT = "\n".join(
    [
        bold("⭐ Отзыв"),
        "",
        "Напишите пару слов о покупке — их увидят в канале отзывов.",
        "Ник будет скрыт звёздочками.",
        "",
        "Можно и без текста — кнопкой ниже.",
    ]
)


def review_ask_stars(*, title: str) -> str:
    return "\n".join([bold("⭐ Отзыв"), "", f"{esc(title)}", "", "Поставьте оценку."])


def review_done(*, published: bool) -> str:
    tail = (
        "Он появится в канале отзывов."
        if published
        else "Он появится в канале после проверки."
    )
    return "\n".join([bold("✅ Отзыв принят"), "", tail])


# --------------------------------------------------------------------------- #
#  Поддержка
# --------------------------------------------------------------------------- #
def support(*, hours: str, has_bot: bool) -> str:
    lines = [bold("💬 Поддержка"), ""]
    if has_bot:
        lines.append("Напишите в бот поддержки — ответим в рабочее время.")
    else:
        lines.append("Бот поддержки подключается. Пока напишите позже.")
    lines += ["", f"Часы работы: {hours}"]
    return "\n".join(lines)


def support_start(*, hours: str) -> str:
    """Первый экран бота поддержки: часы работы и что делать дальше."""
    return "\n".join(
        [
            bold("💬 Поддержка магазина"),
            "",
            f"Часы работы: {esc(hours)}",
            "",
            "Опишите вопрос одним сообщением — номер заказа, если он есть.",
            "Ответ придёт сюда же.",
        ]
    )


SUPPORT_SENT = "✅ Сообщение передано поддержке."

SUPPORT_ONLY_TEXT = "ℹ️ Пришлите вопрос текстом или картинкой — остальное не дойдёт."

SUPPORT_TOO_LONG = "ℹ️ Сообщение слишком длинное. Разбейте его на части."

SUPPORT_FLOOD = "⏳ Сообщение уже принято, подождите ответа."


def support_reply(*, text: str) -> str:
    """Ответ админа клиенту. Текст пишет человек, разметку не трогаем."""
    return "\n".join([bold("💬 Поддержка"), "", text])


SUPPORT_CLOSED_NOTE = "✅ Обращение закрыто. Если вопрос остался — напишите ещё раз."
