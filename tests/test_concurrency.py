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
from datetime import date, datetime, timedelta, timezone

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
from bot.models import (
    Appointment,
    AppointmentConclusion,
    Measurement,
    MedicationLog,
    MeasurementMedication,
    Reminder,
    User as DbUser,
    WebUser,
)
from bot.services import appointments as appointments_service
from bot.services import measurements as measurements_service
from bot.services import medications as medications_service
from bot.services.reminders import RemindersService, next_slot_occurrence

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


class TestParallelEditSave:
    """Параллельная правка одной записи: UPDATE идемпотентен, смеси нет (§7.2)."""

    async def test_parallel_updates_are_idempotent(
        self, session_factory, user: DbUser
    ) -> None:
        """Два конкурентных сохранения одинаковых значений — одна строка, значения те."""
        async with session_factory() as session:
            saved = await measurements_service.save_measurement(session, user, 135, 85, 72)
            await session.commit()
            measurement_id = saved.id

        async def edit(systolic: int, diastolic: int, pulse: int) -> bool:
            async with session_factory() as session:
                result = await measurements_service.update_measurement_values(
                    session, user, measurement_id, systolic, diastolic, pulse
                )
                await session.commit()
                return result is not None

        results = await asyncio.gather(edit(140, 90, 70), edit(140, 90, 70))
        assert results == [True, True]
        assert await count(session_factory, Measurement) == 1
        async with session_factory() as session:
            row = (
                await session.execute(
                    select(Measurement).where(Measurement.id == measurement_id)
                )
            ).scalar_one()
        assert (row.systolic, row.diastolic, row.pulse) == (140, 90, 70)

    async def test_parallel_updates_do_not_mix(
        self, session_factory, user: DbUser
    ) -> None:
        """Конкурентные правки разными значениями — финальная тройка ровно одна из двух."""
        async with session_factory() as session:
            saved = await measurements_service.save_measurement(session, user, 135, 85, 72)
            await session.commit()
            measurement_id = saved.id

        async def edit(systolic: int, diastolic: int, pulse: int) -> bool:
            async with session_factory() as session:
                result = await measurements_service.update_measurement_values(
                    session, user, measurement_id, systolic, diastolic, pulse
                )
                await session.commit()
                return result is not None

        results = await asyncio.gather(edit(100, 60, 55), edit(200, 120, 80))
        assert results == [True, True]
        async with session_factory() as session:
            row = (
                await session.execute(
                    select(Measurement).where(Measurement.id == measurement_id)
                )
            ).scalar_one()
        assert (row.systolic, row.diastolic, row.pulse) in {(100, 60, 55), (200, 120, 80)}


class TestParallelMomentEdit:
    """Правка момента из ЛК против параллельных сохранений: дедуп атомарен (§7.2)."""

    @staticmethod
    async def _edit(session_factory, user, measurement_id: int, ts: datetime) -> bool:
        async with session_factory() as session:
            try:
                await measurements_service.update_measurement_values(
                    session, user, measurement_id, 140, 90, None, timestamp=ts
                )
                await session.commit()
                return True
            except measurements_service.DuplicateMeasurementError:
                await session.rollback()
                return False

    async def test_edit_and_save_same_minute(
        self, session_factory, user: DbUser
    ) -> None:
        """Перенос записи на минуту и новая запись в ту же минуту — выживает одна."""
        target = datetime(2025, 3, 12, 5, 30, tzinfo=timezone.utc)
        async with session_factory() as session:
            saved = await measurements_service.save_measurement(
                session, user, 135, 85, timestamp=target - timedelta(hours=2)
            )
            await session.commit()
            measurement_id = saved.id

        async def save() -> bool:
            async with session_factory() as session:
                try:
                    await measurements_service.save_measurement(
                        session, user, 120, 80, timestamp=target + timedelta(seconds=20)
                    )
                    await session.commit()
                    return True
                except measurements_service.DuplicateMeasurementError:
                    await session.rollback()
                    return False

        results = await asyncio.gather(
            self._edit(session_factory, user, measurement_id, target), save()
        )
        assert sorted(results) == [False, True]
        async with session_factory() as session:
            in_minute = (
                await session.execute(
                    select(func.count()).select_from(Measurement).where(
                        Measurement.timestamp >= target,
                        Measurement.timestamp < target + timedelta(minutes=1),
                    )
                )
            ).scalar_one()
        assert in_minute == 1

    async def test_two_edits_to_same_minute(
        self, session_factory, user: DbUser
    ) -> None:
        """Десять записей параллельно переносят на одну минуту — переносится одна."""
        target = datetime(2025, 3, 12, 5, 30, tzinfo=timezone.utc)
        ids = []
        async with session_factory() as session:
            for hours in range(1, 11):
                saved = await measurements_service.save_measurement(
                    session, user, 135, 85, timestamp=target - timedelta(hours=hours)
                )
                ids.append(saved.id)
            await session.commit()

        results = await asyncio.gather(
            *(self._edit(session_factory, user, mid, target) for mid in ids)
        )
        assert results.count(True) == 1
        async with session_factory() as session:
            in_minute = (
                await session.execute(
                    select(func.count()).select_from(Measurement).where(
                        Measurement.timestamp == target
                    )
                )
            ).scalar_one()
        assert in_minute == 1


