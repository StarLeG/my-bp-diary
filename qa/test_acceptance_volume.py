"""Приёмка: полный объём (SPEC.md, §22; `doc/LOAD-CRITERIA-QUESTIONS.md`, В1–В3).

Стенд профиля «три года» (~50 000 записей, `seed_all.py --scale three-years`)
получает обычные операции кабинета: дашборд, история за 30 дней, CSV. Время
отклика не должно выйти за «число из Т3» (пороги `doc/TEST TZ.md`): p95
дашборда < 2000 мс, p95 CSV < 3000 мс. Итог —
`qa/reports/load/acceptance_volume.json`. Запуск: `make acceptance` или
`ACCEPTANCE=1 uv run pytest qa/test_acceptance_volume.py -v`.
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
    make_app,
    p95,
    require_volume,
    session_cookie,
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


async def _run(db, tmp_path) -> dict:
    app = make_app(db, tmp_path / "backups")
    cookie = await session_cookie(db)
    transport = httpx.ASGITransport(app=app)
    timings: dict[str, list[float]] = {"dashboard": [], "csv": []}
    errors: list[str] = []
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", cookies={"cabinet_session": cookie}
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
    dashboard_p95 = p95(timings["dashboard"])
    csv_p95 = p95(timings["csv"])
    return {
        "measurements": require_volume(db),
        "calls": {"dashboard": len(timings["dashboard"]), "csv": len(timings["csv"])},
        "p95_ms": {
            "dashboard": round(dashboard_p95, 2),
            "csv": round(csv_p95, 2),
        },
        "limits_ms": {"dashboard": DASHBOARD_P95_LIMIT_MS, "csv": CSV_P95_LIMIT_MS},
        "errors": errors,
        "dashboard_p95_ok": dashboard_p95 < DASHBOARD_P95_LIMIT_MS,
        "csv_p95_ok": csv_p95 < CSV_P95_LIMIT_MS,
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
