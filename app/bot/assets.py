"""Static banners used by the main Telegram bot screens."""

from __future__ import annotations

from pathlib import Path

from app.config import MEDIA_DIR

BOT_MEDIA_DIR = MEDIA_DIR / "bot"

# Стартовый экран — отдельный баннер, чтобы обновление дизайна не меняло
# внутренние экраны магазина.
# JPEG-копии — те же баннеры, но примерно в 8 раз меньше PNG. Это важно для
# первого показа через Telegram: файл быстрее проходит через нестабильный
# прокси, а после первой отправки всё равно используется file_id Telegram.
WELCOME: Path = BOT_MEDIA_DIR / "welcome.jpg"
TOPUP: Path = BOT_MEDIA_DIR / "topup.jpg"
PROFILE: Path = BOT_MEDIA_DIR / "profile.jpg"
SUPPORT: Path = BOT_MEDIA_DIR / "support.jpg"
SHOP: Path = BOT_MEDIA_DIR / "shop.jpg"
COUNTRIES: Path = BOT_MEDIA_DIR / "countries.jpg"
