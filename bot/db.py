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
from sqlalchemy.schema import CreateIndex, CreateTable
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

#: Поколение схемы БД (`PRAGMA user_version`); сравнивается при восстановлении
#: бэкапов (backup.verify_compatibility: копия не должна быть новее схемы).
#: 2 — дозировка лекарства опциональна, слот приёма `crisis` (§4.4).
#: 3 — врач через Telegram: `doctors.telegram_id` / `telegram_username`, e-mail
#:     необязателен (§4.8), `doctor_invites` (§4.13).
#: 4 — согласия (§4.15): таблица `consents`, `doctors.consent_*`,
#:     `users.deletion_requested_at`.
#: 5 — данные пациента (§4.1, §6.12): ФИО, дата рождения, пол, имя из
#:     Telegram, отметка предложения заполнить.
SCHEMA_VERSION = 5

#: Колонки поколений 4 и 5 (`ALTER TABLE … ADD COLUMN`, §4.15, §4.1).
_ADDED_COLUMNS = (
    ("users", "deletion_requested_at", "DATETIME"),
    ("doctors", "consent_version", "VARCHAR(32)"),
    ("doctors", "consent_at", "DATETIME"),
    (
        "doctors",
        "consent_channel",
        "VARCHAR(16) CONSTRAINT ck_doctors_consent_channel "
        "CHECK (consent_channel IS NULL OR consent_channel IN ('bot', 'cabinet'))",
    ),
    ("users", "last_name", "VARCHAR(50)"),
    ("users", "first_name", "VARCHAR(50)"),
    ("users", "middle_name", "VARCHAR(50)"),
    ("users", "birth_date", "DATE"),
    (
        "users",
        "sex",
        "VARCHAR(1) CONSTRAINT ck_users_sex CHECK (sex IS NULL OR sex IN ('m', 'f'))",
    ),
    ("users", "telegram_name", "VARCHAR(64)"),
    ("users", "profile_prompted_at", "DATETIME"),
)

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
    """Создать таблицы по моделям и обновить схему существующих БД (идемпотентно).

    `create_all` не трогает уже существующие таблицы, поэтому старые БД
    обновляются миграциями (`_migrate_medications`, `_migrate_doctors`). В конце
    поднимается `PRAGMA user_version` до `SCHEMA_VERSION`.
    """
    from bot.models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await _migrate_medications(engine)
    await _migrate_doctors(engine)
    await _add_missing_columns(engine)
    async with engine.connect() as conn:
        current = int((await conn.exec_driver_sql("PRAGMA user_version")).scalar() or 0)
        if current < SCHEMA_VERSION:
            await conn.exec_driver_sql(f"PRAGMA user_version = {SCHEMA_VERSION}")
            await conn.commit()


async def _migrate_medications(engine: AsyncEngine) -> None:
    """Пересобрать `medications` под новые CHECK (дозировка опциональна, слот `crisis`).

    SQLite не умеет изменять CHECK у существующей таблицы — официальный приём:
    создать таблицу с новой схемой, перенести строки, удалить старую и
    переименовать. Пересборка нужна только старым БД: новые создаются сразу
    правильными (`create_all`), повторный запуск ничего не меняет. DDL берётся
    из модели и не дублируется руками.

    `PRAGMA foreign_keys=OFF` обязателен: иначе `DROP TABLE medications`
    каскадно удалит `medication_logs` (`ON DELETE CASCADE`).
    """
    from bot.models import Medication

    async with engine.connect() as conn:
        row = (
            await conn.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='medications'"
            )
        ).first()
        if row is None or "crisis" in (row[0] or ""):
            return  # таблицы ещё нет или схема уже новая
        ddl = str(CreateTable(Medication.__table__).compile(dialect=engine.dialect))
        ddl = ddl.replace("CREATE TABLE medications", "CREATE TABLE medications_migrate", 1)
        columns = ", ".join(column.name for column in Medication.__table__.columns)
        try:
            await conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
            await conn.exec_driver_sql("DROP TABLE IF EXISTS medications_migrate")
            await conn.exec_driver_sql("BEGIN")  # пересборка — одной транзакцией
            await conn.exec_driver_sql(ddl)
            await conn.exec_driver_sql(
                f"INSERT INTO medications_migrate ({columns}) SELECT {columns} FROM medications"
            )
            await conn.exec_driver_sql("DROP TABLE medications")
            await conn.exec_driver_sql(
                "ALTER TABLE medications_migrate RENAME TO medications"
            )
            await conn.commit()
            logger.info("Схема medications обновлена (дозировка опциональна, слот crisis)")
        finally:
            await conn.exec_driver_sql("PRAGMA foreign_keys=ON")  # вернуть в пул


