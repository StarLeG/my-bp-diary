"""Приёмка: деградация (SPEC.md, §22; `doc/LOAD-CRITERIA-QUESTIONS.md`, В4).

«Тысячная операция заметно медленнее первой» — отказ. Ревью: час ждать не
нужно — достаточно тысячи операций подряд; деградация ловит и не-меморные
вещи (перебор списка, переоткрытие файла). Проверяются три обычные операции,
по каждой отдельно: «сохранение измерения» со сдвигом времени на минуту
(дедуп §7.2 — иначе 1000 сохранений дадут 1 запись), «дашборд» ×1000 и
«CSV за 30 дней» × 250. Прогрев — одна операция, она не в счёт; сравниваются
медианы первых 10 против последних 10, порог ×1,5. Итог —
`qa/reports/load/acceptance_degradation.json`.
"""
from __future__ import annotations

import asyncio
import os
import statistics
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from qa.acceptance_common import (
    ACCEPTANCE_ENV,
    copy_stand,
    first_patient,
    make_app,
    session_cookie,
    write_artifact,
)

pytestmark = pytest.mark.skipif(
    os.environ.get(ACCEPTANCE_ENV, "").strip() != "1",
    reason="приёмка перед релизом — только под ACCEPTANCE=1 (make acceptance)",
)

#: «Заметно медленнее» — медиана последних 10 больше 1,5 × медианы первых 10 (В4).
DEGRADATION_LIMIT = 1.5
WINDOW = 10

SAVE_CALLS = 1000

#: Окно сохранений — далёкое прошлое: минуты свободны от записей сида (§7.2).
EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)
DASHBOARD_CALLS = 1000
CSV_CALLS = 250


def _check(name: str, durations_ms: list[float]) -> dict:
    """Прогрев (1-я операция) не в счёт; окна — первые и последние 10 (В4)."""
    measured = durations_ms[1:]
    first = statistics.median(measured[:WINDOW])
    last = statistics.median(measured[-WINDOW:])
    ratio = last / first if first else 0.0
    return {
        "operation": name,
        "calls": len(durations_ms),
        "first_window_median_ms": round(first, 3),
        "last_window_median_ms": round(last, 3),
        "ratio": round(ratio, 3),
        "limit": DEGRADATION_LIMIT,
        "ok": ratio <= DEGRADATION_LIMIT,
    }


async def _save_cycles(db) -> list[float]:
    """1000 сохранений с шагом времени в минуту (дедуп §7.2 не мешает)."""
    from bot.db import create_engine, create_session_factory
    from bot.models import User
    from bot.services.measurements import save_measurement

    user_id, _ = first_patient(db)
    engine = create_engine(str(db))
    durations: list[float] = []
    try:
        async with create_session_factory(engine)() as session:
            user = await session.get(User, user_id)
        moment = EPOCH
        for _ in range(SAVE_CALLS):
            async with create_session_factory(engine)() as session:
                started = time.perf_counter()
                await save_measurement(session, user, 128, 82, timestamp=moment)
                await session.commit()
                durations.append((time.perf_counter() - started) * 1000)
            moment += timedelta(minutes=1)
    finally:
        await engine.dispose()
    return durations


async def _http_cycles(db, tmp_path, url: str, calls: int) -> list[float]:
    app = make_app(db, tmp_path / "backups")
    cookie = await session_cookie(db)
    durations: list[float] = []
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", cookies={"cabinet_session": cookie}
    ) as client:
        for _ in range(calls):
            started = time.perf_counter()
            response = await client.get(url)
            durations.append((time.perf_counter() - started) * 1000)
            assert response.status_code < 400, f"{url} → {response.status_code}"
    return durations


def test_degradation(tmp_path) -> None:
    db = copy_stand(tmp_path / "acceptance-test-stand-degradation.sqlite3")
    results = [
        _check("save_measurement", asyncio.run(_save_cycles(db))),
        _check("dashboard", asyncio.run(_http_cycles(db, tmp_path, "/?days=30", DASHBOARD_CALLS))),
        _check("csv", asyncio.run(_http_cycles(db, tmp_path, "/export/measurements.csv?days=30", CSV_CALLS))),
    ]
    write_artifact("degradation", {"operations": results})
    for result in results:
        assert result["ok"], (
            f"деградация «{result['operation']}»: медиана последних 10 "
            f"{result['last_window_median_ms']:.1f} мс > 1,5 × медианы первых 10 "
            f"{result['first_window_median_ms']:.1f} мс (отношение {result['ratio']:.2f})"
        )
