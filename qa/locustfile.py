"""Нагрузочный сценарий кабинета для Locust (SPEC.md, §22, «Нагрузочное тестирование»).

20 одновременных пользователей проходят типовой сценарий: вход → дашборд →
экспорт CSV → отметка лекарства → настройки. Каждый виртуальный пользователь
работает под своей учётной записью (см. `qa/seed_load_users.py`).

Запуск (против ТЕСТОВОГО стенда, не против прод-данных):

    # 1. один раз подготовить учётные записи и историю измерений:
    uv run python qa/seed_load_users.py --db data/test-stand.sqlite3

    # 2. нагрузить кабинет: 20 пользователей, вход 2/сек, 5 минут
    uv run locust -f qa/locustfile.py --host=http://127.0.0.1:8000 \
        -u 20 -r 2 -t 5m --headless --csv=qa/reports/load/locust

Веб-интерфейс Locust (без --headless): http://127.0.0.1:8089.

Переменные окружения:
    LOAD_USERS            число учётных записей, созданных seed-скриптом (20)
    LOAD_PASSWORD         пароль всех нагрузочных аккаунтов (LoadTest!2026)
    LOAD_EMAIL_TEMPLATE   шаблон e-mail, {n} — номер аккаунта (load{n}@load.test)

Критерии приёмки: 0 ошибок 5xx и 403/429, p95 дашборда < 2 с,
CSV за 30 дней < 3 с (см. SPEC.md, §22).
"""
from __future__ import annotations

import os
import threading
from itertools import count

from locust import HttpUser, between, task

#: Учётные записи распределяются по виртуальным пользователям по кругу.
_counter = count(1)
_counter_lock = threading.Lock()

LOAD_USERS = int(os.getenv("LOAD_USERS", "20"))
LOAD_PASSWORD = os.getenv("LOAD_PASSWORD", "LoadTest!2026")
LOAD_EMAIL_TEMPLATE = os.getenv("LOAD_EMAIL_TEMPLATE", "load{n}@load.test")

#: Число полей формы настроек, отправляемых как есть (§16.2).
SETTINGS_FORM = {
    "target_systolic": "130",
    "target_diastolic": "80",
    "timezone": "Europe/Minsk",
    "slot_morning_time": "08:00",
    "slot_morning_enabled": "on",
    "slot_noon_time": "13:00",
    "slot_noon_enabled": "on",
    "slot_evening_time": "20:00",
    "slot_evening_enabled": "on",
    "med_remeasure_delay": "60",
}


class CabinetUser(HttpUser):
    """Один «пациент» в личном кабинете."""

    #: Пауза между действиями — как у живого человека.
    wait_time = between(1, 3)

    def on_start(self) -> None:
        """Вход e-mail + пароль; дальше сценарий работает с сессионной кукой."""
        with _counter_lock:
            index = next(_counter)
        n = (index - 1) % LOAD_USERS + 1
        self.email = LOAD_EMAIL_TEMPLATE.format(n=n)

        # GET /login выдаёт CSRF-куку, без неё POST отбивается 403.
        self.client.get("/login", name="/login [форма]")
        csrf = self.client.cookies.get("csrf_token", "")
        response = self.client.post(
            "/login",
            data={
                "email": self.email,
                "password": LOAD_PASSWORD,
                "csrf_token": csrf,
            },
            name="/login [вход]",
            allow_redirects=True,
        )
        if self.client.cookies.get("cabinet_session") is None:
            response.failure(
                f"сессия не открыта для {self.email}: "
                f"{response.status_code} (проверьте seed_load_users.py и LOAD_PASSWORD)"
            )

    def _csrf(self) -> str:
        return self.client.cookies.get("csrf_token", "")

    @task(4)
    def dashboard(self) -> None:
        """Главная страница: карточка, история, график (самая частая операция)."""
        self.client.get("/", name="/ [дашборд]")

    @task(2)
    def export_measurements(self) -> None:
        """Экспорт CSV измерений — тяжёлая операция, частая у живых пользователей."""
        self.client.get("/export/measurements.csv", name="/export/measurements.csv")

    @task(1)
    def export_medications(self) -> None:
        """Экспорт CSV приёмов лекарств."""
        self.client.get("/export/medications.csv", name="/export/medications.csv")

    @task(1)
    def add_medication(self) -> None:
        """Добавление лекарства (имя уникально — UNIQUE(user, name, slot) не мешает)."""
        name = f"Нагрузка-{os.urandom(4).hex()}"
        self.client.post(
            "/medications",
            data={
                "csrf_token": self._csrf(),
                "name": name,
                "dosage": "5 мг",
                "slot_morning": "on",
            },
            name="/medications [добавить]",
            allow_redirects=True,
        )

    @task(1)
    def save_settings(self) -> None:
        """Сохранение настроек — запись в users + перепланировка напоминаний."""
        self.client.post(
            "/settings",
            data={**SETTINGS_FORM, "csrf_token": self._csrf()},
            name="/settings [сохранить]",
            allow_redirects=True,
        )
