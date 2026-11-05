"""Тесты конкурентности и дедупликации (SPEC.md, раздел 7; план — шаг 8).

Кейсы доставки (двойной тик job'а, catch-up «не чаще 1/день», snooze) — в
tests/test_services_reminders.py; здесь — гонки на уровне сервисов и
middleware: параллельные сохранения, двойной тап в debounce-окне,
идемпотентность отметок приёма и повтора замера, параллельный /start,
футер «Поддержать». БД — файловая SQLite с PRAGMA продакшена (реальные
блокировки и WAL), не in-memory.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from aiogram.types import CallbackQuery, User
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bot import texts
from bot.db import create_engine, create_session_factory, init_db
from bot.handlers import support
from bot.handlers.stats import _support_footer
from bot.middlewares import DebounceMiddleware
from bot.models import Measurement, MedicationLog, Reminder, User as DbUser, WebUser
from bot.services import measurements as measurements_service
from bot.services import medications as medications_service
from bot.services.reminders import RemindersService

TG_ID = 111_111


class FakeClock:
    """Управляемое время вместо time.monotonic."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


class FakeScheduler:
    """Лёгкий фейк APScheduler: фиксирует add_job/remove_job."""

    def __init__(self) -> None:
        self.jobs: dict[str, dict] = {}

    def add_job(self, func, **kwargs) -> str:
        self.jobs[kwargs["id"]] = kwargs
        return kwargs["id"]

    def remove_job(self, job_id: str) -> None:
        self.jobs.pop(job_id, None)


def make_callback(data: str, user_id: int = TG_ID) -> CallbackQuery:
    """CallbackQuery с моком answer() для проверки ответов."""
    return CallbackQuery(
        id="q1",
        from_user=User(id=user_id, is_bot=False, first_name="Тест"),
        chat_instance="ci",
        data=data,
    )


@pytest_asyncio.fixture
async def engine(tmp_path) -> AsyncEngine:
    """Файловая SQLite: WAL и busy_timeout — как в проде (раздел 7)."""
    engine = create_engine(str(tmp_path / "test.sqlite3"))
    await init_db(engine)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(engine)


@pytest_asyncio.fixture
async def user(session_factory: async_sessionmaker[AsyncSession]) -> DbUser:
    async with session_factory() as session:
        user = await measurements_service.get_or_create_user(session, TG_ID)
        await session.commit()
        return user


@pytest_asyncio.fixture
def svc(session_factory: async_sessionmaker[AsyncSession]) -> RemindersService:
    """Сервис напоминаний с фейк-планировщиком."""

    async def sender(user_id: int, text: str, markup) -> None:
        pass

    return RemindersService(session_factory, sender, FakeScheduler())


async def count(session_factory: async_sessionmaker[AsyncSession], model) -> int:
    """Число строк таблицы."""
    async with session_factory() as session:
        return (await session.execute(select(func.count()).select_from(model))).scalar_one()


class TestParallelMeasurements:
    """Параллельное сохранение измерений: дублей нет, дедуп по минуте."""

    async def test_parallel_saves_create_single_record(
        self, session_factory, user: DbUser
    ) -> None:
        """Два конкурентных сохранения с одним временем — ровно одна запись."""
        ts = datetime.now(timezone.utc).replace(second=0, microsecond=0) + timedelta(minutes=5)

        async def save() -> bool:
            async with session_factory() as session:
                try:
                    await measurements_service.save_measurement(
                        session, user, 130, 85, timestamp=ts
                    )
                    await session.commit()
                    return True
                except measurements_service.DuplicateMeasurementError:
                    await session.rollback()
                    return False

        results = await asyncio.gather(save(), save())
        assert sorted(results) == [False, True]
        assert await count(session_factory, Measurement) == 1

    async def test_same_minute_rejected(self, session_factory, user: DbUser) -> None:
        """Разные секунды одной минуты — второе сохранение отклоняется."""
        base = datetime.now(timezone.utc).replace(second=0, microsecond=0) + timedelta(minutes=5)
        async with session_factory() as session:
            await measurements_service.save_measurement(
                session, user, 130, 85, timestamp=base
            )
            await session.commit()
        async with session_factory() as session:
            with pytest.raises(measurements_service.DuplicateMeasurementError):
                await measurements_service.save_measurement(
                    session, user, 128, 84, timestamp=base + timedelta(seconds=30)
                )
        assert await count(session_factory, Measurement) == 1


