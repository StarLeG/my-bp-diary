"""Бэкап под нагрузкой (SPEC.md, §22, уровень 6; §20).

`BackupService.create_backup()` (`VACUUM INTO` → `integrity_check` →
AES-GCM) запускается посреди burst-нагрузки: 20 пациентов × 50 сохранений
(дымовой профиль — 3 × 5); бэкап стартует, пока burst ещё идёт.

«Основной поток не заблокирован» проверяется пробой на всё окно нагрузки
(burst + бэкап): тик строго по расписанию раз в 100 мс — задержка event loop
(насколько тик опоздал против расписания) и пробная запись мимо лока дедупа
(`UPDATE users` в своей сессии — сколько запись SQLite ждёт бэкап и чужие
коммиты). Каждая запись — отдельной задачей, не дожидаясь предыдущей
(решение V6 Б): медленная проба не вытесняет быстрые из выборки, и p95
отражает долю медленных записей, а не их длительность. Очередь самих
сохранений за общим локом дедупа (§7.2) сюда не входит — её меряет
`test_load_burst.py`. Ключ шифрования — тестовый, S3 выключен.

Критерии: копия создана и расшифровывается; `PRAGMA integrity_check` копии —
`ok`; число строк ключевых таблиц в копии — между снимком на старте бэкапа и
итогом; бэкап пересёкся с burst; максимальная задержка event loop < 5 с
(«основной поток не заблокирован»); p95 пробной записи мимо лока < 1 с
(БД не встаёт колом — решение V1 Б); burst и проба без ошибок. Max пробной
записи не ограничен: под искусственным потоком ~64 записи/с запись изредка
ждёт блокировку до `busy_timeout` (неравноправность busy-обработчика SQLite,
не бэкап) и проходит повтором `run_with_retry`; max и медленные пробы > 1 с —
в отчёте. Калибровка машины (V6 А, без порога) — в отчёте и в сообщении о
падении: под троттлингом ноутбука медленных проб больше.
Итог — `qa/reports/load/test_load_backup.json`.
"""
from __future__ import annotations

import asyncio
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest_asyncio
from sqlalchemy import func, select

from bot.db import create_engine, create_session_factory, run_with_retry
from bot.models import Measurement, Medication, MedicationLog, Reminder, User
from bot.services import measurements as measurements_service
from bot.services.backup import BackupService, decrypt_file, derive_key
from qa.conftest import CPU_BENCH_REFERENCE_MS, machine_calibration
from qa.seed import load_patients

#: Тестовый ключ шифрования копий (не прод-ключ).
BACKUP_KEY = "load-test-backup-key"

#: Предел задержки event loop во время бэкапа (SPEC §22, уровень 6).
PROBE_LIMIT_S = 5.0

#: Предел p95 пробной записи мимо лока дедупа (решение V1 Б).
PROBE_P95_LIMIT_S = 1.0

#: Интервал пробных сохранений.
PROBE_EVERY_S = 0.1

#: Telegram ID пробного пользователя (вне диапазона стенда).
PROBE_TG_ID = 870_001

#: Ключевые таблицы для сверки копии со снимком.
KEY_TABLES = {
    "users": User,
    "measurements": Measurement,
    "medications": Medication,
    "medication_logs": MedicationLog,
    "reminders": Reminder,
}


@pytest_asyncio.fixture
async def session_factory(load_db: str):
    engine = create_engine(load_db)
    yield create_session_factory(engine)
    await engine.dispose()


async def table_counts(session_factory) -> dict[str, int]:
    """Число строк ключевых таблиц."""
    async with session_factory() as session:
        return {
            name: (await session.execute(select(func.count()).select_from(model))).scalar_one()
            for name, model in KEY_TABLES.items()
        }