class TestParallelValueEdits:
    """Правки одной записи из бота: итог — один набор целиком, без смеси (§7.2).

    Детерминированное чередование вместо гонки: обе сессии прочитали строку
    до коммита друг друга (как хендлер правки), потом правят по очереди.
    Исходные значения частично совпадают с обоими наборами — при записи
    «только изменённых полей» первая правка обновила бы лишь `systolic`,
    вторая — лишь `diastolic` и `pulse`, и в строке осталась бы смесь.
    """

    INITIAL = (141, 92, 72)
    FIRST = (142, 92, 72)
    SECOND = (141, 91, 71)

    async def test_interleaved_edits_keep_whole_set(
        self, session_factory, user: DbUser
    ) -> None:
        async with session_factory() as session:
            saved = await measurements_service.save_measurement(
                session, user, *self.INITIAL[:2], pulse=self.INITIAL[2],
                timestamp=datetime(2025, 3, 12, 5, 30, tzinfo=timezone.utc),
            )
            await session.commit()
            measurement_id = saved.id
        first, second = session_factory(), session_factory()
        try:
            for session in (first, second):  # обе правки прочитали исходную строку
                row = await measurements_service.get_measurement(session, user, measurement_id)
                assert (row.systolic, row.diastolic, row.pulse) == self.INITIAL
            edited = await measurements_service.update_measurement_values(
                first, user, measurement_id, *self.FIRST
            )
            await first.commit()
            assert (edited.systolic, edited.diastolic, edited.pulse) == self.FIRST
            edited = await measurements_service.update_measurement_values(
                second, user, measurement_id, *self.SECOND
            )
            await second.commit()
            assert (edited.systolic, edited.diastolic, edited.pulse) == self.SECOND
        finally:
            await first.close()
            await second.close()
        async with session_factory() as session:
            row = await session.get(Measurement, measurement_id)
            assert (row.systolic, row.diastolic, row.pulse) == self.SECOND

    async def test_foreign_or_missing_record_is_none(
        self, session_factory, user: DbUser
    ) -> None:
        async with session_factory() as session:
            assert await measurements_service.update_measurement_values(
                session, user, 999_999, 120, 80, None
            ) is None


class TestParallelSlotReconfigure:
    """Две перенастройки одного слота: `local_time` и `next_trigger_time` — из одной (§7.2).

    Детерминированно: обёртка фабрики сессий держит каждую `ensure_slot` после
    её первого SELECT, пока строку не прочитают обе. Исходная строка частично
    совпадает с обоими наборами (время — с одним, следующий момент — с другим),
    поэтому запись «только изменённых полей» дала бы смесь в любом порядке
    коммитов.
    """

    async def test_interleaved_reconfigure_is_consistent(
        self, session_factory, user: DbUser
    ) -> None:
        tz = "UTC"
        now = datetime.now(timezone.utc)
        early, late = "07:00", "08:00"
        async with session_factory() as session:
            session.add(Reminder(
                user_id=user.id, type="morning", local_time=early,
                next_trigger_time=next_slot_occurrence(late, tz, now), enabled=False,
            ))
            await session.commit()

        both_read = asyncio.Barrier(2)

        def gated_factory():
            session = session_factory()
            execute, calls = session.execute, [0]

            async def gated_execute(*args, **kwargs):
                result = await execute(*args, **kwargs)
                calls[0] += 1
                if calls[0] == 1:  # строка слота прочитана — ждём вторую правку
                    await asyncio.wait_for(both_read.wait(), 5)
                return result

            session.execute = gated_execute
            return session

        async def sender(user_id: int, text: str, markup) -> None:
            pass

        svc = RemindersService(gated_factory, sender, FakeScheduler())
        await asyncio.gather(
            svc.ensure_slot(user.id, "morning", late, tz),
            svc.ensure_slot(user.id, "morning", early, tz),
        )
        async with session_factory() as session:
            row = (await session.execute(select(Reminder).where(Reminder.user_id == user.id))).scalar_one()
        assert row.enabled is True
        assert row.local_time in (early, late)
        expected = next_slot_occurrence(row.local_time, tz, now)
        assert abs(row.next_trigger_time - expected) < timedelta(minutes=1), (
            f"смесь: local_time {row.local_time}, next_trigger_time {row.next_trigger_time}"
        )