async def _migrate_doctors(engine: AsyncEngine) -> None:
    """Пересобрать `doctors` под приглашения через Telegram (SPEC.md, §4.8).

    `email` становится NULL-able, добавляются `telegram_id` (UNIQUE) и
    `telegram_username`, CHECK «e-mail или Telegram». Тот же приём, что у
    `_migrate_medications`: новая таблица → перенос строк → замена старой.
    Признак старой схемы — нет колонки `telegram_id`; повторный запуск ничего
    не меняет. `PRAGMA foreign_keys=OFF` обязателен: иначе `DROP TABLE
    doctors` каскадно удалит `doctor_patients` и `doctor_notes`.
    """
    from bot.models import Doctor

    async with engine.connect() as conn:
        exists = (
            await conn.exec_driver_sql(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='doctors'"
            )
        ).first()
        if exists is None:
            return
        columns_now = {
            row[1] for row in (await conn.exec_driver_sql("PRAGMA table_info(doctors)")).all()
        }
        if "telegram_id" in columns_now:
            return  # схема уже новая
        ddl = str(CreateTable(Doctor.__table__).compile(dialect=engine.dialect))
        ddl = ddl.replace("CREATE TABLE doctors", "CREATE TABLE doctors_migrate", 1)
        columns = ", ".join(
            column.name for column in Doctor.__table__.columns if column.name in columns_now
        )
        try:
            await conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
            await conn.exec_driver_sql("DROP TABLE IF EXISTS doctors_migrate")
            await conn.exec_driver_sql("BEGIN")  # пересборка — одной транзакцией
            await conn.exec_driver_sql(ddl)
            await conn.exec_driver_sql(
                f"INSERT INTO doctors_migrate ({columns}) SELECT {columns} FROM doctors"
            )
            await conn.exec_driver_sql("DROP TABLE doctors")
            await conn.exec_driver_sql("ALTER TABLE doctors_migrate RENAME TO doctors")
            for index in Doctor.__table__.indexes:
                await conn.exec_driver_sql(
                    str(CreateIndex(index).compile(dialect=engine.dialect))
                )
            await conn.commit()
            logger.info("Схема doctors обновлена (приглашение врача через Telegram)")
        finally:
            await conn.exec_driver_sql("PRAGMA foreign_keys=ON")  # вернуть в пул


async def _add_missing_columns(engine: AsyncEngine) -> None:
    """Добавить колонки поколения 4 в существующие таблицы (§4.15).

    Новые колонки NULL-able, поэтому хватает `ALTER TABLE … ADD COLUMN` без
    пересборки. Повторный запуск ничего не меняет: колонка, которая уже есть,
    пропускается. Новые БД получают их сразу из моделей (`create_all`).
    """
    async with engine.connect() as conn:
        for table, column, ddl in _ADDED_COLUMNS:
            existing = {
                row[1]
                for row in (await conn.exec_driver_sql(f"PRAGMA table_info({table})")).all()
            }
            if column in existing:
                continue
            await conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            logger.info("Схема %s: добавлена колонка %s", table, column)
        await conn.commit()


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
