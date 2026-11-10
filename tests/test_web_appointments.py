"""ЛК: «Визиты и анализы» (SPEC.md, §6.13, §16.2; 0.5.8).

Добавление, правка, отмена, «Был(а)» / «Не был(а)», «Перенесли на…»,
удаление, ошибки формы, лимит, CSRF, изоляция (чужой id — 404).
Даты — относительно реального «сегодня» по Минску.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.test_web_auth import _csrf, _register, app_client, settings  # noqa: F401

TODAY = datetime.now(ZoneInfo("Europe/Minsk")).date()


def _sql(settings, query: str, args: tuple = ()) -> list[tuple]:  # noqa: F811
    con = sqlite3.connect(settings.db_path)
    try:
        rows = con.execute(query, args).fetchall()
        con.commit()
    finally:
        con.close()
    return rows


def _form(client, **extra):
    data = {
        "csrf_token": _csrf(client),
        "kind": "doctor",
        "title": "Кардиолог",
        "date": (TODAY + timedelta(days=3)).isoformat(),
        "time": "10:30",
        "note": "каб. 212",
    }
    data.update(extra)
    return data


def _insert(settings, user_id, days, status="planned", title="Старое") -> int:  # noqa: F811
    con = sqlite3.connect(settings.db_path)
    cur = con.execute(
        "INSERT INTO appointments (user_id, kind, title, starts_on, starts_at, note, status, created_at)"
        " VALUES (?, 'doctor', ?, ?, '10:00', '', ?, '2026-01-01 00:00:00')",
        (user_id, title, (TODAY + timedelta(days=days)).isoformat(), status),
    )
    con.commit()
    new_id = cur.lastrowid
    con.close()
    return new_id


def _location(response) -> str:
    assert response.status_code == 303
    return response.headers["location"]


class TestAdd:
    def test_add_and_show(self, app_client, settings) -> None:  # noqa: F811
        client, user_id = app_client
        _register(client)
        assert _location(client.post("/appointments", data=_form(client))) == "/?saved=appointment#appointments"
        assert _sql(settings, "SELECT kind, title, starts_at, note, status FROM appointments") == [
            ("doctor", "Кардиолог", "10:30", "каб. 212", "planned")
        ]
        page = client.get("/?saved=appointment").text
        assert "Визиты и анализы обновлены." in page
        assert 'id="appointments"' in page and 'value="Кардиолог"' in page
        assert '<option value="Липидный профиль">' in page

    def test_without_time(self, app_client, settings) -> None:  # noqa: F811
        client, _ = app_client
        _register(client)
        client.post("/appointments", data=_form(client, time="", note=""))
        assert _sql(settings, "SELECT starts_at, note FROM appointments") == [(None, "")]

    @pytest.mark.parametrize(
        "extra",
        [
            {"kind": "dentist"},
            {"title": "   "},
            {"title": "я" * 61},
            {"note": "я" * 201},
            {"date": "завтра"},
            {"date": (TODAY - timedelta(days=1)).isoformat()},
            {"date": (TODAY + timedelta(days=800)).isoformat()},
            {"time": "25:00"},
            {"date": TODAY.isoformat(), "time": "00:00"},
        ],
    )
    def test_invalid(self, app_client, settings, extra) -> None:  # noqa: F811
        client, _ = app_client
        _register(client)
        assert _location(client.post("/appointments", data=_form(client, **extra))) == "/?error=appointment#appointments"
        assert _sql(settings, "SELECT COUNT(*) FROM appointments") == [(0,)]
        assert "Проверьте название, дату и время." in client.get("/?error=appointment").text

    def test_limit(self, app_client, settings) -> None:  # noqa: F811
        client, user_id = app_client
        _register(client)
        for index in range(30):
            _insert(settings, user_id, 5, title=f"В{index}")
        assert _location(client.post("/appointments", data=_form(client))) == "/?error=appointment_limit#appointments"
        assert "Запланировано уже 30 событий" in client.get("/?error=appointment_limit").text

    def test_csrf(self, app_client, settings) -> None:  # noqa: F811
        client, _ = app_client
        _register(client)
        response = client.post("/appointments", data=_form(client, csrf_token="forged"))
        assert response.status_code == 403
        assert _sql(settings, "SELECT COUNT(*) FROM appointments") == [(0,)]


class TestChange:
    def test_update(self, app_client, settings) -> None:  # noqa: F811
        client, user_id = app_client
        _register(client)
        client.post("/appointments", data=_form(client))
        (appt_id,) = _sql(settings, "SELECT id FROM appointments")[0]
        new_day = (TODAY + timedelta(days=10)).isoformat()
        response = client.post(f"/appointments/{appt_id}", data=_form(client, title="Терапевт", date=new_day, time="", note=""))
        assert _location(response) == "/?saved=appointment#appointments"
        assert _sql(settings, "SELECT title, starts_on, starts_at, note, sent_eve_at FROM appointments") == [
            ("Терапевт", new_day, None, "", None)
        ]
        same = client.post(f"/appointments/{appt_id}", data=_form(client, title="Терапевт", date=new_day, time="", note="к 9"))
        assert _location(same) == "/?saved=appointment#appointments"
        assert _sql(settings, "SELECT note FROM appointments") == [("к 9",)]

    def test_update_invalid_or_past(self, app_client, settings) -> None:  # noqa: F811
        client, user_id = app_client
        _register(client)
        past = _insert(settings, user_id, -1)
        assert _location(client.post(f"/appointments/{past}", data=_form(client))) == "/?error=appointment#appointments"
        client.post("/appointments", data=_form(client))
        (upcoming,) = _sql(settings, "SELECT id FROM appointments WHERE title = 'Кардиолог'")[0]
        assert _location(client.post(f"/appointments/{upcoming}", data=_form(client, title=""))) == "/?error=appointment#appointments"

    def test_cancel(self, app_client, settings) -> None:  # noqa: F811
        client, user_id = app_client
        _register(client)
        upcoming = _insert(settings, user_id, 2)
        past = _insert(settings, user_id, -1)
        token = {"csrf_token": _csrf(client)}
        assert _location(client.post(f"/appointments/{upcoming}/cancel", data=token)) == "/?saved=appointment#appointments"
        assert _location(client.post(f"/appointments/{past}/cancel", data=token)) == "/?error=appointment#appointments"
        assert _sql(settings, "SELECT status FROM appointments ORDER BY id") == [("cancelled",), ("planned",)]

    def test_status(self, app_client, settings) -> None:  # noqa: F811
        client, user_id = app_client
        _register(client)
        waiting = _insert(settings, user_id, -1)
        old = _insert(settings, user_id, -5)
        upcoming = _insert(settings, user_id, 2)
        page = client.get("/").text
        assert "ждёт отметки" in page and "без отметки" in page and "✓ Был(а)" in page
        ok = client.post(f"/appointments/{waiting}/status", data={"csrf_token": _csrf(client), "status": "done"})
        assert _location(ok) == "/?saved=appointment#appointments"
        for target, status in ((old, "missed"), (upcoming, "done"), (waiting, "missed"), (old, "lost")):
            bad = client.post(f"/appointments/{target}/status", data={"csrf_token": _csrf(client), "status": status})
            assert _location(bad) == "/?error=appointment#appointments"
        assert _sql(settings, "SELECT status FROM appointments ORDER BY id") == [("done",), ("planned",), ("planned",)]

    def test_moved(self, app_client, settings) -> None:  # noqa: F811
        client, user_id = app_client
        _register(client)
        waiting = _insert(settings, user_id, -1, title="ЭКГ")
        day = (TODAY + timedelta(days=4)).isoformat()
        response = client.post(f"/appointments/{waiting}/moved", data={"csrf_token": _csrf(client), "date": day, "time": "09:15"})
        assert _location(response) == "/?saved=appointment#appointments"
        assert _sql(settings, "SELECT title, starts_on, starts_at, status FROM appointments ORDER BY id") == [
            ("ЭКГ", (TODAY - timedelta(days=1)).isoformat(), "10:00", "moved"),
            ("ЭКГ", day, "09:15", "planned"),
        ]
        again = client.post(f"/appointments/{waiting}/moved", data={"csrf_token": _csrf(client), "date": day, "time": ""})
        assert _location(again) == "/?error=appointment#appointments"
        bad_date = _insert(settings, user_id, -1, title="УЗИ")
        bad = client.post(f"/appointments/{bad_date}/moved", data={"csrf_token": _csrf(client), "date": "x", "time": ""})
        assert _location(bad) == "/?error=appointment#appointments"

    def test_moved_limit(self, app_client, settings) -> None:  # noqa: F811
        client, user_id = app_client
        _register(client)
        waiting = _insert(settings, user_id, -1)
        for index in range(30):
            _insert(settings, user_id, 5, title=f"В{index}")
        day = (TODAY + timedelta(days=4)).isoformat()
        response = client.post(f"/appointments/{waiting}/moved", data={"csrf_token": _csrf(client), "date": day, "time": ""})
        assert _location(response) == "/?error=appointment_limit#appointments"
        assert _sql(settings, "SELECT status FROM appointments WHERE id = ?", (waiting,)) == [("planned",)]

    def test_moved_race(self, app_client, settings, mocker) -> None:  # noqa: F811
        from bot.services import appointments as ap

        client, user_id = app_client
        _register(client)
        waiting = _insert(settings, user_id, -1)
        mocker.patch.object(ap, "moved_after", mocker.AsyncMock(return_value=None))
        day = (TODAY + timedelta(days=4)).isoformat()
        response = client.post(f"/appointments/{waiting}/moved", data={"csrf_token": _csrf(client), "date": day, "time": ""})
        assert _location(response) == "/?error=appointment#appointments"

    def test_delete_and_isolation(self, app_client, settings) -> None:  # noqa: F811
        client, user_id = app_client
        _register(client)
        own = _insert(settings, user_id, 2)
        con = sqlite3.connect(settings.db_path)
        con.execute(
            "INSERT INTO users (telegram_id, timezone, target_systolic, target_diastolic, med_remeasure_delay,"
            " onboarded, created_at) VALUES (999, 'Europe/Minsk', 130, 80, 60, 1, '2026-01-01 00:00:00')"
        )
        other_id = con.execute("SELECT id FROM users WHERE telegram_id = 999").fetchone()[0]
        con.commit()
        con.close()
        foreign = _insert(settings, other_id, 2, title="Чужое")
        token = {"csrf_token": _csrf(client)}
        for path in (f"/appointments/{foreign}", f"/appointments/{foreign}/cancel", f"/appointments/{foreign}/status",
                     f"/appointments/{foreign}/moved", f"/appointments/{foreign}/delete"):
            assert client.post(path, data={**token, "title": "x", "date": TODAY.isoformat(), "status": "done"}).status_code == 404
        assert "Чужое" not in client.get("/").text
        token = {"csrf_token": _csrf(client)}  # страница выдала новый CSRF
        assert _location(client.post(f"/appointments/{own}/delete", data=token)) == "/?saved=appointment#appointments"
        assert _sql(settings, "SELECT title FROM appointments") == [("Чужое",)]
