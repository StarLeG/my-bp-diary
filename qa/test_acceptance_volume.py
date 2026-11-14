"""Приёмка: полный объём (SPEC.md, §22; `doc/LOAD-CRITERIA-QUESTIONS.md`, В1–В3).

Стенд профиля «три года» (~50 000 записей, `seed_all.py --scale three-years`)
получает обычные операции кабинета: дашборд, история за 30 дней, CSV. Время
отклика не должно выйти за «число из Т3» (пороги `doc/TEST TZ.md`): p95
дашборда < 2000 мс, p95 CSV < 3000 мс. Плюс **окно конкуренции** (уточнение
2026-11-14, связка с нагрузочным контуром §22): 100 одновременных обычных
операций на полной базе — те же пороги и 0 ошибок.

Замеры идут против **реального** сервера (`uvicorn --workers 2` —
прод-конфигурация из `docker-compose.yml`): рендер кабинета CPU-bound, и
окно конкуренции должно измерять параллелизм сервера, а не одного процесса
ASGI-транспорта. Итог — `qa/reports/load/acceptance_volume.json`. Запуск:
`make acceptance` или `ACCEPTANCE=1 uv run pytest qa/test_acceptance_volume.py -v`.
"""
from __future__ import annotations

import asyncio
import os
import time

import httpx
import pytest

from qa.acceptance_common import (
    ACCEPTANCE_ENV,
    copy_stand,
    first_patient,
    free_port,
    p95,
    require_volume,
    session_cookie,
    spawn_uvicorn,
    write_artifact,
)

pytestmark = pytest.mark.skipif(
    os.environ.get(ACCEPTANCE_ENV, "").strip() != "1",
    reason="приёмка перед релизом — только под ACCEPTANCE=1 (make acceptance)",
)

#: «Число из Т3» (`doc/TEST TZ.md`): p95 дашборда < 2000 мс, CSV < 3000 мс.
DASHBOARD_P95_LIMIT_MS = 2000
CSV_P95_LIMIT_MS = 3000

#: Обычные операции (В3): дашборд и история — по 60, CSV — по 20.
DASHBOARD_CALLS = 60
CSV_CALLS = 20

#: Окно конкуренции (уточнение 2026-11-14, связка с нагрузочным контуром §22):
#: 100 одновременных обычных операций на полной базе; пороги — «число из Т3».
CONCURRENT_OPS = 100
CONCURRENT_PLAN = (
    ("dashboard", "/", 30),
    ("dashboard", "/?days=30", 50),
    ("csv", "/export/measurements.csv?days=30", 20),
)

#: Воркеры кабинета — как в `docker-compose.yml` (прод-конфигурация).
UVICORN_WORKERS = 2


async def _wait_ready(port: int, timeout: float = 60.0) -> None:
    """Дождаться, пока реальный сервер кабинета начнёт отвечать."""
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
        while time.monotonic() < deadline:
            try:
                response = await client.get("/login", timeout=5)
                if response.status_code == 200:
                    return
            except Exception:
                pass
            await asyncio.sleep(0.5)
    raise AssertionError(f"кабинет на 127.0.0.1:{port} не поднялся за {timeout:.0f} с")


