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
from bot.models import (
    Measurement,
    MedicationLog,
    MeasurementMedication,
    Reminder,
    User as DbUser,
    WebUser,
)
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
                await measure.cb_med_done(callback, state, session)

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
