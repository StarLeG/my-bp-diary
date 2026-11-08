"""HTTP-нагрузка личного кабинета пациента — Locust (SPEC.md, §22, уровень 1).

20 виртуальных пользователей (вход 2/с, 5 минут), каждый под своей
учётной записью стенда (`load{n}@load.test`, `qa/seed/seed_all.py`). Веса
задач (SPEC §22): вход → дашборд → выход — 40; история за 30 дней — 20;
CSV измерений за 30 дней — 15; добавление измерения — 10; профиль — 10;
лекарство — 5; настройки напоминаний — 3; CSV лекарств — 2. После выхода
следующая задача входит заново (вход попадает в метрику «/login [вход]»).

Против ложно-зелёного прогона: GET-запросы идут без перехода по
редиректам — потерянная сессия (303 на `/login`) считается ошибкой, а не
страницей логина с кодом 200; на дашборде проверяются три графика
(`canvas` `bp-chart`, `bp-chart-dia`, `bp-chart-pulse`). Locust считает
ошибкой любой ответ 4xx/5xx — критерии §22: 0 ответов 5xx, 403, 429.

Запуск (против ТЕСТОВОГО стенда):

    # 1. сид стенда (один раз; --clean — пересоздать нагрузочные данные):
    LOAD_PASSWORD='<пароль стенда>' \\
        uv run python qa/seed/seed_all.py --db data/test-stand.sqlite3

    # 2. кабинет на стенде:
    DB_PATH=data/test-stand.sqlite3 uv run uvicorn web.cabinet.app:app --port 8000

    # 3. нагрузка: 20 пользователей, вход 2/с, 5 минут
    LOAD_PASSWORD='<пароль стенда>' \\
        uv run locust -f qa/locustfile.py --host=http://127.0.0.1:8000 \\
        -u 20 -r 2 -t 5m --headless --csv=qa/reports/load/locust

Переменные окружения:
    LOAD_USERS            число учётных записей стенда (20)
    LOAD_PASSWORD         пароль нагрузочных аккаунтов; обязателен — в коде
                          пароля нет (CLAUDE.md, «Правила для Claude Code» §2)
    LOAD_EMAIL_TEMPLATE   шаблон e-mail, {n} — номер аккаунта (load{n}@load.test)
"""
from __future__ import annotations

import os
import random
import threading
from datetime import datetime, timedelta
from itertools import count
from zoneinfo import ZoneInfo

from locust import HttpUser, between, task

#: Учётные записи распределяются по виртуальным пользователям по кругу.
_counter = count(1)
_counter_lock = threading.Lock()

LOAD_USERS = int(os.getenv("LOAD_USERS", "20"))
LOAD_PASSWORD = os.environ.get("LOAD_PASSWORD", "")
LOAD_EMAIL_TEMPLATE = os.getenv("LOAD_EMAIL_TEMPLATE", "load{n}@load.test")

if not LOAD_PASSWORD:
    raise RuntimeError(
        "LOAD_PASSWORD не задан. Пароль нагрузочных аккаунтов берётся только из "
        "окружения (CLAUDE.md: пароли не хардкодятся в коде). Пример: "
        "LOAD_PASSWORD=... uv run locust -f qa/locustfile.py ..."
    )

#: Три графика сводки (SPEC §16.2) — признак, что дашборд действительно собран.
CHART_IDS = ('id="bp-chart"', 'id="bp-chart-dia"', 'id="bp-chart-pulse"')

#: Время новых измерений считается в Минске: у пациентов стенда пояса UTC+3…+5,
#: поэтому «сейчас − 1 ч» по Минску у всех — в прошлом (§9: будущее запрещено).
FORM_TZ = ZoneInfo("Europe/Minsk")

#: Форма «Напоминания» (POST /settings) — поля как в dashboard.html (§16.2).
SETTINGS_FORM = {
    "slot_morning_time": "08:00",
    "slot_morning_enabled": "1",
    "slot_noon_time": "13:00",
    "slot_noon_enabled": "1",
    "slot_evening_time": "20:00",
    "slot_evening_enabled": "1",
    "med_remeasure_delay": "60",
}

#: Форма «Личная карточка» (POST /profile): цели давления и часовой пояс.
#: `new_password` не отправляем — пароль нагрузочных аккаунтов не меняем.
PROFILE_FORM = {
    "target_systolic": "130",
    "target_diastolic": "80",
    "timezone": "Europe/Minsk",
}


