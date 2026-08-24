"""Движок базы и сессии. Асинхронный SQLAlchemy 2."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar, Token

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


class Base(DeclarativeBase):
    pass


def _engine_kwargs() -> dict:
    kwargs: dict = {"echo": settings.db_echo, "future": True}
    if not settings.is_sqlite:
        kwargs.update(pool_size=10, max_overflow=20, pool_pre_ping=True)
    return kwargs


engine = create_async_engine(settings.database_url, **_engine_kwargs())

if settings.is_sqlite:

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record) -> None:
        """Апдейты обрабатываются параллельно, а писать в sqlite может только один.

        WAL разводит чтение и запись, busy_timeout даёт второму дождаться своей
        очереди вместо падения с «database is locked».
        """
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

# Aiogram processes updates concurrently. Keep the update session in task-local
# context so Telegram send helpers can finish the current DB transaction before
# waiting on the network. ContextVar values do not leak between asyncio tasks.
_current_session: ContextVar[AsyncSession | None] = ContextVar(
    "current_db_session", default=None
)


def bind_session(session: AsyncSession) -> Token[AsyncSession | None]:
    return _current_session.set(session)


def unbind_session(token: Token[AsyncSession | None]) -> None:
    _current_session.reset(token)


async def commit_before_io() -> None:
    """Release a request transaction before a potentially slow network call."""
    session = _current_session.get()
    if session is not None and session.in_transaction():
        await session.commit()


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Сессия с коммитом на выходе и откатом на исключении."""
    async with Session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_session() -> AsyncIterator[AsyncSession]:
    """Зависимость FastAPI."""
    async with Session() as session:
        yield session


async def create_all() -> None:
    """Только для локальной sqlite. В проде схему двигает alembic."""
    from app import models  # noqa: F401  — регистрирует таблицы в метадате

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def dispose() -> None:
    await engine.dispose()
