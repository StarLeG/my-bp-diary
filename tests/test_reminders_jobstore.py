"""Job'ы и `SQLAlchemyJobStore`: сериализация и восстановление после рестарта.

Регрессия: `_add_job` передавал в jobstore bound method `self._fire` — `pickle`
тащил весь сервис (вместе с `_scheduler`) и `SQLAlchemyJobStore` падал с
`PicklingError`, job'ы не сохранялись. Точка входа — module-level
`fire_reminder`, экземпляр сервиса привязывается через `bind_service`.
"""
from __future__ import annotations

import pickle
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.services import reminders as rem

UTC = timezone.utc


class FakeScheduler:
    """Лёгкий фейк APScheduler: фиксирует add_job/remove_job."""

    def __init__(self) -> None:
        self.jobs: dict[str, dict] = {}

    def add_job(self, func, **kwargs) -> str:
        self.jobs[kwargs["id"]] = {"func": func, **kwargs}
        return kwargs["id"]

    def remove_job(self, job_id: str) -> None:
        self.jobs.pop(job_id, None)


def make_scheduler(db_path: Path) -> AsyncIOScheduler:
    """Планировщик как в проде: `SQLAlchemyJobStore` на файле SQLite."""
    return AsyncIOScheduler(
        jobstores={"default": SQLAlchemyJobStore(url=f"sqlite:///{db_path}")},
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 300},
        timezone="UTC",
    )


@pytest.fixture(autouse=True)
def restore_binding() -> Iterator[None]:
    """Изоляция: прежняя привязка сервиса восстанавливается после теста."""
    previous = rem._service
    yield
    rem.bind_service(previous)


@pytest.fixture
def scheduler() -> FakeScheduler:
    return FakeScheduler()


@pytest.fixture
def sent() -> list[tuple[int, str, object]]:
    return []


@pytest.fixture
def svc(
    session_factory: async_sessionmaker[AsyncSession],
    scheduler: FakeScheduler,
    sent: list[tuple[int, str, object]],
) -> rem.RemindersService:
    async def sender(telegram_id: int, text: str, markup) -> None:
        sent.append((telegram_id, text, markup))

    return rem.RemindersService(session_factory, sender, scheduler)


class TestJobSerialization:
    """Job сериализуем: точка входа — module-level функция, args — данные."""

    def test_job_func_is_module_level_fire_reminder(
        self, svc: rem.RemindersService, scheduler: FakeScheduler
    ) -> None:
        run_date = datetime.now(UTC) + timedelta(hours=1)
        svc._add_job(7, run_date, rem.MODE_SLOT)
        job = scheduler.jobs["reminder:7"]
        assert job["func"] is rem.fire_reminder
        assert job["args"] == [7, rem.MODE_SLOT]

    def test_job_func_pickles_by_reference(
        self, svc: rem.RemindersService, scheduler: FakeScheduler
    ) -> None:
        run_date = datetime.now(UTC) + timedelta(hours=1)
        svc._add_job(7, run_date, rem.MODE_SNOOZE)
        func = scheduler.jobs["reminder:7"]["func"]
        assert pickle.loads(pickle.dumps(func)) is rem.fire_reminder


class TestJobStorePersistence:
    """Настоящий `SQLAlchemyJobStore`: сохранение job'а и рестарт планировщика."""

    async def test_jobs_persist_and_survive_restart(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        user,
        sent: list[tuple[int, str, object]],
        tmp_path: Path,
    ) -> None:
        async def sender(telegram_id: int, text: str, markup) -> None:
            sent.append((telegram_id, text, markup))

        store_path = tmp_path / "jobs.sqlite3"
        scheduler = make_scheduler(store_path)
        svc = rem.RemindersService(session_factory, sender, scheduler)
        rem.bind_service(svc)

        reminder = await svc.ensure_slot(user.id, "morning", "08:00", "Europe/Minsk")
        job_id = f"reminder:{reminder.id}"

        # Сохранение в jobstore (здесь старый код падал с PicklingError).
        scheduler.start()
        try:
            assert scheduler.get_job(job_id) is not None
        finally:
            scheduler.shutdown(wait=False)

        # Рестарт: job восстанавливается из jobstore с той же точкой входа.
        restored_scheduler = make_scheduler(store_path)
        restored_scheduler.start()
        try:
            job = restored_scheduler.get_job(job_id)
            assert job is not None
            assert job.func is rem.fire_reminder
            assert list(job.args) == [reminder.id, rem.MODE_SLOT]
        finally:
            restored_scheduler.shutdown(wait=False)


class TestFireReminder:
    """Module-level точка входа делегирует привязанному сервису."""

    async def test_delegates_to_bound_service(self, mocker) -> None:
        service = mocker.Mock()
        service._fire = mocker.AsyncMock()
        rem.bind_service(service)
        await rem.fire_reminder(3, rem.MODE_SNOOZE)
        service._fire.assert_awaited_once_with(3, rem.MODE_SNOOZE)

    async def test_unbound_service_is_ignored(self) -> None:
        rem.bind_service(None)
        await rem.fire_reminder(3, rem.MODE_SLOT)  # не должно упасть
