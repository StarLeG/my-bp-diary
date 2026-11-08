"""Конкурентные гонки §7.2 в масштабе (SPEC.md, §22, уровень 7).

Те же гонки, что в `tests/test_concurrency.py`, но у всех пациентов стенда
сразу и по 10 конкурентных вызовов на каждого (дымовой профиль — 3 × 3):

- одинаковые `save_measurement` одного пользователя → одна запись;
- `ensure_remeasure` на одной записи → одна строка повтора и один job
  в запущенном прод-планировщике (`build_scheduler`);
- «Принял всё ✓» на слот → ровно N логов (N — лекарства слота), не 2N;
- правки одной записи → итог — один из наборов целиком, `timestamp` не меняется;
- параллельные `/start` → одна строка `users`.

Каждая операция — своя сессия и `run_with_retry`. Итог всех гонок —
`qa/reports/load/test_load_concurrent.json`.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterator
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from bot.db import create_engine, create_session_factory
from bot.main import build_scheduler
from bot.models import Measurement, MedicationLog, Reminder, User
from bot.services import measurements as measurements_service
from bot.services import medications as medications_service
from bot.services.measurements import day_bounds_utc
from bot.services.reminders import RemindersService
from qa.conftest import run_burst
from qa.seed import load_patients

#: Telegram ID «новых» пользователей гонки /start (вне стенда и burst).
NEW_TG_BASE = 860_000

_RESULTS: dict[str, dict] = {}


@pytest.fixture(scope="module", autouse=True)
def concurrent_report(load_report, load_log) -> Iterator[None]:
    """Один JSON-итог всех гонок модуля (SPEC §22)."""
    _RESULTS.clear()
    yield
    passed = all(result["passed"] for result in _RESULTS.values())
    load_report("test_load_concurrent", {"passed": passed, "races": dict(_RESULTS)})
    load_log.summary(f"concurrent: {len(_RESULTS)} гонок, passed={passed}")


@pytest_asyncio.fixture
async def session_factory(load_db: str):
    engine = create_engine(load_db)
    yield create_session_factory(engine)
    await engine.dispose()


@pytest_asyncio.fixture
async def patients(session_factory, load_scale) -> list[User]:
    async with session_factory() as session:
        pairs = await load_patients(session, load_scale)
    assert len(pairs) == load_scale.patients
    return [user for _, user in pairs]


@pytest.fixture
def taps(load_full: bool) -> int:
    """Конкурентных вызовов одной операции на пользователя."""
    return 10 if load_full else 3


def record(name: str, stats: dict, **checks: object) -> dict:
    """Итог гонки: метрики + проверки; passed — нет ошибок и все флаги True."""
    flags = [value for value in checks.values() if isinstance(value, bool)]
    result = {**stats, **checks, "passed": stats["failures"] == 0 and all(flags)}
    _RESULTS[name] = result
    return result


async def test_same_save(session_factory, patients, taps, load_log, lock_counter) -> None:
    """Одинаковые сохранения одного пользователя → ровно одна запись на каждого."""
    moment = datetime.now(timezone.utc).replace(second=0, microsecond=0) + timedelta(days=5)

    def save(user: User):
        async def _save() -> bool:
            async with session_factory() as session:
                try:
                    await measurements_service.save_measurement(session, user, 133, 83, timestamp=moment)
                    await session.commit()
                    return True
                except measurements_service.DuplicateMeasurementError:
                    await session.rollback()
                    return False

        return _save

    operations = [(user.id, save(user)) for user in patients for _ in range(taps)]
    results, stats = await run_burst("race_same_save", operations, load_log, lock_counter)
    async with session_factory() as session:
        counts = dict(
            (
                await session.execute(
                    select(Measurement.user_id, func.count())
                    .where(Measurement.timestamp == moment)
                    .group_by(Measurement.user_id)
                )
            ).all()
        )
    wrong = {user.id: counts.get(user.id, 0) for user in patients if counts.get(user.id, 0) != 1}
    record("same_save", stats, one_row_per_user=not wrong, violations=wrong,
           saved=sum(1 for r in results if r is True))
    assert stats["failures"] == 0, stats["failure_samples"]
    assert not wrong, f"не одна запись у пользователей: {wrong}"


async def test_remeasure_once(session_factory, patients, taps, load_db, load_log, lock_counter) -> None:
    """`ensure_remeasure` на одной записи → одна строка и один job планировщика."""
    targets: dict[int, int] = {}
    async with session_factory() as session:
        for user in patients:
            linked = select(Reminder.measurement_id).where(Reminder.measurement_id.is_not(None))
            measurement_id = (
                await session.execute(
                    select(Measurement.id)
                    .where(Measurement.user_id == user.id, Measurement.id.not_in(linked))
                    .order_by(Measurement.timestamp)
                    .limit(1)
                )
            ).scalar_one()
            targets[user.id] = measurement_id

    async def sender(telegram_id: int, text: str, markup) -> None:  # job'ы не наступят
        raise AssertionError("повтор замера не должен сработать во время теста")

    scheduler = build_scheduler(load_db)
    service = RemindersService(session_factory, sender, scheduler)
    scheduler.start()
    try:
        def ensure(user: User):
            async def _ensure() -> bool:
                return await service.ensure_remeasure(user.id, targets[user.id], 60)

            return _ensure

        operations = [(user.id, ensure(user)) for user in patients for _ in range(taps)]
        results, stats = await run_burst("race_remeasure", operations, load_log, lock_counter)
        job_ids = {job.id for job in scheduler.get_jobs()}
    finally:
        scheduler.shutdown(wait=False)
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(Reminder.measurement_id, Reminder.id).where(
                    Reminder.measurement_id.in_(targets.values())
                )
            )
        ).all()
    by_measurement: dict[int, list[int]] = {}
    for measurement_id, reminder_id in rows:
        by_measurement.setdefault(measurement_id, []).append(reminder_id)
    wrong_rows = {mid: len(by_measurement.get(mid, [])) for mid in targets.values()
                  if len(by_measurement.get(mid, [])) != 1}
    missing_jobs = [rid for ids in by_measurement.values() for rid in ids if f"reminder:{rid}" not in job_ids]
    created = sum(1 for result in results if result is True)
    record("remeasure_once", stats, one_row_per_measurement=not wrong_rows,
           one_job_per_row=not missing_jobs, created_true=created,
           created_once_per_user=created == len(patients),
           violations=wrong_rows, missing_jobs=missing_jobs)
    assert stats["failures"] == 0, stats["failure_samples"]
    assert not wrong_rows, f"не одна строка повтора на запись: {wrong_rows}"
    assert not missing_jobs, f"нет job'а у строк повтора: {missing_jobs}"
    assert created == len(patients), f"True вернули {created} вызовов, ожидалось {len(patients)}"


async def test_take_all_once(session_factory, patients, taps, load_log, lock_counter) -> None:
    """«Принял всё ✓» на слот конкурентно → N логов, не kN."""
    plans: dict[int, tuple[str, dict[int, str]]] = {}
    async with session_factory() as session:
        for user in patients:
            for slot in ("evening", "noon", "morning"):
                meds = await medications_service.slot_medications(session, user, slot)
                if meds:
                    plans[user.id] = (slot, {med.id: "taken" for med in meds})
                    break
    moment = datetime.now(timezone.utc)

    def take(user: User):
        slot, decisions = plans[user.id]

        async def _take() -> None:
            async with session_factory() as session:
                await medications_service.log_slot_intake(session, user, slot, decisions, timestamp=moment)
                await session.commit()

        return _take

    operations = [(user.id, take(user)) for user in patients for _ in range(taps)]
    _, stats = await run_burst("race_take_all", operations, load_log, lock_counter)
    wrong: list[dict] = []
    async with session_factory() as session:
        for user in patients:
            slot, decisions = plans[user.id]
            start, end = day_bounds_utc(user.timezone, moment.astimezone(ZoneInfo(user.timezone)).date())
            logs = (
                await session.execute(
                    select(func.count()).select_from(MedicationLog).where(
                        MedicationLog.user_id == user.id, MedicationLog.slot == slot,
                        MedicationLog.timestamp >= start, MedicationLog.timestamp < end,
                    )
                )
            ).scalar_one()
            if logs != len(decisions):
                wrong.append({"user_id": user.id, "expected": len(decisions), "got": logs})
    record("take_all_once", stats, n_logs_not_kn=not wrong, violations=wrong)
    assert stats["failures"] == 0, stats["failure_samples"]
    assert not wrong, f"не N логов за слот: {wrong}"


async def test_edits_no_mix(session_factory, patients, taps, load_log, lock_counter) -> None:
    """Правки одной записи → один из наборов целиком, timestamp не меняется."""
    targets: dict[int, tuple[int, datetime]] = {}
    async with session_factory() as session:
        for user in patients:
            latest = await measurements_service.get_latest_measurement(session, user)
            targets[user.id] = (latest.id, latest.timestamp)

    def values(op: int) -> tuple[int, int, int]:
        return 140 + op, 90 + op, 70 + op

    def edit(user: User, op: int):
        async def _edit() -> None:
            async with session_factory() as session:
                await measurements_service.update_measurement_values(
                    session, user, targets[user.id][0], *values(op)
                )
                await session.commit()

        return _edit

    operations = [(user.id, edit(user, op)) for user in patients for op in range(taps)]
    _, stats = await run_burst("race_edits", operations, load_log, lock_counter)
    allowed = {values(op) for op in range(taps)}
    mixed, moved = [], []
    async with session_factory() as session:
        for user in patients:
            measurement_id, moment = targets[user.id]
            row = await session.get(Measurement, measurement_id)
            if (row.systolic, row.diastolic, row.pulse) not in allowed:
                mixed.append(user.id)
            if row.timestamp != moment:
                moved.append(user.id)
    record("edits_no_mix", stats, no_mixed_values=not mixed, timestamps_unchanged=not moved,
           mixed=mixed, moved=moved)
    assert stats["failures"] == 0, stats["failure_samples"]
    assert not mixed, f"смесь значений: {mixed}"
    assert not moved, f"timestamp изменился: {moved}"


async def test_parallel_start(session_factory, patients, taps, load_log, lock_counter) -> None:
    """Параллельные /start нового пользователя → одна строка users."""
    new_ids = [NEW_TG_BASE + index for index in range(len(patients))]

    def start(telegram_id: int):
        async def _start() -> int:
            async with session_factory() as session:
                user = await measurements_service.get_or_create_user(session, telegram_id)
                await session.commit()
                return user.id

        return _start

    operations = [(tg, start(tg)) for tg in new_ids for _ in range(taps)]
    _, stats = await run_burst("race_start", operations, load_log, lock_counter)
    async with session_factory() as session:
        counts = dict(
            (
                await session.execute(
                    select(User.telegram_id, func.count())
                    .where(User.telegram_id.in_(new_ids))
                    .group_by(User.telegram_id)
                )
            ).all()
        )
    wrong = {tg: counts.get(tg, 0) for tg in new_ids if counts.get(tg, 0) != 1}
    record("parallel_start", stats, one_row_per_telegram_id=not wrong, violations=wrong)
    assert stats["failures"] == 0, stats["failure_samples"]
    assert not wrong, f"не одна строка users: {wrong}"
