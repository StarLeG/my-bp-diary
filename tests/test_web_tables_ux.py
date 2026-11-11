"""ЛК и кабинет врача: таблицы, карточки и кнопки (SPEC.md, §16.2, §21; 0.5.9).

Покрывается (§13): карточки «Скоро» (заметка целиком, `white-space: pre-wrap`,
«⋯», подпись отмены по типу — В7а — и только у `planned`, форма правки в
карточке позволяет сменить тип (Вид) — в отличие от бота, Н4, §6.13);
«Прошедшие» аккордеоном по месяцам (текущий развёрнут, «Ждёт отметки» сверху
вне групп, форма «событие / события / событий»; `planned` > 3 дней — в группе
своего месяца с подписью «без отметки», не в «Ждёт отметки»); история
аккордеоном по дням (заголовок: число записей, средние, точка зоны;
развёрнуты дни за последние 7 дней; период ≤ 7 дней — без свёртки и без
кнопки; «Свернуть все / Развернуть все»; > 92 дней — «Показать ещё 30 дней»
и `?shown=`, кнопка исчезает на полном периоде, ≤ 92 — кнопки нет); правка
лекарств формой `?edit_med={id}` (в строке нет инпутов и нет пары
«Сохранить»/«Удалить»); единые роли кнопок и словарь подписей (все
подтверждения удаления — блоком «Удалить «…»? Это действие нельзя отменить.»,
подтверждение отмены события — по типу); врач — тот же `history_day.html`
без действий.
"""
from __future__ import annotations

import html
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tests.test_web_auth import _csrf, _register, app_client, settings  # noqa: F401
from tests.test_web_doctor import _link, _login_doctor
from web.cabinet.app import _MONTHS_NOMINATIVE, _WEEKDAYS_SHORT
from web.cabinet.chart_zones import day_zone

TODAY = datetime.now(ZoneInfo("Europe/Minsk")).date()
TZ = ZoneInfo("Europe/Minsk")
STYLE_CSS = Path(__file__).resolve().parents[1] / "web" / "cabinet" / "static" / "style.css"


def _sql(settings, query: str, args: tuple = ()) -> list[tuple]:  # noqa: F811
    con = sqlite3.connect(settings.db_path)
    try:
        rows = con.execute(query, args).fetchall()
        con.commit()
    finally:
        con.close()
    return rows


def _insert_ap(
    settings,
    user_id: int,
    day: date,
    *,
    status: str = "planned",
    kind: str = "doctor",
    title: str = "Кардиолог",
    note: str = "",
    starts_at: str = "10:00",
) -> int:
    """Событие на конкретный день (локальная дата `starts_on`)."""
    con = sqlite3.connect(settings.db_path)
    cur = con.execute(
        "INSERT INTO appointments (user_id, kind, title, starts_on, starts_at, note, status, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, '2026-01-01 00:00:00')",
        (user_id, kind, title, day.isoformat(), starts_at, note, status),
    )
    con.commit()
    new_id = cur.lastrowid
    con.close()
    return new_id


def _insert_measurement(
    settings,
    user_id: int,
    day: date,
    systolic: int,
    diastolic: int,
    *,
    pulse: int = 64,
    note: str = "",
    hour: int = 12,
) -> int:
    """Измерение в полдень UTC выбранного дня (локальная дата — та же)."""
    moment = datetime(day.year, day.month, day.day, hour, 0, tzinfo=timezone.utc)
    con = sqlite3.connect(settings.db_path)
    cur = con.execute(
        "INSERT INTO measurements (user_id, timestamp, systolic, diastolic, pulse,"
        " medication_taken, feeling, note)"
        " VALUES (?, ?, ?, ?, ?, 0, NULL, ?)",
        (
            user_id,
            moment.strftime("%Y-%m-%d %H:%M:%S"),
            systolic,
            diastolic,
            pulse,
            note,
        ),
    )
    con.commit()
    new_id = cur.lastrowid
    con.close()
    return new_id


def _insert_med(
    settings, user_id: int, name: str = "Лозартан", dosage: str = "50 мг", slot: str = "morning"
) -> int:
    con = sqlite3.connect(settings.db_path)
    cur = con.execute(
        "INSERT INTO medications (user_id, name, dosage, slot, created_at)"
        " VALUES (?, ?, ?, ?, '2026-01-01 00:00:00')",
        (user_id, name, dosage, slot),
    )
    con.commit()
    new_id = cur.lastrowid
    con.close()
    return new_id


