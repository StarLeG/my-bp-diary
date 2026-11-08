"""Записи измерений и импорт CSV в личном кабинете (SPEC.md, §16.2).

Покрывается: добавление/правка/удаление записей (момент ≤ текущего, дубль за
минуту — «Это давление уже записано», удаление с подтверждением) и импорт CSV
(разбор в потоке, отчёт «Импортировано…», двойная отправка — все дубли, §7.2 п.4).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from bot.config import Settings
from web.cabinet import auth
from web.cabinet.app import create_app

FAKE_TOKEN = f"123456:{'A' * 35}"
SECRET = "test-secret"
TG_ID = 111_111
TZ = "Europe/Minsk"

CSV_HEADER = "timestamp_local;systolic;diastolic;pulse;medication_taken;feeling;note"


@pytest.fixture
def settings(tmp_path) -> Settings:
    """Настройки кабинета с тестовым секретом и файловой SQLite."""
    return Settings(
        bot_token=FAKE_TOKEN,
        web_secret_key=SECRET,
        db_path=str(tmp_path / "cabinet.sqlite3"),
    )


@pytest.fixture
def app_client(settings: Settings):
    """TestClient с готовой схемой и пользователем бота в users.

    Пользователь вставляется синхронным sqlite3 после init_db (lifespan):
    так данные создаются без конфликта event loop'ов TestClient и pytest.
    """
    app = create_app(settings)
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as client:
        con = sqlite3.connect(settings.db_path)
        con.execute(
            "INSERT INTO users (telegram_id, timezone, target_systolic, target_diastolic,"
            " med_remeasure_delay, onboarded, created_at)"
            " VALUES (?, ?, 130, 80, 60, 1, '2026-01-01 00:00:00')",
            (TG_ID, TZ),
        )
        con.commit()
        user_id = con.execute("SELECT id FROM users WHERE telegram_id = ?", (TG_ID,)).fetchone()[0]
        con.close()
        yield client, user_id


def _csrf(client: TestClient) -> str:
    """CSRF-токен из cookie (его же клиент кладёт в hidden-поле формы)."""
    return client.cookies.get("csrf_token", "")


def _register(client: TestClient) -> None:
    """Завести учётную запись кабинета по magic link (email/пароль по умолчанию)."""
    magic = auth.MagicLink(SECRET)
    client.get(f"/register?token={magic.make(TG_ID)}")
    client.post(
        "/register",
        data={
            "csrf_token": _csrf(client),
            "token": magic.make(TG_ID),
            "email": "user@example.org",
            "password": "secret-pass",
            "password_confirm": "secret-pass",
        },
    )


def _form(**overrides) -> dict:
    """Форма записи: вчерашний момент в таймзоне пользователя (§16.2)."""
    yesterday = datetime.now(ZoneInfo(TZ)).date() - timedelta(days=1)
    form = {
        "csrf_token": "",
        "date": yesterday.isoformat(),
        "time": "08:00",
        "systolic": "135",
        "diastolic": "85",
        "pulse": "72",
        "feeling": "",
        "feeling_other": "",
        "note": "",
    }
    form.update(overrides)
    return form


def _add(client: TestClient, **overrides) -> None:
    """Добавить запись формой (с CSRF-токеном из cookie)."""
    form = _form(**overrides)
    form["csrf_token"] = _csrf(client)
    response = client.post("/measurements", data=form)
    assert response.status_code == 303, response.text


def _ids(settings: Settings) -> list[tuple[int, str]]:
    """(id, timestamp-строка) всех записей по порядку — для проверок БД."""
    con = sqlite3.connect(settings.db_path)
    rows = con.execute("SELECT id, timestamp FROM measurements ORDER BY id").fetchall()
    con.close()
    return [(int(row[0]), str(row[1])) for row in rows]


def _sql(settings: Settings, query: str, args: tuple = ()) -> list[tuple]:
    """Синхронный SELECT к файлу БД — вне event loop'а TestClient."""
    con = sqlite3.connect(settings.db_path)
    rows = con.execute(query, args).fetchall()
    con.close()
    return rows


def csv_file(*rows: str) -> bytes:
    """Файл импорта: UTF-8 (BOM допустим), разделитель «;», CRLF (§10)."""
    return ("\r\n".join([CSV_HEADER, *rows]) + "\r\n").encode("utf-8-sig")


# ─────────────────────────── Доступ ───────────────────────────


class TestAccess:
    def test_forms_require_login(self, app_client) -> None:
        client, _ = app_client
        for url in ("/?add=1#history", "/?import=1#history"):
            response = client.get(url)
            assert response.status_code == 303
            assert response.headers["location"] == "/login"

    def test_post_requires_login(self, app_client) -> None:
        client, _ = app_client
        response = client.post("/measurements", data=_form(csrf_token="x"))
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    def test_post_requires_csrf(self, app_client, settings) -> None:
        client, _ = app_client
        _register(client)
        response = client.post("/measurements", data=_form(csrf_token=""))
        assert response.status_code == 403
        assert _ids(settings) == []


# ─────────────────────────── Добавление ───────────────────────────


