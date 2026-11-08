"""Burst-нагрузка сервисного слоя (SPEC.md, §22, уровень 2).

20 пациентов стенда × 50 конкурентных операций (`asyncio.gather`) на копии
шаблонной БД (файловая SQLite с PRAGMA продакшена; фоном — 5000 записей
сида). Сценарии: массовая запись измерений, двойные нажатия «Записать
давление», конкурентные правки одной записи, одновременные «Принял всё ✓»
на одном слоте, параллельные `/start`.

Каждая операция — своя сессия (§7.2 п.1) и `run_with_retry`, как в проде
(§7.2 п.3). Критерии: ни одна операция не упала (в том числе с
`database is locked` после повторов), дублей за минуту нет, сценарий укладывается
в 60 с. Повторы при блокировке считаются по логгеру `bot.db` и попадают в
отчёт. Итог всех сценариев — `qa/reports/load/test_load_burst.json`.

Профиль: без флага — дымовой (3 пациента × 5 операций), `LOAD_TEST=1` —
полный (20 × 50). Запуск:

    LOAD_TEST=1 uv run pytest qa/test_load_burst.py -v -s
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Iterator
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.db import create_engine, create_session_factory, run_with_retry
from bot.models import Measurement, MedicationLog, User
from bot.services import measurements as measurements_service
from bot.services import medications as medications_service
from qa.seed import load_patients

#: Предел времени одного сценария (SPEC §22, уровень 2).
SCENARIO_LIMIT_S = 60.0

#: Telegram ID «новых» пользователей сценария параллельных /start (вне стенда).
NEW_TG_BASE = 880_000

#: Итоги сценариев модуля → один JSON-отчёт в конце (фикстура `burst_report`).
_RESULTS: dict[str, dict] = {}


class LockCounter(logging.Handler):
    """Считает повторы `run_with_retry` при «database is locked» (логгер bot.db)."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.retries = 0

    def emit(self, record: logging.LogRecord) -> None:
        if "заблокирован" in record.getMessage():
            self.retries += 1


@pytest.fixture
def lock_counter() -> Iterator[LockCounter]:
    handler = LockCounter()
    logger = logging.getLogger("bot.db")
    logger.addHandler(handler)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)


@pytest_asyncio.fixture
async def session_factory(load_db: str) -> async_sessionmaker[AsyncSession]:
    """Сессии к копии шаблона стенда (PRAGMA прода — из `create_engine`)."""
    engine = create_engine(load_db)
    yield create_session_factory(engine)
    await engine.dispose()


@pytest_asyncio.fixture
async def patients(session_factory, load_scale) -> list[User]:
    """Нагрузочные пациенты шаблона (20 в полном профиле, 3 — в дымовом)."""
    async with session_factory() as session:
        pairs = await load_patients(session, load_scale)
    assert len(pairs) == load_scale.patients, "шаблон стенда без пациентов"
    return [user for _, user in pairs]


@pytest.fixture
def ops(load_full: bool) -> int:
    """Операций на пользователя: 50 в полном профиле, 5 — в дымовом."""
    return 50 if load_full else 5


@pytest.fixture(scope="module", autouse=True)
def burst_report(load_report, load_log) -> Iterator[None]:
    """После всех сценариев модуля — один JSON-итог для регрессии (SPEC §22)."""
    _RESULTS.clear()
    yield
    passed = all(result["passed"] for result in _RESULTS.values())
    load_report("test_load_burst", {"passed": passed, "scenarios": dict(_RESULTS)})
    load_log.summary(f"burst: {len(_RESULTS)} сценариев, passed={passed}")


