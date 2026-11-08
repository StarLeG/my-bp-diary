"""Критерии приёмки нагрузки (SPEC.md, §22, «Критерии приёмки»).

Запускается после полного прогона (`LOAD_TEST=1 uv run pytest qa/ …` и
Locust) отдельной командой:

    LOAD_TEST=1 uv run pytest qa/test_load_regression.py -v

Источники: JSON-итоги нагрузочных тестов (`qa/reports/load/<тест>.json`;
нет файла — FAIL «не запускался»), Locust CSV (`qa/reports/load/locust_*.csv`;
нет — проверки Locust SKIP) и БД стенда (`LOAD_DB`, по умолчанию
`data/test-stand.sqlite3`). Каждая проверка падает с понятным сообщением и
id нарушителей. В дымовом прогоне (без `LOAD_TEST=1`) — SKIP целиком.
"""
from __future__ import annotations

import csv
import io
import json
import locale
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from qa.seed import FULL, PATIENT_TG_RANGE, patient_timezone, stand_db_path
from qa.seed.seed_5000_measurements import plan_measurements

pytestmark = pytest.mark.skipif(
    os.environ.get("LOAD_TEST", "").strip() != "1",
    reason="регрессия критериев — только после полного прогона (LOAD_TEST=1)",
)

#: Артефакты нагрузки (SPEC §22, «Логи и отчёты»).
REPORTS = Path("qa") / "reports" / "load"

#: JSON-итоги, без которых приёмка невозможна.
ARTIFACTS = (
    "test_load_burst",
    "test_load_bot",
    "test_load_doctor",
    "test_load_reminders",
    "test_load_backup",
    "test_load_concurrent",
    "test_load_admin",
    "test_load_screenshots",
)

#: Пороги p95 Locust (SPEC §22, решение S5): запрос → мс.
P95_LIMITS_MS = {
    "/ [дашборд]": 2000,
    "/?days=30 [история]": 2000,
    "/export/measurements.csv [30 дней]": 3000,
    "/export/medications.csv [30 дней]": 3000,
}

#: Коды, которых не должно быть ни одного (SPEC §22, решение D3).
FORBIDDEN_CODES = re.compile(r"\b(5\d\d|403|429)\b")


def load_artifact(name: str) -> dict[str, Any]:
    path = REPORTS / f"{name}.json"
    if not path.exists():
        pytest.fail(f"{name}: не запускался — нет {path} (полный прогон: LOAD_TEST=1 uv run pytest qa/)")
    return json.loads(path.read_text(encoding="utf-8"))


# ─────────────────────────── JSON-итоги тестов ───────────────────────────


@pytest.mark.parametrize("name", ARTIFACTS)
def test_artifact_passed(name: str) -> None:
    """Каждый нагрузочный тест полного профиля отработал и выполнил свои критерии."""
    report = load_artifact(name)
    assert report.get("profile") == "full", f"{name}: итог не полного профиля ({report.get('profile')})"
    details = {
        key: value
        for key, value in report.items()
        if key not in {"name", "profile", "finished_at"} and value not in ({}, [], 0, None, False, True)
        and any(word in key for word in ("violation", "failure", "error", "missing", "duplicate", "short", "wrong"))
    }
    assert report.get("passed") is True, f"{name}: критерии не выполнены, нарушители: {details or report}"


def test_reminders_no_duplicates() -> None:
    """Дублей отправленных напоминаний — 0 (SPEC §22, таблица)."""
    report = load_artifact("test_load_reminders")
    assert report["duplicates"] == {}, f"напоминания отправлены повторно (id: число): {report['duplicates']}"
    assert report["double_tick_sends"] == 0, f"двойной тик отправил {report['double_tick_sends']}"
    assert report["sent"] == report["reminders"], f"доставлено {report['sent']} из {report['reminders']}"


def test_backup_integrity() -> None:
    """`integrity_check` копии после бэкапа под нагрузкой — `ok`."""
    report = load_artifact("test_load_backup")
    assert report["integrity_check"] == "ok", f"integrity_check: {report['integrity_check']}"
    assert report["tables_short"] == {}, f"в копии меньше строк, чем в снимке: {report['tables_short']}"


# ─────────────────────────── Locust ───────────────────────────


def read_csv(path: Path) -> list[dict[str, str]]:
    """CSV Locust: UTF-8, а под Windows без `PYTHONUTF8=1` — системная кодировка."""
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode(locale.getpreferredencoding(False))
    return list(csv.DictReader(io.StringIO(text)))


def locust_rows() -> list[dict[str, str]]:
    path = REPORTS / "locust_stats.csv"
    if not path.exists():
        pytest.skip(f"нет {path}: запустите Locust (qa/README.md, «Запуск», шаг 4)")
    return read_csv(path)


