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


# ─────────────────── Миграция doctors (SCHEMA_VERSION 3, §4.8) ───────────────────

#: DDL таблицы `doctors` до 0.4.14: e-mail обязателен, Telegram-полей нет.
OLD_DOCTORS_DDL = """
CREATE TABLE doctors (
    id INTEGER NOT NULL,
    email VARCHAR(255) NOT NULL,
    name VARCHAR(100) NOT NULL,
    clinic VARCHAR(100),
    password_hash VARCHAR(255) NOT NULL,
    created_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_doctors_name_length CHECK (length(name) BETWEEN 1 AND 100),
    CONSTRAINT ck_doctors_clinic_length CHECK (clinic IS NULL OR length(clinic) <= 100),
    UNIQUE (email)
)
"""


async def _install_old_doctors(engine: AsyncEngine) -> None:
    """БД «как в 0.4.13»: старый `doctors` + пациент, связь и заметка врача."""
    async with engine.connect() as conn:
        await conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        await conn.exec_driver_sql("DROP TABLE doctors")
        await conn.exec_driver_sql(OLD_DOCTORS_DDL)
        await conn.exec_driver_sql(
            "INSERT INTO users (id, telegram_id, timezone, target_systolic, "
            "target_diastolic, onboarded, created_at) "
            "VALUES (1, 111111, 'Europe/Minsk', 130, 80, 1, '2026-11-01 08:00:00')"
        )
        await conn.exec_driver_sql(
            "INSERT INTO doctors (id, email, name, password_hash, created_at) "
            "VALUES (7, 'doc@example.org', 'doc', 'hash', '2026-11-01 08:00:00')"
        )
        await conn.exec_driver_sql(
            "INSERT INTO doctor_patients (id, doctor_id, user_id, status, created_at) "
            "VALUES (1, 7, 1, 'active', '2026-11-01 08:00:00')"
        )
        await conn.exec_driver_sql(
            "INSERT INTO doctor_notes (id, doctor_id, user_id, text, created_at) "
            "VALUES (1, 7, 1, 'Совет', '2026-11-01 08:00:00')"
        )
        await conn.commit()
        await conn.exec_driver_sql("PRAGMA foreign_keys=ON")


class TestMigrateDoctors:
    async def test_old_schema_rebuilt(self, tmp_path) -> None:
        """Врач, связь и заметка сохранены; e-mail необязателен; CHECK «e-mail или Telegram»."""
        engine = create_engine(str(tmp_path / "doctors.sqlite3"))
        try:
            await init_db(engine)
            await _install_old_doctors(engine)
            await init_db(engine)  # миграция
            await init_db(engine)  # повторно — без изменений
            async with engine.connect() as conn:
                row = (
                    await conn.exec_driver_sql(
                        "SELECT id, email, telegram_id, telegram_username, name FROM doctors"
                    )
                ).first()
                assert tuple(row) == (7, "doc@example.org", None, None, "doc")
                links = (await conn.exec_driver_sql("SELECT COUNT(*) FROM doctor_patients")).scalar()
                notes = (await conn.exec_driver_sql("SELECT COUNT(*) FROM doctor_notes")).scalar()
                assert (links, notes) == (1, 1)  # DROP TABLE не съел связи каскадом
                assert (await conn.exec_driver_sql("PRAGMA foreign_key_check")).all() == []
                await conn.exec_driver_sql(
                    "INSERT INTO doctors (email, telegram_id, telegram_username, name, "
                    "password_hash, created_at) VALUES (NULL, 555, 'doc_tg', 'Врач', 'h', "
                    "'2026-11-09 08:00:00')"
                )
                with pytest.raises(IntegrityError):
                    await conn.exec_driver_sql(
                        "INSERT INTO doctors (email, telegram_id, name, password_hash, created_at) "
                        "VALUES (NULL, NULL, 'Никто', 'h', '2026-11-09 08:00:00')"
                    )
                await conn.rollback()
                indexes = {
                    r[0] for r in (await conn.exec_driver_sql(
                        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='doctors'"
                    )).all()
                }
                assert "ix_doctors_telegram_username" in indexes
                version = (await conn.exec_driver_sql("PRAGMA user_version")).scalar()
                assert version == SCHEMA_VERSION == 8
                tables = {
                    r[0] for r in (await conn.exec_driver_sql(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )).all()
                }
                assert {"doctor_invites", "doctors"} <= tables
                assert "doctors_migrate" not in tables
        finally:
            await engine.dispose()

    async def test_no_doctors_table_is_skipped(self, tmp_path) -> None:
        from bot.db import _migrate_doctors

        engine = create_engine(str(tmp_path / "empty.sqlite3"))
        try:
            await _migrate_doctors(engine)  # таблицы нет — тихо выходим
        finally:
            await engine.dispose()


