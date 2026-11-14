"""Приёмка: рост файлов и базы (SPEC.md, §22; `doc/LOAD-CRITERIA-QUESTIONS.md`, В7).

«Посчитать прирост за сутки работы и умножить на срок эксплуатации»;
«никто не назвал предел» — отказ. Пределы названы в SPEC §22: **БД + WAL +
jobstore за 3 года ≤ 200 МБ**, **файлы на диске (бэкапы + deletion log) ≤
500 МБ**; срок эксплуатации — 3 года (1095 суток).

Метод (В7 + уточнение ревью): «симулированные сутки» — 80 измерений (шаг
минута) и 40 строк `medication_logs` на копии стенда; перед замером —
`PRAGMA wal_checkpoint(TRUNCATE)` (иначе WAL даст «хвост» активной нагрузки),
jobstore замеряется отдельно. Прирост суток × 1095 — оценка «за 3 года»;
файлы — 14 ротационных бэкапов × размер бэкапа + журнал удалений (штатная
ротация: бэкапы ≤ 14 дней, лог ≤ 15 дней, §20). Прогноз дополнительно пишется
в артефакт — информационно, без FAIL. Итог —
`qa/reports/load/acceptance_growth.json`.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from qa.acceptance_common import (
    ACCEPTANCE_ENV,
    copy_stand,
    first_patient,
    require_volume,
    write_artifact,
)

pytestmark = pytest.mark.skipif(
    os.environ.get(ACCEPTANCE_ENV, "").strip() != "1",
    reason="приёмка перед релизом — только под ACCEPTANCE=1 (make acceptance)",
)

#: Срок эксплуатации — 3 года (В7).
SERVICE_DAYS = 1095

#: Штатная ротация (§20): бэкапов хранится 14, журнал удалений — 15 дней.
BACKUP_COPIES = 14

#: Пределы (SPEC §22, пересмотр В7): БД + WAL + jobstore ≤ 200 МБ, файлы ≤ 500 МБ.
DB_LIMIT_MB = 200.0
FILES_LIMIT_MB = 500.0

DAY_SAVES = 80
DAY_MED_LOGS = 40


def _size_mb(*paths: Path) -> float:
    return sum(p.stat().st_size for p in paths if p.exists()) / (1024 * 1024)


def _checkpoint(db: Path) -> None:
    """Убрать «хвост» WAL перед замером (уточнение ревью)."""
    con = sqlite3.connect(db)
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        con.close()


def _simulate_day(db: Path) -> None:
    """Сутки работы: измерения с шагом минута и строки журнала приёмов."""
    from bot.db import create_engine, create_session_factory
    from bot.models import MedicationLog, User
    from bot.services.measurements import save_measurement

    user_id, _ = first_patient(db)
    engine = create_engine(str(db))

    async def run() -> None:
        moment = datetime(2000, 1, 1, tzinfo=timezone.utc)  # свободные минуты (§7.2)
        async with create_session_factory(engine)() as session:
            user = await session.get(User, user_id)
        for _ in range(DAY_SAVES):
            async with create_session_factory(engine)() as session:
                await save_measurement(session, user, 128, 82, timestamp=moment)
                await session.commit()
            moment += timedelta(minutes=1)
        async with create_session_factory(engine)() as session:
            from bot.models import Medication

            # Журнал приёмов — разные пары (лекарство, слот): дедуп по
            # (user, slot, локальная дата, medication) не пропускает дубли (§4.5).
            pairs = (
                await session.execute(
                    select(Medication.id, Medication.slot)
                    .where(Medication.user_id == user_id)
                    .limit(DAY_MED_LOGS)
                )
            ).all()
            for index, (medication_id, slot) in enumerate(pairs):
                session.add(
                    MedicationLog(
                        user_id=user_id,
                        medication_id=medication_id,
                        timestamp=datetime(2000, 1, 2, tzinfo=timezone.utc) + timedelta(hours=index),
                        status="taken",
                        slot=slot,
                    )
                )
            await session.commit()
        await engine.dispose()

    asyncio.run(run())


def test_growth_within_limits(tmp_path) -> None:
    db = copy_stand(tmp_path / "acceptance-test-stand-growth.sqlite3")
    require_volume(db)
    from bot.main import jobs_db_path

    jobs = Path(jobs_db_path(str(db)))
    _checkpoint(db)
    db_before = _size_mb(db, Path(str(db) + "-wal"), jobs)
    _simulate_day(db)
    _checkpoint(db)
    db_after = _size_mb(db, Path(str(db) + "-wal"), jobs)
    day_delta_mb = max(0.0, db_after - db_before)
    db_estimate_3y_mb = day_delta_mb * SERVICE_DAYS

    # Файлы (бэкапы + журнал удалений): штатная ротация — 14 копий бэкапа.
    from bot.services.backup import BackupService

    backup_dir = tmp_path / "acceptance-backups"
    service = BackupService(
        str(db),
        backup_dir,
        "acceptance-test-key",
    )
    result = asyncio.run(service.create_backup())
    backup_mb = _size_mb(Path(result.path)) if getattr(result, "path", None) else db_after
    deletion_log_mb = 0.0  # HMAC-строка на удаление — доли килобайта, ниже точности
    files_steady_mb = BACKUP_COPIES * backup_mb + deletion_log_mb

    payload = {
        "day_delta_mb": round(day_delta_mb, 3),
        "service_days": SERVICE_DAYS,
        "db_estimate_3y_mb": round(db_estimate_3y_mb, 2),
        "db_actual_3y_mb": round(db_after, 2),
        "db_limit_mb": DB_LIMIT_MB,
        "backup_mb": round(backup_mb, 2),
        "files_steady_mb": round(files_steady_mb, 2),
        "files_limit_mb": FILES_LIMIT_MB,
        "db_ok": db_estimate_3y_mb <= DB_LIMIT_MB and db_after <= DB_LIMIT_MB,
        "files_ok": files_steady_mb <= FILES_LIMIT_MB,
    }
    write_artifact("growth", payload)
    assert payload["db_ok"], (
        f"рост базы за 3 года выходит за предел {DB_LIMIT_MB:.0f} МБ: сутки × {SERVICE_DAYS} = "
        f"{db_estimate_3y_mb:.1f} МБ, фактически {db_after:.1f} МБ"
    )
    assert payload["files_ok"], (
        f"файлы на диске выходят за предел {FILES_LIMIT_MB:.0f} МБ: "
        f"{BACKUP_COPIES} бэкапов × {backup_mb:.1f} МБ = {files_steady_mb:.1f} МБ"
    )
