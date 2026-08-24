"""Общие куски экранов бота: главное меню, безопасная отправка и правка сообщений."""

from __future__ import annotations

import logging
from contextlib import suppress
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
    ReplyKeyboardMarkup,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import assets, texts
from app.bot.keyboards import main_menu
from app.db import commit_before_io
from app.services import settings_store

log = logging.getLogger("bot")

# Тексты из админки идут как HTML: админу нужны ссылки, жирный, премиум-эмодзи.
# Если разметка битая, Telegram отказывается отправлять — тогда шлём то же
# сообщение простым текстом, чтобы бот не «замолчал» из-за одной угловой скобки.
_PARSE_ERRORS = ("can't parse entities", "unsupported start tag", "can't find end tag")
_PHOTO_FILE_IDS: dict[Path, str] = {}
CAPTION_LIMIT = 1000

ReplyMarkup = InlineKeyboardMarkup | ReplyKeyboardMarkup


def _is_parse_error(exc: TelegramBadRequest) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _PARSE_ERRORS)


async def answer_callback(cb: CallbackQuery, text: str | None = None, **kw) -> None:
    """Закрыть индикатор кнопки, не роняя апдейт из-за просроченного callback.

    После сетевого таймаута Telegram может доставить основной запрос, но ответ
    на кнопку уже не принять: query живёт недолго. Экран при этом обновлён, и
    превращать служебный ответ на callback в Internal error нельзя.
    """
    try:
        await cb.answer(text, **kw)
    except TelegramBadRequest as exc:
        message = str(exc).lower()
        if "query is too old" not in message and "query id is invalid" not in message:
            raise
        log.info("callback уже истёк, экран оставлен обновлённым: %s", exc)
    except TelegramNetworkError as exc:
        log.warning("не удалось закрыть индикатор callback: %s", exc)


async def answer(
    message: Message, text: str, keyboard: ReplyMarkup | None = None, **kw
) -> Message:
    await commit_before_io()
    try:
        return await message.answer(text, reply_markup=keyboard, **kw)
    except TelegramBadRequest as exc:
        if not _is_parse_error(exc):
            raise
        log.warning("битая разметка в тексте из админки, отправляю как есть: %s", exc)
        return await message.answer(text, reply_markup=keyboard, parse_mode=None, **kw)


def _photo_input(photo: Path) -> str | FSInputFile:
    return _PHOTO_FILE_IDS.get(photo) or FSInputFile(photo)


def _remember_photo(photo: Path, message: object) -> None:
    sizes = getattr(message, "photo", None) or []
    file_id = getattr(sizes[-1], "file_id", None) if sizes else None
    if file_id:
        _PHOTO_FILE_IDS[photo] = file_id


async def answer_photo(
    message: Message,
    photo: Path,
    caption: str,
    keyboard: ReplyMarkup | None = None,
    **kw,
) -> Message:
    """Send a screen banner and cache Telegram's file_id for later users."""
    if len(caption) > CAPTION_LIMIT:
        await commit_before_io()
        try:
            sent_photo = await message.answer_photo(_photo_input(photo))
        except TelegramNetworkError as exc:
            log.warning("не удалось отправить длинный баннер %s: %s; отправляю экран текстом", photo, exc)
            return await answer(message, caption, keyboard, **kw)
        _remember_photo(photo, sent_photo)
        return await answer(message, caption, keyboard, **kw)

    await commit_before_io()
    try:
        sent = await message.answer_photo(
            _photo_input(photo), caption=caption, reply_markup=keyboard, **kw
        )
    except TelegramBadRequest as exc:
        if not _is_parse_error(exc):
            raise
        log.warning("битая разметка в подписи к картинке: %s", exc)
        sent = await message.answer_photo(
            _photo_input(photo),
            caption=caption,
            reply_markup=keyboard,
            parse_mode=None,
            **kw,
        )
    except TelegramNetworkError as exc:
        # Uploading a local image can time out on a proxy or a slow connection.
        # Do not let that hide the screen or its inline buttons: a text fallback
        # is immediately usable and avoids an unhandled update error. We do not
        # blindly retry an upload because Telegram may have accepted it before
        # the client timed out, which would create duplicate banners.
        log.warning("не удалось отправить баннер %s: %s; отправляю экран текстом", photo, exc)
        return await answer(message, caption, keyboard, **kw)
    _remember_photo(photo, sent)
    return sent