class TestMigrateConsents:
    """Поколение 4 (§4.15): `consents`, `doctors.consent_*`, `users.deletion_requested_at`."""

    async def test_columns_added_to_old_db(self, tmp_path) -> None:
        engine = create_engine(str(tmp_path / "gen3.sqlite3"))
        try:
            async with engine.begin() as conn:
                await conn.exec_driver_sql(
                    "CREATE TABLE users (id INTEGER PRIMARY KEY, telegram_id INTEGER NOT NULL UNIQUE,"
                    " timezone VARCHAR(64) NOT NULL, target_systolic INTEGER NOT NULL,"
                    " target_diastolic INTEGER NOT NULL, med_remeasure_delay INTEGER,"
                    " onboarded BOOLEAN NOT NULL, last_support_shown_at DATETIME,"
                    " created_at DATETIME NOT NULL)"
                )
                await conn.exec_driver_sql(
                    "INSERT INTO users VALUES (1, 111, 'Europe/Minsk', 130, 80, 60, 1, NULL,"
                    " '2026-01-01 00:00:00')"
                )
            await init_db(engine)
            await init_db(engine)  # повторно — без изменений
            async with engine.connect() as conn:
                users = {r[1] for r in (await conn.exec_driver_sql("PRAGMA table_info(users)")).all()}
                doctors = {r[1] for r in (await conn.exec_driver_sql("PRAGMA table_info(doctors)")).all()}
                assert "deletion_requested_at" in users
                assert {"last_name", "first_name", "middle_name", "birth_date", "sex",
                        "telegram_name", "profile_prompted_at"} <= users  # поколение 5
                assert {"consent_version", "consent_at", "consent_channel"} <= doctors
                assert (await conn.exec_driver_sql("SELECT telegram_id FROM users")).scalar() == 111
                tables = {r[0] for r in (await conn.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='table'")).all()}
                assert "consents" in tables
                assert (await conn.exec_driver_sql("PRAGMA user_version")).scalar() == SCHEMA_VERSION == 8
        finally:
            await engine.dispose()


class TestMigrateReminders:
    """Поколение 6 (0.5.4): `reminders.last_message_id` (§4.3, §6.7)."""

    async def test_column_added_to_old_db(self, tmp_path) -> None:
        engine = create_engine(str(tmp_path / "gen5.sqlite3"))
        try:
            async with engine.begin() as conn:
                await conn.exec_driver_sql(
                    "CREATE TABLE users (id INTEGER PRIMARY KEY, telegram_id INTEGER NOT NULL UNIQUE,"
                    " timezone VARCHAR(64) NOT NULL, target_systolic INTEGER NOT NULL,"
                    " target_diastolic INTEGER NOT NULL, med_remeasure_delay INTEGER,"
                    " onboarded BOOLEAN NOT NULL, last_support_shown_at DATETIME,"
                    " created_at DATETIME NOT NULL)"
                )
                await conn.exec_driver_sql(
                    "CREATE TABLE reminders (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,"
                    " type VARCHAR(16) NOT NULL, local_time VARCHAR(5), next_trigger_time DATETIME NOT NULL,"
                    " enabled BOOLEAN NOT NULL, last_sent_date DATE, measurement_id INTEGER)"
                )
                await conn.exec_driver_sql(
                    "INSERT INTO users VALUES (1, 111, 'Europe/Minsk', 130, 80, 60, 1, NULL,"
                    " '2026-01-01 00:00:00')"
                )
                await conn.exec_driver_sql(
                    "INSERT INTO reminders VALUES (1, 1, 'morning', '08:00', '2026-11-11 05:00:00', 1, NULL, NULL)"
                )
            await init_db(engine)
            await init_db(engine)  # повторно — без изменений
            async with engine.connect() as conn:
                columns = {r[1] for r in (await conn.exec_driver_sql("PRAGMA table_info(reminders)")).all()}
                assert {"last_message_id", "mode", "followup_at"} <= columns  # поколения 6–7
                row = (await conn.exec_driver_sql(
                    "SELECT type, local_time, last_message_id, mode, followup_at FROM reminders")).one()
                assert tuple(row) == ("morning", "08:00", None, "both", None)
                users = {r[1] for r in (await conn.exec_driver_sql("PRAGMA table_info(users)")).all()}
                assert {"followup_enabled", "reminders_paused_until"} <= users
                urow = (await conn.exec_driver_sql(
                    "SELECT followup_enabled, reminders_paused_until FROM users")).one()
                assert tuple(urow) == (1, None)
                with pytest.raises(Exception):
                    await conn.exec_driver_sql("UPDATE reminders SET mode = 'zzz'")
                assert (await conn.exec_driver_sql("PRAGMA user_version")).scalar() == SCHEMA_VERSION == 8
        finally:
            await engine.dispose()


class TestMigrateAppointments:
    """Поколение 8 (0.5.8): таблица `appointments` в старой БД (§4.16)."""

    async def test_table_created_in_old_db(self, tmp_path) -> None:
        engine = create_engine(str(tmp_path / "gen7.sqlite3"))
        try:
            async with engine.begin() as conn:
                await conn.exec_driver_sql(
                    "CREATE TABLE users (id INTEGER PRIMARY KEY, telegram_id INTEGER NOT NULL UNIQUE,"
                    " timezone VARCHAR(64) NOT NULL, target_systolic INTEGER NOT NULL,"
                    " target_diastolic INTEGER NOT NULL, med_remeasure_delay INTEGER,"
                    " onboarded BOOLEAN NOT NULL, last_support_shown_at DATETIME,"
                    " created_at DATETIME NOT NULL)"
                )
                await conn.exec_driver_sql("PRAGMA user_version = 7")
            await init_db(engine)
            async with engine.connect() as conn:
                tables = {r[0] for r in (await conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")).all()}
                assert "appointments" in tables
                indexes = {r[1] for r in (await conn.exec_driver_sql("PRAGMA index_list(appointments)")).all()}
                assert {"ix_appointments_user_day", "ix_appointments_status_day"} <= indexes
                assert (await conn.exec_driver_sql("PRAGMA user_version")).scalar() == SCHEMA_VERSION == 8
                await conn.exec_driver_sql(
                    "INSERT INTO users (id, telegram_id, timezone, target_systolic, target_diastolic, onboarded, created_at)"
                    " VALUES (1, 111, 'Europe/Minsk', 130, 80, 1, '2026-01-01 00:00:00')"
                )
                for bad in ("kind = 'dentist'", "status = 'lost'", "title = ''"):
                    column, value = bad.split(" = ")
                    values = {"kind": "'doctor'", "status": "'planned'", "title": "'Кардиолог'"}
                    values[column] = value
                    with pytest.raises(Exception):
                        await conn.exec_driver_sql(
                            "INSERT INTO appointments (user_id, kind, title, starts_on, status, note, created_at)"
                            f" VALUES (1, {values['kind']}, {values['title']}, '2026-11-20', {values['status']}, '',"
                            " '2026-11-11 00:00:00')"
                        )
        finally:
            await engine.dispose()