async def _run(db, tmp_path) -> dict:
    port = free_port()
    server = await spawn_uvicorn(db, port, workers=UVICORN_WORKERS)
    await _wait_ready(port)
    cookie = await session_cookie(db)
    timings: dict[str, list[float]] = {"dashboard": [], "csv": []}
    errors: list[str] = []
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            cookies={"cabinet_session": cookie},
            timeout=60,
        ) as client:
            plan = (
                [("dashboard", "/"), ("dashboard", "/?days=30")] * (DASHBOARD_CALLS // 2)
                + [
                    ("csv", "/export/measurements.csv?days=30"),
                    ("csv", "/export/medications.csv?days=30"),
                ]
                * CSV_CALLS
            )
            for kind, url in plan:
                started = time.perf_counter()
                response = await client.get(url)
                timings[kind].append((time.perf_counter() - started) * 1000)
                if response.status_code >= 400:
                    errors.append(f"{url} → {response.status_code}")

            # Окно конкуренции: все операции стартуют одновременно (`asyncio.gather`).
            concurrent_plan = [
                (kind, url)
                for kind, url, count in CONCURRENT_PLAN
                for _ in range(count)
            ]
            assert len(concurrent_plan) == CONCURRENT_OPS

            async def timed(kind: str, url: str):
                started = time.perf_counter()
                response = await client.get(url)
                return kind, (time.perf_counter() - started) * 1000, response.status_code

            window = await asyncio.gather(*(timed(kind, url) for kind, url in concurrent_plan))
    finally:
        server.terminate()
        await server.wait()

    window_timings: dict[str, list[float]] = {"dashboard": [], "csv": []}
    window_errors = [f"{kind} → {code}" for kind, _, code in window if code >= 400]
    for kind, ms, _code in window:
        window_timings[kind].append(ms)
    window_p95 = {kind: p95(values) for kind, values in window_timings.items() if values}
    dashboard_p95 = p95(timings["dashboard"])
    csv_p95 = p95(timings["csv"])
    return {
        "measurements": require_volume(db),
        "server": f"uvicorn --workers {UVICORN_WORKERS}",
        "calls": {"dashboard": len(timings["dashboard"]), "csv": len(timings["csv"])},
        "p95_ms": {
            "dashboard": round(dashboard_p95, 2),
            "csv": round(csv_p95, 2),
        },
        "limits_ms": {"dashboard": DASHBOARD_P95_LIMIT_MS, "csv": CSV_P95_LIMIT_MS},
        "errors": errors,
        "dashboard_p95_ok": dashboard_p95 < DASHBOARD_P95_LIMIT_MS,
        "csv_p95_ok": csv_p95 < CSV_P95_LIMIT_MS,
        "concurrency": {
            "ops": CONCURRENT_OPS,
            "p95_ms": {kind: round(value, 2) for kind, value in window_p95.items()},
            "errors": window_errors,
            "dashboard_p95_ok": window_p95.get("dashboard", 0.0) < DASHBOARD_P95_LIMIT_MS,
            "csv_p95_ok": window_p95.get("csv", 0.0) < CSV_P95_LIMIT_MS,
        },
    }


def test_full_volume(tmp_path) -> None:
    db = copy_stand(tmp_path / "acceptance-test-stand-volume.sqlite3")
    first_patient(db)  # пациенты сида на месте
    result = asyncio.run(_run(db, tmp_path))
    write_artifact("volume", result)
    assert not result["errors"], f"ошибки ответов: {result['errors'][:5]}"
    assert result["dashboard_p95_ok"], (
        f"время отклика вышло за число из Т3: p95 дашборда {result['p95_ms']['dashboard']:.0f} мс"
        f" ≥ {DASHBOARD_P95_LIMIT_MS} мс ({result['measurements']} измерений в базе)"
    )
    assert result["csv_p95_ok"], (
        f"время отклика вышло за число из Т3: p95 CSV {result['p95_ms']['csv']:.0f} мс"
        f" ≥ {CSV_P95_LIMIT_MS} мс ({result['measurements']} измерений в базе)"
    )
    window = result["concurrency"]
    assert not window["errors"], f"окно конкуренции ({window['ops']} операций): ошибки {window['errors'][:5]}"
    assert window["dashboard_p95_ok"], (
        f"окно конкуренции ({window['ops']} одновременных операций, {result['server']}): p95 дашборда "
        f"{window['p95_ms'].get('dashboard', 0):.0f} мс ≥ {DASHBOARD_P95_LIMIT_MS} мс"
    )
    assert window["csv_p95_ok"], (
        f"окно конкуренции ({window['ops']} одновременных операций, {result['server']}): p95 CSV "
        f"{window['p95_ms'].get('csv', 0):.0f} мс ≥ {CSV_P95_LIMIT_MS} мс"
    )