class TestParallelCsvImport:
    """Двойная отправка импорта параллельно: строки не дублируются (§16.2, §7.2 п.4)."""

    async def test_parallel_imports_insert_once(
        self, session_factory, user: DbUser
    ) -> None:
        from zoneinfo import ZoneInfo

        from web.cabinet import csv_import

        data = (
            "timestamp_local;systolic;diastolic;pulse;medication_taken;feeling;note\r\n"
            "12.03.2024 08:30;135;85;;;;\r\n"
            "13.03.2024 09:00;120;80;;;;\r\n"
        ).encode("utf-8")
        parsed = csv_import.parse(
            data,
            "Europe/Minsk",
            now=datetime(2025, 3, 20, 12, 0, tzinfo=ZoneInfo("Europe/Minsk")),
        )

        async def run() -> int:
            async with session_factory() as session:
                report = await csv_import.import_rows(session, user, parsed)
                await session.commit()
                return report.imported

        results = await asyncio.gather(*(run() for _ in range(5)))
        assert sorted(results) == [0, 0, 0, 0, 2]
        assert await count(session_factory, Measurement) == 2


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

        callback = make_callback("m:key:ok")
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

    async def test_parallel_take_all_logs_once(
        self, session_factory, user: DbUser
    ) -> None:
        """Пять конкурентных «Принял всё ✓» — ровно один набор логов.

        Одновременные вызовы `log_slot_intake` в собственных сессиях (как
        серия тапов по кнопке под нагрузкой): дубли не создаются, метка
        достаётся только победителю, остальные получают `already_logged=True`.
        """
        async with session_factory() as session:
            meds = await medications_service.add_medication(
                session, user, "Лозартан", "50 мг", ["morning"]
            )
            await session.commit()
        decisions = {meds[0].id: "taken"}

        async def tap() -> tuple[bool, list[str]]:
            async with session_factory() as session:
                result = await medications_service.log_slot_intake(
                    session, user, "morning", decisions
                )
                await session.commit()
            return result.already_logged, result.taken

        results = await asyncio.gather(*(tap() for _ in range(5)))
        assert sorted(flag for flag, _ in results) == [False] + [True] * 4
        assert sum(len(labels) for _, labels in results) == 1  # метка — победителю
        assert await count(session_factory, MedicationLog) == 1

    async def test_insert_conflict_take_all_logs_once(
        self, session_factory, user: DbUser
    ) -> None:
        """Гонка «Принял всё ✓» с конкурентной вставкой: дубль не создаётся (§7.2).

        Гонка воспроизводится детерминированно: лекарство уже закоммичено,
        строка-конкурент вставлена, но не закоммичена — проверка «уже
        отмечено» её ещё не видит, а вставка после коммита конкурента не
        должна создавать вторую запись.
        """
        async with session_factory() as session:
            meds = await medications_service.add_medication(
                session, user, "Лозартан", "50 мг", ["morning"]
            )
            await session.commit()
        async with session_factory() as session:
            session.add(
                MedicationLog(
                    user_id=user.id,
                    medication_id=meds[0].id,
                    timestamp=datetime.now(timezone.utc),
                    status="taken",
                    slot="morning",
                )
            )
            await session.flush()  # строка есть в индексе, но транзакция открыта

            async def tap() -> tuple[bool, list[str]]:
                async with session_factory() as own:
                    result = await medications_service.log_slot_intake(
                        own, user, "morning", {meds[0].id: "taken"}
                    )
                    await own.commit()
                return result.already_logged, result.taken

            task = asyncio.create_task(tap())
            await asyncio.sleep(0.2)  # сервис успевает пройти проверку и встать на вставку
            await session.commit()
            already_logged, labels = await task
            assert already_logged is True
            assert labels == []
        assert await count(session_factory, MedicationLog) == 1

    async def test_double_done_logs_once(
        self, session_factory, user: DbUser
    ) -> None:
        """Двойное «Готово» мультивыбора — один набор логов за сутки (§6.3)."""
        async with session_factory() as session:
            meds = await medications_service.add_medication(
                session, user, "Лозартан", "50 мг", ["morning"]
            )
            await session.commit()
        async with session_factory() as session:
            first = await medications_service.log_manual_intake(session, user, [meds[0].id])
            await session.commit()
        async with session_factory() as session:
            second = await medications_service.log_manual_intake(session, user, [meds[0].id])
            await session.commit()
        assert first.already_logged is False
        assert second.already_logged is True
        assert first.taken == second.taken == ["Лозартан 50 мг"]
        assert await count(session_factory, MedicationLog) == 1

    async def test_parallel_done_logs_once(
        self, session_factory, user: DbUser
    ) -> None:
        """Пять конкурентных «Готово» с одним набором — ровно один набор логов."""
        async with session_factory() as session:
            meds = await medications_service.add_medication(
                session, user, "Лозартан", "50 мг", ["morning"]
            )
            await session.commit()

        async def tap() -> tuple[bool, list[str]]:
            async with session_factory() as own:
                result = await medications_service.log_manual_intake(
                    own, user, [meds[0].id]
                )
                await own.commit()
            return result.already_logged, result.taken

        results = await asyncio.gather(*(tap() for _ in range(5)))
        assert sorted(flag for flag, _ in results) == [False] + [True] * 4
        # Подписи возвращаются всем (для «💊 Принято: …»), но лог ровно один.
        assert all(labels == ["Лозартан 50 мг"] for _, labels in results)
        assert await count(session_factory, MedicationLog) == 1

    async def test_parallel_link_measurements_once(
        self, session_factory, user: DbUser
    ) -> None:
        """Пять конкурентных «Готово» — ровно одна связь с записью (§4.11, §7.2)."""
        async with session_factory() as session:
            meds = await medications_service.add_medication(
                session, user, "Лозартан", "50 мг", ["morning"]
            )
            measurement = await measurements_service.save_measurement(
                session, user, 130, 85
            )
            await session.commit()

        async def tap() -> None:
            async with session_factory() as own:
                await medications_service.link_measurement_intake(
                    own, user, measurement.id, [meds[0].id]
                )
                await own.commit()

        await asyncio.gather(*(tap() for _ in range(5)))
        assert await count(session_factory, MeasurementMedication) == 1

    async def test_double_link_measurements_once(
        self, session_factory, user: DbUser
    ) -> None:
        """Повторное «Готово» (вне окна debounce) — связь не дублируется (§7.2)."""
        async with session_factory() as session:
            meds = await medications_service.add_medication(
                session, user, "Лозартан", "50 мг", ["morning"]
            )
            measurement = await measurements_service.save_measurement(
                session, user, 130, 85
            )
            await session.commit()
        async with session_factory() as session:
            await medications_service.link_measurement_intake(
                session, user, measurement.id, [meds[0].id]
            )
            await session.commit()
        async with session_factory() as session:
            await medications_service.link_measurement_intake(
                session, user, measurement.id, [meds[0].id]
            )
            await session.commit()
        assert await count(session_factory, MeasurementMedication) == 1

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

    async def test_parallel_yes_without_measurement_creates_one_remeasure(
        self, session_factory, user: DbUser, svc: RemindersService
    ) -> None:
        """Конкурентная двойная «Да» без привязки к записи — один повтор.

        У повтора с `measurement_id IS NULL` UNIQUE по `measurement_id` не
        действует (NULL не равны NULL) — сериализация локом не даёт
        создать второй повтор замера (§7.2).
        """
        results = await asyncio.gather(
            svc.ensure_remeasure(user.id, None, 60),
            svc.ensure_remeasure(user.id, None, 60),
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


class TestMedsDoneLockOrder:
    """«Готово» мультивыбора не ждёт лок приёмов, держа запись SQLite (T1, §7.2).

    Ошибка, найденная нагрузкой (`qa/test_load_bot.py`): `cb_med_done` писал
    журнал под `_current_intake_lock`, отпускал лок, не закрыв транзакцию, и
    ждал тот же лок второй раз для связей с записью. Пока лок у другой
    операции, а та ждёт запись SQLite, — взаимоблокировка до `busy_timeout`
    и `database is locked`.

    Порядок здесь детерминирован очередью `asyncio.Lock`: тест держит лок,
    хендлер встаёт в очередь первым, тест — вторым. Хендлер получает лок,
    пишет и отпускает; следующим лок получает тест и держит его. Хендлеру,
    которому лок нужен один раз, это не мешает завершиться; хендлер, который
    ждёт лок повторно, застревает — тест падает по таймауту.
    """

    async def test_done_needs_intake_lock_once(
        self, session_factory, user: DbUser, mocker
    ) -> None:
        from aiogram.fsm.context import FSMContext
        from aiogram.fsm.storage.base import StorageKey
        from aiogram.fsm.storage.memory import MemoryStorage
        from aiogram.types import Chat, Message

        from bot.handlers import measure

        async with session_factory() as session:
            meds = await medications_service.add_medication(
                session, user, "Лозартан", "50 мг", ["morning"]
            )
            measurement = await measurements_service.save_measurement(session, user, 130, 85)
            await session.commit()
        mocker.patch.object(CallbackQuery, "answer", new_callable=mocker.AsyncMock)
        mocker.patch.object(Message, "edit_text", new_callable=mocker.AsyncMock)
        state = FSMContext(
            storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=TG_ID, user_id=TG_ID)
        )
        await state.set_data(
            {measure.M_ID: measurement.id, measure.M_MED_CHECKED: [meds[0].id]}
        )
        callback = CallbackQuery(
            id="done",
            from_user=User(id=TG_ID, is_bot=False, first_name="Тест"),
            chat_instance="ci",
            data="m:med:done",
            message=Message(
                message_id=1,
                date=datetime.now(timezone.utc),
                chat=Chat(id=TG_ID, type="private"),
                text="…",
            ),
        )

        lock = medications_service._current_intake_lock()
        await lock.acquire()

        async def done() -> None:
            async with session_factory() as session:
                await measure.cb_med_done(
                    callback,
                    state,
                    session,
                    mocker.Mock(ensure_remeasure=mocker.AsyncMock()),
                )

        task = asyncio.create_task(done())
        for _ in range(200):  # хендлер дошёл до лока (очередь непуста)
            if lock._waiters:
                break
            await asyncio.sleep(0.01)
        assert lock._waiters, "хендлер не дошёл до лока приёмов"
        lock.release()
        await lock.acquire()  # в очереди сразу за хендлером
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=3)
        except TimeoutError:
            pytest.fail(
                "cb_med_done ждёт _current_intake_lock повторно, держа запись "
                "SQLite: взаимоблокировка с конкурентной отметкой приёма (T1)"
            )
        finally:
            lock.release()
            await task

        async with session_factory() as session:
            logs = (
                await session.execute(select(func.count()).select_from(MedicationLog))
            ).scalar_one()
            links = (
                await session.execute(
                    select(func.count()).select_from(MeasurementMedication)
                )
            ).scalar_one()
        assert (logs, links) == (1, 1), "журнал и связь — в одной транзакции"