def percentile(values: list[float], share: float) -> float:
    """Перцентиль по ближайшему рангу (p95 → share=0.95); пустой список → 0."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(0, min(len(ordered) - 1, round(share * len(ordered) + 0.5) - 1))
    return ordered[rank]


async def run_burst(
    name: str,
    operations: list[tuple[int, Callable[[], Awaitable[object]]]],
    load_log,
    lock_counter: LockCounter,
) -> tuple[list[object], dict]:
    """Запустить операции одним `gather`, замерить и залогировать каждую.

    `operations` — пары (user_id, фабрика корутины). Исключения не теряются:
    `return_exceptions=True`, упавшие операции считаются в `failures`.
    """
    durations: list[float] = []

    async def timed(user_id: int, factory: Callable[[], Awaitable[object]]) -> object:
        started = time.perf_counter()
        try:
            return await run_with_retry(factory)
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            durations.append(elapsed_ms)
            load_log.op(name, user_id=user_id, duration_ms=round(elapsed_ms, 2))

    started = time.perf_counter()
    results = await asyncio.gather(
        *(timed(user_id, factory) for user_id, factory in operations), return_exceptions=True
    )
    elapsed = time.perf_counter() - started
    failures = [repr(result) for result in results if isinstance(result, BaseException)]
    stats = {
        "operations": len(operations),
        "failures": len(failures),
        "failure_samples": failures[:5],
        "locked_retries": lock_counter.retries,
        "elapsed_s": round(elapsed, 3),
        "p50_ms": round(percentile(durations, 0.50), 2),
        "p95_ms": round(percentile(durations, 0.95), 2),
    }
    load_log.summary(
        f"{name}: {len(operations)} операций за {elapsed:.2f} с, "
        f"p95 {stats['p95_ms']} мс, ошибок {len(failures)}, повторов {lock_counter.retries}",
        scenario=name,
        **stats,
    )
    return results, stats


async def minute_duplicates(session_factory) -> list[tuple[int, str]]:
    """Нарушители «одна запись в минуту»: (user_id, минута) с COUNT > 1."""
    minute = func.strftime("%Y-%m-%d %H:%M", Measurement.timestamp)
    async with session_factory() as session:
        rows = await session.execute(
            select(Measurement.user_id, minute)
            .group_by(Measurement.user_id, minute)
            .having(func.count() > 1)
        )
        return [(user_id, value) for user_id, value in rows]


def record(name: str, stats: dict, **checks: object) -> dict:
    """Сохранить итог сценария: метрики + проверки; passed — все проверки True."""
    flags = {key: value for key, value in checks.items() if isinstance(value, bool)}
    result = {
        **stats,
        **checks,
        "within_limit": stats["elapsed_s"] < SCENARIO_LIMIT_S,
        "passed": stats["failures"] == 0
        and stats["elapsed_s"] < SCENARIO_LIMIT_S
        and all(flags.values()),
    }
    _RESULTS[name] = result
    return result


def future_base(days: int) -> datetime:
    """Начало минуты через `days` суток: вне расписания сида (оно — в прошлом)."""
    return datetime.now(timezone.utc).replace(second=0, microsecond=0) + timedelta(days=days)


class TestBurstMeasurements:
    """Массовая запись и двойные нажатия «Записать давление»."""

    async def test_mass_writes(self, session_factory, patients, ops, load_log, lock_counter) -> None:
        """USERS × OPS конкурентных сохранений в разные минуты — все строки на месте."""
        base = future_base(1)
        users = len(patients)

        def save(user: User, index: int, op: int):
            async def _save() -> None:
                async with session_factory() as session:
                    await measurements_service.save_measurement(
                        session, user, 120 + (index + op) % 40, 75 + (index * 3 + op) % 20,
                        pulse=60 + (index + op) % 30,
                        timestamp=base + timedelta(minutes=op * users + index),
                    )
                    await session.commit()

            return _save

        async with session_factory() as session:
            before = (await session.execute(select(func.count()).select_from(Measurement))).scalar_one()
        operations = [
            (user.id, save(user, index, op))
            for index, user in enumerate(patients)
            for op in range(ops)
        ]
        _, stats = await run_burst("mass_writes", operations, load_log, lock_counter)
        async with session_factory() as session:
            after = (await session.execute(select(func.count()).select_from(Measurement))).scalar_one()
        duplicates = await minute_duplicates(session_factory)
        result = record(
            "mass_writes",
            stats,
            rows_added=after - before,
            rows_expected=len(operations),
            all_rows_saved=after - before == len(operations),
            no_minute_duplicates=not duplicates,
            duplicate_samples=duplicates[:5],
        )
        assert stats["failures"] == 0, f"упавшие сохранения: {stats['failure_samples']}"
        assert result["all_rows_saved"], f"ожидалось +{len(operations)}, добавлено {after - before}"
        assert not duplicates, f"дубли за минуту (user_id, минута): {duplicates[:5]}"
        assert result["within_limit"], f"{stats['elapsed_s']} с ≥ {SCENARIO_LIMIT_S} с"

    async def test_double_taps(self, session_factory, patients, ops, load_log, lock_counter) -> None:
        """Каждый пользователь OPS раз «жмёт» сохранение одной минуты — ровно одна запись."""
        base = future_base(2)

        def save(user: User, index: int):
            async def _save() -> bool:
                async with session_factory() as session:
                    try:
                        await measurements_service.save_measurement(
                            session, user, 135, 85, timestamp=base + timedelta(minutes=index)
                        )
                        await session.commit()
                        return True
                    except measurements_service.DuplicateMeasurementError:
                        await session.rollback()
                        return False

            return _save

        operations = [
            (user.id, save(user, index)) for index, user in enumerate(patients) for _ in range(ops)
        ]
        results, stats = await run_burst("double_taps", operations, load_log, lock_counter)
        saved = sum(1 for result in results if result is True)
        rejected = sum(1 for result in results if result is False)
        duplicates = await minute_duplicates(session_factory)
        result = record(
            "double_taps",
            stats,
            saved=saved,
            rejected_as_duplicate=rejected,
            one_row_per_user=saved == len(patients),
            no_minute_duplicates=not duplicates,
            duplicate_samples=duplicates[:5],
        )
        assert stats["failures"] == 0, f"упавшие нажатия: {stats['failure_samples']}"
        assert result["one_row_per_user"], f"сохранено {saved}, ожидалось {len(patients)}"
        assert rejected == len(operations) - len(patients)
        assert not duplicates, f"дубли за минуту: {duplicates[:5]}"
        assert result["within_limit"]


class TestBurstEdits:
    """Конкурентные правки одной записи (§7.2 п.4: UPDATE той же строки)."""

    async def test_concurrent_edits(self, session_factory, patients, ops, load_log, lock_counter) -> None:
        """OPS правок одной записи каждого пользователя — итог целиком один из наборов."""
        async with session_factory() as session:
            targets = {}
            for user in patients:
                latest = await measurements_service.get_latest_measurement(session, user)
                assert latest is not None, f"user {user.id}: у пациента шаблона нет записей"
                targets[user.id] = (latest.id, latest.timestamp)

        def value_set(op: int) -> tuple[int, int, int]:
            return 110 + op % 60, 70 + op % 30, 55 + op % 50

        def edit(user: User, op: int):
            measurement_id = targets[user.id][0]

            async def _edit() -> bool:
                async with session_factory() as session:
                    systolic, diastolic, pulse = value_set(op)
                    updated = await measurements_service.update_measurement_values(
                        session, user, measurement_id, systolic, diastolic, pulse
                    )
                    await session.commit()
                    return updated is not None

            return _edit

        operations = [(user.id, edit(user, op)) for user in patients for op in range(ops)]
        results, stats = await run_burst("concurrent_edits", operations, load_log, lock_counter)
        allowed = {value_set(op) for op in range(ops)}
        mixed: list[int] = []
        moved: list[int] = []
        async with session_factory() as session:
            for user in patients:
                measurement_id, timestamp = targets[user.id]
                row = (
                    await session.execute(select(Measurement).where(Measurement.id == measurement_id))
                ).scalar_one()
                if (row.systolic, row.diastolic, row.pulse) not in allowed:
                    mixed.append(user.id)
                if row.timestamp != timestamp:
                    moved.append(user.id)
        result = record(
            "concurrent_edits",
            stats,
            all_updates_found_row=all(result is True for result in results),
            no_mixed_values=not mixed,
            timestamps_unchanged=not moved,
            mixed_user_ids=mixed,
            moved_user_ids=moved,
        )
        assert stats["failures"] == 0, f"упавшие правки: {stats['failure_samples']}"
        assert result["all_updates_found_row"]
        assert not mixed, f"смесь значений правок у пользователей {mixed}"
        assert not moved, f"timestamp изменился у пользователей {moved}"
        assert result["within_limit"]


class TestBurstIntake:
    """Одновременные «Принял всё ✓» на одном слоте — ровно один набор логов."""

    async def test_concurrent_take_all(self, session_factory, patients, ops, load_log, lock_counter) -> None:
        plans: dict[int, tuple[str, dict[int, str]]] = {}
        async with session_factory() as session:
            for user in patients:
                for slot in ("morning", "noon", "evening"):
                    meds = await medications_service.slot_medications(session, user, slot)
                    if meds:
                        plans[user.id] = (slot, {med.id: "taken" for med in meds})
                        break
                assert user.id in plans, f"user {user.id}: в шаблоне нет лекарств по слотам"
        moment = datetime.now(timezone.utc)

        async def logs_today(user: User, slot: str) -> int:
            async with session_factory() as session:
                day_start, day_end = measurements_service.day_bounds_utc(
                    user.timezone, moment.astimezone(ZoneInfo(user.timezone)).date()
                )
                return (
                    await session.execute(
                        select(func.count()).select_from(MedicationLog).where(
                            MedicationLog.user_id == user.id,
                            MedicationLog.slot == slot,
                            MedicationLog.timestamp >= day_start,
                            MedicationLog.timestamp < day_end,
                        )
                    )
                ).scalar_one()

        before = {user.id: await logs_today(user, plans[user.id][0]) for user in patients}

        def take_all(user: User):
            slot, decisions = plans[user.id]

            async def _take() -> None:
                async with session_factory() as session:
                    await medications_service.log_slot_intake(
                        session, user, slot, decisions, timestamp=moment
                    )
                    await session.commit()

            return _take

        taps = max(2, ops // 5)  # 10 одновременных нажатий в полном профиле
        operations = [(user.id, take_all(user)) for user in patients for _ in range(taps)]
        _, stats = await run_burst("concurrent_take_all", operations, load_log, lock_counter)
        wrong: list[dict] = []
        for user in patients:
            slot, decisions = plans[user.id]
            added = await logs_today(user, slot) - before[user.id]
            if added != len(decisions):
                wrong.append({"user_id": user.id, "slot": slot, "expected": len(decisions), "added": added})
        result = record(
            "concurrent_take_all",
            stats,
            taps_per_user=taps,
            one_log_set_per_slot=not wrong,
            violations=wrong,
        )
        assert stats["failures"] == 0, f"упавшие отметки: {stats['failure_samples']}"
        assert not wrong, f"не один набор логов за слот: {wrong[:5]}"
        assert result["within_limit"]


class TestBurstStart:
    """Параллельные /start: get-or-create даёт ровно одну строку users."""

    async def test_parallel_start(self, session_factory, patients, ops, load_log, lock_counter) -> None:
        new_ids = [NEW_TG_BASE + index for index in range(len(patients))]
        existing_ids = [user.telegram_id for user in patients]
        taps = max(2, ops // 5)

        def start(telegram_id: int):
            async def _start() -> int:
                async with session_factory() as session:
                    user = await measurements_service.get_or_create_user(session, telegram_id)
                    await session.commit()
                    return user.id

            return _start

        operations = [
            (telegram_id, start(telegram_id))
            for telegram_id in [*new_ids, *existing_ids]
            for _ in range(taps)
        ]
        results, stats = await run_burst("parallel_start", operations, load_log, lock_counter)
        async with session_factory() as session:
            rows = dict(
                (
                    await session.execute(
                        select(User.telegram_id, func.count())
                        .where(User.telegram_id.in_([*new_ids, *existing_ids]))
                        .group_by(User.telegram_id)
                    )
                ).all()
            )
        wrong = {tg: rows.get(tg, 0) for tg in [*new_ids, *existing_ids] if rows.get(tg, 0) != 1}
        ids_by_tg: dict[int, set[int]] = {}
        for (telegram_id, _), result in zip(operations, results):
            if isinstance(result, int):
                ids_by_tg.setdefault(telegram_id, set()).add(result)
        split = [tg for tg, ids in ids_by_tg.items() if len(ids) != 1]
        result = record(
            "parallel_start",
            stats,
            starts_per_user=taps,
            one_row_per_telegram_id=not wrong,
            same_user_for_all_taps=not split,
            violations=wrong,
        )
        assert stats["failures"] == 0, f"упавшие /start: {stats['failure_samples']}"
        assert not wrong, f"не одна строка users на telegram_id: {wrong}"
        assert not split, f"разные user.id у одного telegram_id: {split}"
        assert result["within_limit"]
