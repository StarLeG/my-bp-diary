"""Тесты доступа к БД (bot/db.py): PRAGMA, сессии, повторы при блокировках."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine

from bot.db import (
    RETRY_ATTEMPTS,
    SCHEMA_VERSION,
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


# ─────────────────── Миграция medications (SCHEMA_VERSION 2) ───────────────────

#: DDL таблицы `medications` до миграции: дозировка обязательна, слота crisis нет.
OLD_MEDICATIONS_DDL = """
CREATE TABLE medications (
    id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    name VARCHAR(100) NOT NULL,
    dosage VARCHAR(50) NOT NULL,
    slot VARCHAR(16) NOT NULL,
    created_at DATETIME NOT NULL,
    CONSTRAINT pk_medications PRIMARY KEY (id),
    CONSTRAINT uq_medications_user_name_slot UNIQUE (user_id, name, slot),
    CONSTRAINT ck_medications_slot CHECK (slot IN ('morning', 'noon', 'evening')),
    CONSTRAINT ck_medications_name_length CHECK (length(name) BETWEEN 1 AND 100),
    CONSTRAINT ck_medications_dosage_length CHECK (length(dosage) BETWEEN 1 AND 50),
    FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
)
"""


async def _install_old_schema(engine: AsyncEngine) -> None:
    """Смонтировать БД «как до миграции»: старый medications + данные + лог приёма.

    `users` и `medication_logs` уже созданы `init_db` (новые); `medications`
    подменяется старой схемой — ровно то, что видит миграция на проде.
    """
    async with engine.connect() as conn:
        await conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        await conn.exec_driver_sql("DROP TABLE medications")
        await conn.exec_driver_sql(OLD_MEDICATIONS_DDL)
        await conn.exec_driver_sql(
            "INSERT INTO users (id, telegram_id, timezone, target_systolic, "
            "target_diastolic, onboarded, created_at) "
            "VALUES (1, 111111, 'Europe/Minsk', 130, 80, 0, '2026-11-01 08:00:00')"
        )
        await conn.exec_driver_sql(
            "INSERT INTO medications (id, user_id, name, dosage, slot, created_at) "
            "VALUES (1, 1, 'Лозартан', '50 мг', 'morning', '2026-11-01 08:00:00')"
        )
        await conn.exec_driver_sql(
            "INSERT INTO medication_logs (id, user_id, medication_id, timestamp, status, slot) "
            "VALUES (1, 1, 1, '2026-11-01 08:05:00', 'taken', 'morning')"
        )
        await conn.commit()
        await conn.exec_driver_sql("PRAGMA foreign_keys=ON")  # вернуть в пул


class TestMigrateMedications:
    async def test_old_schema_rebuilt_with_new_checks(self, tmp_path) -> None:
        """Старая БД: данные и логи сохранены, новые CHECK работают, user_version = 2."""
        engine = create_engine(str(tmp_path / "old.sqlite3"))
        try:
            await init_db(engine)
            await _install_old_schema(engine)
            # Старая схема действительно старая: crisis и пустая дозировка запрещены.
            async with engine.connect() as conn:
                with pytest.raises(IntegrityError):
                    await conn.exec_driver_sql(
                        "INSERT INTO medications (id, user_id, name, dosage, slot, created_at) "
                        "VALUES (2, 1, 'Каптоприл', '', 'crisis', '2026-11-01 09:00:00')"
                    )

            await init_db(engine)  # миграция

            async with engine.connect() as conn:
                row = (
                    await conn.exec_driver_sql("SELECT name, dosage, slot FROM medications")
                ).first()
                assert tuple(row) == ("Лозартан", "50 мг", "morning")
                # DROP TABLE не должен был каскадно съесть логи приёмов.
                logs = (
                    await conn.exec_driver_sql("SELECT COUNT(*) FROM medication_logs")
                ).scalar()
                assert logs == 1
                # Новые CHECK: пустая дозировка и слот crisis проходят.
                await conn.exec_driver_sql(
                    "INSERT INTO medications (id, user_id, name, dosage, slot, created_at) "
                    "VALUES (2, 1, 'Каптоприл', '', 'crisis', '2026-11-01 09:00:00')"
                )
                await conn.commit()
                version = (await conn.exec_driver_sql("PRAGMA user_version")).scalar()
                assert version == SCHEMA_VERSION
        finally:
            await engine.dispose()

    async def test_migration_is_idempotent(self, tmp_path) -> None:
        """Повторный init_db после миграции ничего не меняет и не падает."""
        engine = create_engine(str(tmp_path / "twice.sqlite3"))
        try:
            await init_db(engine)
            await _install_old_schema(engine)
            await init_db(engine)
            await init_db(engine)
            async with engine.connect() as conn:
                rows = (
                    await conn.exec_driver_sql("SELECT name, dosage, slot FROM medications")
                ).all()
                assert [tuple(r) for r in rows] == [("Лозартан", "50 мг", "morning")]
                logs = (
                    await conn.exec_driver_sql("SELECT COUNT(*) FROM medication_logs")
                ).scalar()
                assert logs == 1
        finally:
            await engine.dispose()

    async def test_fresh_db_sets_schema_version(self, tmp_path) -> None:
        """Новая БД сразу получает SCHEMA_VERSION (для verify_compatibility бэкапов)."""
        engine = create_engine(str(tmp_path / "fresh.sqlite3"))
        try:
            await init_db(engine)
            async with engine.connect() as conn:
                version = (await conn.exec_driver_sql("PRAGMA user_version")).scalar()
            assert version == SCHEMA_VERSION
        finally:
            await engine.dispose()
