"""Приёмка: возврат памяти (SPEC.md, §22; `doc/LOAD-CRITERIA-QUESTIONS.md`, В6).

«Замер, несколько тысяч циклов операции, замер»; «память не вернулась к
исходному уровню» — отказ. Прогрев — 100 циклов (pymalloc, кэш SQLite,
aiosqlite-пул заполняются на первых циклах), затем замер RSS после
`gc.collect()`, 3000 циклов «сохранение с шагом минута → чтение истории»,
пауза 60 с, снова `gc.collect()` и замер. Допуск — ±10% или ±20 МБ (по
большему). Второй сигнал — `tracemalloc` (снимки до/после): прирост
Python-heap > 5 МБ — FAIL (аллокатор может держать арены, RSS это не видит).
Итог — `qa/reports/load/acceptance_memory.json`.
"""
from __future__ import annotations

import asyncio
import gc
import os
import time
import tracemalloc
from datetime import datetime, timedelta, timezone

import pytest

from qa.acceptance_common import (
    ACCEPTANCE_ENV,
    copy_stand,
    first_patient,
    rss_mb,
    write_artifact,
)

pytestmark = pytest.mark.skipif(
    os.environ.get(ACCEPTANCE_ENV, "").strip() != "1",
    reason="приёмка перед релизом — только под ACCEPTANCE=1 (make acceptance)",
)

#: Циклы операции (В6: «несколько тысяч») и прогрев до «замера до».
CYCLES = 3000
WARMUP_CYCLES = 100

#: Покой перед вторым замером (секунды).
REST_SECONDS = int(os.environ.get("ACCEPTANCE_REST_SEC", "60"))

#: Допуск возврата RSS: ±10% или ±20 МБ — по большему (В6).
RSS_TOLERANCE_RATIO = 0.10
RSS_TOLERANCE_MB = 20.0

#: Прирост Python-heap между снимками — FAIL выше 5 МБ (В6).
HEAP_GROWTH_LIMIT_MB = 5.0

#: Окно операций — далёкое прошлое (2000-й год): минуты гарантированно свободны
#: от записей любого сида (дедуп по минуте, §7.2, не задевает замеры).
EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)


async def _run(db) -> dict:
    from bot.db import create_engine, create_session_factory
    from bot.models import User
    from bot.services.measurements import measurements_for_period, save_measurement

    user_id, _ = first_patient(db)
    engine = create_engine(str(db))
    moment = EPOCH

    async def one_cycle(user) -> None:
        nonlocal moment
        async with create_session_factory(engine)() as session:
            await save_measurement(session, user, 128, 82, timestamp=moment)
            await session.commit()
        end = moment + timedelta(days=1)
        async with create_session_factory(engine)() as session:
            await measurements_for_period(session, user, moment.date() - timedelta(days=2), end.date())
        moment += timedelta(minutes=1)

    try:
        async with create_session_factory(engine)() as session:
            user = await session.get(User, user_id)
        for _ in range(WARMUP_CYCLES):
            await one_cycle(user)  # прогрев: pymalloc / кэш SQLite / пул соединений

        gc.collect()
        baseline = rss_mb()
        tracemalloc.start()
        heap_before = tracemalloc.get_traced_memory()[0]

        for _ in range(CYCLES):
            await one_cycle(user)

        heap_after_cycles = tracemalloc.get_traced_memory()[0]
        await asyncio.sleep(REST_SECONDS)
        gc.collect()
        final = rss_mb()
        heap_after = tracemalloc.get_traced_memory()[0]
        tracemalloc.stop()
    finally:
        await engine.dispose()

    delta = final - baseline
    tolerance = max(RSS_TOLERANCE_RATIO * baseline, RSS_TOLERANCE_MB)
    heap_growth_mb = max(heap_after, heap_after_cycles) / (1024 * 1024) - heap_before / (1024 * 1024)
    return {
        "cycles": CYCLES,
        "warmup_cycles": WARMUP_CYCLES,
        "rest_seconds": REST_SECONDS,
        "baseline_rss_mb": round(baseline, 2),
        "final_rss_mb": round(final, 2),
        "delta_rss_mb": round(delta, 2),
        "rss_tolerance_mb": round(tolerance, 2),
        "rss_ok": abs(delta) <= tolerance,
        "heap_growth_mb": round(heap_growth_mb, 2),
        "heap_limit_mb": HEAP_GROWTH_LIMIT_MB,
        "heap_ok": heap_growth_mb <= HEAP_GROWTH_LIMIT_MB,
    }


def test_memory_returns(tmp_path) -> None:
    db = copy_stand(tmp_path / "acceptance-test-stand-memory.sqlite3")
    result = asyncio.run(_run(db))
    write_artifact("memory", result)
    assert result["rss_ok"], (
        f"память не вернулась к исходному уровню: RSS {result['baseline_rss_mb']:.1f} → "
        f"{result['final_rss_mb']:.1f} МБ (Δ {result['delta_rss_mb']:+.1f} МБ, "
        f"допуск ±{result['rss_tolerance_mb']:.1f} МБ)"
    )
    assert result["heap_ok"], (
        f"утечка Python-heap: прирост {result['heap_growth_mb']:.1f} МБ "
        f"> {HEAP_GROWTH_LIMIT_MB} МБ за {CYCLES} циклов (tracemalloc)"
    )