class TestCabinetIntakeLockOrder:
    """Форма записи кабинета берёт лок приёмов до первой записи в БД (§16.2, §7.2).

    Добавление, правка и удаление пишут запись, связи и журнал одной
    транзакцией под `intake_lock`. Если бы хендлер успел записать (например,
    измерение) и только потом встал в очередь лока, он держал бы запись
    SQLite, пока лок у другой операции, — шаблон T1. Проверка
    детерминирована: тест держит лок, хендлер (в том же event loop, через
    `httpx.ASGITransport`) встаёт в очередь, и постороннее соединение
    обязано сразу получить `BEGIN IMMEDIATE`.
    """

    @staticmethod
    def _can_take_write_lock(db_path: str) -> bool:
        import sqlite3

        con = sqlite3.connect(db_path, timeout=0.2)
        try:
            con.execute("BEGIN IMMEDIATE")
            con.rollback()
            return True
        except sqlite3.OperationalError:
            return False
        finally:
            con.close()

    @staticmethod
    async def _cabinet(engine, session_factory):
        """Кабинет на той же БД, клиент с сессией и CSRF, лекарство и запись с журналом."""
        import httpx

        from bot.config import Settings
        from web.cabinet import auth
        from web.cabinet.app import CSRF_COOKIE, SESSION_COOKIE, create_app

        async with session_factory() as session:
            owner = DbUser(telegram_id=TG_ID, timezone="Europe/Minsk")
            session.add(owner)
            await session.flush()
            web_user = WebUser(
                user_id=owner.id, email="user@example.org", password_hash="not-used"
            )
            session.add(web_user)
            await session.flush()
            token = await auth.create_web_session(session, web_user.id)
            meds = await medications_service.add_medication(
                session, owner, "Лозартан", "50 мг", ["morning"]
            )
            moment = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(days=2)
            measurement = await measurements_service.save_measurement(
                session, owner, 130, 85, timestamp=moment
            )
            await medications_service.sync_medication_logs(
                session, owner, measurement.id, measurement.timestamp, meds
            )
            await session.commit()
        settings = Settings(
            bot_token=f"123456:{'A' * 35}",
            web_secret_key="test-secret",
            db_path=engine.url.database,
        )
        app = create_app(settings, session_factory=session_factory)
        csrf_token = app.state.csrf.issue()
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://testserver",
            follow_redirects=False,
            cookies={SESSION_COOKIE: token, CSRF_COOKIE: csrf_token},
        )
        return client, csrf_token, meds[0].id, measurement.id

    @staticmethod
    def _form(csrf_token: str, med_id: int, days_ago: int, hhmm: str) -> dict:
        from zoneinfo import ZoneInfo

        day = datetime.now(ZoneInfo("Europe/Minsk")).date() - timedelta(days=days_ago)
        return {
            "csrf_token": csrf_token,
            "date": day.isoformat(),
            "time": hhmm,
            "systolic": "135",
            "diastolic": "85",
            "medication_taken": "1",
            "medication_ids": str(med_id),
        }

    @pytest.mark.parametrize("action", ["add", "edit", "delete"])
    async def test_no_write_while_waiting_for_lock(self, engine, session_factory, action) -> None:
        client, csrf_token, med_id, measurement_id = await self._cabinet(engine, session_factory)
        url, data = {
            "add": ("/measurements", self._form(csrf_token, med_id, 1, "08:30")),
            "edit": (f"/measurements/{measurement_id}", self._form(csrf_token, med_id, 3, "09:00")),
            "delete": (f"/measurements/{measurement_id}/delete", {"csrf_token": csrf_token}),
        }[action]
        lock = medications_service.intake_lock()
        await lock.acquire()
        task = asyncio.create_task(client.post(url, data=data))
        try:
            for _ in range(300):  # хендлер дошёл до лока (очередь непуста)
                if lock._waiters:
                    break
                await asyncio.sleep(0.01)
            assert lock._waiters, "хендлер не дошёл до лока приёмов"
            assert self._can_take_write_lock(engine.url.database), (
                f"{action}: хендлер ждёт лок приёмов, держа запись SQLite (§7.2, T1)"
            )
        finally:
            lock.release()
        response = await asyncio.wait_for(task, timeout=5)
        await client.aclose()
        assert response.status_code == 303
        assert "saved=measurement" in response.headers["location"]

    async def test_parallel_adds_log_intake_once_per_day(self, engine, session_factory) -> None:
        """Две записи за одни сутки с одним лекарством: две связи, одна строка журнала (§7.2)."""
        client, csrf_token, med_id, _ = await self._cabinet(engine, session_factory)
        responses = await asyncio.gather(
            client.post("/measurements", data=self._form(csrf_token, med_id, 1, "08:00")),
            client.post("/measurements", data=self._form(csrf_token, med_id, 1, "08:30")),
        )
        await client.aclose()
        assert [r.headers["location"] for r in responses] == ["/?saved=measurement#history"] * 2
        async with session_factory() as session:
            yesterday_logs = (
                await session.execute(
                    select(func.count())
                    .select_from(MedicationLog)
                    .where(MedicationLog.timestamp > datetime.now(timezone.utc) - timedelta(days=1.9))
                )
            ).scalar_one()
            links = (
                await session.execute(select(func.count()).select_from(MeasurementMedication))
            ).scalar_one()
        assert yesterday_logs == 1, "приём за сутки отмечается один раз"
        assert links == 3, "запись из сида + две новые, у каждой — своя связь"