def _section(page: str, anchor: str) -> str:
    """HTML секции карточки по её id (до закрывающего </section>)."""
    start = page.index(f'id="{anchor}"')
    return page[start : page.index("</section>", start)]


def _day_label(day: date) -> str:
    return f"{_WEEKDAYS_SHORT[day.weekday()]}, {day:%d.%m}"


def _month_label(day: date) -> str:
    return f"{_MONTHS_NOMINATIVE[day.month - 1]} {day.year}"


def _day_chunks(page: str) -> list[str]:
    """Куски `<details class="history-day"…` — по одному на день (без префикса страницы)."""
    return page.split('<details class="history-day"')[1:]


def _month_chunks(page: str) -> list[str]:
    """Куски `<details class="month-group"…` — по одному на месяц (без префикса страницы)."""
    return page.split('<details class="month-group"')[1:]


# ─────────────────────────── Карточки «Скоро» ───────────────────────────


class TestUpcomingCards:
    def test_note_shown_fully_with_pre_wrap(self, app_client, settings) -> None:  # noqa: F811
        """Заметка — целиком, переносами (`white-space: pre-wrap`, §16.2)."""
        client, user_id = app_client
        _register(client)
        note = "каб. 212\nвторой этаж, вход со двора"
        _insert_ap(settings, user_id, TODAY + timedelta(days=3), note=note)
        page = client.get("/").text
        assert '<p class="event-note">каб. 212\nвторой этаж, вход со двора</p>' in page
        css = STYLE_CSS.read_text(encoding="utf-8")
        note_rule = css[css.index(".event-note {") : css.index("}", css.index(".event-note {"))]
        assert "white-space: pre-wrap" in note_rule

    def test_more_menu_and_cancel_labels_by_type(self, app_client, settings) -> None:  # noqa: F811
        """«⋯» в каждой карточке; подпись отмены — по типу события (В7а)."""
        client, user_id = app_client
        _register(client)
        doctor = _insert_ap(settings, user_id, TODAY + timedelta(days=2), kind="doctor", title="Кардиолог")
        lab = _insert_ap(settings, user_id, TODAY + timedelta(days=3), kind="lab", title="Анализы")
        exam = _insert_ap(settings, user_id, TODAY + timedelta(days=4), kind="exam", title="ЭКГ")
        page = client.get("/").text
        section = _section(page, "appointments")
        assert section.count('<details class="event-more">') == 3
        assert '<summary class="btn btn-ghost">⋯</summary>' in section
        assert f'href="/?confirm_cancel_ap={doctor}#appointments">Отменить визит</a>' in section
        assert f'href="/?confirm_cancel_ap={lab}#appointments">Отменить сдачу</a>' in section
        assert f'href="/?confirm_cancel_ap={exam}#appointments">Отменить обследование</a>' in section
        for appt_id in (doctor, lab, exam):
            assert f'href="/?confirm_delete_ap={appt_id}#appointments">🗑 Удалить…</a>' in section

    def test_cancel_only_for_upcoming_planned(self, app_client, settings) -> None:  # noqa: F811
        """У прошедших («ждёт отметки», «без отметки») отмены нет — только удаление."""
        client, user_id = app_client
        _register(client)
        _insert_ap(settings, user_id, TODAY - timedelta(days=1), title="ЖдётА")
        _insert_ap(settings, user_id, TODAY - timedelta(days=5), title="Старое")
        page = client.get("/").text
        assert "Отменить визит" not in page
        assert "confirm_cancel_ap=" not in page
        assert "🗑 Удалить…" in page

    def test_edit_form_switches_kind(self, app_client, settings) -> None:  # noqa: F811
        """Форма правки в карточке: все три типа (Вид) — и смена типа сохраняется (Н4)."""
        client, user_id = app_client
        _register(client)
        appt_id = _insert_ap(settings, user_id, TODAY + timedelta(days=3), kind="doctor", title="Кардиолог")
        page = client.get(f"/?edit_ap={appt_id}").text
        assert '<h4 class="event-edit-title">Изменить событие</h4>' in page
        form = page[page.index('class="stack form-narrow event-edit-form"') :]
        assert '<select name="kind">' in form
        for kind in ("doctor", "lab", "exam"):
            assert f'<option value="{kind}"' in form
        assert '<option value="doctor" selected>' in form
        assert '<button type="submit" class="btn">Сохранить</button>' in form
        assert 'class="btn btn-ghost" href="/#appointments">Отмена</a>' in form
        assert f'href="/?confirm_delete_ap={appt_id}#appointments">🗑 Удалить</a>' in form
        response = client.post(
            f"/appointments/{appt_id}",
            data={
                "csrf_token": _csrf(client),
                "kind": "lab",
                "title": "Общий анализ крови",
                "date": (TODAY + timedelta(days=3)).isoformat(),
                "time": "07:30",
                "note": "натощак",
            },
        )
        assert response.status_code == 303
        assert _sql(settings, "SELECT kind, title FROM appointments") == [("lab", "Общий анализ крови")]
        assert '<span class="event-kind">🧪 Анализы</span>' in client.get("/").text


