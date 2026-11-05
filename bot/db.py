"""Доступ к SQLite: engine, фабрика сессий, PRAGMA, повторы при блокировках.

Конкурентная безопасность (SPEC.md, раздел 7): `journal_mode=WAL`,
`foreign_keys=ON`, `busy_timeout=5000`, сессия на операцию, короткие
транзакции, повтор при `database is locked`.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TypeVar

from sqlalchemy import event
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

logger = logging.getLogger(__name__)

#: Сколько попыток делать при «database is locked».
RETRY_ATTEMPTS = 3
#: Базовая задержка перед повтором (экспоненциально растёт).
RETRY_BASE_DELAY = 0.05

T = TypeVar("T")


def create_engine(db_path: str) -> AsyncEngine:
    """Создать async-engine SQLite с PRAGMA для конкурентной работы.

    Аргументы:
        db_path: путь к файлу БД; `":memory:"` — тестовая in-memory БД
            (общий StaticPool, чтобы все сессии видели одни данные).
    """
    if db_path == ":memory:":
        from sqlalchemy.pool import StaticPool

        engine = create_async_engine(
            "sqlite+aiosqlite://",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
    else:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")

    @event.listens_for(engine.sync_engine, "connect")
    def _set_pragmas(dbapi_connection, _record) -> None:
        """PRAGMA выставляются на каждое соединение из пула."""
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Фабрика сессий: одна сессия — одна операция (SPEC.md, раздел 7.2)."""
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db(engine: AsyncEngine) -> None:
    """Создать таблицы по моделям (идемпотентно)."""
    from bot.models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@asynccontextmanager
async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Сессия на операцию: commit при успехе, rollback при ошибке, close всегда.

    Ничего «человеческого» (отправка сообщений) внутри блока делать нельзя:
    сначала фиксируем состояние, потом отвечаем пользователю.
    """
    session = factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def run_with_retry(fn: Callable[..., Awaitable[T]], *args: object, **kwargs: object) -> T:
    """Выполнить асинхронную операцию, повторяя при «database is locked».

    До `RETRY_ATTEMPTS` попыток с экспоненциальной задержкой. Прочие ошибки
    и окончательная неудача пробрасываются дальше.
    """
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return await fn(*args, **kwargs)
        except OperationalError as exc:
            locked = "locked" in str(exc).lower()
            if not locked or attempt == RETRY_ATTEMPTS:
                raise
            delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
            logger.warning(
                "SQLite заблокирован, повтор %s/%s через %.2f с", attempt, RETRY_ATTEMPTS, delay
            )
            await asyncio.sleep(delay)
    raise RuntimeError(  # pragma: no cover — недостижимо: цикл всегда завершается return/raise
        "недостижимо: цикл повторов завершился без результата"
    )
