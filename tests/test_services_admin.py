"""Тесты административной панели (bot/services/admin.py, SPEC.md, §18).

Проверяются: агрегаты БД без мед-данных (§18.2, приватность), кэш статистики
5 минут, здоровье системы (§18.4), ErrorCounter за 24 часа, карточка поиска
(§18.6) и рассылка (§18.5): Semaphore(20), Forbidden → «заблокировали»,
прогресс каждые 10 отправок.
"""
from __future__ import annotations

import asyncio
import logging
import platform
from datetime import datetime, timedelta, timezone

import pytest
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError

from bot.models import Measurement, Medication, Reminder, User
from bot.services.admin import (
    AdminService,
    ErrorCounter,
    broadcast_message,
    read_app_version,
)

NOW = datetime(2026, 11, 5, 12, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    """Управляемый источник времени для кэша статистики."""

    def __init__(self, moment: datetime = NOW) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, delta: timedelta) -> None:
        self.moment += delta


class FakeScheduler:
    """Лёгкий фейк APScheduler для экрана здоровья системы."""

    def __init__(self, running: bool = True, jobs: int = 0) -> None:
        self.running = running
        self._jobs = [object()] * jobs

    def get_jobs(self) -> list:
        return list(self._jobs)


class FakeDatetime:
    """Замена `datetime.now` в модуле admin: детерминированный ErrorCounter."""

    current = NOW

    @classmethod
    def now(cls, tz=None) -> datetime:  # noqa: ANN001 — сигнатура datetime.now
        return cls.current


async def add_user(session, telegram_id: int, **kwargs) -> User:
    user = User(telegram_id=telegram_id, **kwargs)
    session.add(user)
    await session.commit()
    return user


async def add_measurement(session, user: User, moment: datetime) -> Measurement:
    measurement = Measurement(
        user_id=user.id, timestamp=moment, systolic=120, diastolic=80
    )
    session.add(measurement)
    await session.commit()
    return measurement


async def add_reminder(
    session, user: User, slot: str, enabled: bool = True, local_time: str = "08:00"
) -> Reminder:
    reminder = Reminder(
        user_id=user.id,
        type=slot,
        local_time=local_time,
        next_trigger_time=NOW,
        enabled=enabled,
    )
    session.add(reminder)
    await session.commit()
    return reminder