class TestGetOrCreateReleasesWrite:
    """`get_or_create_user` не оставляет открытой запись SQLite (U2, §7.2).

    Хендлеры вызывают его перед операциями под asyncio-локом (дедуп
    сохранений, отметки приёмов). Если созданная строка `users` осталась бы
    незакоммиченной, сессия ждала бы лок, держа запись SQLite, — тот же
    шаблон взаимоблокировки, что в «Готово» мультивыбора (T1). Проверка
    детерминирована: сессия ещё открыта, а постороннее соединение обязано
    сразу получить блокировку записи (`BEGIN IMMEDIATE`, без ожидания).
    """

    @staticmethod
    def _can_take_write_lock(db_path: str) -> bool:
        import sqlite3

        con = sqlite3.connect(db_path, timeout=0.2)
        try:
            con.execute("BEGIN IMMEDIATE")
            con.rollback()
            return True
        except sqlite3.OperationalError:
            return False
        finally:
            con.close()

    async def test_created_user_is_committed(self, engine, session_factory) -> None:
        db_path = engine.url.database
        async with session_factory() as session:
            user = await measurements_service.get_or_create_user(session, 333_333)
            assert user.id is not None
            assert self._can_take_write_lock(db_path), (
                "после создания пользователя сессия держит запись SQLite"
            )
        async with session_factory() as session:
            count_users = (
                await session.execute(
                    select(func.count()).select_from(DbUser).where(DbUser.telegram_id == 333_333)
                )
            ).scalar_one()
        assert count_users == 1, "созданный пользователь сохранён и без commit вызывающего"

    async def test_existing_user_takes_no_write(self, engine, session_factory, user) -> None:
        db_path = engine.url.database
        async with session_factory() as session:
            found = await measurements_service.get_or_create_user(session, TG_ID)
            assert found.id == user.id
            assert self._can_take_write_lock(db_path)


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