class TestAddMeasurement:
    def test_add_form_renders(self, app_client) -> None:
        client, _ = app_client
        _register(client)
        response = client.get("/?add=1#history")
        assert response.status_code == 200
        assert "Добавить запись" in response.text
        assert 'action="/measurements"' in response.text
        assert "Принял лекарство" in response.text

    def test_add_saves_row(self, app_client, settings) -> None:
        client, user_id = app_client
        _register(client)
        response = client.post("/measurements", data=_form(csrf_token=_csrf(client)))
        assert response.status_code == 303
        assert response.headers["location"] == "/?saved=measurement#history"
        rows = _sql(
            settings,
            "SELECT systolic, diastolic, pulse, medication_taken, feeling, note"
            " FROM measurements WHERE user_id = ?",
            (user_id,),
        )
        assert rows == [(135, 85, 72, None, None, None)]
        # Момент — вчерашние 08:00 по таймзоне пользователя (UTC+3) = 05:00 UTC.
        yesterday = datetime.now(ZoneInfo(TZ)).date() - timedelta(days=1)
        assert _ids(settings)[0][1].startswith(f"{yesterday.isoformat()} 05:00:00")

    def test_add_with_optional_fields(self, app_client, settings) -> None:
        client, user_id = app_client
        _register(client)
        form = _form(
            csrf_token=_csrf(client),
            medication_taken="1",
            feeling="other",
            feeling_other="лёгкое недомогание",
        )
        response = client.post("/measurements", data=form)
        assert response.status_code == 303
        rows = _sql(
            settings,
            "SELECT medication_taken, feeling, note FROM measurements WHERE user_id = ?",
            (user_id,),
        )
        assert rows == [(1, "other", "лёгкое недомогание")]

    def test_add_note_kept_with_feeling_other(self, app_client, settings) -> None:
        client, user_id = app_client
        _register(client)
        form = _form(
            csrf_token=_csrf(client), feeling="other",
            feeling_other="тошнота", note="после обеда",
        )
        client.post("/measurements", data=form)
        rows = _sql(settings, "SELECT note FROM measurements WHERE user_id = ?", (user_id,))
        assert rows == [("после обеда\nтошнота",)]

    def test_add_without_pulse(self, app_client, settings) -> None:
        client, user_id = app_client
        _register(client)
        client.post("/measurements", data=_form(csrf_token=_csrf(client), pulse=""))
        rows = _sql(settings, "SELECT pulse FROM measurements WHERE user_id = ?", (user_id,))
        assert rows == [(None,)]

    @pytest.mark.parametrize(
        "overrides",
        [
            {"date": "2999-01-01"},  # будущее (§9)
            {"systolic": "300"},  # вне диапазона
            {"diastolic": "140"},  # не ниже систолического
            {"time": "25:00"},
            {"date": "абырвалг"},
            {"systolic": "сто"},
        ],
    )
    def test_add_invalid_redirects_error(self, app_client, settings, overrides) -> None:
        client, _ = app_client
        _register(client)
        response = client.post(
            "/measurements", data=_form(csrf_token=_csrf(client), **overrides)
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/?error=measurement#history"
        assert _ids(settings) == []

    def test_error_reopens_form(self, app_client) -> None:
        client, _ = app_client
        _register(client)
        response = client.get("/?error=measurement#history")
        assert response.status_code == 200
        assert "Добавить запись" in response.text
        assert "Не удалось сохранить измерение" in response.text

    def test_duplicate_minute_rejected(self, app_client, settings) -> None:
        client, _ = app_client
        _register(client)
        _add(client)
        response = client.post("/measurements", data=_form(csrf_token=_csrf(client)))
        assert response.headers["location"] == "/?error=duplicate#history"
        assert len(_ids(settings)) == 1

    def test_double_submit_creates_one_row(self, app_client, settings) -> None:
        """Двойная отправка формы не создаёт дублей (§7.2 п.4)."""
        client, _ = app_client
        _register(client)
        form = _form(csrf_token=_csrf(client))
        first = client.post("/measurements", data=form)
        second = client.post("/measurements", data=form)
        assert first.headers["location"] == "/?saved=measurement#history"
        assert second.headers["location"] == "/?error=duplicate#history"
        assert len(_ids(settings)) == 1


# ─────────────────────────── Правка ───────────────────────────


class TestEditMeasurement:
    def test_edit_form_prefilled(self, app_client, settings) -> None:
        client, _ = app_client
        _register(client)
        _add(client, systolic="150", diastolic="95", note="после кофе")
        measurement_id, _ = _ids(settings)[0]
        response = client.get(f"/?edit={measurement_id}#history")
        assert response.status_code == 200
        assert "Изменить запись" in response.text
        assert f'action="/measurements/{measurement_id}"' in response.text
        assert 'value="150"' in response.text
        assert 'value="95"' in response.text
        assert "после кофе" in response.text

    def test_edit_updates_values_and_moment(self, app_client, settings) -> None:
        client, user_id = app_client
        _register(client)
        _add(client)
        measurement_id, _ = _ids(settings)[0]
        response = client.post(
            f"/measurements/{measurement_id}",
            data=_form(
                csrf_token=_csrf(client), date="2026-01-15", time="21:30",
                systolic="140", diastolic="90", pulse="",
            ),
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/?saved=measurement#history"
        rows = _sql(
            settings,
            "SELECT timestamp, systolic, diastolic, pulse FROM measurements WHERE id = ?",
            (measurement_id,),
        )
        # 21:30 Europe/Minsk (UTC+3) = 18:30 UTC
        assert rows[0][0].startswith("2026-01-15 18:30:00")
        assert rows[0][1:] == (140, 90, None)

    @pytest.mark.parametrize(
        ("stored", "checked", "expected"),
        [
            (0, False, 0),  # «нет» из бота не затирается снятым чекбоксом
            (0, True, 1),
            (1, False, None),  # снятый чекбокс снимает «да»
            (None, False, None),
            (None, True, 1),
        ],
    )
    def test_edit_medication_flag(
        self, app_client, settings, stored, checked, expected
    ) -> None:
        """Флаг «Принял лекарство» трёхзначен, чекбокс — двузначен (§16.2)."""
        client, _ = app_client
        _register(client)
        _add(client)
        measurement_id, _ = _ids(settings)[0]
        con = sqlite3.connect(settings.db_path)
        con.execute(
            "UPDATE measurements SET medication_taken = ? WHERE id = ?",
            (stored, measurement_id),
        )
        con.commit()
        con.close()
        form = _form(csrf_token=_csrf(client), pulse="75")
        if checked:
            form["medication_taken"] = "1"
        response = client.post(f"/measurements/{measurement_id}", data=form)
        assert response.headers["location"] == "/?saved=measurement#history"
        rows = _sql(
            settings,
            "SELECT medication_taken, pulse FROM measurements WHERE id = ?",
            (measurement_id,),
        )
        assert rows[0] == (expected, 75)

    def test_edit_duplicate_moment_rejected(self, app_client, settings) -> None:
        client, _ = app_client
        _register(client)
        _add(client, time="08:00")
        _add(client, time="09:00")
        second_id, _ = _ids(settings)[1]
        response = client.post(
            f"/measurements/{second_id}",
            data=_form(csrf_token=_csrf(client), time="08:00"),
        )
        assert response.headers["location"] == f"/?edit={second_id}&error=duplicate#history"
        rows = _sql(settings, "SELECT timestamp FROM measurements WHERE id = ?", (second_id,))
        assert " 06:00:00" in rows[0][0]  # 09:00 МСК — не изменилось

    @pytest.mark.parametrize(
        ("overrides", "error"),
        [({"systolic": "999"}, "measurement"), ({"time": "08:00"}, "duplicate")],
    )
    def test_edit_error_reopens_edit_form(
        self, app_client, settings, overrides, error
    ) -> None:
        """Ошибка правки возвращает на форму правки той же записи, не добавления."""
        client, _ = app_client
        _register(client)
        _add(client, time="08:00")
        _add(client, time="09:00")
        second_id, _ = _ids(settings)[1]
        response = client.post(
            f"/measurements/{second_id}",
            data=_form(csrf_token=_csrf(client), **overrides),
        )
        location = response.headers["location"]
        assert location == f"/?edit={second_id}&error={error}#history"
        page = client.get(location)
        assert "Изменить запись" in page.text
        assert f'action="/measurements/{second_id}"' in page.text

    def test_edit_own_moment_is_not_duplicate(self, app_client, settings) -> None:
        """Проверка дубля не считает саму правимую запись (§16.2)."""
        client, _ = app_client
        _register(client)
        _add(client, time="08:00")
        measurement_id, _ = _ids(settings)[0]
        response = client.post(
            f"/measurements/{measurement_id}",
            data=_form(csrf_token=_csrf(client), time="08:30"),
        )
        assert response.headers["location"] == "/?saved=measurement#history"

    def test_edit_missing_id_redirects_error(self, app_client, settings) -> None:
        client, _ = app_client
        _register(client)
        _add(client)
        response = client.post("/measurements/999", data=_form(csrf_token=_csrf(client)))
        assert response.headers["location"] == "/?error=measurement#history"
        rows = _sql(settings, "SELECT systolic FROM measurements")
        assert rows == [(135,)]

    def test_edit_unknown_id_renders_no_form(self, app_client) -> None:
        client, _ = app_client
        _register(client)
        response = client.get("/?edit=999#history")
        assert response.status_code == 200
        assert "Изменить запись" not in response.text


# ─────────────────────────── Удаление ───────────────────────────


class TestDeleteMeasurement:
    def test_confirm_screen(self, app_client, settings) -> None:
        client, _ = app_client
        _register(client)
        _add(client)
        measurement_id, _ = _ids(settings)[0]
        response = client.get(f"/?confirm_delete={measurement_id}#history")
        assert response.status_code == 200
        assert "Удалить запись? Это действие нельзя отменить." in response.text
        assert f'action="/measurements/{measurement_id}/delete"' in response.text
        assert "Отмена" in response.text

    def test_delete_removes_row(self, app_client, settings) -> None:
        client, _ = app_client
        _register(client)
        _add(client)
        measurement_id, _ = _ids(settings)[0]
        response = client.post(
            f"/measurements/{measurement_id}/delete", data={"csrf_token": _csrf(client)}
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/?saved=measurement#history"
        assert _ids(settings) == []

    def test_double_delete_is_idempotent(self, app_client, settings) -> None:
        client, _ = app_client
        _register(client)
        _add(client)
        measurement_id, _ = _ids(settings)[0]
        for _ in range(2):
            response = client.post(
                f"/measurements/{measurement_id}/delete", data={"csrf_token": _csrf(client)}
            )
            assert response.status_code == 303
            assert response.headers["location"] == "/?saved=measurement#history"
        assert _ids(settings) == []

    def test_foreign_delete_is_noop(self, app_client, settings) -> None:
        """Чужая запись выглядит как отсутствующая и не удаляется (§7.1)."""
        client, _ = app_client
        _register(client)
        con = sqlite3.connect(settings.db_path)
        con.execute(
            "INSERT INTO users (telegram_id, timezone, target_systolic, target_diastolic,"
            " med_remeasure_delay, onboarded, created_at)"
            " VALUES (222222, 'Europe/Minsk', 130, 80, 60, 1, '2026-01-01 00:00:00')"
        )
        con.execute(
            "INSERT INTO measurements (user_id, timestamp, systolic, diastolic)"
            " SELECT id, '2026-01-15 08:00:00', 111, 70 FROM users WHERE telegram_id = 222222"
        )
        con.commit()
        foreign_id = con.execute("SELECT id FROM measurements").fetchone()[0]
        con.close()
        response = client.post(
            f"/measurements/{foreign_id}/delete", data={"csrf_token": _csrf(client)}
        )
        assert response.status_code == 303
        rows = _sql(settings, "SELECT systolic FROM measurements WHERE id = ?", (foreign_id,))
        assert rows == [(111,)]  # чужая запись на месте

    def test_confirm_foreign_id_shows_nothing(self, app_client) -> None:
        client, _ = app_client
        _register(client)
        response = client.get("/?confirm_delete=999#history")
        assert response.status_code == 200
        assert "Удалить запись?" not in response.text


# ─────────────────────────── Импорт CSV ───────────────────────────


class TestImportCsv:
    def test_import_form_renders(self, app_client) -> None:
        client, _ = app_client
        _register(client)
        response = client.get("/?import=1#history")
        assert response.status_code == 200
        assert "Импорт CSV" in response.text
        assert 'action="/import"' in response.text

    def test_import_reports_and_saves(self, app_client, settings) -> None:
        client, user_id = app_client
        _register(client)
        response = client.post(
            "/import",
            data={"csrf_token": _csrf(client), "days": "7", "date_from": "", "date_to": ""},
            files={"file": ("archive.csv", csv_file(
                "12.03.2024 08:30;135;85;72;да;норма;после кофе",
                "13.03.2024 09:00;120;80",
            ), "text/csv")},
        )
        assert response.status_code == 200
        assert "Импортировано: 2 · Пропущено дублей: 0 · Ошибок: 0" in response.text
        rows = _sql(settings, "SELECT COUNT(*) FROM measurements WHERE user_id = ?", (user_id,))
        assert rows == [(2,)]

    def test_import_too_large_file_rejected(
        self, app_client, settings, monkeypatch
    ) -> None:
        """Файл больше лимита байт — отказ без разбора и без вставки."""
        from web.cabinet import csv_import

        client, user_id = app_client
        _register(client)
        payload = csv_file("12.03.2024 08:30;135;85")
        monkeypatch.setattr(csv_import, "MAX_IMPORT_BYTES", len(payload) - 1)
        response = client.post(
            "/import",
            data={"csrf_token": _csrf(client)},
            files={"file": ("archive.csv", payload, "text/csv")},
        )
        assert response.status_code == 200
        assert csv_import.FILE_TOO_MANY_ROWS in response.text
        rows = _sql(settings, "SELECT COUNT(*) FROM measurements WHERE user_id = ?", (user_id,))
        assert rows == [(0,)]

    def test_import_row_errors_reported(self, app_client) -> None:
        client, _ = app_client
        _register(client)
        response = client.post(
            "/import",
            data={"csrf_token": _csrf(client)},
            files={"file": ("archive.csv", csv_file(
                "12.03.2024 08:30;135;85",
                "абырвалг;135;85",
            ), "text/csv")},
        )
        assert response.status_code == 200
        assert "Импортировано: 1 · Пропущено дублей: 0 · Ошибок: 1" in response.text
        assert "Строка 3: дата и время не распознаны" in response.text

    def test_double_submit_all_duplicates(self, app_client, settings) -> None:
        """Повторная отправка того же файла: все строки — дубли (§7.2 п.4)."""
        client, user_id = app_client
        _register(client)
        payload = {"csrf_token": _csrf(client)}
        upload = ("archive.csv", csv_file("12.03.2024 08:30;135;85"), "text/csv")
        first = client.post("/import", data=payload, files={"file": upload})
        assert "Импортировано: 1 · Пропущено дублей: 0 · Ошибок: 0" in first.text
        # Страница отдала свежий CSRF-токен — им же браузер повторит отправку.
        payload = {"csrf_token": _csrf(client)}
        second = client.post("/import", data=payload, files={"file": upload})
        assert "Импортировано: 0 · Пропущено дублей: 1 · Ошибок: 0" in second.text
        rows = _sql(settings, "SELECT COUNT(*) FROM measurements WHERE user_id = ?", (user_id,))
        assert rows == [(1,)]

    def test_import_without_file(self, app_client) -> None:
        client, _ = app_client
        _register(client)
        response = client.post("/import", data={"csrf_token": _csrf(client)})
        assert response.status_code == 200
        assert "Выберите файл." in response.text

    def test_import_bad_header(self, app_client) -> None:
        client, _ = app_client
        _register(client)
        response = client.post(
            "/import",
            data={"csrf_token": _csrf(client)},
            files={"file": ("archive.csv", b"when;sys\r\n", "text/csv")},
        )
        assert response.status_code == 200
        assert "нет колонок" in response.text

    def test_import_not_utf8(self, app_client) -> None:
        client, _ = app_client
        _register(client)
        data = "timestamp_local;systolic;diastolic".encode("utf-16")
        response = client.post(
            "/import",
            data={"csrf_token": _csrf(client)},
            files={"file": ("archive.csv", data, "text/csv")},
        )
        assert response.status_code == 200
        assert "Файл должен быть в кодировке UTF-8" in response.text

    def test_import_too_many_rows(self, app_client) -> None:
        client, _ = app_client
        _register(client)
        rows = ["12.03.2024 08:30;135;85"] * 5001
        response = client.post(
            "/import",
            data={"csrf_token": _csrf(client)},
            files={"file": ("archive.csv", csv_file(*rows), "text/csv")},
        )
        assert response.status_code == 200
        assert "Слишком много строк, разделите файл" in response.text

    def test_import_keeps_period_in_form(self, app_client) -> None:
        """Форма импорта несёт текущий период — страница открывается в нём же."""
        client, _ = app_client
        _register(client)
        response = client.post(
            "/import",
            data={"csrf_token": _csrf(client), "days": "30", "date_from": "", "date_to": ""},
            files={"file": ("archive.csv", csv_file("12.03.2024 08:30;135;85"), "text/csv")},
        )
        assert response.status_code == 200
        assert '<option value="30" selected>' in response.text


# ─────────────────────── Выбор лекарств в форме записи (§16.2) ───────────────────────


def _add_medication(client: TestClient, settings: Settings, name: str, *slots: str) -> dict[str, int]:
    """Добавить лекарство формой кабинета; {слот: id строки}."""
    data = {"csrf_token": _csrf(client), "name": name, "dosage": "50 мг"}
    data.update({f"slot_{slot}": "1" for slot in slots})
    response = client.post("/medications", data=data)
    assert response.headers["location"] == "/?saved=medication#medications"
    rows = _sql(settings, "SELECT slot, id FROM medications WHERE name = ?", (name,))
    return {str(slot): int(med_id) for slot, med_id in rows}


def _set_reminder(settings: Settings, user_id: int, slot: str, local_time: str, enabled: bool) -> None:
    """Строка напоминания слота (источник времени предотметки, §16.2)."""
    con = sqlite3.connect(settings.db_path)
    con.execute(
        "INSERT INTO reminders (user_id, type, local_time, next_trigger_time, enabled)"
        " VALUES (?, ?, ?, '2030-01-01 00:00:00.000000', ?)",
        (user_id, slot, local_time, int(enabled)),
    )
    con.commit()
    con.close()


def _links(settings: Settings) -> list[tuple[int, int]]:
    return sorted(
        (int(a), int(b))
        for a, b in _sql(settings, "SELECT measurement_id, medication_id FROM measurement_medications")
    )


def _med_logs(settings: Settings) -> list[tuple[int, str, str, str]]:
    """(medication_id, timestamp, status, slot) журнала по порядку."""
    return [
        (int(row[0]), str(row[1]), str(row[2]), str(row[3]))
        for row in _sql(
            settings,
            "SELECT medication_id, timestamp, status, slot FROM medication_logs"
            " ORDER BY timestamp, medication_id",
        )
    ]


def _checked_ids(html: str) -> set[int]:
    """Id отмеченных чекбоксов `medication_ids` в разметке формы."""
    import re

    return {
        int(match.group(1))
        for match in re.finditer(
            r'name="medication_ids" value="(\d+)"\s+data-slot="\w+"\s+checked', html
        )
    }


def _hhmm_shift(hours: int) -> str:
    """Текущее локальное время пользователя со сдвигом, «ЧЧ:ММ»."""
    return (datetime.now(ZoneInfo(TZ)) + timedelta(hours=hours)).strftime("%H:%M")


class TestPrefillSlot:
    """Ближайший слот в пределах ±2 ч (§16.2, N1) — та же логика, что в inline-скрипте."""

    SLOTS = [("morning", "08:00"), ("noon", "13:00"), ("evening", "21:00")]

    @pytest.mark.parametrize(
        ("hhmm", "expected"),
        [
            ("08:00", "morning"),
            ("06:00", "morning"),  # граница окна включительно
            ("05:59", None),  # за окном
            ("11:30", "noon"),  # обед в окне (1:30), утро — нет (3:30)
            ("16:00", None),  # между обедом и вечером — ни одного в окне
            ("23:00", "evening"),
            ("02:00", None),
        ],
    )
    def test_nearest_in_window(self, hhmm: str, expected) -> None:
        from web.cabinet.app import prefill_slot

        assert prefill_slot(hhmm, self.SLOTS) == expected

    def test_window_wraps_midnight(self) -> None:
        """Расстояние — по кругу суток: 00:30 — час до вечернего слота в 23:30."""
        from web.cabinet.app import prefill_slot

        assert prefill_slot("00:30", [("morning", "08:00"), ("evening", "23:30")]) == "evening"

    def test_tie_prefers_earlier_slot(self) -> None:
        from web.cabinet.app import prefill_slot

        assert prefill_slot("09:00", [("morning", "08:00"), ("noon", "10:00")]) == "morning"

    def test_slot_times_fallback_to_defaults(self) -> None:
        """Время включённого напоминания; выключенное или отсутствующее — умолчание."""
        from types import SimpleNamespace

        from web.cabinet.app import _slot_times

        reminders = {
            "morning": SimpleNamespace(enabled=True, local_time="07:15"),
            "noon": SimpleNamespace(enabled=False, local_time="11:40"),
        }
        assert _slot_times(reminders) == [
            ("morning", "07:15"),
            ("noon", "13:00"),
            ("evening", "21:00"),
        ]


class TestMedicationPickerRender:
    def test_groups_by_slot(self, app_client, settings) -> None:
        client, _ = app_client
        _register(client)
        losartan = _add_medication(client, settings, "Лозартан", "morning", "evening")
        crisis = _add_medication(client, settings, "Каптоприл", "crisis")
        html = client.get("/?add=1#history").text
        picker = html[html.index('class="med-picker"'):html.index("Самочувствие")]
        for slot_label in ("Утро", "Вечер", "Кризис"):
            assert f'<legend class="med-group-title">{slot_label}</legend>' in picker
        assert "Обед" not in picker, "пустые группы не показываются"
        for med_id in (*losartan.values(), *crisis.values()):
            assert f'name="medication_ids" value="{med_id}"' in picker
        assert "Лозартан (50 мг)" in picker

    def test_no_medications_hint(self, app_client) -> None:
        client, _ = app_client
        _register(client)
        html = client.get("/?add=1#history").text
        assert "У вас пока нет добавленных лекарств." in html
        assert '<a href="#medications">Перейти в Управление лекарствами</a>' in html

    def test_hidden_without_flag_css(self, app_client) -> None:
        """Блок скрывается CSS :has(), пока галочка «Принял лекарство» снята."""
        client, _ = app_client
        css = client.get("/static/style.css").text
        assert 'form:not(:has(input[name="medication_taken"]:checked)) .med-picker' in css

    def test_prefill_by_current_slot(self, app_client, settings) -> None:
        """Форма добавления: отмечены лекарства слота, чьё время совпадает с текущим."""
        client, user_id = app_client
        _register(client)
        meds = _add_medication(client, settings, "Лозартан", "morning", "noon", "crisis")
        _set_reminder(settings, user_id, "morning", _hhmm_shift(0), enabled=True)
        html = client.get("/?add=1#history").text
        assert _checked_ids(html) == {meds["morning"]}, "только утро; «Кризис» — никогда"
        assert "data-prefill=" in html and "data-prefill-window=\"120\"" in html
        assert 'getElementById("measurement-form")' in html, "inline-скрипт пересчёта"

    def test_no_prefill_outside_window(self, app_client, settings) -> None:
        client, user_id = app_client
        _register(client)
        _add_medication(client, settings, "Лозартан", "morning", "noon", "evening")
        for slot, shift in (("morning", 5), ("noon", 10), ("evening", 15)):
            _set_reminder(settings, user_id, slot, _hhmm_shift(shift), enabled=True)
        html = client.get("/?add=1#history").text
        assert _checked_ids(html) == set()

    def test_disabled_reminder_uses_default_time(self, app_client, settings) -> None:
        """Выключенное напоминание не даёт своего времени для предотметки (умолчание)."""
        client, user_id = app_client
        _register(client)
        meds = _add_medication(client, settings, "Лозартан", "morning")
        _set_reminder(settings, user_id, "morning", _hhmm_shift(0), enabled=False)
        html = client.get("/?add=1#history").text
        from web.cabinet.app import prefill_slot

        default_slot = prefill_slot(
            _hhmm_shift(0), [("morning", "08:00"), ("noon", "13:00"), ("evening", "21:00")]
        )
        expected = {meds["morning"]} if default_slot == "morning" else set()
        assert _checked_ids(html) == expected

    def test_edit_shows_current_links_without_prefill(self, app_client, settings) -> None:
        client, user_id = app_client
        _register(client)
        meds = _add_medication(client, settings, "Лозартан", "morning", "evening")
        _set_reminder(settings, user_id, "morning", _hhmm_shift(0), enabled=True)
        _add(client, medication_taken="1", medication_ids=[str(meds["evening"])])
        measurement_id = _ids(settings)[0][0]
        html = client.get(f"/?edit={measurement_id}#history").text
        assert _checked_ids(html) == {meds["evening"]}
        assert "data-prefill=" not in html
        assert 'getElementById("measurement-form")' not in html

    def test_missing_error_message(self, app_client) -> None:
        client, _ = app_client
        _register(client)
        html = client.get("/?error=medication_missing#history").text
        assert "Лекарство не найдено." in html
        assert "Добавить запись" in html, "форма добавления открыта снова"


class TestMedicationPickerAdd:
    def test_links_and_log_at_measurement_moment(self, app_client, settings) -> None:
        client, _ = app_client
        _register(client)
        meds = _add_medication(client, settings, "Лозартан", "morning", "crisis")
        _add(
            client,
            medication_taken="1",
            medication_ids=[str(meds["morning"]), str(meds["crisis"])],
        )
        ((measurement_id, moment),) = _ids(settings)
        assert _links(settings) == sorted(
            [(measurement_id, meds["morning"]), (measurement_id, meds["crisis"])]
        )
        assert _med_logs(settings) == sorted(
            [
                (meds["crisis"], moment, "taken", "crisis"),
                (meds["morning"], moment, "taken", "morning"),
            ],
            key=lambda row: (row[1], row[0]),
        )
        flag = _sql(settings, "SELECT medication_taken FROM measurements")
        assert flag == [(1,)]
        history = client.get("/?days=7#history").text
        assert "Принято: Лозартан 50 мг, Лозартан 50 мг" in history

    def test_selection_ignored_without_flag(self, app_client, settings) -> None:
        client, _ = app_client
        _register(client)
        meds = _add_medication(client, settings, "Лозартан", "morning")
        _add(client, medication_ids=[str(meds["morning"])])
        assert _links(settings) == [] and _med_logs(settings) == []
        assert _sql(settings, "SELECT medication_taken FROM measurements") == [(None,)]

    def test_flag_without_selection(self, app_client, settings) -> None:
        client, _ = app_client
        _register(client)
        _add_medication(client, settings, "Лозартан", "morning")
        _add(client, medication_taken="1")
        assert _links(settings) == [] and _med_logs(settings) == []
        assert _sql(settings, "SELECT medication_taken FROM measurements") == [(1,)]

    @pytest.mark.parametrize("bad_id", ["987654", "абв", "foreign"])
    def test_foreign_or_bad_id_rejected(self, app_client, settings, bad_id) -> None:
        client, _ = app_client
        _register(client)
        if bad_id == "foreign":
            con = sqlite3.connect(settings.db_path)
            con.execute(
                "INSERT INTO users (telegram_id, timezone, target_systolic, target_diastolic,"
                " med_remeasure_delay, onboarded, created_at)"
                " VALUES (222222, ?, 130, 80, 60, 1, '2026-01-01 00:00:00')",
                (TZ,),
            )
            other_id = con.execute("SELECT id FROM users WHERE telegram_id = 222222").fetchone()[0]
            con.execute(
                "INSERT INTO medications (user_id, name, dosage, slot, created_at)"
                " VALUES (?, 'Чужое', '', 'morning', '2026-01-01 00:00:00')",
                (other_id,),
            )
            con.commit()
            bad_id = str(con.execute("SELECT id FROM medications").fetchone()[0])
            con.close()
        form = _form(csrf_token=_csrf(client), medication_taken="1", medication_ids=[bad_id])
        response = client.post("/measurements", data=form)
        assert response.headers["location"] == "/?error=medication_missing#history"
        assert _ids(settings) == [] and _med_logs(settings) == []

    def test_double_submit_logs_once(self, app_client, settings) -> None:
        client, _ = app_client
        _register(client)
        meds = _add_medication(client, settings, "Лозартан", "morning")
        form = _form(csrf_token=_csrf(client), medication_taken="1", medication_ids=[str(meds["morning"])])
        first = client.post("/measurements", data=form)
        second = client.post("/measurements", data=form)
        assert first.headers["location"] == "/?saved=measurement#history"
        assert second.headers["location"] == "/?error=duplicate#history"
        assert len(_med_logs(settings)) == 1 and len(_links(settings)) == 1


class TestMedicationPickerEdit:
    def _edit(self, client: TestClient, measurement_id: int, **overrides):
        form = _form(**overrides)
        form["csrf_token"] = _csrf(client)
        return client.post(f"/measurements/{measurement_id}", data=form)

    def test_replace_selection(self, app_client, settings) -> None:
        """Добавленные галочки — связь и журнал; снятые — связь и журнал записи уходят."""
        client, _ = app_client
        _register(client)
        losartan = _add_medication(client, settings, "Лозартан", "morning")
        amlodipine = _add_medication(client, settings, "Амлодипин", "morning")
        _add(client, medication_taken="1", medication_ids=[str(losartan["morning"])])
        ((measurement_id, moment),) = _ids(settings)
        response = self._edit(
            client, measurement_id, medication_taken="1", medication_ids=[str(amlodipine["morning"])]
        )
        assert response.headers["location"] == "/?saved=measurement#history"
        assert _links(settings) == [(measurement_id, amlodipine["morning"])]
        assert _med_logs(settings) == [(amlodipine["morning"], moment, "taken", "morning")]

    def test_time_change_moves_log(self, app_client, settings) -> None:
        client, _ = app_client
        _register(client)
        meds = _add_medication(client, settings, "Лозартан", "morning")
        _add(client, medication_taken="1", medication_ids=[str(meds["morning"])])
        ((measurement_id, _),) = _ids(settings)
        two_days_ago = (datetime.now(ZoneInfo(TZ)).date() - timedelta(days=2)).isoformat()
        self._edit(
            client, measurement_id, date=two_days_ago, time="09:15",
            medication_taken="1", medication_ids=[str(meds["morning"])],
        )
        ((_, new_moment),) = _ids(settings)
        assert new_moment.startswith(f"{two_days_ago} 06:15:00")
        assert _med_logs(settings) == [(meds["morning"], new_moment, "taken", "morning")]
        assert _links(settings) == [(measurement_id, meds["morning"])]

    def test_unchecked_flag_removes_all(self, app_client, settings) -> None:
        client, _ = app_client
        _register(client)
        meds = _add_medication(client, settings, "Лозартан", "morning", "evening")
        _add(client, medication_taken="1", medication_ids=[str(m) for m in meds.values()])
        ((measurement_id, _),) = _ids(settings)
        # Чекбоксы лекарств остались отмеченными в скрытом блоке — сервер их не учитывает.
        self._edit(client, measurement_id, medication_ids=[str(m) for m in meds.values()])
        assert _links(settings) == [] and _med_logs(settings) == []
        assert _sql(settings, "SELECT medication_taken FROM measurements") == [(None,)]

    def test_bot_log_untouched(self, app_client, settings) -> None:
        """Журнал бота (момент «Готово», не момент записи) снятие связи не удаляет (N3 А)."""
        client, user_id = app_client
        _register(client)
        meds = _add_medication(client, settings, "Лозартан", "morning")
        _add(client)
        ((measurement_id, _),) = _ids(settings)
        con = sqlite3.connect(settings.db_path)
        con.execute(
            "INSERT INTO medication_logs (user_id, medication_id, timestamp, status, slot)"
            " VALUES (?, ?, '2026-01-02 03:04:05.000000', 'taken', 'morning')",
            (user_id, meds["morning"]),
        )
        con.execute(
            "INSERT INTO measurement_medications (measurement_id, medication_id) VALUES (?, ?)",
            (measurement_id, meds["morning"]),
        )
        con.commit()
        con.close()
        self._edit(client, measurement_id, medication_taken="1")
        assert _links(settings) == []
        assert _med_logs(settings) == [
            (meds["morning"], "2026-01-02 03:04:05.000000", "taken", "morning")
        ]

    def test_foreign_id_back_to_edit(self, app_client, settings) -> None:
        client, _ = app_client
        _register(client)
        meds = _add_medication(client, settings, "Лозартан", "morning")
        _add(client, medication_taken="1", medication_ids=[str(meds["morning"])])
        ((measurement_id, moment),) = _ids(settings)
        response = self._edit(
            client, measurement_id, systolic="150", medication_taken="1", medication_ids=["987654"]
        )
        assert (
            response.headers["location"]
            == f"/?edit={measurement_id}&error=medication_missing#history"
        )
        assert _sql(settings, "SELECT systolic FROM measurements") == [(135,)]
        assert _med_logs(settings) == [(meds["morning"], moment, "taken", "morning")]

    def test_duplicate_keeps_links(self, app_client, settings) -> None:
        """Дубль момента при правке: ничего не меняется, журнал на месте."""
        client, _ = app_client
        _register(client)
        meds = _add_medication(client, settings, "Лозартан", "morning")
        _add(client, time="07:00")
        _add(client, medication_taken="1", medication_ids=[str(meds["morning"])])
        second_id, moment = _ids(settings)[1]
        response = self._edit(client, second_id, time="07:00", medication_taken="1")
        assert response.headers["location"] == f"/?edit={second_id}&error=duplicate#history"
        assert _links(settings) == [(second_id, meds["morning"])]
        assert _med_logs(settings) == [(meds["morning"], moment, "taken", "morning")]

    def test_missing_record(self, app_client, settings) -> None:
        client, _ = app_client
        _register(client)
        response = self._edit(client, 987_654)
        assert response.headers["location"] == "/?error=measurement#history"


class TestMedicationPickerDelete:
    def test_delete_removes_own_log(self, app_client, settings) -> None:
        client, user_id = app_client
        _register(client)
        meds = _add_medication(client, settings, "Лозартан", "morning")
        _add(client, medication_taken="1", medication_ids=[str(meds["morning"])])
        ((measurement_id, _),) = _ids(settings)
        con = sqlite3.connect(settings.db_path)
        con.execute(
            "INSERT INTO medication_logs (user_id, medication_id, timestamp, status, slot)"
            " VALUES (?, ?, '2026-01-02 03:04:05.000000', 'taken', 'morning')",
            (user_id, meds["morning"]),
        )
        con.commit()
        con.close()
        response = client.post(
            f"/measurements/{measurement_id}/delete", data={"csrf_token": _csrf(client)}
        )
        assert response.headers["location"] == "/?saved=measurement#history"
        assert _ids(settings) == [] and _links(settings) == []
        assert _med_logs(settings) == [
            (meds["morning"], "2026-01-02 03:04:05.000000", "taken", "morning")
        ]


# ─────────────── Подсветка строк «Истории записей» по зонам давления (§16.2) ───────────────


class TestRowZone:
    """Худшая из зон САД/ДАД; пороги — из chart_zones.py, как у графиков."""

    @pytest.mark.parametrize(
        ("systolic", "diastolic", "expected"),
        [
            (129, 84, None),  # обе зелёные — без подсветки
            (130, 84, "zone-row-elevated"),  # граница САД 130 — уже жёлтая
            (139, 84, "zone-row-elevated"),
            (140, 84, "zone-row-high"),  # граница САД 140 — уже красная
            (129, 85, "zone-row-elevated"),  # граница ДАД 85
            (129, 89, "zone-row-elevated"),
            (129, 90, "zone-row-high"),  # граница ДАД 90
            (154, 84, "zone-row-high"),  # пример из SPEC: красная по САД
            (132, 90, "zone-row-high"),  # жёлтая САД + красная ДАД → красная
            (135, 87, "zone-row-elevated"),
        ],
    )
    def test_worst_zone(self, systolic: int, diastolic: int, expected) -> None:
        from web.cabinet.chart_zones import row_zone

        zone = row_zone(systolic, diastolic)
        assert (zone["css_class"] if zone else None) == expected

    def test_fill_and_title_from_chart_zones(self) -> None:
        from web.cabinet.chart_zones import CHART_ZONES, row_zone

        zone = row_zone(132, 90)
        assert zone["fill"] == CHART_ZONES["systolic"][2]["fill"]
        assert zone["title"] == "САД: 130–139, ДАД: ≥ 90"
        assert row_zone(154, 84)["title"] == "САД: ≥ 140", "ДАД в норме — в title не пишется"

    def test_legend_two_chips(self) -> None:
        from web.cabinet.chart_zones import CHART_ZONES, table_legend

        assert table_legend() == [
            {"fill": CHART_ZONES["systolic"][1]["fill"], "label": "САД 130–139 · ДАД 85–89"},
            {"fill": CHART_ZONES["systolic"][2]["fill"], "label": "САД ≥ 140 · ДАД ≥ 90"},
        ]

    def test_band_index_requires_open_top(self) -> None:
        from web.cabinet.chart_zones import band_index

        with pytest.raises(ValueError):
            band_index(200, [{"to": 130}])


class TestHistoryZonesRender:
    def _insert(self, settings: Settings, user_id: int, *pairs: tuple[int, int]) -> None:
        con = sqlite3.connect(settings.db_path)
        base = datetime.now(ZoneInfo("UTC")).replace(microsecond=0) - timedelta(hours=1)
        for index, (systolic, diastolic) in enumerate(pairs):
            con.execute(
                "INSERT INTO measurements (user_id, timestamp, systolic, diastolic)"
                " VALUES (?, ?, ?, ?)",
                (user_id, (base - timedelta(minutes=index)).strftime("%Y-%m-%d %H:%M:%S"),
                 systolic, diastolic),
            )
        con.commit()
        con.close()

    def test_rows_highlighted_by_worst_zone(self, app_client, settings) -> None:
        client, user_id = app_client
        _register(client)
        self._insert(settings, user_id, (154, 84), (135, 80), (120, 70))
        html = client.get("/?days=7#history").text
        assert html.count('class="zone-row zone-row-high"') == 1
        assert html.count('class="zone-row zone-row-elevated"') == 1
        assert 'title="САД: ≥ 140"' in html
        assert 'style="--zone-fill: rgba(192,57,43,0.12)"' in html
        rows = html[html.index("<tbody>", html.index('id="history"')):]
        assert rows.count("<tr>") == 1, "строка в норме — без подсветки"

    def test_legend_above_table(self, app_client, settings) -> None:
        client, user_id = app_client
        _register(client)
        self._insert(settings, user_id, (120, 70))
        html = client.get("/?days=7#history").text
        history = html[html.index('id="history"'):]
        legend = history.index('class="zone-legend history-zone-legend"')
        assert legend < history.index('<table class="stacked-table">')
        assert "САД 130–139 · ДАД 85–89" in history and "САД ≥ 140 · ДАД ≥ 90" in history

    def test_no_legend_without_records(self, app_client) -> None:
        client, _ = app_client
        _register(client)
        html = client.get("/?days=7#history").text
        assert "history-zone-legend" not in html

    def test_css_colors_row_and_card(self, app_client) -> None:
        """Одно правило — фон строки и строки-карточки на узких экранах (Z6 А)."""
        client, _ = app_client
        # Окончания строк зависят от checkout (CRLF на Windows) — сравниваем без них.
        css = client.get("/static/style.css").text.replace("\r\n", "\n")
        assert ".stacked-table tr.zone-row {\n  background: var(--zone-fill);" in css


class TestChartAxisLabels:
    def test_two_line_ticks_with_padding(self, app_client) -> None:
        """Подписи оси X «дд.мм ЧЧ:ММ» — в две строки, с отступом автопропуска (30 дней не наезжают)."""
        client, _ = app_client
        script = client.get("/static/bp-charts.js").text
        assert "autoSkipPadding: 12" in script
        assert 'return String(this.getLabelForValue(value)).split(" ");' in script