class TestReadAppVersion:
    def test_reads_version_from_pyproject(self) -> None:
        import tomllib
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        expected = tomllib.loads((root / "pyproject.toml").read_text("utf-8"))["project"][
            "version"
        ]
        assert read_app_version(root) == expected

    def test_unknown_when_pyproject_missing(self, tmp_path) -> None:
        assert read_app_version(tmp_path / "nope") == "unknown"

    def test_unknown_when_version_missing(self, tmp_path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'", encoding="utf-8")
        assert read_app_version(tmp_path) == "unknown"


class TestErrorCounter:
    def test_counts_errors_but_not_warnings(self) -> None:
        counter = ErrorCounter()
        log = logging.getLogger("tests.error_counter")
        log.addHandler(counter)
        try:
            log.warning("не ошибка")
            log.error("раз")
            log.critical("два")
            assert counter.count == 2
        finally:
            log.removeHandler(counter)

    def test_prunes_events_outside_window(self, mocker) -> None:
        FakeDatetime.current = NOW
        mocker.patch("bot.services.admin.datetime", FakeDatetime)
        counter = ErrorCounter()
        log = logging.getLogger("tests.error_counter")
        log.addHandler(counter)
        try:
            log.error("старая ошибка")
            assert counter.count == 1
            FakeDatetime.current += timedelta(hours=25)
            assert counter.count == 0  # выпала из окна 24 часа
            log.error("свежая ошибка")
            assert counter.count == 1
        finally:
            log.removeHandler(counter)

    def test_handler_level_is_error(self) -> None:
        assert ErrorCounter().level == logging.ERROR


class TestStats:
    async def test_counts_only_aggregates(
        self, session, user, session_factory
    ) -> None:
        other = await add_user(session, 222_222)
        await add_measurement(session, user, NOW - timedelta(hours=1))
        await add_measurement(session, user, NOW - timedelta(hours=2))
        await add_measurement(session, other, NOW - timedelta(hours=3))
        session.add(Medication(user_id=user.id, name="Лозартан", dosage="50 мг", slot="morning"))
        await session.commit()

        service = AdminService(session_factory, ":memory:", now=FakeClock())
        stats = await service.get_stats()
        assert stats.users == 2
        assert stats.measurements == 3
        assert stats.medications == 1
        assert stats.consented == 2  # согласие по умолчанию (tests/conftest.py)
        assert stats.profiles == 0  # данных пациента никто не заполнял (§6.12)

    async def test_dau_wau_distinct_users(
        self, session, user, session_factory
    ) -> None:
        other = await add_user(session, 222_222)
        third = await add_user(session, 333_333)
        await add_measurement(session, user, NOW - timedelta(hours=1))  # DAU и WAU
        await add_measurement(session, other, NOW - timedelta(days=3))  # только WAU
        await add_measurement(session, third, NOW - timedelta(days=10))  # мимо

        service = AdminService(session_factory, ":memory:", now=FakeClock())
        stats = await service.get_stats()
        assert stats.dau == 1
        assert stats.wau == 2

    async def test_slots_enabled_counts_enabled_slots(
        self, session, user, session_factory
    ) -> None:
        other = await add_user(session, 222_222)
        await add_reminder(session, user, "morning", enabled=True)
        await add_reminder(session, user, "noon", enabled=True)
        await add_reminder(session, user, "evening", enabled=False)
        await add_reminder(session, user, "med_remeasure", enabled=True, local_time=None)
        await add_reminder(session, other, "morning", enabled=True)

        service = AdminService(session_factory, ":memory:", now=FakeClock())
        stats = await service.get_stats()
        # med_remeasure не слот; отключённые слоты не считаются (§18.2).
        assert stats.slots_enabled == {"morning": 2, "noon": 1, "evening": 0}

    async def test_file_sizes_from_os_path_getsize(
        self, session_factory, tmp_path
    ) -> None:
        db_path = tmp_path / "bot.sqlite3"
        db_path.write_bytes(b"x" * 100)
        service = AdminService(session_factory, str(db_path), now=FakeClock())
        stats = await service.get_stats()
        assert stats.db_size == 100
        assert stats.wal_size == 0  # WAL-файла ещё нет

        (tmp_path / "bot.sqlite3-wal").write_bytes(b"w" * 25)
        stats = await service.get_stats(force=True)
        assert stats.wal_size == 25

    async def test_memory_db_has_no_files(self, session_factory) -> None:
        service = AdminService(session_factory, ":memory:", now=FakeClock())
        stats = await service.get_stats()
        assert stats.db_size == 0
        assert stats.wal_size == 0

    async def test_cache_five_minutes_and_force(
        self, session, session_factory
    ) -> None:
        clock = FakeClock()
        service = AdminService(session_factory, ":memory:", now=clock)
        assert (await service.get_stats()).users == 0

        await add_user(session, 111_111)
        assert (await service.get_stats()).users == 0  # кэш ещё свежий

        assert (await service.get_stats(force=True)).users == 1  # «Обновить»
        await add_user(session, 222_222)
        assert (await service.get_stats()).users == 1  # снова кэш

        clock.advance(timedelta(minutes=6))  # TTL истёк
        assert (await service.get_stats()).users == 2


class TestHealth:
    def test_full_health(self, mocker) -> None:
        counter = ErrorCounter()
        counter.emit(logging.LogRecord("x", logging.ERROR, "m", 1, "err", None, None))
        service = AdminService(
            mocker.Mock(),
            ":memory:",
            FakeScheduler(running=True, jobs=3),
            error_counter=counter,
        )
        health = service.get_health()
        assert health.uptime_seconds >= 0
        assert health.app_version == read_app_version()
        assert health.python_version == platform.python_version()
        assert health.scheduler_running is True
        assert health.jobs_count == 3
        assert health.errors_24h == 1

    def test_health_without_scheduler_and_counter(self, mocker) -> None:
        service = AdminService(mocker.Mock(), ":memory:")
        health = service.get_health()
        assert health.scheduler_running is False
        assert health.jobs_count == 0
        assert health.errors_24h == 0

    def test_stopped_scheduler(self, mocker) -> None:
        service = AdminService(
            mocker.Mock(), ":memory:", FakeScheduler(running=False, jobs=2)
        )
        health = service.get_health()
        assert health.scheduler_running is False
        assert health.jobs_count == 2


class TestFindUser:
    async def test_card_without_medical_data(
        self, session, user, session_factory
    ) -> None:
        await add_measurement(session, user, NOW - timedelta(hours=1))
        await add_measurement(session, user, NOW - timedelta(hours=2))
        await add_reminder(session, user, "morning", enabled=True)
        await add_reminder(session, user, "noon", enabled=False)
        await add_reminder(session, user, "med_remeasure", enabled=True, local_time=None)

        service = AdminService(session_factory, ":memory:", now=FakeClock())
        card = await service.find_user(111_111)
        assert card is not None
        assert card.telegram_id == 111_111
        assert card.timezone == "Europe/Minsk"
        assert card.measurements_count == 2
        assert card.onboarded is False
        assert sorted(card.reminder_slots) == ["med_remeasure", "morning"]
        assert card.consent_at is not None

    async def test_missing_user_is_none(self, session_factory) -> None:
        service = AdminService(session_factory, ":memory:", now=FakeClock())
        assert await service.find_user(999_999) is None

    async def test_all_user_ids(self, session, user, session_factory) -> None:
        await add_user(session, 222_222)
        service = AdminService(session_factory, ":memory:", now=FakeClock())
        ids = await service.all_user_ids()
        assert sorted(ids) == [111_111, 222_222]


class TestBroadcast:
    """Рассылка (§18.5): gather + Semaphore(20) + Forbidden не рвёт отправку."""

    def make_bot(self, mocker, blocked: set[int], failed: set[int]):
        """Бот с фиксацией конкурентности; ошибки — по множествам id."""
        state = {"in_flight": 0, "max_seen": 0, "calls": []}

        async def send_message(user_id, text, **kwargs):
            state["calls"].append((user_id, text))
            state["in_flight"] += 1
            state["max_seen"] = max(state["max_seen"], state["in_flight"])
            await asyncio.sleep(0.005)  # даём задачам накопиться в полёте
            state["in_flight"] -= 1
            if user_id in blocked:
                raise TelegramForbiddenError(
                    method="sendMessage", message="Forbidden: bot was blocked by the user"
                )
            if user_id in failed:
                raise TelegramAPIError(method="sendMessage", message="Bad Request: chat not found")

        bot = mocker.Mock()
        bot.send_message = send_message
        return bot, state

    async def test_mixed_outcomes_and_progress(self, mocker) -> None:
        mocker.patch("bot.services.admin.BROADCAST_PAUSE", 0)
        blocked = {1, 2, 3, 4, 5}
        failed = {6, 7, 8, 9, 10}
        bot, state = self.make_bot(mocker, blocked, failed)
        progress = mocker.AsyncMock()

        result = await broadcast_message(bot, list(range(1, 26)), "Текст", progress)

        assert result.delivered == 15
        assert result.blocked == 5
        assert result.failed == 5
        # Прогресс каждые 10 отправок (§18.5).
        assert progress.await_args_list == [
            ((10, 25), {}),
            ((20, 25), {}),
        ]
        # Конкурентность ограничена семафором и реально достигает его.
        assert state["max_seen"] == 20
        assert len(state["calls"]) == 25
        assert state["calls"][0] == (1, "Текст")

    async def test_empty_recipients(self, mocker) -> None:
        bot = mocker.Mock()
        bot.send_message = mocker.AsyncMock()
        progress = mocker.AsyncMock()
        result = await broadcast_message(bot, [], "Текст", progress)
        assert (result.delivered, result.blocked, result.failed) == (0, 0, 0)
        bot.send_message.assert_not_awaited()
        progress.assert_not_awaited()

    async def test_without_progress_callback(self, mocker) -> None:
        mocker.patch("bot.services.admin.BROADCAST_PAUSE", 0)
        bot, _ = self.make_bot(mocker, set(), set())
        result = await broadcast_message(bot, [1, 2, 3], "Текст")
        assert result.delivered == 3
