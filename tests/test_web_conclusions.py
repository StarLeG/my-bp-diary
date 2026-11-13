"""ЛК: заключения к «Визитам и анализам» (SPEC.md, §16.2, §6.13, §4.17; 0.5.11).

Покрывается (§13): просмотр списка и превью (одно — «📝 Заключение: «…»»,
несколько — «📝 Заключений: N»), блок «📝 {Подпись заключения}» со списком
записей (текст целиком, «✏️ Изменить», «🗑 Удалить…», «➕ Добавить» внизу),
добавление (успех, пустое после `trim()`, >4000, лимит 5 — общий с ботом),
правка, удаление с подтверждением блоком «Удалить «…»? Это действие нельзя
отменить.», чужой id — 404, CSRF (без токена — 403), «с дня события» (до
события блока и формы нет), врач не видит заключения (ни блока, ни
endpoints). Фото (`photo_file_id`) в ЛК не показывается и не загружается.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.test_web_auth import _csrf, _register, app_client, settings  # noqa: F401
from tests.test_web_doctor import _link, _login_doctor

TODAY = datetime.now(ZoneInfo("Europe/Minsk")).date()


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
    day,
    *,
    status: str = "planned",
    kind: str = "doctor",
    title: str = "Кардиолог",
) -> int:
    """Событие на конкретный день (локальная дата `starts_on`)."""
    con = sqlite3.connect(settings.db_path)
    cur = con.execute(
        "INSERT INTO appointments (user_id, kind, title, starts_on, starts_at, note, status, created_at)"
        " VALUES (?, ?, ?, ?, '10:00', '', ?, '2026-01-01 00:00:00')",
        (user_id, kind, title, day.isoformat(), status),
    )
    con.commit()
    new_id = cur.lastrowid
    con.close()
    return new_id


def _insert_conc(
    settings, appointment_id: int, text: str, photo_file_id: str | None = None
) -> int:
    """Заключение события прямой вставкой (порядок — по id)."""
    con = sqlite3.connect(settings.db_path)
    cur = con.execute(
        "INSERT INTO appointment_conclusions (appointment_id, text, photo_file_id, created_at)"
        " VALUES (?, ?, ?, '2026-01-01 00:00:00')",
        (appointment_id, text, photo_file_id),
    )
    con.commit()
    new_id = cur.lastrowid
    con.close()
    return new_id


def _location(response) -> str:
    assert response.status_code == 303
    return response.headers["location"]


def _section(page: str, anchor: str) -> str:
    """HTML секции по её id (до закрывающего </section>)."""
    start = page.index(f'id="{anchor}"')
    return page[start : page.index("</section>", start)]


def _card_chunk(page: str, title: str) -> str:
    """Карточка события целиком (до </article>) — по заголовку."""
    start = page.index(f'<h4 class="event-title">{title}</h4>')
    return page[start : page.index("</article>", start)]


def _conc_block(page: str) -> str:
    """Содержимое блока «📝 …» (до первого </details>)."""
    return page.split('<details class="event-conc">', 1)[1].split("</details>", 1)[0]


# ─────────────────────────── Просмотр ───────────────────────────


class TestView:
    def test_preview_single_truncated_and_many(self, app_client, settings) -> None:  # noqa: F811
        """Превью: одно — первые 60 символов + «…», несколько — «📝 Заключений: N»."""
        client, user_id = app_client
        _register(client)
        one = _insert_ap(settings, user_id, TODAY - timedelta(days=3), title="Одно")
        _insert_conc(settings, one, "я" * 70)
        two = _insert_ap(settings, user_id, TODAY - timedelta(days=2), title="Два")
        _insert_conc(settings, two, "Первое")
        _insert_conc(settings, two, "Второе")
        page = client.get("/").text
        assert f"📝 Заключение: «{'я' * 60}…»" in page
        assert "📝 Заключений: 2" in page

    def test_preview_collapses_whitespace(self, app_client, settings) -> None:  # noqa: F811
        """Превью — одна строка: переводы строк и лишние пробелы схлопываются."""
        client, user_id = app_client
        _register(client)
        appt_id = _insert_ap(settings, user_id, TODAY - timedelta(days=3))
        _insert_conc(settings, appt_id, "Строка первая\n\n   Строка вторая")
        assert "📝 Заключение: «Строка первая Строка вторая»" in client.get("/").text

    def test_block_lists_full_texts_with_actions(self, app_client, settings) -> None:  # noqa: F811
        """Блок «📝 …»: текст каждой целиком, «✏️ Изменить» / «🗑 Удалить…», «➕ Добавить» внизу."""
        client, user_id = app_client
        _register(client)
        appt_id = _insert_ap(settings, user_id, TODAY - timedelta(days=3), title="Кардиолог")
        first = _insert_conc(settings, appt_id, "Первое заключение")
        second = _insert_conc(settings, appt_id, "Второе заключение")
        block = _conc_block(client.get("/").text)
        assert "<summary>📝 Заключение врача</summary>" in block
        assert '<p class="conc-text">Первое заключение</p>' in block
        assert '<p class="conc-text">Второе заключение</p>' in block
        assert f'href="/?edit_conc={first}#appointments">✏️ Изменить</a>' in block
        assert f'href="/?confirm_delete_conc={first}#appointments">🗑 Удалить…</a>' in block
        assert f'href="/?edit_conc={second}#appointments">✏️ Изменить</a>' in block
        assert f'href="/?confirm_delete_conc={second}#appointments">🗑 Удалить…</a>' in block
        # список — по порядку добавления, «➕ Добавить» — внизу блока
        assert block.index("Первое заключение") < block.index("Второе заключение")
        assert block.index("Второе заключение") < block.index("➕ Добавить")
        assert f'href="/?add_conc={appt_id}#appointments">➕ Добавить</a>' in block

    def test_block_label_by_kind(self, app_client, settings) -> None:  # noqa: F811
        """Подпись блока — по типу события (В27): lab и exam."""
        client, user_id = app_client
        _register(client)
        for kind, label in (
            ("lab", "📝 Результаты анализов"),
            ("exam", "📝 Протокол обследования"),
        ):
            appt_id = _insert_ap(settings, user_id, TODAY - timedelta(days=2), kind=kind)
            _insert_conc(settings, appt_id, f"Текст {kind}")
            assert f"<summary>{label}</summary>" in client.get("/").text

    def test_photo_not_shown_or_uploaded(self, app_client, settings) -> None:  # noqa: F811
        """Фото в ЛК не показывается и не загружается (`photo_file_id` — Telegram)."""
        client, user_id = app_client
        _register(client)
        appt_id = _insert_ap(settings, user_id, TODAY - timedelta(days=2))
        _insert_conc(settings, appt_id, "Текст", photo_file_id="file-1")
        block = _conc_block(client.get("/").text)
        assert "<img" not in block and "photo" not in block and "Фото" not in block
        form = (
            client.get(f"/?add_conc={appt_id}")
            .text.split("Добавить заключение", 1)[1]
            .split("</form>", 1)[0]
        )
        assert "<input type=\"file\"" not in form
        assert "photo" not in form and "Фото" not in form

    def test_no_preview_line_without_conclusions(self, app_client, settings) -> None:  # noqa: F811
        """Без заключений строки-превью нет; блок с «➕ Добавить» — с дня события."""
        client, user_id = app_client
        _register(client)
        _insert_ap(settings, user_id, TODAY - timedelta(days=1), title="Без заключений")
        page = client.get("/").text
        chunk = _card_chunk(page, "Без заключений")
        assert "event-conc-line" not in chunk
        assert "📝 Заключение: " not in chunk
        assert '<details class="event-conc">' in chunk
        assert "➕ Добавить" in chunk


# ─────────────────────────── Добавление ───────────────────────────


class TestAdd:
    def test_form_renders(self, app_client, settings) -> None:  # noqa: F811
        """`?add_conc={id}` — форма «Добавить заключение»: textarea ≤ 4000, «Сохранить» / «Отмена»."""
        client, user_id = app_client
        _register(client)
        appt_id = _insert_ap(settings, user_id, TODAY - timedelta(days=1))
        page = client.get(f"/?add_conc={appt_id}").text
        assert '<h4 class="event-edit-title">Добавить заключение</h4>' in page
        form = page.split("Добавить заключение", 1)[1].split("</form>", 1)[0]
        assert f'action="/appointments/{appt_id}/conclusions"' in form
        assert '<textarea name="text" maxlength="4000" rows="6" required></textarea>' in form
        assert '<button type="submit" class="btn">Сохранить</button>' in form
        assert 'class="btn btn-ghost" href="/#appointments">Отмена</a>' in form

    def test_add_success(self, app_client, settings) -> None:  # noqa: F811
        """Успех: текст сохранён с `trim()`, страница — с отметкой обновления."""
        client, user_id = app_client
        _register(client)
        appt_id = _insert_ap(settings, user_id, TODAY - timedelta(days=1))
        response = client.post(
            f"/appointments/{appt_id}/conclusions",
            data={"csrf_token": _csrf(client), "text": "  Заключение врача  "},
        )
        assert _location(response) == "/?saved=appointment#appointments"
        assert _sql(settings, "SELECT text FROM appointment_conclusions") == [
            ("Заключение врача",)
        ]
        assert "📝 Заключение: «Заключение врача»" in client.get("/").text

    def test_add_max_length_ok(self, app_client, settings) -> None:  # noqa: F811
        """Ровно 4000 символов — принимается (граница §4.17)."""
        client, user_id = app_client
        _register(client)
        appt_id = _insert_ap(settings, user_id, TODAY - timedelta(days=1))
        response = client.post(
            f"/appointments/{appt_id}/conclusions",
            data={"csrf_token": _csrf(client), "text": "я" * 4000},
        )
        assert _location(response) == "/?saved=appointment#appointments"
        (text,) = _sql(settings, "SELECT text FROM appointment_conclusions")[0]
        assert len(text) == 4000

    def test_add_empty(self, app_client, settings) -> None:  # noqa: F811
        """Пустое после `trim()` — «Напишите текст заключения.», форма остаётся."""
        client, user_id = app_client
        _register(client)
        appt_id = _insert_ap(settings, user_id, TODAY - timedelta(days=1))
        response = client.post(
            f"/appointments/{appt_id}/conclusions",
            data={"csrf_token": _csrf(client), "text": "  \n  "},
        )
        assert _location(response) == f"/?add_conc={appt_id}&error=conc_empty#appointments"
        page = client.get(f"/?add_conc={appt_id}&error=conc_empty").text
        assert "Напишите текст заключения." in page
        assert '<h4 class="event-edit-title">Добавить заключение</h4>' in page
        assert _sql(settings, "SELECT COUNT(*) FROM appointment_conclusions") == [(0,)]

    def test_add_too_long(self, app_client, settings) -> None:  # noqa: F811
        """Длиннее 4000 — «Слишком длинно — до 4000 символов.», ничего не сохраняется."""
        client, user_id = app_client
        _register(client)
        appt_id = _insert_ap(settings, user_id, TODAY - timedelta(days=1))
        response = client.post(
            f"/appointments/{appt_id}/conclusions",
            data={"csrf_token": _csrf(client), "text": "я" * 4001},
        )
        assert _location(response) == f"/?add_conc={appt_id}&error=conc_long#appointments"
        page = client.get(f"/?add_conc={appt_id}&error=conc_long").text
        assert "Слишком длинно — до 4000 символов." in page
        assert _sql(settings, "SELECT COUNT(*) FROM appointment_conclusions") == [(0,)]

    def test_add_limit(self, app_client, settings) -> None:  # noqa: F811
        """Лимит 5 на событие — общий с ботом: переполнение с текстом ошибки."""
        client, user_id = app_client
        _register(client)
        appt_id = _insert_ap(settings, user_id, TODAY - timedelta(days=1))
        for index in range(5):
            _insert_conc(settings, appt_id, f"Заключение {index}")
        response = client.post(
            f"/appointments/{appt_id}/conclusions",
            data={"csrf_token": _csrf(client), "text": "Лишнее"},
        )
        assert _location(response) == f"/?add_conc={appt_id}&error=conc_limit#appointments"
        page = client.get(f"/?add_conc={appt_id}&error=conc_limit").text
        assert "Заключений уже 5. Удалите ненужные, чтобы добавить новое." in page
        assert _sql(settings, "SELECT COUNT(*) FROM appointment_conclusions") == [(5,)]


# ─────────────────────────── Правка ───────────────────────────


class TestEdit:
    def test_form_renders_prefilled(self, app_client, settings) -> None:  # noqa: F811
        """`?edit_conc={cid}` — «Изменить заключение» с текущим текстом; `?edit=` — не о нём."""
        client, user_id = app_client
        _register(client)
        appt_id = _insert_ap(settings, user_id, TODAY - timedelta(days=1))
        cid = _insert_conc(settings, appt_id, "Старый текст")
        page = client.get(f"/?edit_conc={cid}").text
        assert '<h4 class="event-edit-title">Изменить заключение</h4>' in page
        form = page.split("Изменить заключение", 1)[1].split("</form>", 1)[0]
        assert f'action="/conclusions/{cid}"' in form
        assert ">Старый текст</textarea>" in form
        assert '<button type="submit" class="btn">Сохранить</button>' in form
        assert 'class="btn btn-ghost" href="/#appointments">Отмена</a>' in form
        # `?edit={id}` занят правкой измерения (Р9) — форму заключения не открывает
        assert 'Изменить заключение' not in client.get(f"/?edit={cid}").text

    def test_edit_success(self, app_client, settings) -> None:  # noqa: F811
        """Правка заменяет текст; страница — с отметкой обновления."""
        client, user_id = app_client
        _register(client)
        appt_id = _insert_ap(settings, user_id, TODAY - timedelta(days=1))
        cid = _insert_conc(settings, appt_id, "Старый текст")
        response = client.post(
            f"/conclusions/{cid}",
            data={"csrf_token": _csrf(client), "text": " Новый текст "},
        )
        assert _location(response) == "/?saved=appointment#appointments"
        assert _sql(settings, "SELECT text FROM appointment_conclusions") == [
            ("Новый текст",)
        ]
        assert "Новый текст" in client.get("/").text

    def test_edit_empty_and_too_long(self, app_client, settings) -> None:  # noqa: F811
        """Пустое и >4000 — те же тексты ошибок, текст не меняется."""
        client, user_id = app_client
        _register(client)
        appt_id = _insert_ap(settings, user_id, TODAY - timedelta(days=1))
        cid = _insert_conc(settings, appt_id, "Остаётся")
        for raw, code, message in (
            ("   ", "conc_empty", "Напишите текст заключения."),
            ("я" * 4001, "conc_long", "Слишком длинно — до 4000 символов."),
        ):
            response = client.post(
                f"/conclusions/{cid}",
                data={"csrf_token": _csrf(client), "text": raw},
            )
            assert _location(response) == f"/?edit_conc={cid}&error={code}#appointments"
            page = client.get(f"/?edit_conc={cid}&error={code}").text
            assert message in page
            assert ">Остаётся</textarea>" in page  # форма открыта с прежним текстом
            assert _sql(settings, "SELECT text FROM appointment_conclusions") == [
                ("Остаётся",)
            ]


# ─────────────────────────── Удаление ───────────────────────────


class TestDelete:
    def test_confirm_block(self, app_client, settings) -> None:  # noqa: F811
        """Подтверждение — блоком: «Удалить «…»? Это действие нельзя отменить.» (канон §16.2)."""
        client, user_id = app_client
        _register(client)
        appt_id = _insert_ap(settings, user_id, TODAY - timedelta(days=1))
        cid = _insert_conc(settings, appt_id, "Текст заключения")
        section = _section(client.get(f"/?confirm_delete_conc={cid}").text, "appointments")
        assert "Удалить «Текст заключения»? Это действие нельзя отменить." in section
        assert f'action="/conclusions/{cid}/delete"' in section
        assert '<button type="submit" class="btn btn-danger">Удалить</button>' in section
        assert 'class="btn btn-ghost" href="/#appointments">Отмена</a>' in section

    def test_delete_after_confirm(self, app_client, settings) -> None:  # noqa: F811
        """POST удаления убирает заключение; событие остаётся."""
        client, user_id = app_client
        _register(client)
        appt_id = _insert_ap(settings, user_id, TODAY - timedelta(days=1), title="Кардиолог")
        cid = _insert_conc(settings, appt_id, "Текст заключения")
        response = client.post(
            f"/conclusions/{cid}/delete", data={"csrf_token": _csrf(client)}
        )
        assert _location(response) == "/?saved=appointment#appointments"
        assert _sql(settings, "SELECT COUNT(*) FROM appointment_conclusions") == [(0,)]
        assert _sql(settings, "SELECT title FROM appointments") == [("Кардиолог",)]
        assert "Текст заключения" not in client.get("/").text


# ─────────────────────────── «С дня события» ───────────────────────────


class TestDayGate:
    def test_before_event_day_no_block_no_form(self, app_client, settings) -> None:  # noqa: F811
        """До события блока и формы нет (Р3); POST — отказ, строки не появляется."""
        client, user_id = app_client
        _register(client)
        future = _insert_ap(settings, user_id, TODAY + timedelta(days=3), title="Завтра")
        section = _section(client.get("/").text, "appointments")
        assert "📝" not in section
        assert '<details class="event-conc">' not in section
        assert "add_conc" not in section
        page = client.get(f"/?add_conc={future}").text
        assert "Добавить заключение" not in page
        assert "<textarea" not in page
        response = client.post(
            f"/appointments/{future}/conclusions",
            data={"csrf_token": _csrf(client), "text": "Рано"},
        )
        assert _location(response) == "/?error=appointment#appointments"
        assert _sql(settings, "SELECT COUNT(*) FROM appointment_conclusions") == [(0,)]

    def test_event_day_and_past_show_block(self, app_client, settings) -> None:  # noqa: F811
        """С дня события (включительно) блок и форма добавления доступны."""
        client, user_id = app_client
        _register(client)
        today_ap = _insert_ap(settings, user_id, TODAY, title="Сегодня")
        past = _insert_ap(settings, user_id, TODAY - timedelta(days=2), title="Вчера")
        page = client.get("/").text
        assert page.count('<details class="event-conc">') == 2
        assert f'href="/?add_conc={today_ap}#appointments"' in page
        assert f'href="/?add_conc={past}#appointments"' in page
        assert "Добавить заключение" in client.get(f"/?add_conc={today_ap}").text

    def test_closed_statuses_like_bot(self, app_client, settings) -> None:  # noqa: F811
        """Закрытые статусы — как кнопка в боте: done/moved — всегда, missed/cancelled — с заключениями."""
        client, user_id = app_client
        _register(client)
        done = _insert_ap(settings, user_id, TODAY - timedelta(days=10), status="done", title="Был")
        moved = _insert_ap(settings, user_id, TODAY - timedelta(days=20), status="moved", title="Перенёс")
        missed_empty = _insert_ap(
            settings, user_id, TODAY - timedelta(days=30), status="missed", title="Пропустил"
        )
        cancelled = _insert_ap(
            settings, user_id, TODAY - timedelta(days=40), status="cancelled", title="Отменил"
        )
        missed_conc = _insert_ap(
            settings, user_id, TODAY - timedelta(days=50), status="missed", title="Есть заключение"
        )
        _insert_conc(settings, missed_conc, "Текст")
        page = client.get("/").text
        assert page.count('<details class="event-conc">') == 3
        for title in ("Был", "Перенёс", "Есть заключение"):
            assert '<details class="event-conc">' in _card_chunk(page, title)
        for title in ("Пропустил", "Отменил"):
            assert '<details class="event-conc">' not in _card_chunk(page, title)

    def test_hidden_event_keeps_preview_but_blocks_forms(self, app_client, settings) -> None:  # noqa: F811
        """Событие уехало в будущее: превью остаётся (как в боте), блок и формы — нет."""
        client, user_id = app_client
        _register(client)
        appt_id = _insert_ap(settings, user_id, TODAY - timedelta(days=3), title="Уедет")
        cid = _insert_conc(settings, appt_id, "Остаётся")
        _sql(
            settings,
            "UPDATE appointments SET starts_on = ? WHERE id = ?",
            ((TODAY + timedelta(days=5)).isoformat(), appt_id),
        )
        chunk = _card_chunk(client.get("/").text, "Уедет")
        assert '<details class="event-conc">' not in chunk
        assert "event-conc-line" in chunk
        assert "Изменить заключение" not in client.get(f"/?edit_conc={cid}").text
        edit = client.post(
            f"/conclusions/{cid}",
            data={"csrf_token": _csrf(client), "text": "Нельзя"},
        )
        assert _location(edit) == "/?error=appointment#appointments"
        delete = client.post(f"/conclusions/{cid}/delete", data={"csrf_token": _csrf(client)})
        assert _location(delete) == "/?error=appointment#appointments"
        assert _sql(settings, "SELECT text FROM appointment_conclusions") == [("Остаётся",)]


# ─────────────────────────── Изоляция и CSRF ───────────────────────────


class TestIsolation:
    def test_foreign_id_404_and_hidden(self, app_client, settings) -> None:  # noqa: F811
        """Чужой id — 404 на POST и молчаливое отсутствие форм (§7.1)."""
        client, user_id = app_client
        _register(client)
        con = sqlite3.connect(settings.db_path)
        con.execute(
            "INSERT INTO users (telegram_id, timezone, target_systolic, target_diastolic,"
            " med_remeasure_delay, onboarded, created_at)"
            " VALUES (999, 'Europe/Minsk', 130, 80, 60, 1, '2026-01-01 00:00:00')"
        )
        other_id = con.execute("SELECT id FROM users WHERE telegram_id = 999").fetchone()[0]
        con.commit()
        con.close()
        foreign_ap = _insert_ap(settings, other_id, TODAY - timedelta(days=1), title="Чужое")
        foreign_conc = _insert_conc(settings, foreign_ap, "Чужой текст")
        token = {"csrf_token": _csrf(client)}
        assert (
            client.post(
                f"/appointments/{foreign_ap}/conclusions", data={**token, "text": "x"}
            ).status_code
            == 404
        )
        assert (
            client.post(f"/conclusions/{foreign_conc}", data={**token, "text": "x"}).status_code
            == 404
        )
        assert (
            client.post(f"/conclusions/{foreign_conc}/delete", data=token).status_code == 404
        )
        for url in (
            f"/?add_conc={foreign_ap}",
            f"/?edit_conc={foreign_conc}",
            f"/?confirm_delete_conc={foreign_conc}",
        ):
            page = client.get(url).text
            assert "Чужой текст" not in page
            assert "Добавить заключение" not in page
            assert "Изменить заключение" not in page
            assert "confirm_delete_conc" not in page
        assert _sql(settings, "SELECT text FROM appointment_conclusions") == [
            ("Чужой текст",)
        ]
        assert "Чужое" not in client.get("/").text


class TestCsrf:
    def test_posts_require_csrf(self, app_client, settings) -> None:  # noqa: F811
        """Все три POST — с CSRF: без токена или с чужим — 403, данные целы."""
        client, user_id = app_client
        _register(client)
        appt_id = _insert_ap(settings, user_id, TODAY - timedelta(days=1))
        cid = _insert_conc(settings, appt_id, "Было")
        for path, data in (
            (f"/appointments/{appt_id}/conclusions", {"text": "x"}),
            (f"/appointments/{appt_id}/conclusions", {"csrf_token": "forged", "text": "x"}),
            (f"/conclusions/{cid}", {"csrf_token": "", "text": "x"}),
            (f"/conclusions/{cid}/delete", {"csrf_token": "forged"}),
        ):
            assert client.post(path, data=data).status_code == 403
        assert _sql(settings, "SELECT text FROM appointment_conclusions") == [("Было",)]


# ─────────────────────────── Кабинет врача ───────────────────────────


class TestDoctor:
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

    def test_doctor_page_has_no_conclusions(self, doctor_client, settings) -> None:  # noqa: F811
        """Врач не видит заключений пациента (В34, §21) — ни блока, ни превью."""
        client, user_id = doctor_client
        appt_id = _insert_ap(settings, user_id, TODAY - timedelta(days=2), title="Кардиолог")
        _insert_conc(settings, appt_id, "Секретный текст")
        page = client.get(f"/doctor/patient/{user_id}?days=30").text
        assert "Секретный текст" not in page
        assert "Заключение" not in page
        assert "Заключений" not in page
        assert "add_conc" not in page and "edit_conc" not in page
        assert "event-conc" not in page

    def test_doctor_session_has_no_endpoints(self, doctor_client, settings) -> None:  # noqa: F811
        """Сессия врача не открывает endpoints заключений — только вход пациента."""
        client, user_id = doctor_client
        appt_id = _insert_ap(settings, user_id, TODAY - timedelta(days=2))
        cid = _insert_conc(settings, appt_id, "Было")
        token = {"csrf_token": _csrf(client)}
        for path, data in (
            (f"/appointments/{appt_id}/conclusions", {**token, "text": "x"}),
            (f"/conclusions/{cid}", {**token, "text": "x"}),
            (f"/conclusions/{cid}/delete", token),
        ):
            response = client.post(path, data=data)
            assert response.status_code == 303
            assert response.headers["location"] == "/login"
        assert _sql(settings, "SELECT text FROM appointment_conclusions") == [("Было",)]