# ─────────────────────────── «Прошедшие» аккордеоном ───────────────────────────


class TestPastAccordion:
    def _past_day(self) -> date:
        """День прошлого месяца, гарантированно старше 3 дней (не «Ждёт отметки»)."""
        candidate = TODAY.replace(day=1) - timedelta(days=1)
        while (TODAY - candidate).days <= 3:
            candidate = candidate.replace(day=1) - timedelta(days=1)
        return candidate

    def test_awaiting_on_top_old_planned_in_month(self, app_client, settings) -> None:  # noqa: F811
        """«Ждёт отметки» — сверху вне групп; `planned` > 3 дней — «без отметки» в месяце."""
        client, user_id = app_client
        _register(client)
        _insert_ap(settings, user_id, TODAY - timedelta(days=1), title="ЖдётА")
        _insert_ap(settings, user_id, TODAY - timedelta(days=5), title="Старое")
        page = client.get("/").text
        awaiting_pos = page.index("ЖдётА")
        month_pos = page.index('class="month-group"')
        old_pos = page.index("Старое")
        assert awaiting_pos < month_pos < old_pos
        assert '<h4 class="awaiting-head">Ждёт отметки</h4>' in page
        assert 'class="awaiting-block"' in page[:month_pos]
        assert "Ждёт отметки" not in page[month_pos:]
        assert "без отметки" in page[month_pos:]
        assert "без отметки" not in page[:month_pos]

    def test_current_month_open_others_closed(self, app_client, settings) -> None:  # noqa: F811
        """`<details open>` — только текущий месяц; группы и события — новые сверху."""
        client, user_id = app_client
        _register(client)
        cur_day = max(TODAY - timedelta(days=1), TODAY.replace(day=1))
        prev_day = TODAY.replace(day=1) - timedelta(days=1)
        _insert_ap(settings, user_id, cur_day, status="done", title="Текущее")
        _insert_ap(settings, user_id, prev_day, status="done", title="Прошлое")
        page = client.get("/").text
        groups = [chunk.split("</details>", 1)[0] for chunk in _month_chunks(page)]
        assert len(groups) == 2
        current, previous = groups  # новые сверху
        assert current.startswith(" open>") and _month_label(cur_day) in current
        assert not previous.startswith(" open>") and _month_label(prev_day) in previous
        assert page.index("Текущее") < page.index("Прошлое")

    @pytest.mark.parametrize(
        ("count", "expected"),
        [(1, "1 событие"), (2, "2 события"), (5, "5 событий")],
    )
    def test_month_count_plural(self, app_client, settings, count, expected) -> None:  # noqa: F811
        """Заголовок группы — «Ноябрь 2026 · 5 событий» (правильная форма)."""
        client, user_id = app_client
        _register(client)
        prev_day = self._past_day()
        for index in range(count):
            _insert_ap(settings, user_id, prev_day, status="done", title=f"С{index}")
        page = client.get("/").text
        assert f"{_month_label(prev_day)} · {expected}" in page


# ─────────────────────────── История аккордеоном ───────────────────────────


