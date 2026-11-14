"""Приёмка: длительная работа (SPEC.md, §22; `doc/LOAD-CRITERIA-QUESTIONS.md`, В5).

Прогон под нагрузкой не меньше часа, память пишется в журнал раз в минуту;
«основание кривой памяти растёт» — отказ. На копии стенда работают оба
процесса («бот» и «кабинет» — `qa/acceptance_common runtime`), нагрузка —
обычные запросы кабинета каждые 2 с; RSS **каждого** процесса пишется раз в
минуту в `qa/reports/load/acceptance_memory.jsonl` (память генератора
нагрузки не меряется — В5). Первые 10 минут — прогрев, из регрессии
исключаются. Порог: наклон линейной регрессии RSS каждого процесса ≤ 0,5
МБ/мин (за час это 30 МБ — заметный течь). `tracemalloc` в процессе бота
(снимки Python-heap раз в 5 минут) — **только warning**: наклон > 1 МБ/мин
пишется в артефакт, но не валит. Итог —
`qa/reports/load/acceptance_soak.json`.

Длительность — `ACCEPTANCE_SOAK_MIN` (по умолчанию 60); «Остановка» (В8)
использует тот же runtime, здесь процессы завершаются штатно после прогона.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import httpx
import pytest

from qa.acceptance_common import (
    ACCEPTANCE_ENV,
    REPORTS_DIR,
    copy_stand,
    free_port,
    rss_mb,
    slope_per_unit,
    spawn_runtime,
    write_artifact,
)

pytestmark = pytest.mark.skipif(
    os.environ.get(ACCEPTANCE_ENV, "").strip() != "1",
    reason="приёмка перед релизом — только под ACCEPTANCE=1 (make acceptance)",
)

#: Длительность прогона (минуты) и прогрев, не входящий в регрессию (В5).
#: Укороченные значения — только для самопроверки модуля, приёмка — 60/10.
SOAK_MINUTES = int(os.environ.get("ACCEPTANCE_SOAK_MIN", "60"))
WARMUP_MINUTES = int(os.environ.get("ACCEPTANCE_WARMUP_MIN", "10"))

#: Порог наклона RSS (В5): 0,5 МБ/мин — за час это 30 МБ роста.
SLOPE_LIMIT_MB_MIN = 0.5

#: Warning-порог Python-heap в процессе бота (В5): только в артефакт.
HEAP_WARNING_MB_MIN = 1.0


async def _run(db: Path, memory_log: Path) -> dict:
    port = free_port()
    status_bot = db.parent / "status-bot.json"
    status_cabinet = db.parent / "status-cabinet.json"
    heap_log = db.parent / "heap.jsonl"
    bot = await spawn_runtime(db, status_bot, serve=None, heap_log=heap_log)
    cabinet = await spawn_runtime(db, status_cabinet, serve=port)
    await asyncio.sleep(3)  # старт планировщика и uvicorn

    samples: list[tuple[float, float, float]] = []  # (минута, RSS бота, RSS кабинета)
    started = time.monotonic()
    next_sample = started
    deadline = started + SOAK_MINUTES * 60
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            while time.monotonic() < deadline:
                try:
                    await client.get("/?days=30", timeout=10)
                except Exception:
                    pass  # кабинет мог перезапускаться; RSS интереснее непрерывности запросов
                now = time.monotonic()
                if now >= next_sample:
                    samples.append(
                        (
                            (now - started) / 60,
                            rss_mb(bot.pid),
                            rss_mb(cabinet.pid),
                        )
                    )
                    next_sample = now + 60
                await asyncio.sleep(2)
    finally:
        for process in (bot, cabinet):
            try:
                process.stdin.write(b"stop\n")
                await process.stdin.drain()
                await asyncio.wait_for(process.wait(), timeout=60)
            except Exception:
                process.kill()

    memory_log.parent.mkdir(parents=True, exist_ok=True)
    with memory_log.open("w", encoding="utf-8") as out:
        for minute, bot_mb, cabinet_mb in samples:
            out.write(json.dumps({"minute": round(minute, 2), "bot_mb": bot_mb, "cabinet_mb": cabinet_mb}) + "\n")

    steady = [(m, b, c) for m, b, c in samples if m >= WARMUP_MINUTES]
    # Для приёмки (60 мин) — не меньше 10 точек; укороченный смоук мерит
    # столько, сколько осталось после прогрева (минимум 2).
    min_samples = min(10, max(2, SOAK_MINUTES - WARMUP_MINUTES))
    if len(steady) < min_samples:
        raise AssertionError(f"слишком короткий прогон: после {WARMUP_MINUTES} мин прогрева осталось {len(steady)} замеров")
    bot_slope = slope_per_unit([(m, b) for m, b, _ in steady])
    cabinet_slope = slope_per_unit([(m, c) for m, _, c in steady])
    heap_slope = _heap_slope(heap_log)
    return {
        "minutes": SOAK_MINUTES,
        "warmup_minutes": WARMUP_MINUTES,
        "samples": len(samples),
        "bot_slope_mb_min": round(bot_slope, 4),
        "cabinet_slope_mb_min": round(cabinet_slope, 4),
        "slope_limit_mb_min": SLOPE_LIMIT_MB_MIN,
        "bot_ok": bot_slope <= SLOPE_LIMIT_MB_MIN,
        "cabinet_ok": cabinet_slope <= SLOPE_LIMIT_MB_MIN,
        "heap_slope_mb_min": round(heap_slope, 4),
        "heap_warning": heap_slope > HEAP_WARNING_MB_MIN,
    }


def _heap_slope(heap_log: Path) -> float:
    """Наклон Python-heap процесса бота (warning-only, В5)."""
    if not heap_log.exists():
        return 0.0
    points = []
    for line in heap_log.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        points.append((row["minute"] / 60, row["heap_mb"]))
    return slope_per_unit(points)


def test_soak_memory_stable(tmp_path) -> None:
    db = copy_stand(tmp_path / "acceptance-test-stand-soak.sqlite3")
    memory_log = REPORTS_DIR / "acceptance_memory.jsonl"
    result = asyncio.run(_run(db, memory_log))
    write_artifact("soak", result)
    assert result["bot_ok"], (
        f"основание кривой памяти растёт: RSS бота +{result['bot_slope_mb_min']:.2f} МБ/мин "
        f"> {SLOPE_LIMIT_MB_MIN} МБ/мин (за {SOAK_MINUTES} мин)"
    )
    assert result["cabinet_ok"], (
        f"основание кривой памяти растёт: RSS кабинета +{result['cabinet_slope_mb_min']:.2f} МБ/мин "
        f"> {SLOPE_LIMIT_MB_MIN} МБ/мин (за {SOAK_MINUTES} мин)"
    )
