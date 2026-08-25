"""Конфигурация проекта. Один источник правды, читается из .env."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
MEDIA_DIR = BASE_DIR / "media"

# Схемы прокси, которые понимают python_socks (через aiogram) и httpx.
# socks5h/socks4a — те же протоколы, просто с DNS на стороне прокси; он и так там.
PROXY_SCHEMES = {
    "socks5": "socks5",
    "socks5h": "socks5",
    "socks4": "socks4",
    "socks4a": "socks4",
    "http": "http",
    "https": "http",
}

DEFAULT_ADMIN_SECRET = "change-me-in-env"
PRODUCTION_ENVS = {"prod", "production"}


def normalize_proxy(raw: str | None) -> str | None:
    """Привести строку прокси к виду `scheme://user:pass@host:port`.

    Продавцы прокси выдают `host:port:user:pass`, и на такой строке urllib падает
    с «Invalid port component»: портом он считает всё после первого двоеточия.
    Понимаем оба порядка и логин через @, схему по умолчанию берём socks5.
    Логин и пароль экранируем — python_socks и httpx распакуют их обратно, зато
    символы вроде @ и : в пароле больше не ломают разбор.
    """
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None

    scheme = "socks5"
    if "://" in value:
        scheme, value = value.split("://", 1)
        scheme = scheme.strip().lower()
    if scheme not in PROXY_SCHEMES:
        supported = ", ".join(sorted(PROXY_SCHEMES))
        raise ValueError(f"прокси: неизвестная схема «{scheme}», поддерживаются: {supported}")
    scheme = PROXY_SCHEMES[scheme]

    value = value.strip("/").strip()
    user = password = ""
    if "@" in value:
        # Пароль сам может содержать @, поэтому режем по последнему и проверяем:
        # если справа не host:port, значит @ — это символ пароля, а не разделитель.
        creds, tail = value.rsplit("@", 1)
        tail_parts = tail.split(":")
        if len(tail_parts) == 2 and tail_parts[1].strip().isdigit():
            user, _, password = creds.partition(":")
            value = tail

    parts = value.split(":")
    if len(parts) == 2:
        host, port = parts
    elif len(parts) == 4 and not user:
        # host:port:user:pass или user:pass:host:port — различаем по числовому порту.
        if parts[1].isdigit():
            host, port, user, password = parts
        elif parts[3].isdigit():
            user, password, host, port = parts
        else:
            raise ValueError("прокси: не понял, где host:port — порт должен быть числом")
    else:
        raise ValueError(
            "прокси: ожидал host:port, host:port:user:pass или user:pass@host:port, "
            f"а получил {len(parts)} часть(ей) через двоеточие"
        )

    host = host.strip()
    if not host:
        raise ValueError("прокси: пустой хост")
    if not port.strip().isdigit() or not 0 < int(port) < 65536:
        raise ValueError(f"прокси: «{port}» не похоже на порт")

    auth = f"{quote(user, safe='')}:{quote(password, safe='')}@" if user else ""
    return f"{scheme}://{auth}{host}:{int(port)}"


def mask_proxy(url: str | None) -> str:
    """Прокси для показа в консоли: хост и порт видно, пароль — нет."""
    if not url:
        return "не задан"
    scheme, _, rest = url.partition("://")
    if "@" in rest:
        creds, _, endpoint = rest.rpartition("@")
        user = creds.partition(":")[0]
        return f"{scheme}://{user}:***@{endpoint}"
    return url


def normalize_database_url(raw: str) -> str:
    """Anchor relative SQLite files to the project, not the launch directory.

    The bot, web app and admin panel are often started by different wrappers.
    A relative ``./data/shop.db`` then silently points at a different file when
    one wrapper has another current directory, which makes users see screens
    from different databases.  Server databases and in-memory SQLite are
    intentionally left untouched.
    """
    url = make_url(raw)
    if not url.drivername.startswith("sqlite") or url.database in {None, ":memory:"}:
        return raw

    database = Path(url.database)
    if database.is_absolute():
        return raw
    absolute = (BASE_DIR / database).resolve()
    return str(url.set(database=str(absolute)))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- окружение ---
    env: str = "local"
    debug: bool = True
    tz: str = "Europe/Moscow"

    # --- база ---
    database_url: str = f"sqlite+aiosqlite:///{DATA_DIR / 'shop.db'}"
    # На локалке таблицы создаются сами при старте. В проде — только alembic.
    db_auto_create: bool = True
    db_echo: bool = False

    # --- боты ---
    bot_token: str = ""
    support_bot_token: str = ""
    bot_username: str = ""
    support_bot_username: str = ""
    # Прокси до api.telegram.org. Принимаем и socks5://user:pass@host:port,
    # и socks5://host:port:user:pass — приводим к первому виду сами.
    # Пусто = напрямую. Нужен там, где Telegram недоступен с сервера.
    bot_proxy: str | None = None
    bot_menu_button: bool = True  # кнопка мини-аппа в меню чата (нужен https)


    @property
    def bot_link(self) -> str:
        """Ссылка на основного бота без учёта того, вписали @ в .env или нет."""
        return f"https://t.me/{self.bot_username.lstrip('@')}"

    @property
    def support_bot_link(self) -> str:
        return f"https://t.me/{self.support_bot_username.lstrip('@')}"

    # --- админка ---
    admin_host: str = "127.0.0.1"
    admin_port: int = 8080
    admin_secret: str = DEFAULT_ADMIN_SECRET
    admin_base_url: str = "http://127.0.0.1:8080"
    # Первый админ создаётся при старте, если админов в базе нет.
    admin_login: str = "admin"
    admin_password: str = ""

    # --- мини-апп ---
    webapp_base_url: str = "http://127.0.0.1:8081/app"
    web_host: str = "127.0.0.1"
    web_port: int = 8081
    web_base_url: str = "http://127.0.0.1:8081"

    # --- сайт: авторизация ---
    google_client_id: str | None = None
    google_client_secret: str | None = None
    google_redirect_uri: str | None = None
    web_secret: str = "change-me-web-secret"

    # --- LZT Market ---
    lzt_token: str = ""
    lzt_base_url: str = "https://prod-api.lzt.market"
    # ID баланса LZT, с которого списывается закупка. Пусто = баланс по умолчанию
    # («Баланс на Маркете»). У целевого «Баланса для покупки аккаунтов» id числовой,
    # у основного — строка «balance», поэтому тип свободный.
    # Посмотреть свои: python -m app.tools.lzt_check balances
    lzt_balance_id: int | str | None = None
    lzt_timeout: float = 25.0
    lzt_proxy: str | None = None
    # Сколько раз повторять fast-buy при ответе retry_request (в докупе LZT — до 100).
    lzt_retry_request_limit: int = 30

    # --- платежи ---
    # Токен приложения из @CryptoBot → Crypto Pay → Create App.
    cryptobot_token: str = ""
    # Тестовая сеть живёт на https://testnet-pay.crypt.bot с отдельным токеном.
    cryptobot_base_url: str = "https://pay.crypt.bot"
    # Токен приложения из @xRocket → Rocket Pay.
    xrocket_token: str = ""
    xrocket_base_url: str = "https://pay.xrocket.exchange"
    platega_merchant_id: str = ""
    platega_secret: str = ""
    pay_timeout: float = 20.0
    # Прокси для платёжных API. Пусто = напрямую: pay.crypt.bot и
    # pay.xrocket.exchange отвечают с сервера сами, это не api.telegram.org.
    pay_proxy: str | None = None

    # --- каналы ---
    reviews_channel_id: int | None = None

    log_level: str = "INFO"
    log_dir: Path = Field(default=BASE_DIR / "data" / "logs")

    @model_validator(mode="before")
    @classmethod
    def _empty_means_unset(cls, data: object) -> object:
        """Пустая строка в .env = «не задано», а не пустое значение.

        Иначе LZT_BALANCE_ID= роняет старт, а DATABASE_URL= затирает дефолт.
        """
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if not (isinstance(v, str) and v.strip() == "")}
        return data

    @field_validator("bot_proxy", "lzt_proxy", "pay_proxy", mode="after")
    @classmethod
    def _fix_proxy(cls, value: str | None) -> str | None:
        """Разбираем формат прокси на старте, а не в момент первого запроса."""
        return normalize_proxy(value)

    @field_validator("database_url", mode="after")
    @classmethod
    def _fix_database_url(cls, value: str) -> str:
        return normalize_database_url(value)

    @model_validator(mode="after")
    def _secure_admin_session_in_production(self) -> Settings:
        """Не запускать production с публично известным ключом cookie.

        SessionMiddleware подписывает ADMIN_SECRET всю сессию админки. С
        дефолтным значением посетитель может сам изготовить cookie с admin_id,
        поэтому это именно ошибка конфигурации, а не предупреждение в логе.
        """
        if self.env.strip().lower() in PRODUCTION_ENVS:
            secret = self.admin_secret.strip()
            if not secret or secret == DEFAULT_ADMIN_SECRET:
                raise ValueError(
                    "ADMIN_SECRET обязателен в production и не может быть "
                    f"равен {DEFAULT_ADMIN_SECRET!r}"
                )
            web_secret = self.web_secret.strip()
            if not web_secret or web_secret == "change-me-web-secret":
                raise ValueError(
                    "WEB_SECRET обязателен в production и не может быть "
                    "равен значению по умолчанию"
                )
            if not self.web_base_url.strip().lower().startswith("https://"):
                raise ValueError("WEB_BASE_URL в production должен начинаться с https://")
            if not self.webapp_base_url.strip().lower().startswith("https://"):
                raise ValueError("WEBAPP_BASE_URL в production должен начинаться с https://")
        return self

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    s.log_dir.mkdir(parents=True, exist_ok=True)
    return s


settings = get_settings()
