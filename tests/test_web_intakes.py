"""«💊 Отметить приём» и standalone-приёмы в личном кабинете (§16.2; 0.6.2, п. 27).

Покрывается: отметка приёма без измерения (статусы taken/skipped/partial,
момент «Сейчас»/прошедший, `source='cabinet'`), дедуп за локальные сутки по
`medication_id` и апгрейд «пропущено»/«частично» → `taken` (строка бота
обновляется, `source` не меняется; уже `taken` — отмечено и заблокировано),
NULL-повтор замера (`reminders`, job ставит тик бота), валидация момента
(будущее; совпадение до минуты с моментом измерения — «отметьте приём в
форме измерения»), права правки/удаления (только `source='cabinet'` и не
журнал записи, §4.11), чужой id — 404, CSRF.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from tests.test_web_auth import _csrf, _register, app_client, settings  # noqa: F401

TZ = "Europe/Minsk"
LOCAL = ZoneInfo(TZ)
TODAY = datetime.now(LOCAL).date()
YESTERDAY = TODAY - timedelta(days=1)


def _utc_text(local: datetime) -> str:
    """Момент в формате хранения SQLite (UTC с микросекундами — как пишет SQLAlchemy)."""
    return local.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


def _local(day: date, hour: int, minute: int = 0, second: int = 0) -> datetime:
    """Локальный момент (Минск) с tz."""
    return datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=LOCAL)


def _sql(settings, query: str, args: tuple = ()) -> list[tuple]:  # noqa: F811
    con = sqlite3.connect(settings.db_path)
    try:
        rows = con.execute(query, args).fetchall()
        con.commit()
    finally:
        con.close()
    return rows


def _location(response) -> str:
    assert response.status_code == 303
    return response.headers["location"]


def _insert_user(settings, telegram_id: int) -> int:
    """Второй пользователь бота — для проверки изоляции чужих id (§7.1)."""
    con = sqlite3.connect(settings.db_path)
    try:
        cur = con.execute(
            "INSERT INTO users (telegram_id, timezone, target_systolic, target_diastolic,"
            " onboarded, created_at) VALUES (?, 'Europe/Minsk', 130, 80, 1,"
            " '2026-01-01 00:00:00')",
            (telegram_id,),
        )
        con.commit()
        return int(cur.lastrowid)
    finally:
        con.close()


def _insert_med(
    settings, user_id: int, name: str = "Лозартан", dosage: str = "50 мг", slot: str = "morning"
) -> int:
    """Лекарство пользователя (строка лекарства = слот, §4.4)."""
    con = sqlite3.connect(settings.db_path)
    try:
        cur = con.execute(
            "INSERT INTO medications (user_id, name, dosage, slot, created_at)"
            " VALUES (?, ?, ?, ?, '2026-01-01 00:00:00')",
            (user_id, name, dosage, slot),
        )
        con.commit()
        return int(cur.lastrowid)
    finally:
        con.close()


def _insert_log(
    settings,
    user_id: int,
    med_id: int,
    slot: str,
    local: datetime,
    status: str,
    source: str,
) -> int:
    """Строка `medication_logs` напрямую (так пишет бот — `source='bot'`)."""
    con = sqlite3.connect(settings.db_path)
    try:
        cur = con.execute(
            "INSERT INTO medication_logs (user_id, medication_id, timestamp, status, slot, source)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, med_id, _utc_text(local), status, slot, source),
        )
        con.commit()
        return int(cur.lastrowid)
    finally:
        con.close()


def _insert_measurement(settings, user_id: int, local: datetime) -> int:
    """Измерение напрямую в БД (для проверки запрета совпадения момента)."""
    con = sqlite3.connect(settings.db_path)
    try:
        cur = con.execute(
            "INSERT INTO measurements (user_id, timestamp, systolic, diastolic)"
            " VALUES (?, ?, 135, 85)",
            (user_id, _utc_text(local)),
        )
        con.commit()
        return int(cur.lastrowid)
    finally:
        con.close()


def _link_med(settings, measurement_id: int, med_id: int) -> None:
    """Связь «измерение ↔ лекарство» (§4.11): пара с моментом — журнал записи."""
    _sql(
        settings,
        "INSERT INTO measurement_medications (measurement_id, medication_id) VALUES (?, ?)",
        (measurement_id, med_id),
    )


def _form(
    day: date,
    time_: str = "22:00",
    decisions: dict[int, str] | None = None,
    **extra,
) -> dict:
    """Форма «💊 Отметить приём»: момент + решения `status_{id}` (§16.2)."""
    form = {"csrf_token": "", "date": day.isoformat(), "time": time_}
    for med_id, status in (decisions or {}).items():
        form[f"status_{med_id}"] = status
    form.update(extra)
    return form


def _edit_form(day: date, time_: str, status: str) -> dict:
    """Форма правки standalone-приёма: статус и момент (§16.2)."""
    return {"csrf_token": "", "date": day.isoformat(), "time": time_, "status": status}


def _submit(client, url: str, form: dict):
    """Отправить форму с CSRF-токеном из cookie."""
    return client.post(url, data=dict(form, csrf_token=_csrf(client)))


# ─────────────────────────── Форма «💊 Отметить приём» ───────────────────────────


class TestIntakeForm:
    def test_form_renders_meds_by_slot(self, app_client, settings) -> None:
        """Лекарства по слотам со статусами; момент — «Сейчас» по умолчанию (§16.2)."""
        client, user_id = app_client
        _register(client)
        med_a = _insert_med(settings, user_id, "Лозартан", "50 мг", "morning")
        med_b = _insert_med(settings, user_id, "Амлодипин", "10 мг", "evening")
        page = client.get("/?intake=1#history").text
        assert "💊 Отметить приём" in page
        assert 'action="/intakes"' in page
        assert "Лозартан (50 мг)" in page and "Амлодипин (10 мг)" in page
        assert "Утро" in page and "Вечер" in page
        assert "Принято ✓" in page and "Пропущено ✗" in page and "Частично ~" in page
        assert f'name="status_{med_a}"' in page and f'name="status_{med_b}"' in page
        assert f'value="{TODAY.isoformat()}"' in page  # поля даты — текущими значениями

    def test_taken_today_shown_marked_and_blocked(self, app_client, settings) -> None:
        """Уже `taken` за сутки — отмечено и заблокировано (повтор ничего не меняет)."""
        client, user_id = app_client
        _register(client)
        med_id = _insert_med(settings, user_id)
        _insert_log(settings, user_id, med_id, "morning", _local(TODAY, 8, 0), "taken", "bot")
        page = client.get("/?intake=1#history").text
        assert f'<select name="status_{med_id}" disabled>' in page


# ─────────────────────────── Отметка приёма ───────────────────────────


class TestMarkIntake:
    @pytest.mark.parametrize(("status"), ["taken", "skipped", "partial"])
    def test_mark_creates_cabinet_log(self, app_client, settings, status) -> None:
        """Решение `status_{id}` пишет строку `medication_logs` с `source='cabinet'`."""
        client, user_id = app_client
        _register(client)
        med_id = _insert_med(settings, user_id)
        response = _submit(client, "/intakes", _form(YESTERDAY, "22:00", {med_id: status}))
        assert _location(response) == "/?saved=intake#history"
        rows = _sql(
            settings,
            "SELECT medication_id, status, slot, source, timestamp FROM medication_logs"
            " WHERE user_id = ?",
            (user_id,),
        )
        assert rows == [
            (med_id, status, "morning", "cabinet", _utc_text(_local(YESTERDAY, 22, 0)))
        ]
        assert "Приёмы обновлены." in client.get("/?saved=intake").text

    def test_mark_now_moment(self, app_client, settings) -> None:
        """Момент «Сейчас»: дата и время формы текущими значениями."""
        client, user_id = app_client
        _register(client)
        med_id = _insert_med(settings, user_id)
        now = datetime.now(LOCAL)
        response = _submit(
            client,
            "/intakes",
            _form(now.date(), now.strftime("%H:%M"), {med_id: "taken"}),
        )
        assert _location(response) == "/?saved=intake#history"
        rows = _sql(settings, "SELECT timestamp FROM medication_logs WHERE user_id = ?", (user_id,))
        assert rows == [(_utc_text(_local(now.date(), now.hour, now.minute)),)]

    def test_two_meds_different_slots_both_logged(self, app_client, settings) -> None:
        """Два лекарства в разных слотах — оба логируются (дедуп по `medication_id`)."""
        client, user_id = app_client
        _register(client)
        med_a = _insert_med(settings, user_id, "Лозартан", "50 мг", "morning")
        med_b = _insert_med(settings, user_id, "Амлодипин", "10 мг", "evening")
        response = _submit(
            client, "/intakes", _form(YESTERDAY, "22:00", {med_a: "taken", med_b: "skipped"})
        )
        assert _location(response) == "/?saved=intake#history"
        rows = _sql(
            settings,
            "SELECT medication_id, status, slot FROM medication_logs WHERE user_id = ? ORDER BY id",
            (user_id,),
        )
        assert rows == [(med_a, "taken", "morning"), (med_b, "skipped", "evening")]

    def test_repeat_same_day_no_new_row(self, app_client, settings) -> None:
        """Повторная отметка за сутки — новой строки нет (дедуп §7.2)."""
        client, user_id = app_client
        _register(client)
        med_id = _insert_med(settings, user_id)
        _submit(client, "/intakes", _form(YESTERDAY, "08:00", {med_id: "skipped"}))
        response = _submit(client, "/intakes", _form(YESTERDAY, "21:00", {med_id: "skipped"}))
        assert _location(response) == "/?saved=intake#history"
        rows = _sql(settings, "SELECT COUNT(*), timestamp FROM medication_logs WHERE user_id = ?", (user_id,))
        assert rows == [(1, _utc_text(_local(YESTERDAY, 8, 0)))]

    @pytest.mark.parametrize(("initial"), ["skipped", "partial"])
    def test_upgrade_bot_log_to_taken(self, app_client, settings, initial) -> None:
        """Апгрейд: «пропущено»/«частично» → `taken` обновляет строку (даже бота).

        Строка обновляется (статус и момент), `source` не меняется (§16.2:
        единственное исключение из M3 = В).
        """
        client, user_id = app_client
        _register(client)
        med_id = _insert_med(settings, user_id)
        _insert_log(settings, user_id, med_id, "morning", _local(YESTERDAY, 8, 0), initial, "bot")
        response = _submit(client, "/intakes", _form(YESTERDAY, "22:00", {med_id: "taken"}))
        assert _location(response) == "/?saved=intake#history"
        rows = _sql(
            settings,
            "SELECT COUNT(*), status, timestamp, source FROM medication_logs WHERE user_id = ?",
            (user_id,),
        )
        assert rows == [(1, "taken", _utc_text(_local(YESTERDAY, 22, 0)), "bot")]

    def test_already_taken_not_changed(self, app_client, settings) -> None:
        """Уже `taken` за сутки: повторная отметка ничего не меняет (в т.ч. момент)."""
        client, user_id = app_client
        _register(client)
        med_id = _insert_med(settings, user_id)
        _insert_log(settings, user_id, med_id, "morning", _local(TODAY, 8, 0), "taken", "cabinet")
        now = datetime.now(LOCAL)
        response = _submit(
            client,
            "/intakes",
            _form(now.date(), now.strftime("%H:%M"), {med_id: "taken"}),
        )
        assert _location(response) == "/?saved=intake#history"
        rows = _sql(settings, "SELECT COUNT(*), status, timestamp FROM medication_logs WHERE user_id = ?", (user_id,))
        assert rows == [(1, "taken", _utc_text(_local(TODAY, 8, 0)))]


# ─────────────────────────── Повторный замер ───────────────────────────


class TestRemeasure:
    def test_taken_writes_null_remeasure_row(self, app_client, settings) -> None:
        """`taken` + `users.med_remeasure_delay` — строка NULL-повтора (§6.7).

        Job ставит тик бота (`reminders:tick`, до минуты) — «кабинет пишет
        только строки» (§6.13). Момент срабатывания — от момента сохранения
        (канон `ensure_remeasure`; в SPEC §16.2 — «момент отметки + задержка»,
        тест фиксирует текущую реализацию).
        """
        client, user_id = app_client
        _register(client)
        med_id = _insert_med(settings, user_id)
        response = _submit(client, "/intakes", _form(YESTERDAY, "22:00", {med_id: "taken"}))
        assert _location(response) == "/?saved=intake#history"
        rows = _sql(
            settings,
            "SELECT type, measurement_id, local_time, enabled, next_trigger_time"
            " FROM reminders WHERE user_id = ?",
            (user_id,),
        )
        assert len(rows) == 1
        assert rows[0][:4] == ("med_remeasure", None, None, 1)
        trigger = datetime.strptime(rows[0][4], "%Y-%m-%d %H:%M:%S.%f").replace(
            tzinfo=timezone.utc
        )
        delta = trigger - datetime.now(timezone.utc)
        assert timedelta(minutes=55) <= delta <= timedelta(minutes=70)

    def test_one_active_remeasure_per_user(self, app_client, settings) -> None:
        """Идемпотентность: два вызова — одна активная строка (C2/C18)."""
        client, user_id = app_client
        _register(client)
        med_a = _insert_med(settings, user_id, "Лозартан", "50 мг", "morning")
        med_b = _insert_med(settings, user_id, "Амлодипин", "10 мг", "evening")
        _submit(client, "/intakes", _form(YESTERDAY, "08:00", {med_a: "taken"}))
        _submit(client, "/intakes", _form(YESTERDAY, "09:00", {med_b: "taken"}))
        rows = _sql(settings, "SELECT COUNT(*) FROM reminders WHERE user_id = ?", (user_id,))
        assert rows == [(1,)]

    def test_no_remeasure_without_delay(self, app_client, settings) -> None:
        """`users.med_remeasure_delay` не задан — строка повтора не ставится."""
        client, user_id = app_client
        _register(client)
        med_id = _insert_med(settings, user_id)
        _sql(settings, "UPDATE users SET med_remeasure_delay = NULL WHERE id = ?", (user_id,))
        response = _submit(client, "/intakes", _form(YESTERDAY, "22:00", {med_id: "taken"}))
        assert _location(response) == "/?saved=intake#history"
        assert _sql(settings, "SELECT COUNT(*) FROM medication_logs WHERE user_id = ?", (user_id,)) == [(1,)]
        assert _sql(settings, "SELECT COUNT(*) FROM reminders WHERE user_id = ?", (user_id,)) == [(0,)]

    def test_skipped_mark_writes_no_remeasure(self, app_client, settings) -> None:
        """Повтор ставится только при создании строки `taken` (включая апгрейд)."""
        client, user_id = app_client
        _register(client)
        med_id = _insert_med(settings, user_id)
        _submit(client, "/intakes", _form(YESTERDAY, "22:00", {med_id: "skipped"}))
        assert _sql(settings, "SELECT COUNT(*) FROM reminders WHERE user_id = ?", (user_id,)) == [(0,)]


# ─────────────────────────── Валидация момента ───────────────────────────


class TestValidation:
    def test_future_moment_rejected(self, app_client, settings) -> None:
        """Будущий момент — «Время измерения не может быть в будущем» (§9)."""
        client, user_id = app_client
        _register(client)
        med_id = _insert_med(settings, user_id)
        response = _submit(
            client, "/intakes", _form(date(2999, 1, 1), "10:00", {med_id: "taken"})
        )
        assert _location(response) == "/?intake=1&error=intake_future#history"
        assert _sql(settings, "SELECT COUNT(*) FROM medication_logs WHERE user_id = ?", (user_id,)) == [(0,)]
        page = client.get("/?intake=1&error=intake_future").text
        assert "Время измерения не может быть в будущем" in page

    def test_measurement_moment_rejected_on_create(self, app_client, settings) -> None:
        """Момент совпал до минуты с моментом измерения — «отметьте в форме измерения».

        Совпадение — минутные слоты: измерение в 08:00:30 ловит форму 08:00 (§16.2).
        """
        client, user_id = app_client
        _register(client)
        med_id = _insert_med(settings, user_id)
        _insert_measurement(settings, user_id, _local(YESTERDAY, 8, 0, 30))
        response = _submit(client, "/intakes", _form(YESTERDAY, "08:00", {med_id: "taken"}))
        assert _location(response) == "/?intake=1&error=intake_measurement#history"
        assert _sql(settings, "SELECT COUNT(*) FROM medication_logs WHERE user_id = ?", (user_id,)) == [(0,)]
        page = client.get("/?intake=1&error=intake_measurement").text
        assert "На это время уже есть измерение — отметьте приём в форме измерения" in page

    def test_measurement_moment_rejected_on_edit(self, app_client, settings) -> None:
        """Запрет совпадения момента действует и в форме правки (§16.2)."""
        client, user_id = app_client
        _register(client)
        med_id = _insert_med(settings, user_id)
        log_id = _insert_log(settings, user_id, med_id, "morning", _local(YESTERDAY, 7, 0), "skipped", "cabinet")
        _insert_measurement(settings, user_id, _local(YESTERDAY, 9, 0))
        response = _submit(
            client, f"/intakes/{log_id}", _edit_form(YESTERDAY, "09:00", "taken")
        )
        assert _location(response) == f"/?edit_int={log_id}&error=intake_measurement#history"
        rows = _sql(settings, "SELECT status, timestamp FROM medication_logs WHERE id = ?", (log_id,))
        assert rows == [("skipped", _utc_text(_local(YESTERDAY, 7, 0)))]

    def test_future_moment_rejected_on_edit(self, app_client, settings) -> None:
        """Будущий момент в форме правки — та же ошибка §9."""
        client, user_id = app_client
        _register(client)
        med_id = _insert_med(settings, user_id)
        log_id = _insert_log(settings, user_id, med_id, "morning", _local(YESTERDAY, 7, 0), "skipped", "cabinet")
        response = _submit(
            client, f"/intakes/{log_id}", _edit_form(date(2999, 1, 1), "10:00", "taken")
        )
        assert _location(response) == f"/?edit_int={log_id}&error=intake_future#history"
        rows = _sql(settings, "SELECT status FROM medication_logs WHERE id = ?", (log_id,))
        assert rows == [("skipped",)]


# ─────────────────────────── Правка и удаление ───────────────────────────


class TestEditDelete:
    def test_edit_and_delete_own_cabinet_log(self, app_client, settings) -> None:
        """Своя строка `source='cabinet'`: правка и удаление с подтверждением блоком."""
        client, user_id = app_client
        _register(client)
        med_id = _insert_med(settings, user_id)
        log_id = _insert_log(settings, user_id, med_id, "morning", _local(YESTERDAY, 7, 0), "skipped", "cabinet")
        page = client.get(f"/?edit_int={log_id}#history").text
        assert "Изменить приём" in page
        assert f'action="/intakes/{log_id}"' in page
        response = _submit(client, f"/intakes/{log_id}", _edit_form(YESTERDAY, "08:30", "partial"))
        assert _location(response) == "/?saved=intake#history"
        rows = _sql(settings, "SELECT status, timestamp FROM medication_logs WHERE id = ?", (log_id,))
        assert rows == [("partial", _utc_text(_local(YESTERDAY, 8, 30)))]
        # Удаление — только после подтверждения блоком (канон 0.5.9).
        page = client.get(f"/?confirm_delete_int={log_id}#history").text
        assert f'action="/intakes/{log_id}/delete"' in page
        assert "Удалить «Лозартан 50 мг · утро»?" in page
        response = client.post(
            f"/intakes/{log_id}/delete", data={"csrf_token": _csrf(client)}
        )
        assert _location(response) == "/?saved=intake#history"
        assert _sql(settings, "SELECT COUNT(*) FROM medication_logs WHERE id = ?", (log_id,)) == [(0,)]

    def test_bot_log_view_only(self, app_client, settings) -> None:
        """Строка `source='bot'` — только просмотр (M3 = В): правки и удаления нет."""
        client, user_id = app_client
        _register(client)
        med_id = _insert_med(settings, user_id)
        log_id = _insert_log(settings, user_id, med_id, "morning", _local(YESTERDAY, 7, 0), "taken", "bot")
        page = client.get(f"/?edit_int={log_id}#history").text
        assert "Изменить приём" not in page
        assert f"/?edit_int={log_id}#history" not in page  # без кнопок правки/удаления
        token = {"csrf_token": _csrf(client)}
        assert _location(client.post(f"/intakes/{log_id}", data=token)) == "/?error=intake_viewonly#history"
        assert _location(client.post(f"/intakes/{log_id}/delete", data=token)) == "/?error=intake_viewonly#history"
        assert _sql(settings, "SELECT COUNT(*) FROM medication_logs WHERE id = ?", (log_id,)) == [(1,)]
        page = client.get("/?error=intake_viewonly").text
        assert "Этот приём можно только просмотреть." in page

    def test_journal_log_managed_by_measurement_form(self, app_client, settings) -> None:
        """Журнал записи (пара `medication_id + timestamp`, §4.11) — формой измерения.

        Даже `source='cabinet'` такой строке права standalone-правки не даёт;
        в ленте она отдельно не рендерится.
        """
        client, user_id = app_client
        _register(client)
        med_id = _insert_med(settings, user_id)
        moment = _local(YESTERDAY, 8, 0)
        mid = _insert_measurement(settings, user_id, moment)
        _link_med(settings, mid, med_id)
        log_id = _insert_log(settings, user_id, med_id, "morning", moment, "taken", "cabinet")
        page = client.get(f"/?edit_int={log_id}#history").text
        assert "Изменить приём" not in page
        assert "Приём лекарств:" not in page  # журнал — только колонка «Лекарство»
        token = {"csrf_token": _csrf(client)}
        assert _location(client.post(f"/intakes/{log_id}", data=token)) == "/?error=intake_linked#history"
        assert _location(client.post(f"/intakes/{log_id}/delete", data=token)) == "/?error=intake_linked#history"
        assert _sql(settings, "SELECT COUNT(*) FROM medication_logs WHERE id = ?", (log_id,)) == [(1,)]
        page = client.get("/?error=intake_linked").text
        assert "Приём входит в запись измерения — измените его в форме измерения." in page

    def test_foreign_log_404(self, app_client, settings) -> None:
        """Чужая строка приёма на POST правки и удаления — 404 (§7.1)."""
        client, _ = app_client
        _register(client)
        foreign_user = _insert_user(settings, 222222)
        foreign_med = _insert_med(settings, foreign_user)
        foreign_id = _insert_log(
            settings, foreign_user, foreign_med, "morning", _local(YESTERDAY, 7, 0), "taken", "cabinet"
        )
        token = {"csrf_token": _csrf(client)}
        assert client.post(f"/intakes/{foreign_id}", data=token).status_code == 404
        assert client.post(f"/intakes/{foreign_id}/delete", data=token).status_code == 404
        assert _sql(settings, "SELECT COUNT(*) FROM medication_logs WHERE id = ?", (foreign_id,)) == [(1,)]


class TestCsrf:
    def test_intakes_require_token(self, app_client, settings) -> None:
        """POST /intakes и /intakes/{id} без корректного CSRF-токена — 403."""
        client, user_id = app_client
        _register(client)
        med_id = _insert_med(settings, user_id)
        assert client.post("/intakes", data=_form(YESTERDAY, "22:00", {med_id: "taken"}, csrf_token="forged")).status_code == 403
        assert _sql(settings, "SELECT COUNT(*) FROM medication_logs WHERE user_id = ?", (user_id,)) == [(0,)]
        log_id = _insert_log(settings, user_id, med_id, "morning", _local(YESTERDAY, 7, 0), "skipped", "cabinet")
        response = client.post(
            f"/intakes/{log_id}", data=_edit_form(YESTERDAY, "08:00", "taken")
        )
        assert response.status_code == 403
        rows = _sql(settings, "SELECT status, timestamp FROM medication_logs WHERE id = ?", (log_id,))
        assert rows == [("skipped", _utc_text(_local(YESTERDAY, 7, 0)))]