class TestFollowupAnswerRace:
    """Гонка «ответ и повтор» (§6.7, 0.5.6): прав тот, кто первым обнулил `followup_at`.

    Чередование детерминированное: доставка повтора останавливается между
    чтением строки и атомарным захватом (на проверке `_answered`), в этот
    момент проходит ответ.
    """

    async def _setup(self, session_factory, user: DbUser):
        sent: list[tuple] = []

        async def sender(
            telegram_id: int, text: str, markup, silent: bool = False, html: bool = False
        ) -> int:
            sent.append((telegram_id, text, silent))
            return 500

        svc = RemindersService(session_factory, sender, FakeScheduler())
        reminder = await svc.ensure_slot(user.id, "morning", "08:00", "Europe/Minsk")
        async with session_factory() as session:
            await session.execute(
                Reminder.__table__.update()
                .where(Reminder.id == reminder.id)
                .values(followup_at=datetime.now(timezone.utc) - timedelta(minutes=1))
            )
            await session.commit()
        return svc, reminder, sent

    async def _followup_at(self, session_factory, reminder_id: int):
        async with session_factory() as session:
            return (
                await session.execute(select(Reminder.followup_at).where(Reminder.id == reminder_id))
            ).scalar_one()

    async def test_answer_between_read_and_claim_wins(self, session_factory, user: DbUser, monkeypatch) -> None:
        svc, reminder, sent = await self._setup(session_factory, user)
        reached, release = asyncio.Event(), asyncio.Event()
        original = svc._answered

        async def paused_answered(*args, **kwargs) -> bool:
            reached.set()
            await release.wait()
            return await original(*args, **kwargs)

        monkeypatch.setattr(svc, "_answered", paused_answered)
        delivery = asyncio.create_task(svc._deliver_followup(reminder.id))
        await reached.wait()
        assert await svc.cancel_followup(user.id, "morning") is True  # ответ — первым
        release.set()
        assert await delivery is False
        assert sent == []
        assert await self._followup_at(session_factory, reminder.id) is None

    async def test_followup_claimed_first_answer_is_noop(self, session_factory, user: DbUser) -> None:
        svc, reminder, sent = await self._setup(session_factory, user)
        assert await svc._deliver_followup(reminder.id) is True
        assert await svc.cancel_followup(user.id, "morning") is False
        assert [silent for *_, silent in sent] == [True]

    async def test_parallel_followups_send_once(self, session_factory, user: DbUser) -> None:
        svc, reminder, sent = await self._setup(session_factory, user)
        results = await asyncio.gather(
            svc._deliver_followup(reminder.id), svc._deliver_followup(reminder.id)
        )
        assert sorted(results) == [False, True]
        assert len(sent) == 1