def test_locust_no_forbidden_codes() -> None:
    """HTTP 5xx, 403, 429 — 0; прочих ошибок сценария — тоже 0."""
    locust_rows()  # SKIP, если Locust не запускался
    failures = read_csv(REPORTS / "locust_failures.csv")
    forbidden = [row for row in failures if FORBIDDEN_CODES.search(row.get("Error", ""))]
    assert not forbidden, "ответы 5xx/403/429: " + "; ".join(
        f"{row['Method']} {row['Name']} — {row['Error']} ×{row['Occurrences']}" for row in forbidden[:10]
    )
    assert not failures, "ошибки сценария Locust: " + "; ".join(
        f"{row['Method']} {row['Name']} — {row['Error']} ×{row['Occurrences']}" for row in failures[:10]
    )


@pytest.mark.parametrize(("name", "limit_ms"), sorted(P95_LIMITS_MS.items()))
def test_locust_p95(name: str, limit_ms: int) -> None:
    """p95 дашборда и истории < 2 с, CSV за 30 дней < 3 с (SPEC §22, решение S5)."""
    rows = {row["Name"]: row for row in locust_rows()}
    assert name in rows, f"в Locust CSV нет запроса «{name}» — сценарий не выполнялся"
    row = rows[name]
    assert int(row["Request Count"]) > 0, f"«{name}»: ни одного запроса"
    p95 = float(row["95%"])
    assert p95 < limit_ms, f"«{name}»: p95 {p95:.0f} мс ≥ {limit_ms} мс"


# ─────────────────────────── БД стенда ───────────────────────────


@pytest.fixture(scope="module")
def stand() -> sqlite3.Connection:
    path = stand_db_path()
    if not path.exists():
        pytest.fail(f"нет БД стенда {path}: засейте её (qa/README.md, «Запуск», шаг 1)")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    yield con
    con.close()


def load_patients_rows(stand: sqlite3.Connection) -> list[tuple[int, int, str, str]]:
    """(users.id, telegram_id, timezone, created_at) нагрузочных пациентов."""
    return stand.execute(
        "SELECT id, telegram_id, timezone, created_at FROM users "
        "WHERE telegram_id >= ? AND telegram_id < ? ORDER BY telegram_id",
        (PATIENT_TG_RANGE.start, PATIENT_TG_RANGE.stop),
    ).fetchall()


def test_stand_patients_alive(stand) -> None:
    """Все 20 нагрузочных пациентов живы в `users`."""
    rows = load_patients_rows(stand)
    assert len(rows) == FULL.patients, (
        f"пациентов стенда {len(rows)}, ожидалось {FULL.patients}: "
        f"есть telegram_id {[row[1] for row in rows]}"
    )


def test_stand_seeded_measurements_present(stand) -> None:
    """Все 5000 сидированных измерений на месте (новые от Locust допускаются).

    Расписание пересчитывается по поясу из сида (`patient_timezone`), а не по
    текущему в БД: Locust сохраняет профиль с `Europe/Minsk`.
    """

    class Patient:  # то, что читает plan_measurements
        def __init__(self, tz_name: str, created_at: str) -> None:
            self.timezone = tz_name
            self.created_at = datetime.fromisoformat(created_at).replace(tzinfo=timezone.utc)

    missing: dict[int, int] = {}
    found = 0
    for user_id, telegram_id, _, created_at in load_patients_rows(stand):
        n = telegram_id - PATIENT_TG_RANGE.start + 1
        planned = plan_measurements(n, Patient(patient_timezone(n), created_at), FULL)  # type: ignore[arg-type]
        present = {
            datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)
            for (raw,) in stand.execute("SELECT timestamp FROM measurements WHERE user_id = ?", (user_id,))
        }
        hits = sum(1 for p in planned if p.timestamp in present)
        found += hits
        if hits != len(planned):
            missing[user_id] = len(planned) - hits
    assert found == FULL.measurements and not missing, (
        f"сидированных измерений {found} из {FULL.measurements}; не хватает (user_id: число): {missing}"
    )


def test_stand_no_minute_duplicates(stand) -> None:
    """Дублей измерений за минуту — 0."""
    rows = stand.execute(
        "SELECT user_id, strftime('%Y-%m-%d %H:%M', timestamp) AS minute, COUNT(*) "
        "FROM measurements GROUP BY user_id, minute HAVING COUNT(*) > 1 LIMIT 20"
    ).fetchall()
    assert not rows, f"дубли за минуту (user_id, минута, число): {rows}"


def test_stand_no_medication_log_duplicates(stand) -> None:
    """Дублей `medication_logs` по (user_id, slot, локальная дата, medication_id), кроме crisis."""
    zones = dict(stand.execute("SELECT id, timezone FROM users").fetchall())
    seen: dict[tuple, int] = {}
    for log_id, user_id, slot, medication_id, raw in stand.execute(
        "SELECT id, user_id, slot, medication_id, timestamp FROM medication_logs WHERE slot != 'crisis'"
    ):
        local_day = (
            datetime.fromisoformat(raw).replace(tzinfo=timezone.utc).astimezone(ZoneInfo(zones[user_id])).date()
        )
        key = (user_id, slot, local_day, medication_id)
        seen.setdefault(key, 0)
        seen[key] += 1
    duplicates = [(*key[:2], str(key[2]), key[3], count) for key, count in seen.items() if count > 1]
    assert not duplicates, f"дубли логов (user_id, slot, дата, medication_id, число): {duplicates[:20]}"