class CabinetUser(HttpUser):
    """Один «пациент» в личном кабинете."""

    #: Пауза между действиями — как у живого человека.
    wait_time = between(1, 3)

    def on_start(self) -> None:
        """Закрепить за виртуальным пользователем учётную запись и войти."""
        with _counter_lock:
            index = next(_counter)
        n = (index - 1) % LOAD_USERS + 1
        self.email = LOAD_EMAIL_TEMPLATE.format(n=n)
        self.logged_in = False
        self._login()

    # ─────────────────────────── Вспомогательное ───────────────────────────

    def _csrf(self) -> str:
        return self.client.cookies.get("csrf_token", "")

    def _login(self) -> bool:
        """Вход e-mail + пароль; успех — только реальный 303 на `/`.

        При неудаче сервер тоже отвечает 303 — на `/login?error=…`; такой
        «успех» засорял бы метрики, поэтому он помечается ошибкой.
        """
        self.client.get("/login", name="/login [форма]")  # CSRF-кука для формы
        with self.client.post(
            "/login",
            data={"email": self.email, "password": LOAD_PASSWORD, "csrf_token": self._csrf()},
            name="/login [вход]",
            allow_redirects=False,
            catch_response=True,
        ) as response:
            location = response.headers.get("Location", "")
            if response.status_code in (302, 303) and location == "/":
                response.success()
                self.logged_in = True
                return True
            response.failure(
                f"сессия не открыта для {self.email}: {response.status_code} → "
                f"{location or '(без Location)'} (проверьте сид стенда и LOAD_PASSWORD)"
            )
            self.logged_in = False
            return False

    def _ensure_login(self) -> bool:
        return self.logged_in or self._login()

    def _page(self, url: str, name: str, *, charts: bool) -> None:
        """GET страницы кабинета без редиректа: 303 на /login — потеря сессии."""
        with self.client.get(url, name=name, allow_redirects=False, catch_response=True) as response:
            if response.status_code != 200:
                response.failure(f"{response.status_code} → {response.headers.get('Location', '')}")
                self.logged_in = False
                return
            if charts and not all(chart in response.text for chart in CHART_IDS):
                response.failure("на дашборде нет трёх графиков (bp-chart, -dia, -pulse)")

    def _post_form(self, url: str, name: str, data: dict) -> None:
        """POST формы кабинета: успех — 303 (сохранено или ожидаемая ошибка формы)."""
        with self.client.post(
            url,
            data={**data, "csrf_token": self._csrf()},
            name=name,
            allow_redirects=False,
            catch_response=True,
        ) as response:
            location = response.headers.get("Location", "")
            if response.status_code != 303 or location.startswith("/login"):
                response.failure(f"{response.status_code} → {location}")
                self.logged_in = False

    # ─────────────────────────── Задачи (веса SPEC §22) ───────────────────────────

    @task(40)
    def login_dashboard_logout(self) -> None:
        """Вход → дашборд (три графика) → выход."""
        if not self._ensure_login():
            return
        self._page("/", "/ [дашборд]", charts=True)
        with self.client.post(
            "/logout",
            data={"csrf_token": self._csrf()},
            name="/logout",
            allow_redirects=False,
            catch_response=True,
        ) as response:
            if response.status_code != 303:
                response.failure(f"выход: {response.status_code}")
        self.logged_in = False

    @task(20)
    def history_30_days(self) -> None:
        """История записей с фильтром 30 дней — та же страница с ?days=30."""
        if self._ensure_login():
            self._page("/?days=30", "/?days=30 [история]", charts=True)

    @task(15)
    def export_measurements(self) -> None:
        """Экспорт CSV измерений за 30 дней — критерий §22 «CSV < 3 с»."""
        if self._ensure_login():
            self._page(
                "/export/measurements.csv?days=30",
                "/export/measurements.csv [30 дней]",
                charts=False,
            )

    @task(10)
    def add_measurement(self) -> None:
        """Новое измерение: уникальное прошедшее время (now − 1 ч − случайные минуты)."""
        if not self._ensure_login():
            return
        moment = datetime.now(FORM_TZ) - timedelta(hours=1, minutes=random.randint(0, 22 * 60))
        self._post_form(
            "/measurements",
            "/measurements [добавить]",
            {
                "date": moment.date().isoformat(),
                "time": moment.strftime("%H:%M"),
                "systolic": str(random.randint(110, 160)),
                "diastolic": str(random.randint(70, 95)),
                "pulse": str(random.randint(55, 95)),
                "feeling": "",
                "feeling_other": "",
                "note": "",
            },
        )

    @task(10)
    def save_profile(self) -> None:
        """Личная карточка (POST /profile): цели давления и часовой пояс."""
        if self._ensure_login():
            self._post_form("/profile", "/profile [сохранить]", PROFILE_FORM)

    @task(5)
    def add_medication(self) -> None:
        """Добавление лекарства (имя уникально — UNIQUE(user, name, slot) не мешает)."""
        if self._ensure_login():
            self._post_form(
                "/medications",
                "/medications [добавить]",
                {"name": f"Нагрузка-{os.urandom(4).hex()}", "dosage": "5 мг", "slot_morning": "1"},
            )

    @task(3)
    def save_settings(self) -> None:
        """Настройки напоминаний (POST /settings): слоты и повторный замер."""
        if self._ensure_login():
            self._post_form("/settings", "/settings [сохранить]", SETTINGS_FORM)

    @task(2)
    def export_medications(self) -> None:
        """Экспорт CSV приёмов лекарств за 30 дней."""
        if self._ensure_login():
            self._page(
                "/export/medications.csv?days=30",
                "/export/medications.csv [30 дней]",
                charts=False,
            )