class TestAppointmentsRaces:
    """Визиты (§6.13, §7.2): двойной тик — одно уведомление; лимит 30 под гонкой."""

    async def test_parallel_ticks_send_once(self, session_factory, user: DbUser) -> None:
        from bot.services import appointments as ap

        moment = datetime(2026, 11, 11, 17, 1, tzinfo=timezone.utc)
        async with session_factory() as session:
            db_user = await session.get(DbUser, user.id)
            await ap.create(session, db_user, "doctor", "Кардиолог", date(2026, 11, 12), "10:30",
                            now=moment - timedelta(hours=8))
            await session.commit()
        sent = []

        async def sender(telegram_id, text, markup):
            sent.append(text)
            return 1

        results = await asyncio.gather(*(
            ap.deliver_due(session_factory, sender, lambda *a: None, None, now=moment) for _ in range(3)
        ))
        assert sum(results) == 1 and len(sent) == 1

    async def test_parallel_adds_respect_limit(self, session_factory, user: DbUser) -> None:
        from bot.services import appointments as ap

        moment = datetime(2026, 11, 11, 9, 0, tzinfo=timezone.utc)
        async with session_factory() as session:
            db_user = await session.get(DbUser, user.id)
            for index in range(ap.UPCOMING_LIMIT - 1):
                await ap.create(session, db_user, "lab", f"А{index}", date(2026, 12, 1), None, now=moment)
            await session.commit()

        async def one(index: int) -> bool:
            async with session_factory() as session:
                db_user = await session.get(DbUser, user.id)
                try:
                    await ap.create(session, db_user, "lab", f"Б{index}", date(2026, 12, 2), None, now=moment)
                except ap.LimitReached:
                    return False
                await session.commit()
                return True

        results = await asyncio.gather(*(one(i) for i in range(4)))
        assert results.count(True) == 1
        async with session_factory() as session:
            db_user = await session.get(DbUser, user.id)
            assert await ap.count_upcoming(session, db_user, date(2026, 11, 11)) == ap.UPCOMING_LIMIT


