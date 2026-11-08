"""Скриншоты стенда после нагрузки (SPEC.md, §22, уровень 9).

Playwright (Chromium) против запущенного кабинета на стенде
(`LOAD_CABINET_URL`, по умолчанию `http://127.0.0.1:8000`; кабинет — с
`DB_PATH=data/test-stand.sqlite3`): три скриншота в `qa/reports/screenshots/`:

- `load_patient_dashboard.png` — дашборд пациента с тремя графиками;
- `load_history_30d.png` — история записей с фильтром 30 дней;
- `load_doctor_dashboard.png` — карточка пациента у врача (вход по magic link).

Пациент входит e-mail + паролем стенда (`LOAD_PASSWORD`), врач — по ссылке,
подписанной `WEB_SECRET_KEY` кабинета (окружение или `.env` стенда). Только
под `LOAD_TEST=1`; без запущенного кабинета или пароля — SKIP (регрессия
тогда честно отметит «не запускался»). Итог — `test_load_screenshots.json`.
"""
from __future__ import annotations

import os
import sqlite3
import urllib.error
import urllib.request

import pytest

from qa.conftest import SHOTS_DIR
from qa.seed import doctor_email, patient_email, stand_db_path

pytestmark = pytest.mark.skipif(
    os.environ.get("LOAD_TEST", "").strip() != "1",
    reason="скриншоты стенда — только в полном профиле (LOAD_TEST=1)",
)

#: Кабинет стенда.
CABINET_URL = os.environ.get("LOAD_CABINET_URL", "http://127.0.0.1:8000").rstrip("/")

#: Скриншоты (SPEC §22, уровень 9).
SHOTS = ("load_patient_dashboard", "load_history_30d", "load_doctor_dashboard")

#: Графики сводки: ждём, пока Chart.js их нарисует.
CHART_IDS = ("bp-chart", "bp-chart-dia", "bp-chart-pulse")

#: Без анимации Chart.js: иначе снимок ловит линии посреди «вырастания» снизу,
#: и значения на картинке ниже настоящих. Прод-код не меняется — хук ставится
#: на глобальный `Chart` при его загрузке (UMD-сборка с CDN).
NO_CHART_ANIMATION = """
(() => {
  let chart;
  Object.defineProperty(window, 'Chart', {
    configurable: true,
    get() { return chart; },
    set(value) { if (value && value.defaults) { value.defaults.animation = false; } chart = value; },
  });
})();
"""


def cabinet_alive() -> bool:
    try:
        with urllib.request.urlopen(f"{CABINET_URL}/login", timeout=3) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError):
        return False


def web_secret() -> str:
    """`WEB_SECRET_KEY` запущенного кабинета: окружение или `.env` стенда."""
    from dotenv import dotenv_values

    return os.environ.get("WEB_SECRET_KEY", "") or (dotenv_values(".env").get("WEB_SECRET_KEY") or "")


def wait_for_charts(page) -> None:
    """Три canvas на месте и отрисованы (ненулевой размер)."""
    for chart_id in CHART_IDS:
        page.wait_for_selector(f"canvas#{chart_id}", state="visible", timeout=15_000)
    page.wait_for_function(
        "ids => ids.every(id => { const c = document.getElementById(id); "
        "return c && c.width > 0 && c.height > 0; })",
        arg=list(CHART_IDS),
        timeout=15_000,
    )


def test_stand_screenshots(page, shot, load_report) -> None:
    password = os.environ.get("LOAD_PASSWORD", "")
    if not password:
        pytest.skip("нет LOAD_PASSWORD стенда: пациент не войдёт в кабинет")
    if not cabinet_alive():
        pytest.skip(f"кабинет стенда не отвечает: {CABINET_URL} (qa/README.md, «Запуск», шаг 4)")
    secret = web_secret()
    if not secret:
        pytest.skip("нет WEB_SECRET_KEY кабинета: врач не войдёт по ссылке")

    con = sqlite3.connect(f"file:{stand_db_path()}?mode=ro", uri=True)
    try:
        doctor_id, user_id = con.execute(
            "SELECT d.id, dp.user_id FROM doctors d JOIN doctor_patients dp ON dp.doctor_id = d.id "
            "WHERE d.email = ? AND dp.status = 'active'",
            (doctor_email(1),),
        ).fetchone()
    finally:
        con.close()

    page.set_viewport_size({"width": 1366, "height": 900})
    page.add_init_script(NO_CHART_ANIMATION)
    page.goto(f"{CABINET_URL}/login")
    page.fill("input[name=email]", patient_email(1))
    page.fill("input[name=password]", password)
    page.click("button[type=submit]")
    page.wait_for_url(f"{CABINET_URL}/", timeout=15_000)
    wait_for_charts(page)
    page.evaluate("document.getElementById('summary').scrollIntoView()")
    shot(SHOTS[0])

    page.goto(f"{CABINET_URL}/?days=30#history")
    wait_for_charts(page)
    page.evaluate("document.getElementById('history').scrollIntoView()")
    shot(SHOTS[1])

    from web.cabinet.doctor_auth import DoctorMagicLink

    token = DoctorMagicLink(secret).make(doctor_id, user_id)
    page.goto(f"{CABINET_URL}/doctor?token={token}")
    page.click("text=Открыть дневник")  # страница подтверждения ссылки доступа (§21)
    page.wait_for_url(f"{CABINET_URL}/doctor/patient/{user_id}*", timeout=15_000)
    wait_for_charts(page)
    shot(SHOTS[2])

    files = [os.path.join(SHOTS_DIR, f"{name}.png") for name in SHOTS]
    missing = [path for path in files if not os.path.exists(path)]
    load_report("test_load_screenshots", {"passed": not missing, "files": files, "missing": missing})
    assert not missing, f"скриншоты не сохранены: {missing}"
