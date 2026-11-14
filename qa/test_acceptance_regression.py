"""Приёмка: сбор артефактов и повторная сверка порогов (SPEC.md, §22; В9).

Запускается **отдельной командой после** модулей (`make acceptance` делает это
сам): собирает `qa/reports/load/acceptance_*.json` (нет файла — FAIL «не
запускался») и сверяет пороги ещё раз — так проверка не пройдёт «вхолостую»,
даже если модуль что-то не досчитал. Долгие проверки не повторяются: только
чтение артефактов. Итог — `qa/reports/load/acceptance_regression.json`.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from qa.acceptance_common import ACCEPTANCE_ENV, REPORTS_DIR, write_artifact

pytestmark = pytest.mark.skipif(
    os.environ.get(ACCEPTANCE_ENV, "").strip() != "1",
    reason="регрессия приёмки — только под ACCEPTANCE=1 (make acceptance)",
)

#: Артефакты шести проверок (имена `write_artifact`).
ARTIFACTS = ("volume", "degradation", "soak", "memory", "growth", "shutdown")


def _load(name: str) -> dict:
    path = REPORTS_DIR / f"acceptance_{name}.json"
    if not path.exists():
        pytest.fail(f"приёмка «{name}» не запускалась: нет {path} — выполните `make acceptance`")
    return json.loads(path.read_text(encoding="utf-8"))


def test_acceptance_criteria() -> None:
    violations: list[str] = []

    volume = _load("volume")
    if volume.get("errors"):
        violations.append(f"volume: ошибки ответов {volume['errors'][:3]}")
    if not volume.get("dashboard_p95_ok"):
        violations.append(f"volume: p95 дашборда {volume.get('p95_ms', {}).get('dashboard')} мс ≥ 2000 мс (число из Т3)")
    if not volume.get("csv_p95_ok"):
        violations.append(f"volume: p95 CSV {volume.get('p95_ms', {}).get('csv')} мс ≥ 3000 мс (число из Т3)")
    window = volume.get("concurrency", {})
    if window.get("errors"):
        violations.append(f"volume: окно конкуренции ({window.get('ops')} операций) — ошибки {window['errors'][:3]}")
    if not window.get("dashboard_p95_ok"):
        violations.append(
            f"volume: окно конкуренции — p95 дашборда {window.get('p95_ms', {}).get('dashboard')} мс ≥ 2000 мс"
        )
    if not window.get("csv_p95_ok"):
        violations.append(f"volume: окно конкуренции — p95 CSV {window.get('p95_ms', {}).get('csv')} мс ≥ 3000 мс")

    degradation = _load("degradation")
    for operation in degradation.get("operations", []):
        if not operation.get("ok"):
            violations.append(
                f"degradation: «{operation.get('operation')}» — последняя медиана "
                f"{operation.get('last_window_median_ms')} мс > 1,5 × первой "
                f"{operation.get('first_window_median_ms')} мс"
            )

    soak = _load("soak")
    if not soak.get("bot_ok"):
        violations.append(f"soak: RSS бота растёт на {soak.get('bot_slope_mb_min')} МБ/мин (> 0,5)")
    if not soak.get("cabinet_ok"):
        violations.append(f"soak: RSS кабинета растёт на {soak.get('cabinet_slope_mb_min')} МБ/мин (> 0,5)")

    memory = _load("memory")
    if not memory.get("rss_ok"):
        violations.append(
            f"memory: RSS не вернулся к исходному уровню (Δ {memory.get('delta_rss_mb')} МБ, "
            f"допуск ±{memory.get('rss_tolerance_mb')} МБ)"
        )
    if not memory.get("heap_ok"):
        violations.append(f"memory: прирост Python-heap {memory.get('heap_growth_mb')} МБ > 5 МБ")

    growth = _load("growth")
    if not growth.get("db_ok"):
        violations.append(
            f"growth: БД + WAL + jobstore за 3 года > 200 МБ (оценка {growth.get('db_estimate_3y_mb')} МБ, "
            f"факт {growth.get('db_actual_3y_mb')} МБ)"
        )
    if not growth.get("files_ok"):
        violations.append(f"growth: файлы на диске > 500 МБ ({growth.get('files_steady_mb')} МБ)")

    shutdown = _load("shutdown")
    slow = {k: v for k, v in shutdown.get("shutdown_seconds", {}).items() if v > shutdown.get("limit_seconds", 30)}
    if slow:
        violations.append(f"shutdown: завершение дольше 30 с: {slow}")
    threads = {k: v for k, v in shutdown.get("threads_left", {}).items() if v}
    if threads:
        violations.append(f"shutdown: остались потоки после shutdown/dispose: {threads}")
    if not shutdown.get("wal_ok"):
        violations.append("shutdown: `-wal` осиротел после остановки обоих процессов")
    if shutdown.get("integrity_check") != "ok":
        violations.append(f"shutdown: база повреждена: {shutdown.get('integrity_check')}")

    write_artifact("regression", {"violations": violations})
    assert not violations, "приёмка не пройдена:\n- " + "\n- ".join(violations)
