"""Тесты доступа к БД (bot/db.py): PRAGMA, сессии, повторы при блокировках."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine

from bot.db import (
    RETRY_ATTEMPTS,
    create_engine,
    create_session_factory,
    init_db,
    run_with_retry,
    session_scope,
)
from bot.models import User

EXPECTED_TABLES = {
    "users",
    "measurements",
    "reminders",
    "medications",
    "medication_logs",
    "web_users",
}


class TestEngine:
    async def test_init_db_creates_all_tables(self, engine: AsyncEngine) -> None:
        async with engine.connect() as conn:
            tables = await conn.run_sync(lambda sync_conn: set(inspect(sync_conn).get_table_names()))
        assert EXPECTED_TABLES <= tables

    async def test_init_db_idempotent(self, engine: AsyncEngine) -> None:
        # Повторный init_db (например, при рестарте) не должен падать.
        await init_db(engine)

    async def test_pragmas_applied_on_file_db(self, tmp_path) -> None:
        engine = create_engine(str(tmp_path / "test.sqlite3"))
        try:
            async with engine.connect() as conn:
                journal_mode = (await conn.execute(text("PRAGMA journal_mode"))).scalar()
                foreign_keys = (await conn.execute(text("PRAGMA foreign_keys"))).scalar()
                busy_timeout = (await conn.execute(text("PRAGMA busy_timeout"))).scalar()
            assert journal_mode == "wal"
            assert foreign_keys == 1
            assert busy_timeout == 5000
        finally:
            await engine.dispose()

    async def test_foreign_keys_on_memory_db(self, engine: AsyncEngine) -> None:
        async with engine.connect() as conn:
            foreign_keys = (await conn.execute(text("PRAGMA foreign_keys"))).scalar()
        assert foreign_keys == 1

    async def test_memory_engine_shared(self, session_factory) -> None:
        # StaticPool: две сессии видят одни и те же данные.
        async with session_scope(session_factory) as session:
            session.add(User(telegram_id=1))
        async with session_scope(session_factory) as session:
            count = (await session.execute(text("SELECT COUNT(*) FROM users"))).scalar()
        assert count == 1


class TestSessionScope:
    async def test_commits_on_success(self, session_factory) -> None:
        async with session_scope(session_factory) as session:
            session.add(User(telegram_id=100))
        async with session_scope(session_factory) as session:
            count = (await session.execute(text("SELECT COUNT(*) FROM users"))).scalar()
        assert count == 1

    async def test_rollbacks_on_error(self, session_factory) -> None:
        with pytest.raises(RuntimeError):
            async with session_scope(session_factory) as session:
                session.add(User(telegram_id=100))
                raise RuntimeError("отмена")
        async with session_scope(session_factory) as session:
            count = (await session.execute(text("SELECT COUNT(*) FROM users"))).scalar()
        assert count == 0

    async def test_session_closed_after_scope(self, session_factory, mocker) -> None:
        # close() вызывается для каждой сессии после выхода из scope.
        from sqlalchemy.ext.asyncio import AsyncSession as _AsyncSession

        close_spy = mocker.spy(_AsyncSession, "close")
        async with session_scope(session_factory) as session:
            pass
        assert any(call.args[0] is session for call in close_spy.call_args_list)


class TestRunWithRetry:
    async def test_succeeds_first_try(self) -> None:
        fn = AsyncMock(return_value=42)
        assert await run_with_retry(fn, "a", key="b") == 42
        fn.assert_awaited_once_with("a", key="b")

    async def test_retries_on_locked_then_succeeds(self, mocker) -> None:
        sleep = mocker.patch("bot.db.asyncio.sleep", new=AsyncMock())
        locked = OperationalError("SELECT 1", {}, Exception("database is locked"))
        fn = AsyncMock(side_effect=[locked, "ok"])
        assert await run_with_retry(fn) == "ok"
        assert fn.await_count == 2
        sleep.assert_awaited()

    async def test_raises_after_exhausting_attempts(self, mocker) -> None:
        sleep = mocker.patch("bot.db.asyncio.sleep", new=AsyncMock())
        locked = OperationalError("SELECT 1", {}, Exception("database is locked"))
        fn = AsyncMock(side_effect=locked)
        with pytest.raises(OperationalError):
            await run_with_retry(fn)
        assert fn.await_count == RETRY_ATTEMPTS
        assert sleep.await_count == RETRY_ATTEMPTS - 1

    async def test_no_retry_on_other_operational_error(self) -> None:
        broken = OperationalError("SELECT 1", {}, Exception("disk I/O error"))
        fn = AsyncMock(side_effect=broken)
        with pytest.raises(OperationalError):
            await run_with_retry(fn)
        assert fn.await_count == 1


def test_create_session_factory_returns_async_session(engine: AsyncEngine) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession

    factory = create_session_factory(engine)
    assert isinstance(factory(), AsyncSession)