class TestHistoryAccordion:
    def test_day_headers_counts_averages_zones(self, app_client, settings) -> None:  # noqa: F811
        """Заголовок дня: дата, число записей, средние САД/ДАД, точка зоны."""
        client, user_id = app_client
        _register(client)
        for hour in (8, 20):
            _insert_measurement(settings, user_id, TODAY, 132, 84, hour=hour)
        _insert_measurement(settings, user_id, TODAY - timedelta(days=3), 120, 70)
        for hour in range(8, 13):
            _insert_measurement(settings, user_id, TODAY - timedelta(days=5), 150, 95, hour=hour)
        page = client.get("/").text
        for day, count_text, avg in (
            (TODAY, "2 записи", "132/84"),
            (TODAY - timedelta(days=3), "1 запись", "120/70"),
            (TODAY - timedelta(days=5), "5 записей", "150/95"),
        ):
            label = _day_label(day)
            assert f'<span class="history-day-title">{label}</span>' in page
            assert f'<span class="history-day-meta">· {count_text} · в среднем {avg} ·</span>' in page
            sys_avg, dia_avg = (int(v) for v in avg.split("/"))
            zone = day_zone(sys_avg, dia_avg)
            assert (
                f'<span class="day-dot" title="{html.escape(zone["title"])}">{zone["dot"]}</span>'
                in page
            )

    def test_recent_days_open_old_closed(self, app_client, settings) -> None:  # noqa: F811
        """Развёрнуты дни за последние 7 дней; старше — свёрнуты."""
        client, user_id = app_client
        _register(client)
        _insert_measurement(settings, user_id, TODAY, 128, 82)
        _insert_measurement(settings, user_id, TODAY - timedelta(days=10), 128, 82)
        page = client.get("/?days=30").text
        chunks = _day_chunks(page)
        assert len(chunks) == 2
        today_chunk = next(c for c in chunks if _day_label(TODAY) in c)
        old_chunk = next(c for c in chunks if _day_label(TODAY - timedelta(days=10)) in c)
        assert today_chunk.startswith(" open>")
        assert not old_chunk.startswith(" open>")
        assert 'data-days-toggle="open">Развернуть все</button>' in page
        assert 'data-days-toggle="close">Свернуть все</button>' in page

    def test_short_period_flat_without_buttons(self, app_client, settings) -> None:  # noqa: F811
        """Период ≤ 7 дней — без свёртки и без кнопки «Свернуть все / Развернуть все»."""
        client, user_id = app_client
        _register(client)
        _insert_measurement(settings, user_id, TODAY, 128, 82)
        _insert_measurement(settings, user_id, TODAY - timedelta(days=3), 128, 82)
        page = client.get("/").text
        assert 'class="history-day history-day-flat"' in page
        assert '<details class="history-day"' not in page
        assert "Свернуть все" not in page
        assert "Развернуть все" not in page

    def test_long_period_more_button_and_shown(self, app_client, settings) -> None:  # noqa: F811
        """> 92 дней: «Показать ещё 30 дней» и `?shown=`; кнопка исчезает на полном периоде."""
        client, user_id = app_client
        _register(client)
        date_from = (TODAY - timedelta(days=119)).isoformat()
        date_to = TODAY.isoformat()
        _insert_measurement(settings, user_id, TODAY, 128, 82)
        _insert_measurement(
            settings, user_id, TODAY - timedelta(days=110), 128, 82, note="дремучая запись"
        )
        base = f"/?days=custom&date_from={date_from}&date_to={date_to}"
        page = client.get(base).text
        assert "Показать ещё 30 дней" in page
        assert f"shown=120#history" in page
        assert "дремучая запись" not in page
        full = client.get(f"{base}&shown=120").text
        assert "дремучая запись" in full
        assert "Показать ещё 30 дней" not in full

    def test_no_more_button_for_short_periods(self, app_client, settings) -> None:  # noqa: F811
        """≤ 92 дней — кнопки «Показать ещё» нет (30 дней и ровно 92)."""
        client, user_id = app_client
        _register(client)
        _insert_measurement(settings, user_id, TODAY, 128, 82)
        assert "Показать ещё 30 дней" not in client.get("/?days=30").text
        date_from = (TODAY - timedelta(days=91)).isoformat()
        page = client.get(f"/?days=custom&date_from={date_from}&date_to={TODAY.isoformat()}").text
        assert "Показать ещё 30 дней" not in page


# ─────────────────────────── Лекарства ───────────────────────────


