"""Нагрузочный burst сервисного слоя: 20 пользователей × N операций (SPEC.md, §22).

Дополняет юнит-тесты гонок (tests/test_concurrency.py) нагрузочным профилем:
сотни конкурентных операций на файловой SQLite с PRAGMA продакшена (WAL,
busy_timeout). Проверяется корректность (нет дублей, идемпотентность
сохраняется) и измеряется время прохождения.

В обычный прогон pytest не входит (в `testpaths` только tests/) и дополнительно
закрыт флагом LOAD_TEST. Запуск вручную:

    LOAD_TEST=1 uv run pytest qa/test_load_burst.py -v -s

Нагрузка HTTP-кабинета — отдельно, через Locust (qa/locustfile.py).
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bot.db import create_engine, create_session_factory, init_db
from bot.models import Measurement, MedicationLog, Reminder, User as DbUser
from bot.services import measurements as measurements_service
from bot.services import medications as medications_service
from bot.services.reminders import RemindersService

pytestmark = pytest.mark.skipif(
    os.getenv("LOAD_TEST") != "1",
    reason="нагрузочный профиль: запускать с LOAD_TEST=1",
)

#: Профиль нагрузки: 20 одновременных пользователей.
USERS = 20
#: Сохранений давления на пользователя (итого USERS × OPS записей).
OPS = 25

TG_ID_BASE = 880_000


class FakeScheduler:
    """Лёгкий фейк APScheduler: фиксирует add_job/remove_job."""

    def __init__(self) -> None:
        self.jobs: dict[str, dict] = {}

    def add_job(self, func, **kwargs) -> str:
        self.jobs[kwargs["id"]] = kwargs
        return kwargs["id"]

    def remove_job(self, job_id: str) -> None:
        self.jobs.pop(job_id, None)


@pytest_asyncio.fixture
async def engine(tmp_path) -> AsyncEngine:
    """Файловая SQLite: WAL и busy_timeout — как в проде (раздел 7)."""
    engine = create_engine(str(tmp_path / "load.sqlite3"))
    await init_db(engine)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(engine)


@pytest_asyncio.fixture
async def users(session_factory) -> list[DbUser]:
    """USERS пользователей, созданных последовательно (стартовая сетка)."""
    result: list[DbUser] = []
    for i in range(USERS):
        async with session_factory() as session:
            user = await measurements_service.get_or_create_user(
                session, TG_ID_BASE + i
            )
            await session.commit()
            result.append(user)
    return result


async def count(session_factory, model) -> int:
    """Число строк таблицы."""
    async with session_factory() as session:
        return (
            await session.execute(select(func.count()).select_from(model))
        ).scalar_one()


class TestBurstMeasurements:
    """Массовая запись измерений: 20 × 25 конкурентных сохранений."""

    async def test_burst_saves_create_all_rows(
        self, session_factory, users: list[DbUser]
    ) -> None:
        """USERS × OPS параллельных сохранений — ровно столько строк, без ошибок.

        Времена уникальны по минуте (дедуп по (user, минута) не срабатывает) —
        проверяем именно поведение под конкуренцией писателей, а не дедуп.
        """
        base = datetime.now(timezone.utc).replace(second=0, microsecond=0) + timedelta(
            days=1
        )

        async def save(user: DbUser, i: int, op: int) -> None:
            ts = base + timedelta(minutes=op * USERS + i)
            async with session_factory() as session:
                await measurements_service.save_measurement(
                    session, user, 120 + (i + op) % 40, 75 + (i * 3 + op) % 20,
                    pulse=60 + (i + op) % 30,
                    timestamp=ts,
                )
                await session.commit()

        started = time.perf_counter()
        await asyncio.gather(
            *(save(user, i, op) for i, user in enumerate(users) for op in range(OPS))
        )
        elapsed = time.perf_counter() - started
        total = USERS * OPS
        print(
            f"\n[load] {total} сохранений за {elapsed:.2f} с "
            f"({total / elapsed:.0f} оп/с, {USERS} пользователей)"
        )
        assert await count(session_factory, Measurement) == total

    async def test_burst_double_tap_saves_once(
        self, session_factory, users: list[DbUser]
    ) -> None:
        """20 × 2 конкурентных сохранения с одним временем — ровно 20 строк.

        Двойной тап каждого пользователя обрабатывается под нагрузкой остальных:
        дедуп по минуте не должен «проскакивать» из-за конкуренции.
        """
        ts = datetime.now(timezone.utc).replace(second=0, microsecond=0) + timedelta(
            days=2
        )
        duplicates = 0

        async def double_tap(user: DbUser) -> None:
            nonlocal duplicates

            async def save() -> bool:
                async with session_factory() as session:
                    try:
                        await measurements_service.save_measurement(
                            session, user, 135, 85, timestamp=ts
                        )
                        await session.commit()
                        return True
                    except measurements_service.DuplicateMeasurementError:
                        await session.rollback()
                        return False

            results = await asyncio.gather(save(), save())
            duplicates += results.count(False)

        await asyncio.gather(*(double_tap(user) for user in users))
        print(f"\n[load] двойных тапов отсечено: {duplicates} из {USERS}")
        assert duplicates == USERS
        assert await count(session_factory, Measurement) == USERS


class TestBurstMeds:
    """Массовые отметки приёмов и повторов замера: идемпотентность под нагрузкой."""

    async def test_burst_intake_and_remeasure(
        self, session_factory, users: list[DbUser]
    ) -> None:
        """20 пользователей параллельно отмечают приём и «Да» на повтор — без дублей.

        У каждого пользователя конкурентные двойные вызовы
        `log_slot_intake` и `ensure_remeasure`: наружу — ровно одна строка
        MedicationLog и один Reminder на пользователя.
        """

        async def sender(user_id: int, text: str, markup) -> None:
            pass

        async def one_user(user: DbUser, i: int) -> None:
            async with session_factory() as session:
                meds = await medications_service.add_medication(
                    session, user, f"Нагрузка-{i}", "5 мг", ["morning"]
                )
                await session.commit()
                measurement = await measurements_service.save_measurement(
                    session, user, 130, 85
                )
                await session.commit()
            decisions = {meds[0].id: "taken"}
            svc = RemindersService(session_factory, sender, FakeScheduler())
            await asyncio.gather(
                self._log_intake(session_factory, user, decisions),
                self._log_intake(session_factory, user, decisions),
                svc.ensure_remeasure(user.id, measurement.id, 60),
                svc.ensure_remeasure(user.id, measurement.id, 60),
            )

        started = time.perf_counter()
        await asyncio.gather(*(one_user(user, i) for i, user in enumerate(users)))
        elapsed = time.perf_counter() - started
        print(f"\n[load] смешанные операции (приёмы + повторы) за {elapsed:.2f} с")
        assert await count(session_factory, MedicationLog) == USERS
        assert await count(session_factory, Reminder) == USERS

    @staticmethod
    async def _log_intake(
        session_factory,
        user: DbUser,
        decisions: dict,
    ) -> None:
        """Отметка приёма в собственной сессии (сессия на операцию, §7)."""
        async with session_factory() as session:
            await medications_service.log_slot_intake(
                session, user, "morning", decisions
            )
            await session.commit()