class TestDoubleTap:
    """Двойной тап в debounce-окне: второе измерение не создаётся."""

    async def test_double_tap_saves_once(
        self, session_factory, user: DbUser, mocker
    ) -> None:
        answer = mocker.patch.object(
            CallbackQuery, "answer", new_callable=mocker.AsyncMock
        )
        middleware = DebounceMiddleware(clock=FakeClock())
        calls = 0

        async def handler(event, data) -> str:
            nonlocal calls
            async with session_factory() as session:
                await measurements_service.save_measurement(session, user, 130, 85)
                await session.commit()
            calls += 1
            return "ok"

        callback = make_callback("m:sys:135")
        first = await middleware(handler, callback, {})
        second = await middleware(handler, callback, {})
        assert first == "ok"
        assert second is None
        assert calls == 1
        answer.assert_awaited_once_with(texts.DEBOUNCE_NOTICE)
        assert await count(session_factory, Measurement) == 1


class TestMedsIntakeIdempotency:
    """Двойное «Принял всё ✓» и повторная «Да» — без дублей и повторов."""

    async def test_double_take_all_logs_once(
        self, session_factory, user: DbUser
    ) -> None:
        """Повторное «Принял всё ✓» (вне окна debounce) — логи не плодятся."""
        async with session_factory() as session:
            meds = await medications_service.add_medication(
                session, user, "Лозартан", "50 мг", ["morning"]
            )
            await session.commit()
        decisions = {meds[0].id: "taken"}
        async with session_factory() as session:
            first = await medications_service.log_slot_intake(
                session, user, "morning", decisions
            )
            await session.commit()
        async with session_factory() as session:
            second = await medications_service.log_slot_intake(
                session, user, "morning", decisions
            )
            await session.commit()
        assert first.already_logged is False
        assert second.already_logged is True
        assert await count(session_factory, MedicationLog) == 1

    async def test_repeat_yes_creates_one_remeasure(
        self, session_factory, user: DbUser, svc: RemindersService
    ) -> None:
        """Повторная «Да» для той же записи — один активный повтор замера."""
        async with session_factory() as session:
            measurement = await measurements_service.save_measurement(session, user, 130, 85)
            await session.commit()
        assert await svc.ensure_remeasure(user.id, measurement.id, 60) is True
        assert await svc.ensure_remeasure(user.id, measurement.id, 60) is False
        assert await count(session_factory, Reminder) == 1

    async def test_parallel_yes_creates_one_remeasure(
        self, session_factory, user: DbUser, svc: RemindersService
    ) -> None:
        """Конкурентная двойная «Да»: UNIQUE по measurement_id оставляет один повтор."""
        async with session_factory() as session:
            measurement = await measurements_service.save_measurement(session, user, 130, 85)
            await session.commit()
        results = await asyncio.gather(
            svc.ensure_remeasure(user.id, measurement.id, 60),
            svc.ensure_remeasure(user.id, measurement.id, 60),
        )
        assert sorted(results) == [False, True]
        assert await count(session_factory, Reminder) == 1

    async def test_insert_conflict_returns_false(
        self, session_factory, user: DbUser, svc: RemindersService
    ) -> None:
        """Гонка двух «Да»: проигравший коммит получает False, а не ошибку (§7.2).

        Гонка воспроизводится детерминированно: строка конкурента вставлена,
        но не закоммичена — SELECT сервиса её ещё не видит, а INSERT после
        коммита конкурента падает на UNIQUE (`measurement_id`).
        """
        async with session_factory() as session:
            measurement = await measurements_service.save_measurement(session, user, 130, 85)
            await session.commit()
        async with session_factory() as session:
            session.add(
                Reminder(
                    user_id=user.id,
                    type="med_remeasure",
                    local_time=None,
                    next_trigger_time=datetime.now(timezone.utc) + timedelta(minutes=60),
                    enabled=True,
                    measurement_id=measurement.id,
                )
            )
            await session.flush()  # строка есть в индексе, но транзакция открыта
            task = asyncio.create_task(
                svc.ensure_remeasure(user.id, measurement.id, 60)
            )
            await asyncio.sleep(0.2)  # сервис успевает пройти SELECT и встать на INSERT
            await session.commit()
            assert await task is False
        assert await count(session_factory, Reminder) == 1


