"""Приёмка: деградация (SPEC.md, §22; `doc/LOAD-CRITERIA-QUESTIONS.md`, В4).

«Тысячная операция заметно медленнее первой» — отказ. Ревью: час ждать не
нужно — достаточно тысячи операций подряд; деградация ловит и не-меморные
вещи (перебор списка, переоткрытие файла). Проверяются три обычные операции,
по каждой отдельно: «сохранение измерения» со сдвигом времени на минуту
(дедуп §7.2 — иначе 1000 сохранений дадут 1 запись), «дашборд» ×1000 и
«CSV за 30 дней» × 250. Прогрев — одна операция, она не в счёт; сравниваются
медианы первых 10 против последних 10, порог ×1,5.

Плюс сценарий нововедений 0.6.0 (по просьбе от 2026-11-15): «Сценарий
нововедений» × 1000 — быстрый ввод `parse_pressure_line` → сохранение →
«➕ Фактор» (`create_factor_events`) → событие дневника (`create_event`,
типы по кругу + `upsert_sleep`) → «Лента дня» (`day_feed`); и «Лента дня»
× 1000 — чтение заполненных суток отдельно. У сценария моменты сдвигаются на
минуту от третьих суток EPOCH (дедуп §7.2 не мешает), лента читает сутки
первого дня EPOCH — они заполнены циклом сохранений и не растут во время
замера (иначе замерилась бы деградация данных, а не кода). Итог —
`qa/reports/load/acceptance_degradation.json`.
"""
from __future__ import annotations

import asyncio
import os
import statistics
import time
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from bot import schemas
from bot.services import events as events_service
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

#: Сценарий нововедений 0.6.0 (события, факторы, лента дня).
NOVELTY_CALLS = 1000
FEED_CALLS = 1000

#: Моменты сценария — с третьих суток EPOCH: вне окна сохранений (дедуп §7.2).
NOVELTY_EPOCH = datetime(2000, 1, 3, tzinfo=timezone.utc)

#: Лента читает первые сутки EPOCH: заполнены циклом сохранений, не растут.
FEED_DAY = date(2000, 1, 1)

#: Типы событий сценария по кругу (§4.18): четыре из пяти + сон через upsert.
NOVELTY_KINDS = ("activity", "food", "stress", "note")

#: Канонические payload'ы типов — как собирают хендлеры (§4.18).
NOVELTY_PAYLOADS = {
    "activity": events_service.event_payload("activity", duration_min=20),
    "food": events_service.event_payload("food", marks={"salt"}),
    "stress": events_service.event_payload("stress", variant="stress"),
    "note": events_service.event_payload("note"),
}


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


async def _novelty_cycles(db) -> list[float]:
    """1000 сценариев нововедений (0.6.0): быстрый ввод → запись → факторы → событие → лента.

    Один сценарий повторяет путь пользователя экрана «➕ Добавить запись»:
    `parse_pressure_line` (B5) → `save_measurement` → `create_factor_events`
    (B6) → `create_event` по кругу типов, каждый десятый — `upsert_sleep`
    (C23). Моменты — с шагом минуты от `NOVELTY_EPOCH` (дедуп §7.2 не мешает).
    """
    from bot.db import create_engine, create_session_factory
    from bot.models import User
    from bot.services.measurements import save_measurement

    user_id, _ = first_patient(db)
    engine = create_engine(str(db))
    durations: list[float] = []
    try:
        async with create_session_factory(engine)() as session:
            user = await session.get(User, user_id)
        moment = NOVELTY_EPOCH
        for index in range(NOVELTY_CALLS):
            async with create_session_factory(engine)() as session:
                started = time.perf_counter()
                systolic, diastolic, pulse = schemas.parse_pressure_line("128/82 70")
                measurement = await save_measurement(
                    session, user, systolic, diastolic, pulse, timestamp=moment
                )
                await events_service.create_factor_events(
                    session, user, measurement.id, {"salt", "stress"}, moment
                )
                if index % 10 == 9:
                    await events_service.upsert_sleep(
                        session, user, moment.date(), "little", moment
                    )
                else:
                    kind = NOVELTY_KINDS[index % len(NOVELTY_KINDS)]
                    await events_service.create_event(
                        session, user, kind, moment, payload=NOVELTY_PAYLOADS[kind]
                    )
                await session.commit()
                durations.append((time.perf_counter() - started) * 1000)
            moment += timedelta(minutes=1)
    finally:
        await engine.dispose()
    return durations


async def _feed_cycles(db) -> list[float]:
    """1000 чтений «Ленты дня» (0.6.0, §6.4) по заполненным суткам EPOCH."""
    from bot.db import create_engine, create_session_factory
    from bot.models import User

    user_id, _ = first_patient(db)
    engine = create_engine(str(db))
    durations: list[float] = []
    try:
        async with create_session_factory(engine)() as session:
            user = await session.get(User, user_id)
        for _ in range(FEED_CALLS):
            async with create_session_factory(engine)() as session:
                started = time.perf_counter()
                await events_service.day_feed(session, user, FEED_DAY)
                durations.append((time.perf_counter() - started) * 1000)
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
        _check("novelty_scenario", asyncio.run(_novelty_cycles(db))),
        _check("day_feed", asyncio.run(_feed_cycles(db))),
    ]
    write_artifact("degradation", {"operations": results})
    for result in results:
        assert result["ok"], (
            f"деградация «{result['operation']}»: медиана последних 10 "
            f"{result['last_window_median_ms']:.1f} мс > 1,5 × медианы первых 10 "
            f"{result['first_window_median_ms']:.1f} мс (отношение {result['ratio']:.2f})"
        )
