"""Polling that stops when the same Telegram token is used elsewhere.

Aiogram normally retries every Bot API error forever.  That is useful for
temporary network failures, but a ``getUpdates`` conflict is different: it
means that another process is already polling the same bot.  Retrying from two
deployments lets both of them receive adjacent (and sometimes repeated)
updates, potentially against different databases.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from aiogram import Bot, Dispatcher, loggers
from aiogram.dispatcher.dispatcher import DEFAULT_BACKOFF_CONFIG
from aiogram.exceptions import TelegramConflictError
from aiogram.methods import GetUpdates
from aiogram.types import Update
from aiogram.utils.backoff import Backoff, BackoffConfig


# run.py recognises this exit code and deliberately does not restart the
# conflicting child process.  Keep the value in sync with run.py without
# importing the launcher (which exports the hosted ASGI application).
DUPLICATE_INSTANCE_EXIT_CODE = 78


class SingleInstanceDispatcher(Dispatcher):
    """Dispatcher that treats a duplicate polling instance as permanent."""

    @classmethod
    async def _listen_updates(
        cls,
        bot: Bot,
        polling_timeout: int = 30,
        backoff_config: BackoffConfig = DEFAULT_BACKOFF_CONFIG,
        allowed_updates: list[str] | None = None,
    ) -> AsyncGenerator[Update, None]:
        backoff = Backoff(config=backoff_config)
        get_updates = GetUpdates(timeout=polling_timeout, allowed_updates=allowed_updates)
        request_kwargs: dict[str, int] = {}
        if bot.session.timeout:
            request_kwargs["request_timeout"] = int(bot.session.timeout + polling_timeout)

        failed = False
        while True:
            try:
                updates = await bot(get_updates, **request_kwargs)
            except TelegramConflictError:
                loggers.dispatcher.critical(
                    "The bot token is already being polled by another process; "
                    "this instance is stopping to prevent duplicate replies"
                )
                raise
            except Exception as exc:  # network/API failures remain retryable
                failed = True
                loggers.dispatcher.error(
                    "Failed to fetch updates - %s: %s", type(exc).__name__, exc
                )
                loggers.dispatcher.warning(
                    "Sleep for %f seconds and try again... (tryings = %d, bot id = %d)",
                    backoff.next_delay,
                    backoff.counter,
                    bot.id,
                )
                await backoff.asleep()
                continue

            if failed:
                loggers.dispatcher.info(
                    "Connection established (tryings = %d, bot id = %d)",
                    backoff.counter,
                    bot.id,
                )
                backoff.reset()
                failed = False

            for update in updates:
                yield update
                get_updates.offset = update.update_id + 1