class TestMedications:
    def test_rows_without_inputs_and_button_pairs(self, app_client, settings) -> None:  # noqa: F811
        """Строки — только чтение: без инпутов и без пары «Сохранить»/«Удалить» (В6)."""
        client, user_id = app_client
        _register(client)
        med_id = _insert_med(settings, user_id)
        table = _section(client.get("/").text, "medications").split("<table>", 1)[1].split("</table>")[0]
        assert "<input" not in table
        assert "<button" not in table
        assert "Сохранить" not in table
        assert ">Удалить<" not in table
        assert f'href="/?edit_med={med_id}#medications" title="Изменить">✏️</a>' in table
        assert f'href="/?confirm_delete_med={med_id}#medications" title="Удалить">🗑</a>' in table
        assert 'class="btn-del"' in table

    def test_edit_form_by_edit_med(self, app_client, settings) -> None:  # noqa: F811
        """`?edit_med={id}` — форма «Изменить лекарство», предзаполненная (не `?edit={id}`)."""
        client, user_id = app_client
        _register(client)
        med_id = _insert_med(settings, user_id)
        page = client.get(f"/?edit_med={med_id}").text
        assert "<h3>Изменить лекарство</h3>" in page
        form = page[page.index("<h3>Изменить лекарство</h3>") :]
        assert f'action="/medications/{med_id}"' in form
        assert 'value="Лозартан"' in form and 'value="50 мг"' in form
        assert '<select name="slot">' in form
        assert '<button type="submit" class="btn">Сохранить</button>' in form
        assert 'class="btn btn-ghost" href="/#medications">Отмена</a>' in form
        assert "Добавить лекарство" not in form.split("</form>")[0]
        # `?edit=` — про измерения, форму лекарства не открывает
        assert "<h3>Изменить лекарство</h3>" not in client.get(f"/?edit={med_id}").text

    def test_delete_confirm_block(self, app_client, settings) -> None:  # noqa: F811
        """Подтверждение удаления лекарства — блоком, финал — залитая красная."""
        client, user_id = app_client
        _register(client)
        med_id = _insert_med(settings, user_id)
        page = client.get(f"/?confirm_delete_med={med_id}").text
        section = _section(page, "medications")
        assert "Удалить «Лозартан»? Это действие нельзя отменить." in section
        assert '<button type="submit" class="btn btn-danger">Удалить</button>' in section
        assert 'class="btn btn-ghost" href="/#medications">Отмена</a>' in section


# ─────────────────────────── Единый UI-канон ───────────────────────────


