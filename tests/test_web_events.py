"""События дневника в личном кабинете (SPEC.md, §16.2; 0.6.2, п. 27).

Покрывается: полный CRUD событий (все типы и канонический payload §4.18),
будущий момент — «Время измерения не может быть в будущем» (§9), сон — одна
оценка на локальные сутки апсертом (§7.2, момент формы определяет сутки),
правка/удаление событий, созданных ботом, и linked-событий (момент и привязка
`measurement_id` не меняются; удаление снимает чип «Фактор», запись цела),
чужой id, CSRF, чипы «Фактор» в строке измерения и чипы-фильтры смешанной
ленты (§6.4, `d:filt:*`).
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode
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


def _insert_event(
    settings,
    user_id: int,
    *,
    type_: str,
    local: datetime,
    payload: str | None = None,
    note: str | None = None,
    measurement_id: int | None = None,
) -> int:
    """Событие напрямую в БД — так его создаёт бот (правка/удаление из ЛК)."""
    con = sqlite3.connect(settings.db_path)
    try:
        cur = con.execute(
            "INSERT INTO diary_events (user_id, type, moment, note, payload,"
            " measurement_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                user_id,
                type_,
                _utc_text(local),
                note,
                payload,
                measurement_id,
                _utc_text(local),
            ),
        )
        con.commit()
        return int(cur.lastrowid)
    finally:
        con.close()


def _insert_measurement(settings, user_id: int, local: datetime) -> int:
    """Измерение напрямую в БД (канон формы измерения: systolic/diastolic)."""
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


def _form(type_: str, day: date, time_: str = "13:20", note: str = "", **fields) -> dict:
    """Форма события: момент (дата + время, канон формы измерения) и поля типа."""
    form = {
        "csrf_token": "",
        "type": type_,
        "date": day.isoformat(),
        "time": time_,
        "note": note,
    }
    form.update(fields)
    return form


def _submit(client, url: str, form: dict, marks: tuple[str, ...] = ()):
    """Отправить форму события: метки — повторяющееся поле `marks` (1–3).

    Тело кодируется вручную (urlencoded): установленный httpx в `data=`
    список пар не кодирует как форму (уходит пустой body).
    """
    pairs = list(dict(form, csrf_token=_csrf(client)).items())
    pairs += [("marks", mark) for mark in marks]
    return client.post(
        url,
        content=urlencode(pairs),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )


def _event_form_html(page: str) -> str:
    """Тело формы события (по `id="event-form"`) — проверка полей момента."""
    return page.split('id="event-form"', 1)[1].split("</form>", 1)[0]


# ─────────────────────────── Добавление ───────────────────────────


class TestAddEvent:
    def test_add_flow_two_steps(self, app_client) -> None:
        """`?add_ev=1` — шаг выбора типа, `?add_ev={type}` — форма типа (§16.2)."""
        client, _ = app_client
        _register(client)
        page = client.get("/?add_ev=1#history").text
        assert "Добавить событие" in page
        assert "Питание (соль / кофе / алкоголь)" in page
        assert "Стресс / самочувствие" in page
        assert "Сон" in page
        assert "Прогулка / активность" in page
        assert "Заметка / другое" in page
        form_page = client.get("/?add_ev=food#history").text
        assert 'action="/events"' in form_page
        assert 'name="type" value="food"' in form_page
        # Момент — «Сейчас» по умолчанию: поля даты и времени текущими значениями.
        assert f'value="{TODAY.isoformat()}"' in form_page

    @pytest.mark.parametrize(
        ("type_", "fields", "marks", "expected_payload", "expected_note"),
        [
            (
                "food",
                {},
                ("salt", "coffee"),
                '{"marks":["coffee","salt"]}',
                "после обеда",
            ),
            ("food", {}, ("salt", "coffee", "alcohol"), '{"marks":["alcohol","coffee","salt"]}', ""),
            ("stress", {"variant": "anxiety"}, (), '{"variant":"anxiety"}', ""),
            ("stress", {"variant": "other"}, (), '{"variant":"other"}', "тревожно, позвонил врачу"),
            ("sleep", {"rating": "normal"}, (), '{"rating":"normal"}', ""),
            ("activity", {"duration_min": "20"}, (), '{"duration_min":20}', ""),
            ("note", {}, (), None, "текст заметки"),
        ],
    )
    def test_add_saves_canonical_payload(
        self, app_client, settings, type_, fields, marks, expected_payload, expected_note
    ) -> None:
        """Каждый тип создаёт событие с каноническим JSON payload (§4.18)."""
        client, user_id = app_client
        _register(client)
        form = _form(type_, YESTERDAY, "13:20", note=expected_note, **fields)
        response = _submit(client, "/events", form, marks)
        assert _location(response) == "/?saved=event#history"
        rows = _sql(
            settings,
            "SELECT type, payload, note, moment FROM diary_events WHERE user_id = ?",
            (user_id,),
        )
        assert rows == [
            (type_, expected_payload, expected_note or None, _utc_text(_local(YESTERDAY, 13, 20)))
        ]
        assert "Событие сохранено." in client.get("/?saved=event").text

    def test_repeated_note_form_creates_one_row(self, app_client, settings) -> None:
        client, user_id = app_client
        _register(client)
        form = _form("note", YESTERDAY, "13:20", note="Повтор формы")
        for _ in range(2):
            assert _location(_submit(client, "/events", form)) == "/?saved=event#history"
        assert _sql(
            settings, "SELECT COUNT(*) FROM diary_events WHERE user_id = ?", (user_id,)
        ) == [(1,)]

    def test_add_future_moment_rejected(self, app_client, settings) -> None:
        """Будущий момент — единый текст §9 и `?error=event_future` (§16.2)."""
        client, user_id = app_client
        _register(client)
        response = _submit(client, "/events", _form("food", date(2999, 1, 1)), ("coffee",))
        assert _location(response) == "/?add_ev=food&error=event_future#history"
        assert _sql(settings, "SELECT COUNT(*) FROM diary_events WHERE user_id = ?", (user_id,)) == [(0,)]
        page = client.get("/?add_ev=food&error=event_future").text
        assert "Время измерения не может быть в будущем" in page

    @pytest.mark.parametrize(
        ("type_", "marks", "fields"),
        [
            ("stress", (), {"variant": "other"}),  # «Другое» без текста (§6.14)
            ("note", (), {}),  # пустая заметка
            ("food", (), {}),  # без меток
        ],
    )
    def test_add_empty_fields_rejected(self, app_client, settings, type_, marks, fields) -> None:
        """Валидация полей — как в боте (§9): пустые обязательные поля не проходят."""
        client, user_id = app_client
        _register(client)
        response = _submit(client, "/events", _form(type_, YESTERDAY, **fields), marks)
        assert _location(response) == f"/?add_ev={type_}&error=event#history"
        assert _sql(settings, "SELECT COUNT(*) FROM diary_events WHERE user_id = ?", (user_id,)) == [(0,)]
        assert "Не удалось сохранить событие: проверьте поля." in client.get(
            f"/?add_ev={type_}&error=event"
        ).text

    def test_add_note_longer_than_500_rejected(self, app_client, settings) -> None:
        """Заметка > 500 символов — ошибка полей (§9, §16.2)."""
        client, user_id = app_client
        _register(client)
        response = _submit(client, "/events", _form("note", YESTERDAY, note="я" * 501))
        assert _location(response) == "/?add_ev=note&error=event#history"
        assert _sql(settings, "SELECT COUNT(*) FROM diary_events WHERE user_id = ?", (user_id,)) == [(0,)]


# ─────────────────────────── Сон: одна оценка на сутки ───────────────────────────


class TestSleepUpsert:
    def test_one_event_per_local_day_upsert(self, app_client, settings) -> None:
        """Две отправки за одни сутки → одно событие, payload последнего (§7.2).

        В т.ч. задним числом: сутки определяет момент формы, а не «сегодня».
        """
        client, user_id = app_client
        _register(client)
        day = TODAY - timedelta(days=2)
        assert _location(_submit(client, "/events", _form("sleep", day, "23:00", rating="little"))) == "/?saved=event#history"
        # Повтор за те же сутки (другое время и оценка) — обновляет, не дублирует.
        assert _location(_submit(client, "/events", _form("sleep", day, "08:00", rating="much"))) == "/?saved=event#history"
        # Сон за другие сутки — отдельное событие.
        _submit(client, "/events", _form("sleep", YESTERDAY, "07:30", rating="normal"))
        rows = _sql(
            settings,
            "SELECT payload, moment FROM diary_events WHERE user_id = ? ORDER BY moment",
            (user_id,),
        )
        assert rows == [
            ('{"rating":"much"}', _utc_text(_local(day, 8, 0))),
            ('{"rating":"normal"}', _utc_text(_local(YESTERDAY, 7, 30))),
        ]

    def test_edit_sleep_keeps_moment(self, app_client, settings) -> None:
        """У сна момент не редактируется: поле в форме не показывается (C22)."""
        client, user_id = app_client
        _register(client)
        eid = _insert_event(
            settings,
            user_id,
            type_="sleep",
            local=_local(YESTERDAY, 7, 0),
            payload='{"rating":"little"}',
        )
        page = client.get(f"/?edit_ev={eid}#history").text
        assert "name=\"date\"" not in _event_form_html(page)
        # Правка с подделанным моментом: момент не меняется, оценка — меняется.
        response = _submit(
            client,
            f"/events/{eid}",
            _form("sleep", TODAY, "23:59", rating="much"),
        )
        assert _location(response) == "/?saved=event#history"
        rows = _sql(
            settings,
            "SELECT payload, moment FROM diary_events WHERE id = ?", (eid,)
        )
        assert rows == [('{"rating":"much"}', _utc_text(_local(YESTERDAY, 7, 0)))]


# ─────────────────────────── Правка и удаление ───────────────────────────


class TestEditDelete:
    def test_edit_bot_created_event(self, app_client, settings) -> None:
        """Событие, созданное ботом (прямая вставка), правится через POST /events/{id}."""
        client, user_id = app_client
        _register(client)
        eid = _insert_event(
            settings,
            user_id,
            type_="food",
            local=_local(YESTERDAY, 9, 0),
            payload='{"marks":["coffee"]}',
            note="старая",
        )
        page = client.get(f"/?edit_ev={eid}#history").text
        assert f'action="/events/{eid}"' in page
        assert "старая" in page  # заметка предзаполнена
        response = _submit(
            client,
            f"/events/{eid}",
            _form("food", YESTERDAY, "10:30", note="новая"),
            marks=("salt", "alcohol"),
        )
        assert _location(response) == "/?saved=event#history"
        rows = _sql(
            settings,
            "SELECT payload, note, moment FROM diary_events WHERE id = ?", (eid,)
        )
        assert rows == [
            ('{"marks":["alcohol","salt"]}', "новая", _utc_text(_local(YESTERDAY, 10, 30)))
        ]

    def test_linked_event_edit_keeps_moment_and_link(self, app_client, settings) -> None:
        """Правка linked-события меняет только payload/note; `moment` и привязка — нет."""
        client, user_id = app_client
        _register(client)
        mid = _insert_measurement(settings, user_id, _local(YESTERDAY, 8, 0))
        eid = _insert_event(
            settings,
            user_id,
            type_="food",
            local=_local(YESTERDAY, 8, 5),
            payload='{"marks":["coffee"]}',
            measurement_id=mid,
        )
        page = client.get(f"/?edit_ev={eid}#history").text
        # Поле момента в форме linked-события не показывается (§16.2).
        assert "name=\"date\"" not in _event_form_html(page)
        # Подделанный момент в POST игнорируется: сутки linked-события не меняются.
        response = _submit(
            client,
            f"/events/{eid}",
            _form("food", TODAY, "23:59", note="заметка"),
            marks=("salt",),
        )
        assert _location(response) == "/?saved=event#history"
        rows = _sql(
            settings,
            "SELECT payload, note, moment, measurement_id FROM diary_events WHERE id = ?",
            (eid,),
        )
        assert rows == [
            ('{"marks":["salt"]}', "заметка", _utc_text(_local(YESTERDAY, 8, 5)), mid)
        ]

    def test_delete_bot_created_event(self, app_client, settings) -> None:
        """Удаление события бота: подтверждение блоком, затем POST /events/{id}/delete."""
        client, user_id = app_client
        _register(client)
        eid = _insert_event(
            settings,
            user_id,
            type_="food",
            local=_local(YESTERDAY, 9, 0),
            payload='{"marks":["coffee"]}',
        )
        page = client.get(f"/?confirm_delete_ev={eid}#history").text
        assert f'action="/events/{eid}/delete"' in page
        assert "Удалить «Питание: кофе»?" in page
        response = client.post(
            f"/events/{eid}/delete", data={"csrf_token": _csrf(client)}
        )
        assert _location(response) == "/?saved=event#history"
        assert _sql(settings, "SELECT COUNT(*) FROM diary_events WHERE user_id = ?", (user_id,)) == [(0,)]

    def test_delete_linked_event_keeps_measurement_and_removes_chips(self, app_client, settings) -> None:
        """Удаление linked-события: событие уходит, измерение цело, чип «Фактор» исчезает."""
        client, user_id = app_client
        _register(client)
        mid = _insert_measurement(settings, user_id, _local(YESTERDAY, 8, 0))
        eid = _insert_event(
            settings,
            user_id,
            type_="food",
            local=_local(YESTERDAY, 8, 5),
            payload='{"marks":["coffee","salt"]}',
            measurement_id=mid,
        )
        page = client.get("/").text
        assert "☕ Кофе" in page and "🧂 Солёное" in page
        response = client.post(
            f"/events/{eid}/delete", data={"csrf_token": _csrf(client)}
        )
        assert _location(response) == "/?saved=event#history"
        assert _sql(settings, "SELECT COUNT(*) FROM diary_events WHERE user_id = ?", (user_id,)) == [(0,)]
        assert _sql(settings, "SELECT COUNT(*) FROM measurements WHERE id = ?", (mid,)) == [(1,)]
        page = client.get("/").text
        assert "☕ Кофе" not in page
        assert f"/?edit={mid}#history" in page  # запись измерения на месте


# ─────────────────────────── Чужой id и CSRF ───────────────────────────


class TestForeignId:
    def test_edit_and_delete_foreign_event_404(self, app_client, settings) -> None:
        """Чужое событие на POST правки и удаления — 404 (§7.1)."""
        client, _ = app_client
        _register(client)
        foreign_id = _insert_event(
            settings,
            _insert_user(settings, 222222),
            type_="note",
            local=_local(YESTERDAY, 9, 0),
            note="чужая заметка",
        )
        token = {"csrf_token": _csrf(client)}
        assert client.post(f"/events/{foreign_id}", data=token).status_code == 404
        assert client.post(f"/events/{foreign_id}/delete", data=token).status_code == 404
        assert _sql(settings, "SELECT COUNT(*) FROM diary_events WHERE id = ?", (foreign_id,)) == [(1,)]

    def test_get_forms_for_foreign_event_not_shown(self, app_client, settings) -> None:
        """`?edit_ev=` / `?confirm_delete_ev=` с чужим id: форма не показывается.

        Реализация (как и у правки измерений) отдаёт дашборд без формы
        чужого события — 404 только на POST (§7.1).
        """
        client, _ = app_client
        _register(client)
        foreign_id = _insert_event(
            settings,
            _insert_user(settings, 222222),
            type_="food",
            local=_local(YESTERDAY, 9, 0),
            payload='{"marks":["coffee"]}',
        )
        edit_page = client.get(f"/?edit_ev={foreign_id}#history")
        assert edit_page.status_code == 200
        assert f'action="/events/{foreign_id}"' not in edit_page.text
        delete_page = client.get(f"/?confirm_delete_ev={foreign_id}#history")
        assert delete_page.status_code == 200
        assert f'action="/events/{foreign_id}/delete"' not in delete_page.text


class TestCsrf:
    def test_add_edit_delete_require_token(self, app_client, settings) -> None:
        """POST без/с неверным CSRF-токеном — отказ (403), данные не меняются."""
        client, user_id = app_client
        _register(client)
        eid = _insert_event(
            settings,
            user_id,
            type_="food",
            local=_local(YESTERDAY, 9, 0),
            payload='{"marks":["coffee"]}',
            note="до правки",
        )
        add_body = urlencode(
            [
                ("csrf_token", "forged"),
                ("type", "note"),
                ("date", YESTERDAY.isoformat()),
                ("time", "13:20"),
                ("note", "x"),
            ]
        )
        assert client.post(
            "/events",
            content=add_body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ).status_code == 403
        forged = {"csrf_token": "forged"}
        assert client.post(f"/events/{eid}", data=forged).status_code == 403
        assert client.post(f"/events/{eid}/delete", data=forged).status_code == 403
        rows = _sql(
            settings,
            "SELECT payload, note FROM diary_events WHERE id = ?", (eid,)
        )
        assert rows == [('{"marks":["coffee"]}', "до правки")]
        assert _sql(settings, "SELECT COUNT(*) FROM diary_events WHERE user_id = ?", (user_id,)) == [(1,)]


# ─────────────────────────── Чипы «Фактор» и чипы-фильтры ───────────────────────────


class TestChipsAndFilters:
    def test_factor_chips_in_measurement_row(self, app_client, settings) -> None:
        """Linked-событие food с метками → чипы «☕ Кофе» в строке измерения (§16.2)."""
        client, user_id = app_client
        _register(client)
        mid = _insert_measurement(settings, user_id, _local(YESTERDAY, 8, 0))
        _insert_event(
            settings,
            user_id,
            type_="food",
            local=_local(YESTERDAY, 8, 5),
            payload='{"marks":["coffee"]}',
            measurement_id=mid,
        )
        page = client.get("/").text
        assert '<span class="factor-chip">☕ Кофе</span>' in page

    def test_feed_filters_show_and_hide_rows(self, app_client, settings) -> None:
        """Чипы-фильтры `?filt=bp|med|fact|other` — состав, как `d:filt:*` (§6.4)."""
        client, user_id = app_client
        _register(client)
        _sql(
            settings,
            "INSERT INTO medications (user_id, name, dosage, slot, created_at)"
            " VALUES (?, 'Лозартан', '50 мг', 'morning', '2026-01-01 00:00:00')",
            (user_id,),
        )
        med_id = _sql(settings, "SELECT id FROM medications WHERE user_id = ?", (user_id,))[0][0]
        # Измерение без приёмов (только «Давление») и с отмеченным приёмом («Лекарства»).
        bp_plain = _insert_measurement(settings, user_id, _local(YESTERDAY, 8, 0))
        bp_med = _insert_measurement(settings, user_id, _local(YESTERDAY, 9, 0))
        _sql(
            settings,
            "INSERT INTO measurement_medications (measurement_id, medication_id) VALUES (?, ?)",
            (bp_med, med_id),
        )
        food = _insert_event(
            settings,
            user_id,
            type_="food",
            local=_local(YESTERDAY, 10, 0),
            payload='{"marks":["coffee"]}',
        )
        note = _insert_event(
            settings,
            user_id,
            type_="note",
            local=_local(YESTERDAY, 11, 0),
            note="уникальная заметка",
        )
        _sql(
            settings,
            "INSERT INTO medication_logs (user_id, medication_id, timestamp, status, slot, source)"
            " VALUES (?, ?, ?, 'taken', 'morning', 'cabinet')",
            (user_id, med_id, _utc_text(_local(YESTERDAY, 19, 0))),
        )
        log_id = _sql(settings, "SELECT id FROM medication_logs WHERE user_id = ?", (user_id,))[0][0]

        def markers(page: str) -> dict[str, bool]:
            return {
                "bp_plain": f"/?edit={bp_plain}#history" in page,
                "bp_med": f"/?edit={bp_med}#history" in page,
                "food": f"/?edit_ev={food}#history" in page,
                "note": f"/?edit_ev={note}#history" in page,
                "intake": f"/?edit_int={log_id}#history" in page,
            }

        all_page = client.get("/").text
        assert all(markers(all_page).values())
        assert "🥗 Питание: кофе" in all_page
        assert "Приём лекарств: Лозартан 50 мг · утро" in all_page

        assert markers(client.get("/?filt=bp").text) == {
            "bp_plain": True,
            "bp_med": True,
            "food": False,
            "note": False,
            "intake": False,
        }
        assert markers(client.get("/?filt=med").text) == {
            "bp_plain": False,
            "bp_med": True,
            "food": False,
            "note": False,
            "intake": True,
        }
        assert markers(client.get("/?filt=fact").text) == {
            "bp_plain": False,
            "bp_med": False,
            "food": True,
            "note": False,
            "intake": False,
        }
        assert markers(client.get("/?filt=other").text) == {
            "bp_plain": False,
            "bp_med": False,
            "food": False,
            "note": True,
            "intake": False,
        }