async def test_backup_under_load(
    session_factory, load_db, load_scale, load_full, load_log, load_report, tmp_path
) -> None:
    async with session_factory() as session:
        patients = [user for _, user in await load_patients(session, load_scale)]
        probe_user = await measurements_service.get_or_create_user(session, PROBE_TG_ID)
    ops = 50 if load_full else 5
    base = datetime.now(timezone.utc).replace(second=0, microsecond=0) + timedelta(days=3)

    burst_errors: list[str] = []

    async def burst_save(user: User, index: int, op: int) -> None:
        async def _save() -> None:
            async with session_factory() as session:
                await measurements_service.save_measurement(
                    session, user, 125, 82, timestamp=base + timedelta(minutes=op * len(patients) + index)
                )
                await session.commit()

        try:
            await run_with_retry(_save)
        except Exception as exc:  # noqa: BLE001 — в отчёт
            burst_errors.append(repr(exc))

    load_done = asyncio.Event()
    loop_lags: list[float] = []
    probes: list[float] = []
    slow_probes: list[tuple[float, float]] = []  # (момент старта, длительность), > 1 с
    marks: dict[str, float] = {}
    probe_errors: list[str] = []

    async def probe_write(value: int) -> None:
        """Одна пробная запись мимо лока дедупа; длительность — в выборку."""

        async def _write() -> None:
            async with session_factory() as session:
                user = await session.get(User, probe_user.id)
                user.target_systolic = value
                await session.commit()

        started = time.perf_counter()
        try:
            await run_with_retry(_write)
        except Exception as exc:  # noqa: BLE001 — в отчёт
            probe_errors.append(repr(exc))
        probes.append(time.perf_counter() - started)
        if probes[-1] > 1.0:
            slow_probes.append((started, probes[-1]))

    async def probe() -> None:
        """Тик по расписанию раз в 100 мс до конца нагрузки: задержка loop и запись-задача."""
        loop = asyncio.get_running_loop()
        writes: list[asyncio.Task] = []
        tick = loop.time()
        target = 120
        while not load_done.is_set():
            tick += PROBE_EVERY_S
            await asyncio.sleep(max(0.0, tick - loop.time()))
            loop_lags.append(max(0.0, loop.time() - tick))
            target = 121 if target == 120 else 120
            writes.append(asyncio.create_task(probe_write(target)))
        await asyncio.gather(*writes)

    service = BackupService(load_db, tmp_path / "backups", BACKUP_KEY)
    burst = asyncio.gather(
        *(burst_save(user, index, op) for index, user in enumerate(patients) for op in range(ops))
    )
    probe_task = asyncio.create_task(probe())
    await asyncio.sleep(0.02)  # нагрузка уже идёт (и в дымовом профиле ещё не кончилась)
    snapshot = await table_counts(session_factory)
    overlapped = not burst.done()
    backup_started = time.perf_counter()
    try:
        result = await service.create_backup()
        backup_s = time.perf_counter() - backup_started
        await burst
        marks["burst_done"] = time.perf_counter()
    finally:
        load_done.set()
        await probe_task
    final = await table_counts(session_factory)
    calibration = machine_calibration()

    restored = tmp_path / "restored.sqlite3"
    decrypt_file(service.backup_dir / result.name, restored, derive_key(BACKUP_KEY))
    con = sqlite3.connect(restored)
    try:
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        copy_counts = {
            name: con.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in KEY_TABLES
        }
    finally:
        con.close()
    short = {
        name: {"copy": copy_counts[name], "snapshot": snapshot[name]}
        for name in KEY_TABLES
        if not snapshot[name] <= copy_counts[name] <= final[name]
    }
    max_probe = max(probes, default=0.0)
    max_lag = max(loop_lags, default=0.0)
    probe_p95 = sorted(probes)[max(0, round(0.95 * len(probes) + 0.5) - 1)] if probes else 0.0
    report = {
        "passed": result.integrity_ok
        and integrity == "ok"
        and not short
        and overlapped
        and max_lag < PROBE_LIMIT_S
        and probe_p95 < PROBE_P95_LIMIT_S
        and not burst_errors
        and not probe_errors,
        "overlapped_with_burst": overlapped,
        "loop_lag_max_s": round(max_lag, 3),
        "backup_name": result.name,
        "backup_size_bytes": result.size_bytes,
        "backup_s": round(backup_s, 3),
        "integrity_check": integrity,
        "snapshot_counts": snapshot,
        "copy_counts": copy_counts,
        "final_counts": final,
        "tables_short": short,
        "burst_operations": len(patients) * ops,
        "burst_errors": burst_errors[:5],
        "probes": len(probes),
        "slow_probes": [
            {
                "from_backup_start_s": round(start - backup_started, 3),
                "to_burst_end_s": round(marks.get("burst_done", start) - start, 3),
                "duration_s": round(duration, 3),
            }
            for start, duration in slow_probes
        ],
        "probe_max_s": round(max_probe, 3),
        "probe_p95_s": round(probe_p95, 3),
        "probe_p95_limit_s": PROBE_P95_LIMIT_S,
        "loop_lag_limit_s": PROBE_LIMIT_S,
        "probe_errors": probe_errors[:5],
        "calibration_ms": calibration,
        "reference_cpu_bench_ms": CPU_BENCH_REFERENCE_MS,
    }
    load_report("test_load_backup", report)
    load_log.summary(
        f"backup: {result.size_bytes} байт за {backup_s:.2f} с под нагрузкой "
        f"({len(patients) * ops} сохранений), integrity {integrity}, "
        f"задержка loop max {max_lag * 1000:.0f} мс, запись-проба max "
        f"{max_probe * 1000:.0f} мс, p95 {probe_p95 * 1000:.0f} мс из {len(probes)}; "
        f"калибровка: CPU {calibration['cpu_bench']:.0f} мс (×{calibration['slowdown_vs_reference']} к эталону)",
        **{key: value for key, value in report.items() if key not in {"burst_errors", "probe_errors"}},
    )
    assert integrity == "ok" and result.integrity_ok, f"integrity_check копии: {integrity}"
    assert not short, f"в копии меньше строк, чем в снимке: {short}"
    assert overlapped, "бэкап стартовал уже после burst — нагрузки не было"
    assert max_lag < PROBE_LIMIT_S, f"event loop стоял {max_lag:.2f} с (предел {PROBE_LIMIT_S} с)"
    assert probe_p95 < PROBE_P95_LIMIT_S, (
        f"p95 пробной записи мимо лока {probe_p95:.2f} с (предел {PROBE_P95_LIMIT_S} с), медленных > 1 с — "
        f"{len(slow_probes)} из {len(probes)}. Калибровка машины: CPU-бенчмарк {calibration['cpu_bench']:.0f} мс — "
        f"×{calibration['slowdown_vs_reference']} к эталону {CPU_BENCH_REFERENCE_MS:.0f} мс. Множитель ≈ 2–3 — "
        "машина в троттлинге, повторите на машине без него (qa/README.md, «Подготовка»); ≈ 1 — БД деградирует под бэкапом"
    )
    assert not burst_errors, f"ошибки burst: {burst_errors[:3]}"
    assert not probe_errors, f"ошибки пробы: {probe_errors[:3]}"
    assert probes, "пробные сохранения не выполнялись"
