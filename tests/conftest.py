"""Общие fixtures: in-memory БД, фабрики данных, моки aiogram (по мере надобности)."""
from __future__ import annotations

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bot.db import create_engine, create_session_factory, init_db
from bot.models import User


@pytest_asyncio.fixture
async def engine() -> AsyncEngine:
    """In-memory SQLite с созданными таблицами и PRAGMA из продакшена."""
    engine = create_engine(":memory:")
    await init_db(engine)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Фабрика сессий поверх тестовой БД."""
    return create_session_factory(engine)


@pytest_asyncio.fixture
async def session(session_factory: async_sessionmaker[AsyncSession]) -> AsyncSession:
    """Сессия на тест: коммитит явно, в конце — rollback и закрытие."""
    session = session_factory()
    yield session
    await session.rollback()
    await session.close()


@pytest_asyncio.fixture
async def user(session: AsyncSession) -> User:
    """Пользователь с настройками по умолчанию."""
    user = User(telegram_id=111_111, timezone="Europe/Minsk")
    session.add(user)
    await session.commit()
    return user