async def _replace_with_text(
    message: Message, text: str, keyboard: InlineKeyboardMarkup | None
) -> None:
    await answer(message, text, keyboard)
    with suppress(TelegramBadRequest):
        await message.delete()


async def _replace_with_photo(
    message: Message,
    photo: Path,
    text: str,
    keyboard: InlineKeyboardMarkup | None,
) -> None:
    await answer_photo(message, photo, text, keyboard)
    with suppress(TelegramBadRequest):
        await message.delete()


async def send_text(
    bot: Bot, chat_id: int, text: str, keyboard: InlineKeyboardMarkup | None = None, **kw
) -> Message:
    """Отправка не в ответ на сообщение: рассылка, доставка ответов поддержки.

    Та же защита от битой разметки, что и в answer(): текст пишет человек в
    админке, и одна угловая скобка не должна съедать ответ клиенту.
    """
    await commit_before_io()
    try:
        return await bot.send_message(chat_id, text, reply_markup=keyboard, **kw)
    except TelegramBadRequest as exc:
        if not _is_parse_error(exc):
            raise
        log.warning("битая разметка в тексте из админки, отправляю как есть: %s", exc)
        return await bot.send_message(
            chat_id, text, reply_markup=keyboard, parse_mode=None, **kw
        )


async def edit(
    cb: CallbackQuery,
    text: str,
    keyboard: InlineKeyboardMarkup | None = None,
    *,
    answer: bool = True,
    photo: Path | None = None,
    remove_photo: bool = False,
) -> None:
    """Правка сообщения на месте. Повторное нажатие той же кнопки — не ошибка.

    answer=False нужен, когда экран правится дважды за одно нажатие (например,
    «подбираем номер» → карточка аккаунта): на callback можно ответить один раз,
    второй Telegram уже не примет.
    """
    await commit_before_io()
    if not isinstance(cb.message, Message):
        if answer:
            await answer_callback(cb)
        return
    try:
        if cb.message.photo and remove_photo:
            await _replace_with_text(cb.message, text, keyboard)
        elif cb.message.photo and len(text) > CAPTION_LIMIT:
            await _replace_with_text(cb.message, text, keyboard)
        elif not cb.message.photo and photo is not None:
            await _replace_with_photo(cb.message, photo, text, keyboard)
        elif cb.message.photo:
            if photo is not None:
                result = await cb.message.edit_media(
                    InputMediaPhoto(media=_photo_input(photo), caption=text),
                    reply_markup=keyboard,
                )
                _remember_photo(photo, result)
            else:
                await cb.message.edit_caption(caption=text, reply_markup=keyboard)
        else:
            await cb.message.edit_text(
                text, reply_markup=keyboard, disable_web_page_preview=True
            )
    except TelegramBadRequest as exc:
        if "not modified" in str(exc):
            pass
        elif _is_parse_error(exc):
            log.warning("битая разметка в экране бота: %s", exc)
            if cb.message.photo:
                if photo is not None:
                    result = await cb.message.edit_media(
                        InputMediaPhoto(
                            media=_photo_input(photo), caption=text, parse_mode=None
                        ),
                        reply_markup=keyboard,
                    )
                    _remember_photo(photo, result)
                else:
                    await cb.message.edit_caption(
                        caption=text, reply_markup=keyboard, parse_mode=None
                    )
            else:
                await cb.message.edit_text(text, reply_markup=keyboard, parse_mode=None)
        else:
            raise
    except TelegramNetworkError as exc:
        # edit_media не создаёт новое сообщение, поэтому после таймаута
        # безопасно сохранить текущий экран и клавиатуру, убрав только
        # попытку заменить саму фотографию.
        log.warning("не удалось заменить баннер %s: %s; оставляю текущую фотографию", photo, exc)
        if cb.message.photo:
            with suppress(Exception):
                await cb.message.edit_caption(caption=text, reply_markup=keyboard)
        else:
            with suppress(Exception):
                await cb.message.edit_text(text, reply_markup=keyboard)
    if answer:
        await answer_callback(cb)


async def send_menu(message: Message, session: AsyncSession, greeting: str | None = None) -> None:
    """Приветствие и нижняя клавиатура. Текст правится в админке (bot.welcome)."""
    welcome = greeting or (await settings_store.get(session, "bot.welcome")) or texts.SOON
    earn_on = await settings_store.get_bool(session, "earn.enabled")
    await answer_photo(message, assets.WELCOME, welcome, main_menu(earn=earn_on))