class TestParallelConclusions:
    """Заключения и отметка (0.5.11): гонки условной вставки и статусов (§7.2, §13)."""

    async def _appointment(self, session_factory, user: DbUser, title: str = "Кардиолог") -> int:
        async with session_factory() as session:
            appt = Appointment(
                user_id=user.id,
                kind="doctor",
                title=title,
                starts_on=date.today() - timedelta(days=1),
                starts_at="10:00",
                status="planned",
            )
            session.add(appt)
            await session.commit()
            return appt.id

    async def test_limit_race(self, session_factory, user: DbUser) -> None:
        """Две вставки на границе лимита 5 — проходит ровно одна (условная вставка)."""
        appt_id = await self._appointment(session_factory, user)
        async with session_factory() as session:
            for index in range(appointments_service.CONCLUSION_LIMIT - 1):
                await appointments_service.add_conclusion(session, appt_id, f"З-{index}")
            await session.commit()

        async def save():
            async with session_factory() as session:
                created = await appointments_service.add_conclusion(session, appt_id, "Край")
                await session.commit()
                return created

        results = await asyncio.gather(save(), save())
        assert sorted(result is None for result in results) == [False, True]
        assert await count(session_factory, AppointmentConclusion) == appointments_service.CONCLUSION_LIMIT

    async def test_double_mark_idempotent(self, session_factory, user: DbUser) -> None:
        """Двойное нажатие отметки — статус меняется один раз (§7.2)."""
        appt_id = await self._appointment(session_factory, user)

        async def mark() -> bool:
            async with session_factory() as session:
                row = await session.get(Appointment, appt_id)
                won = await appointments_service.set_status(session, row, "done")
                await session.commit()
                return won

        results = await asyncio.gather(mark(), mark())
        assert sorted(results) == [False, True]
        async with session_factory() as session:
            assert (await session.get(Appointment, appt_id)).status == "done"

    async def test_mark_blocks_after_notice(self, session_factory, user: DbUser) -> None:
        """Отметка гасит «Как прошёл визит?»: захват тика после отметки — False (§6.13)."""
        appt_id = await self._appointment(session_factory, user, title="Биохимия")
        async with session_factory() as session:
            row = await session.get(Appointment, appt_id)
            assert await appointments_service.set_status(session, row, "missed")
            await session.commit()
        async with session_factory() as session:
            assert not await appointments_service._claim(
                session, appt_id, "after", datetime.now(timezone.utc)
            )
            await session.commit()

    async def test_double_delete_conclusion(self, session_factory, user: DbUser) -> None:
        """Двойное удаление заключения — одно (§7.2).

        Детерминированное чередование (приём проекта для гонок): обе сессии
        читают строку до первого удаления — второе удаление возвращает False.
        """
        appt_id = await self._appointment(session_factory, user)
        async with session_factory() as session:
            conc_id = await appointments_service.add_conclusion(session, appt_id, "Текст")
            await session.commit()

        async with session_factory() as first, session_factory() as second:
            user_a = await first.get(DbUser, user.id)
            user_b = await second.get(DbUser, user.id)
            row_a = await first.get(AppointmentConclusion, conc_id)
            row_b = await second.get(AppointmentConclusion, conc_id)
            assert row_a is not None and row_b is not None  # обе сессии видят строку
            won_first = await appointments_service.remove_conclusion(first, user_a, row_a.id)
            await first.commit()
            won_second = await appointments_service.remove_conclusion(second, user_b, row_b.id)
            await second.commit()
        assert [won_first, won_second] == [True, False]
        assert await count(session_factory, AppointmentConclusion) == 0
