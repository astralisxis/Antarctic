"""Движок базы и сессии. Асинхронный SQLAlchemy 2."""

from __future__ import annotations

import asyncio
import os
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar, Token
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import make_url
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
_schema_lock = asyncio.Lock()

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

    if not settings.is_sqlite:
        async with engine.begin() as conn:
            await _create_schema(conn)
        return

    # Web, admin, main bot and support bot can start simultaneously.  Protect
    # both concurrent tasks in one process and separate hosted processes: two
    # SQLite CREATE TABLE sequences must never overlap.
    database = make_url(settings.database_url).database
    if not database or database == ":memory:":
        async with _schema_lock:
            async with engine.begin() as conn:
                await _create_schema(conn)
        return

    db_path = Path(database)
    if not db_path.is_absolute():
        db_path = Path.cwd() / db_path
    lock_path = db_path.with_suffix(f"{db_path.suffix}.schema.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    async with _schema_lock:
        lock = lock_path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                lock.seek(0)
                lock.write(b"0")
                lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            async with engine.begin() as conn:
                await _create_schema(conn)
        finally:
            try:
                if os.name == "nt":
                    import msvcrt

                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            lock.close()


async def _create_schema(conn) -> None:
    """Create schema idempotently across independently started workers.

    SQLAlchemy checks table existence before emitting DDL. A worker may have
    opened its SQLite connection before another worker commits a table, so the
    check can be stale and the narrow ``table ... already exists`` error is a
    harmless startup race. The file lock above prevents normal overlap; this
    fallback covers hosts with separate processes or SQLite VFS behaviour.
    """
    try:
        await conn.run_sync(Base.metadata.create_all)
    except Exception as exc:
        cause = getattr(exc, "orig", None)
        message = str(cause or exc).lower()
        if not isinstance(cause or exc, sqlite3.OperationalError):
            raise
        if "table " not in message or " already exists" not in message:
            raise
        # The first pass may have stopped at the raced table. Re-run the
        # idempotent DDL so tables that follow it are created as well.
        await conn.run_sync(Base.metadata.create_all)


async def dispose() -> None:
    await engine.dispose()
