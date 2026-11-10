"""ЛК: удобство напоминаний, часть 2 (SPEC.md, §6.7 и §16.2; 0.5.6).

Покрывается форма POST /settings: режим слота (`both` / `meds` / `pressure`,
подделанный `meds` без лекарств — ошибка формы), «Повторять, если не ответил»
(выключение обнуляет `followup_at`), пауза 1 / 3 / 7 дней и «Снять паузу»,
сброс `meds` → `both` при удалении последнего лекарства слота, CSRF;
отрисовка дашборда (подписи, недоступный «Только лекарства», «На паузе до»).
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from tests.test_web_auth import TG_ID, _csrf, _register, app_client, settings  # noqa: F401

TZ = ZoneInfo("Europe/Minsk")


def _sql(settings, query: str, args: tuple = ()) -> list[tuple]:  # noqa: F811
    """Синхронный запрос к файлу БД кабинета."""
    con = sqlite3.connect(settings.db_path)
    try:
        rows = con.execute(query, args).fetchall()
        con.commit()
    finally:
        con.close()
    return rows


def _form(client: TestClient, **extra: str) -> dict[str, str]:
    """Форма напоминаний: утро включено 08:30, остальное — по аргументам."""
    data = {
        "csrf_token": _csrf(client),
        "slot_morning_enabled": "1",
        "slot_morning_time": "08:30",
        "slot_noon_time": "13:00",
        "slot_evening_time": "21:00",
        "med_remeasure_delay": "60",
        "followup_enabled": "1",
    }
    data.update(extra)
    return {k: v for k, v in data.items() if v is not None}


def _add_med(client: TestClient, slot: str = "morning") -> None:
    response = client.post(
        "/medications",
        data={"csrf_token": _csrf(client), "name": "Лозартан", "dosage": "50 мг", f"slot_{slot}": "1"},
    )
    assert response.status_code == 303


def _today() -> str:
    return datetime.now(TZ).date().isoformat()


class TestSlotMode:
    def test_default_mode_both(self, app_client, settings) -> None:  # noqa: F811
        client, user_id = app_client
        _register(client)
        assert client.post("/settings", data=_form(client)).status_code == 303
        assert _sql(settings, "SELECT type, mode FROM reminders WHERE user_id = ?", (user_id,)) == [
            ("morning", "both")
        ]

    def test_pressure_mode_saved_for_new_and_disabled(self, app_client, settings) -> None:  # noqa: F811
        client, user_id = app_client
        _register(client)
        client.post("/settings", data=_form(client, slot_morning_mode="pressure"))
        client.post(
            "/settings",
            data=_form(client, slot_morning_enabled=None, slot_morning_mode="pressure"),
        )
        assert _sql(settings, "SELECT enabled, mode FROM reminders WHERE user_id = ?", (user_id,)) == [
            (0, "pressure")
        ]

    def test_meds_mode_with_medications(self, app_client, settings) -> None:  # noqa: F811
        client, user_id = app_client
        _register(client)
        _add_med(client)
        client.post("/settings", data=_form(client))
        response = client.post("/settings", data=_form(client, slot_morning_mode="meds"))
        assert "error" not in response.headers["location"]
        assert _sql(settings, "SELECT mode FROM reminders WHERE user_id = ?", (user_id,)) == [("meds",)]

    def test_forged_meds_without_medications_rejected(self, app_client, settings) -> None:  # noqa: F811
        client, user_id = app_client
        _register(client)
        response = client.post("/settings", data=_form(client, slot_noon_mode="meds"))
        assert response.status_code == 303
        assert "error=reminder_meds" in response.headers["location"]
        assert _sql(settings, "SELECT COUNT(*) FROM reminders WHERE user_id = ?", (user_id,)) == [(0,)]
        page = client.get("/")
        assert "Сначала добавьте лекарства в этот слот." in client.get(
            "/?error=reminder_meds"
        ).text
        assert page.status_code == 200

    def test_unknown_mode_rejected(self, app_client, settings) -> None:  # noqa: F811
        client, user_id = app_client
        _register(client)
        response = client.post("/settings", data=_form(client, slot_morning_mode="all"))
        assert "error=reminder" in response.headers["location"]
        assert _sql(settings, "SELECT COUNT(*) FROM reminders WHERE user_id = ?", (user_id,)) == [(0,)]

    def test_deleting_last_medication_resets_meds(self, app_client, settings) -> None:  # noqa: F811
        client, user_id = app_client
        _register(client)
        _add_med(client)
        client.post("/settings", data=_form(client, slot_morning_mode="meds"))
        med_id = _sql(settings, "SELECT id FROM medications WHERE user_id = ?", (user_id,))[0][0]
        response = client.post(
            f"/medications/{med_id}/delete", data={"csrf_token": _csrf(client)}
        )
        assert response.status_code == 303
        assert _sql(settings, "SELECT mode FROM reminders WHERE user_id = ?", (user_id,)) == [("both",)]

    def test_dashboard_mode_hints(self, app_client, settings) -> None:  # noqa: F811
        client, user_id = app_client
        _register(client)
        text = client.get("/").text
        assert "О чём напоминать" in text
        assert "«Только лекарства»: сначала добавьте лекарства в этот слот." in text
        assert re.search(r'value="meds"\s*disabled>💊 Только лекарства', text)
        _add_med(client)
        client.post("/settings", data=_form(client, slot_morning_mode="pressure"))
        text = client.get("/").text
        assert "Лекарства этого слота не будут напоминаться." in text


class TestFollowup:
    def test_checkbox_saved_and_clears_pending(self, app_client, settings) -> None:  # noqa: F811
        client, user_id = app_client
        _register(client)
        client.post("/settings", data=_form(client))
        _sql(settings, "UPDATE reminders SET followup_at = '2030-01-01 00:00:00' WHERE user_id = ?", (user_id,))
        client.post("/settings", data=_form(client, followup_enabled=None))
        assert _sql(settings, "SELECT followup_enabled FROM users WHERE id = ?", (user_id,)) == [(0,)]
        assert _sql(settings, "SELECT followup_at FROM reminders WHERE user_id = ?", (user_id,)) == [(None,)]
        assert "checked" not in client.get("/").text.split('name="followup_enabled"')[1].split(">")[0]

    def test_enabled_keeps_pending(self, app_client, settings) -> None:  # noqa: F811
        client, user_id = app_client
        _register(client)
        client.post("/settings", data=_form(client))
        _sql(settings, "UPDATE reminders SET followup_at = '2030-01-01 00:00:00' WHERE user_id = ?", (user_id,))
        client.post("/settings", data=_form(client))
        assert _sql(settings, "SELECT followup_enabled FROM users WHERE id = ?", (user_id,)) == [(1,)]
        assert _sql(settings, "SELECT followup_at FROM reminders WHERE user_id = ?", (user_id,)) == [
            ("2030-01-01 00:00:00",)
        ]
        assert "Повторять, если не ответил" in client.get("/").text


class TestPause:
    @pytest.mark.parametrize(("days", "offset"), [("1", 0), ("3", 2), ("7", 6)])
    def test_pause_days(self, app_client, settings, days, offset) -> None:  # noqa: F811
        client, user_id = app_client
        _register(client)
        client.post("/settings", data=_form(client, pause=days))
        expected = (datetime.now(TZ).date() + timedelta(days=offset)).isoformat()
        assert _sql(settings, "SELECT reminders_paused_until FROM users WHERE id = ?", (user_id,)) == [
            (expected,)
        ]
        text = client.get("/").text
        until = (datetime.now(TZ).date() + timedelta(days=offset)).strftime("%d.%m")
        assert f"На паузе до {until}" in text
        assert "Снять паузу" in text

    def test_keep_and_remove(self, app_client, settings) -> None:  # noqa: F811
        client, user_id = app_client
        _register(client)
        client.post("/settings", data=_form(client, pause="3"))
        client.post("/settings", data=_form(client, pause="keep"))
        row = _sql(settings, "SELECT reminders_paused_until FROM users WHERE id = ?", (user_id,))
        assert row[0][0] is not None
        client.post("/settings", data=_form(client, pause="keep", pause_off="1"))
        assert _sql(settings, "SELECT reminders_paused_until FROM users WHERE id = ?", (user_id,)) == [(None,)]
        text = client.get("/").text
        assert "Снять паузу" not in text
        assert '<option value="" selected>Нет</option>' in text

    def test_choose_none_removes(self, app_client, settings) -> None:  # noqa: F811
        client, user_id = app_client
        _register(client)
        client.post("/settings", data=_form(client, pause="7"))
        client.post("/settings", data=_form(client, pause=""))
        assert _sql(settings, "SELECT reminders_paused_until FROM users WHERE id = ?", (user_id,)) == [(None,)]

    def test_expired_pause_not_shown(self, app_client, settings) -> None:  # noqa: F811
        client, user_id = app_client
        _register(client)
        _sql(settings, "UPDATE users SET reminders_paused_until = '2020-01-01' WHERE id = ?", (user_id,))
        assert "Снять паузу" not in client.get("/").text

    def test_bad_pause_rejected(self, app_client, settings) -> None:  # noqa: F811
        client, user_id = app_client
        _register(client)
        response = client.post("/settings", data=_form(client, pause="30"))
        assert "error=reminder" in response.headers["location"]
        assert _sql(settings, "SELECT reminders_paused_until FROM users WHERE id = ?", (user_id,)) == [(None,)]


def test_csrf_required(app_client, settings) -> None:  # noqa: F811
    client, user_id = app_client
    _register(client)
    data = _form(client, pause="3", followup_enabled=None)
    data["csrf_token"] = "forged"
    response = client.post("/settings", data=data)
    assert response.status_code in (400, 403)
    assert _sql(settings, "SELECT followup_enabled, reminders_paused_until FROM users WHERE id = ?", (user_id,)) == [
        (1, None)
    ]


class TestTickAfterCabinet:
    """0.5.7 (§6.7): время слота, изменённое в ЛК, доходит до бота тиком — без перезапуска."""

    def test_job_follows_cabinet_time(self, app_client, settings) -> None:  # noqa: F811
        import asyncio
        from types import SimpleNamespace

        from bot.db import create_engine, create_session_factory
        from bot.services import reminders as rem

        class Scheduler:
            def __init__(self) -> None:
                self.jobs: dict[str, dict] = {}

            def add_job(self, func, **kwargs) -> None:
                self.jobs[kwargs["id"]] = kwargs

            def remove_job(self, job_id: str) -> None:
                self.jobs.pop(job_id, None)

            def get_jobs(self):
                return [SimpleNamespace(id=k, next_run_time=v["trigger"].run_date) for k, v in self.jobs.items()]

        client, user_id = app_client
        _register(client)
        client.post("/settings", data=_form(client, slot_morning_time="08:30"))
        scheduler = Scheduler()

        async def run(old_job: bool):
            engine = create_engine(settings.db_path)
            try:
                svc = rem.RemindersService(create_session_factory(engine), None, scheduler)
                if old_job:  # бот поставил job на прежнее время
                    rid, = _sql(settings, "SELECT id FROM reminders WHERE user_id = ?", (user_id,))[0]
                    svc._add_job(rid, datetime.now(TZ) + timedelta(minutes=3), rem.MODE_SLOT)
                    return 0
                return await svc.tick()
            finally:
                await engine.dispose()

        asyncio.run(run(old_job=True))
        client.post("/settings", data=_form(client, slot_morning_time="09:45"))
        assert asyncio.run(run(old_job=False)) == 1
        rid, stored = _sql(settings, "SELECT id, next_trigger_time FROM reminders WHERE user_id = ?", (user_id,))[0]
        run_at = scheduler.jobs[f"reminder:{rid}"]["trigger"].run_date
        assert run_at.astimezone(TZ).strftime("%H:%M") == "09:45"
        assert run_at.replace(tzinfo=None).isoformat(" ").startswith(stored[:16])
        assert "Бот применит изменения в течение минуты." in client.get("/").text
