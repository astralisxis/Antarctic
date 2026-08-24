"""Строковые перечисления. Хранятся в базе как строки — переносимо между sqlite и postgres."""

from __future__ import annotations

try:
    # StrEnum was added to the standard library in Python 3.11.
    from enum import StrEnum
except ImportError:  # pragma: no cover - used on Python 3.10
    from enum import Enum

    class StrEnum(str, Enum):
        """Python 3.10-compatible subset of :class:`enum.StrEnum`."""

        def __str__(self) -> str:
            return str(self.value)


class OrderStatus(StrEnum):
    NEW = "new"  # создан, деньги ещё не списаны
    SEARCHING = "searching"  # ищем лот на LZT
    BUYING = "buying"  # покупаем лот
    PURCHASED = "purchased"  # куплен, код ещё не запрашивали
    CODE_ISSUED = "code_issued"  # код выдан покупателю
    DONE = "done"  # покупатель подтвердил активацию
    FAILED = "failed"  # не получилось, деньги вернулись
    REFUNDED = "refunded"  # возврат вручную


class PaymentProvider(StrEnum):
    CRYPTOBOT = "cryptobot"
    XROCKET = "xrocket"
    PLATEGA = "platega"
    MANUAL = "manual"


class PaymentStatus(StrEnum):
    PENDING = "pending"
    PAID = "paid"
    EXPIRED = "expired"
    FAILED = "failed"


class TxKind(StrEnum):
    TOPUP = "topup"
    PURCHASE = "purchase"
    REFUND = "refund"
    REFERRAL = "referral"
    PROMO = "promo"
    ADMIN = "admin"  # ручная правка баланса


class ReviewStatus(StrEnum):
    PENDING = "pending"
    PUBLISHED = "published"
    REJECTED = "rejected"


class TicketStatus(StrEnum):
    OPEN = "open"  # ждёт ответа поддержки
    ANSWERED = "answered"  # поддержка ответила, ждём клиента
    CLOSED = "closed"


class MessageSender(StrEnum):
    USER = "user"
    ADMIN = "admin"
    SYSTEM = "system"


class BroadcastStatus(StrEnum):
    DRAFT = "draft"
    SENDING = "sending"
    PAUSED = "paused"
    DONE = "done"
    CANCELLED = "cancelled"


class DeliveryStatus(StrEnum):
    QUEUED = "queued"
    SENDING = "sending"  # короткая атомарная бронь строки фоновым воркером
    SENT = "sent"
    FAILED = "failed"
    BLOCKED = "blocked"  # пользователь заблокировал бота


class AdminRole(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    SUPPORT = "support"


class LogSection(StrEnum):
    """Разделы лога — по ним фильтрация в админке."""

    SYSTEM = "system"
    USER = "user"  # старты, апдейты профиля, реферальные привязки
    SHOP = "shop"  # выбор страны, заказ, выдача кода
    LZT = "lzt"  # запросы к маркету
    PAYMENT = "payment"
    BALANCE = "balance"
    REFERRAL = "referral"
    REVIEW = "review"
    SUPPORT = "support"
    EARN = "earn"
    BROADCAST = "broadcast"
    ADMIN = "admin"  # действия админов
    WEBAPP = "webapp"


class LogLevel(StrEnum):
    INFO = "info"
    WARN = "warn"
    ERROR = "error"


class YesNo(StrEnum):
    """Формат трёхзначных фильтров LZT Market."""

    YES = "yes"
    NO = "no"
    NOMATTER = "nomatter"


# --------------------------------------------------------------------------- #
#  Подписи статусов
# --------------------------------------------------------------------------- #
# Живут здесь, а не в шаблонах админки: те же слова видит покупатель в боте,
# и расходиться они не должны.
ORDER_STATUS_TITLES: dict[str, str] = {
    OrderStatus.NEW: "создан",
    OrderStatus.SEARCHING: "поиск лота",
    OrderStatus.BUYING: "покупка",
    OrderStatus.PURCHASED: "куплен",
    OrderStatus.CODE_ISSUED: "код выдан",
    OrderStatus.DONE: "завершён",
    OrderStatus.FAILED: "сбой",
    OrderStatus.REFUNDED: "возврат",
}

PAYMENT_STATUS_TITLES: dict[str, str] = {
    PaymentStatus.PENDING: "ожидает оплаты",
    PaymentStatus.PAID: "зачислено",
    PaymentStatus.EXPIRED: "просрочен",
    PaymentStatus.FAILED: "сбой",
}

PROVIDER_TITLES: dict[str, str] = {
    PaymentProvider.CRYPTOBOT: "Crypto Bot",
    PaymentProvider.XROCKET: "xRocket",
    PaymentProvider.PLATEGA: "Platega",
    PaymentProvider.MANUAL: "вручную",
}

TX_KIND_TITLES: dict[str, str] = {
    TxKind.TOPUP: "пополнение",
    TxKind.PURCHASE: "покупка",
    TxKind.REFUND: "возврат",
    TxKind.REFERRAL: "реферальное",
    TxKind.PROMO: "промокод",
    TxKind.ADMIN: "правка админа",
}

REVIEW_STATUS_TITLES: dict[str, str] = {
    ReviewStatus.PENDING: "на проверке",
    ReviewStatus.PUBLISHED: "опубликован",
    ReviewStatus.REJECTED: "отклонён",
}

TICKET_STATUS_TITLES: dict[str, str] = {
    TicketStatus.OPEN: "ждёт ответа",
    TicketStatus.ANSWERED: "отвечено",
    TicketStatus.CLOSED: "закрыто",
}

SENDER_TITLES: dict[str, str] = {
    MessageSender.USER: "клиент",
    MessageSender.ADMIN: "поддержка",
    MessageSender.SYSTEM: "система",
}

BROADCAST_STATUS_TITLES: dict[str, str] = {
    BroadcastStatus.DRAFT: "черновик",
    BroadcastStatus.SENDING: "идёт",
    BroadcastStatus.PAUSED: "на паузе",
    BroadcastStatus.DONE: "разослана",
    BroadcastStatus.CANCELLED: "отменена",
}

DELIVERY_STATUS_TITLES: dict[str, str] = {
    DeliveryStatus.QUEUED: "в очереди",
    DeliveryStatus.SENDING: "отправляется",
    DeliveryStatus.SENT: "доставлено",
    DeliveryStatus.FAILED: "не дошло",
    DeliveryStatus.BLOCKED: "заблокировал бота",
}

# --------------------------------------------------------------------------- #
#  Источники лота на маркете
# --------------------------------------------------------------------------- #
# Значения параметра origin[] запроса GET /telegram. Перечень и подписи отдаёт
# сам маркет (base_params в GET /telegram/params), поэтому руками их не выдумываем;
# «retrieve» у маркета приходит непереведённым ключом — подписан по смыслу.
LOT_ORIGIN_TITLES: dict[str, str] = {
    "autoreg": "Авторег",
    "self_registration": "Саморег",
    "personal": "Личный",
    "resale": "Перепродажа",
    "brute": "Брут",
    "phishing": "Фишинг",
    "stealer": "Стилер",
    "retrieve": "Восстановление",
    "retrieve_via_support": "Восстановление через поддержку",
    "dummy": "Пустышка",
}