class TestUnifiedLabels:
    def test_no_js_confirm_anywhere(self, app_client, settings) -> None:  # noqa: F811
        """Все подтверждения — блоками на странице, не `js confirm()`."""
        client, user_id = app_client
        _register(client)
        _insert_measurement(settings, user_id, TODAY, 128, 82)
        _insert_med(settings, user_id)
        _insert_ap(settings, user_id, TODAY + timedelta(days=2))
        page = client.get("/").text
        assert "confirm(" not in page
        assert "onsubmit" not in page

    def test_delete_confirmations_use_one_template(self, app_client, settings) -> None:  # noqa: F811
        """«Удалить «…»? Это действие нельзя отменить.» — запись, лекарство, событие."""
        client, user_id = app_client
        _register(client)
        moment = datetime(TODAY.year, TODAY.month, TODAY.day, 12, 0, tzinfo=timezone.utc) - timedelta(days=1)
        measurement_id = _insert_measurement(settings, user_id, TODAY - timedelta(days=1), 128, 82)
        med_id = _insert_med(settings, user_id)
        appt_id = _insert_ap(settings, user_id, TODAY + timedelta(days=2), title="Кардиолог")
        when = moment.astimezone(TZ).strftime("%d.%m.%Y %H:%M")
        assert (
            f"Удалить запись от {when}? Это действие нельзя отменить."
            in client.get(f"/?confirm_delete={measurement_id}").text
        )
        assert (
            "Удалить «Лозартан»? Это действие нельзя отменить."
            in client.get(f"/?confirm_delete_med={med_id}").text
        )
        page = client.get(f"/?confirm_delete_ap={appt_id}").text
        assert f"Удалить «Кардиолог — {(TODAY + timedelta(days=2)):%d.%m}»? Это действие нельзя отменить." in page
        assert '<button type="submit" class="btn btn-danger">Удалить</button>' in _section(page, "appointments")

    def test_cancel_confirmation_by_type(self, app_client, settings) -> None:  # noqa: F811
        """Отмена события — подтверждение с подписью по типу (В7а), не опасная кнопка."""
        client, user_id = app_client
        _register(client)
        day = TODAY + timedelta(days=3)
        doctor = _insert_ap(settings, user_id, day, kind="doctor", title="Кардиолог")
        lab = _insert_ap(settings, user_id, day, kind="lab", title="Общий анализ крови")
        for appt_id, label in ((doctor, "Отменить визит"), (lab, "Отменить сдачу")):
            page = client.get(f"/?confirm_cancel_ap={appt_id}").text
            section = _section(page, "appointments")
            title = "Кардиолог" if appt_id == doctor else "Общий анализ крови"
            assert (
                f'{label} «{title} — {day:%d.%m}»? Событие останется в «Прошедших»'
                f" со статусом «отменено»." in section
            )
            assert f'<button type="submit" class="btn">{label}</button>' in section
            assert 'class="btn btn-ghost" href="/#appointments">Отмена</a>' in section

    def test_button_labels_dictionary(self, app_client, settings) -> None:  # noqa: F811
        """«Записать» / «Сохранить» / «Добавить»; «Сохранить изменения» не используется."""
        client, user_id = app_client
        _register(client)
        measurement_id = _insert_measurement(settings, user_id, TODAY, 128, 82)
        _insert_med(settings, user_id)
        page = client.get(f"/?edit={measurement_id}").text
        assert "<h3>Изменить запись</h3>" in page
        assert 'class="btn">\n              Сохранить\n            </button>' in page
        assert "Сохранить изменения" not in page
        add = client.get("/?add=1").text
        assert "<h3>Добавить запись</h3>" in add
        assert 'class="btn">\n              Записать\n            </button>' in add

    def test_button_roles_in_css(self, app_client) -> None:  # noqa: F811
        """Роли канона в CSS: «Опасное в строке» (`btn-del`) и «Опасное вне строки»."""
        css = STYLE_CSS.read_text(encoding="utf-8")
        assert ".btn-del {" in css
        assert ".btn-danger-ghost {" in css
        assert ".btn-danger {" in css

    def test_profile_delete_is_danger_ghost(self, app_client) -> None:  # noqa: F811
        """«Удалить данные» (личная карточка) — роль «Опасное вне строки»."""
        client, _ = app_client
        _register(client)
        response = client.post(
            "/profile/personal",
            data={
                "csrf_token": _csrf(client),
                "last_name": "Иванов",
                "first_name": "Иван",
                "middle_name": "Иванович",
                "birth_date": "1958-03-12",
                "sex": "m",
            },
        )
        assert response.status_code == 303
        assert 'class="btn btn-danger-ghost">Удалить данные</button>' in client.get("/").text


# ─────────────────────────── Кабинет врача ───────────────────────────


class TestDoctorHistory:
    @pytest.fixture
    def doctor_client(self, app_client, settings):  # noqa: F811
        """Пациент и врач со связью `active` (согласия — триггерами conftest)."""
        client, user_id = app_client
        con = sqlite3.connect(settings.db_path)
        con.execute(
            "INSERT INTO doctors (email, name, password_hash, created_at)"
            " VALUES ('doc@example.org', 'Врач', 'x', '2026-01-01 00:00:00')"
        )
        doctor_id = con.execute("SELECT id FROM doctors").fetchone()[0]
        con.commit()
        con.close()
        _link(settings, doctor_id, user_id)
        _login_doctor(client, doctor_id)
        return client, user_id

    def test_same_history_partial_without_actions(self, doctor_client, settings) -> None:  # noqa: F811
        """Врач видит тот же `history_day.html`: заголовки дней, без колонки действий."""
        client, user_id = doctor_client
        _insert_measurement(settings, user_id, TODAY, 128, 82, note="после сна")
        _insert_measurement(settings, user_id, TODAY - timedelta(days=10), 150, 95)
        page = client.get(f"/doctor/patient/{user_id}?days=30").text
        assert '<summary class="history-day-head">' in page
        assert f'<span class="history-day-title">{_day_label(TODAY)}</span>' in page
        assert "в среднем" in page
        assert 'data-days-toggle="close">Свернуть все</button>' in page
        # только чтение (§21): нет действий и ссылок правки/удаления
        assert "Действия" not in page
        assert "?edit=" not in page
        assert "?confirm_delete" not in page
        assert "btn-del" not in page
        # колонки таблицы дня сохранены
        assert 'data-label="Лекарство"' in page
        assert 'data-label="Заметка"' in page