class TestParallelStart:
    """Параллельный /start — ровно одна строка users."""

    async def test_parallel_start_creates_one_user(self, session_factory) -> None:
        async def start() -> int:
            async with session_factory() as session:
                user = await measurements_service.get_or_create_user(session, 42_000)
                await session.commit()
                return user.id

        ids = await asyncio.gather(start(), start())
        assert ids[0] == ids[1]
        assert await count(session_factory, DbUser) == 1


class TestSupportFooterRateLimit:
    """Футер «Поддержать» — не чаще одного раза в сутки."""

    async def test_footer_shown_once_per_day(
        self, session_factory, user: DbUser
    ) -> None:
        base = datetime.now(timezone.utc) - timedelta(hours=2)
        async with session_factory() as session:
            for index in range(3):
                await measurements_service.save_measurement(
                    session,
                    user,
                    130 + index,
                    85,
                    timestamp=base + timedelta(minutes=index),
                )
            await session.commit()
        async with session_factory() as session:
            fresh = await session.get(DbUser, user.id)
            first = await _support_footer(session, fresh)
            await session.commit()
        async with session_factory() as session:
            fresh = await session.get(DbUser, user.id)
            second = await _support_footer(session, fresh)
            await session.commit()
        assert first == texts.STATS_FOOTER_SUPPORT
        assert second is None


class TestSnoozeRowSemantics:
    """«Через 30 мин» — перезапись строки слота; строк типа snooze нет."""

    async def test_snooze_keeps_slot_row(
        self, session_factory, user: DbUser, svc: RemindersService
    ) -> None:
        reminder = await svc.ensure_slot(user.id, "morning", "08:00", "Europe/Minsk")
        assert await svc.snooze(user.id, reminder.id) is True
        async with session_factory() as session:
            rows = list(
                (
                    await session.execute(
                        select(Reminder).where(Reminder.user_id == user.id)
                    )
                ).scalars()
            )
        assert len(rows) == 1
        assert rows[0].id == reminder.id
        assert rows[0].type == "morning"
        async with session_factory() as session:
            snoozed = list(
                (
                    await session.execute(
                        select(Reminder).where(Reminder.type == "snooze")
                    )
                ).scalars()
            )
        assert snoozed == []


class TestParallelAccountDeletion:
    """Параллельное удаление аккаунта (§19): гонка двух подтверждений безопасна."""

    async def test_parallel_delete_returns_single_true(
        self, session_factory, user: DbUser, svc: RemindersService
    ) -> None:
        """Два конкурентных удаления: выигравший — True, проигравший — False.

        Обе сессии читают пользователя до удаления; у проигравшего
        `session.delete` находит 0 строк и получает `StaleDataError`,
        который `delete_user_data` превращает в `False` без ошибки.
        """

        async def worker() -> bool:
            async with session_factory() as session:
                target = await measurements_service.get_user(session, TG_ID)
                assert target is not None
                await asyncio.sleep(0.05)  # обе сессии успевают прочитать
                return await measurements_service.delete_user_data(
                    session, svc, target
                )

        results = await asyncio.gather(worker(), worker())
        assert sorted(results) == [False, True]
        assert await count(session_factory, DbUser) == 0


class TestCabinetRegistrationRace:
    """Гонка создания ЛК с одним e-mail (§7.2, §16.2): один аккаунт, второй — «занят»."""

    async def test_parallel_same_email_creates_one_account(
        self, session_factory
    ) -> None:
        """Два конкурентных создания с одним e-mail: ровно одна запись web_users.

        UNIQUE по `email` отсекает проигравшего: `_create_web_account`
        получает `IntegrityError`, делает rollback и возвращает `False`
        («e-mail занят»), а не падает.
        """
        email = "race@example.com"

        async def register(telegram_id: int) -> bool:
            async with session_factory() as session:
                user = await measurements_service.get_or_create_user(session, telegram_id)
                await session.commit()
                return await support._create_web_account(session, user, email)

        results = await asyncio.gather(register(1), register(2))
        assert sorted(results) == [False, True]
        assert await count(session_factory, WebUser) == 1
