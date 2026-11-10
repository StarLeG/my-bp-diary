"""«Средние по дням» в личном кабинете и в кабинете врача (SPEC.md, §16.2, §21, 0.5.3).

Покрывается: блок внутри «Сводки за период» (под метриками, над графиками),
все дни периода по возрастанию даты и «—» / «0» у дней без измерений,
подсветка строк по зоне дня (правило «Истории записей»), итог «Среднее за
день» в `<tfoot>`, граница 92 дня (93 — текст вместо таблицы), нет записей —
блока нет, сутки по часовому поясу пациента, только свои данные; у врача —
тот же блок, доступ только при `status='active'`.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from bot.config import Settings
from web.cabinet import auth, doctor_auth
from web.cabinet.app import DAILY_AVERAGES_MAX_DAYS, DOCTOR_COOKIE, create_app

FAKE_TOKEN = f"123456:{'A' * 35}"
SECRET = "test-secret"
TG_ID = 111_111
OTHER_TG_ID = 222_222
TZ = "Europe/Minsk"
TOO_LONG_TEXT = "Выберите период до 92 дней, чтобы увидеть средние по дням"


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        bot_token=FAKE_TOKEN,
        web_secret_key=SECRET,
        db_path=str(tmp_path / "cabinet.sqlite3"),
    )


@pytest.fixture
def app_client(settings: Settings):
    """TestClient: два пациента и врач (вставка синхронным sqlite3 после init_db)."""
    app = create_app(settings)
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as client:
        con = sqlite3.connect(settings.db_path)
        for tg_id in (TG_ID, OTHER_TG_ID):
            con.execute(
                "INSERT INTO users (telegram_id, timezone, target_systolic, target_diastolic,"
                " med_remeasure_delay, onboarded, created_at)"
                " VALUES (?, ?, 130, 80, 60, 1, '2026-01-01 00:00:00')",
                (tg_id, TZ),
            )
        ids = {
            tg: con.execute("SELECT id FROM users WHERE telegram_id = ?", (tg,)).fetchone()[0]
            for tg in (TG_ID, OTHER_TG_ID)
        }
        con.execute(
            "INSERT INTO doctors (email, name, password_hash, created_at)"
            " VALUES ('doc@example.org', 'Врач', 'x', '2026-01-01 00:00:00')"
        )
        doctor_id = con.execute("SELECT id FROM doctors").fetchone()[0]
        con.commit()
        con.close()
        yield client, ids[TG_ID], ids[OTHER_TG_ID], doctor_id


def _sql(settings: Settings, query: str, args: tuple = ()) -> None:
    con = sqlite3.connect(settings.db_path)
    con.execute(query, args)
    con.commit()
    con.close()


def _today() -> date:
    return datetime.now(ZoneInfo(TZ)).date()


def _add(
    settings: Settings,
    user_id: int,
    day: date,
    systolic: int,
    diastolic: int,
    pulse: int | None = 64,
    hour: int = 12,
) -> None:
    """Измерение в `hour` часов локального дня `day` (Минск), в БД — UTC."""
    moment = datetime(day.year, day.month, day.day, hour, tzinfo=ZoneInfo(TZ)).astimezone(timezone.utc)
    _sql(
        settings,
        "INSERT INTO measurements (user_id, timestamp, systolic, diastolic, pulse,"
        " medication_taken, feeling, note) VALUES (?, ?, ?, ?, ?, NULL, NULL, '')",
        (user_id, moment.strftime("%Y-%m-%d %H:%M:%S"), systolic, diastolic, pulse),
    )


def _register(client: TestClient) -> None:
    magic = auth.MagicLink(SECRET)
    client.get(f"/register?token={magic.make(TG_ID)}")
    client.post(
        "/register",
        data={
            "csrf_token": client.cookies.get("csrf_token", ""),
            "token": magic.make(TG_ID),
            "email": "user@example.org",
            "password": "secret-pass",
            "password_confirm": "secret-pass",
        },
    )


def _login_doctor(client: TestClient, doctor_id: int) -> None:
    client.cookies.set(DOCTOR_COOKIE, doctor_auth.DoctorSession(SECRET).make(doctor_id))


def _link(settings: Settings, doctor_id: int, user_id: int, status: str = "active") -> None:
    _sql(
        settings,
        "INSERT INTO doctor_patients (doctor_id, user_id, status, created_at)"
        " VALUES (?, ?, ?, '2026-01-01 00:00:00')",
        (doctor_id, user_id, status),
    )


def _block(html: str) -> str:
    """HTML блока «Средние по дням» (от его `div` до конца таблицы или текста)."""
    start = html.index('id="daily-averages"')
    end = html.index("</div>\n", html.index("<h3>Средние по дням</h3>", start))
    return html[start:end]


def _table(html: str) -> str:
    block = _block(html)
    return block[: block.index("</table>")]


def _rows(html: str) -> list[list[str]]:
    """Ячейки строк `<tbody>` блока."""
    body = _table(html).split("<tbody>")[1].split("</tbody>")[0]
    return [
        [cell.strip() for cell in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        for row in re.findall(r"<tr.*?</tr>", body, re.S)
    ]


def _row_tags(html: str) -> list[str]:
    body = _table(html).split("<tbody>")[1].split("</tbody>")[0]
    return re.findall(r"<tr[^>]*>", body)


def _total(html: str) -> list[str]:
    foot = _table(html).split("<tfoot>")[1]
    return [cell.strip() for cell in re.findall(r"<td[^>]*>(.*?)</td>", foot, re.S)]


# ─────────────────────────── Личный кабинет ───────────────────────────


class TestCabinet:
    def test_block_in_summary_above_charts(self, app_client, settings) -> None:
        client, user_id, *_ = app_client
        _register(client)
        _add(settings, user_id, _today(), 128, 82)
        html = client.get("/?days=7").text
        summary = html[html.index('id="summary"') : html.index('id="history"')]
        assert summary.index("Тренд") < summary.index('id="daily-averages"') < summary.index('class="chart-grid"')

    def test_every_day_ascending_with_gaps(self, app_client, settings) -> None:
        client, user_id, *_ = app_client
        _register(client)
        today = _today()
        d1 = today - timedelta(days=6)
        _add(settings, user_id, d1, 130, 86, pulse=70, hour=8)
        _add(settings, user_id, d1, 138, 86, pulse=72, hour=20)
        _add(settings, user_id, today, 120, 80, pulse=None)
        rows = _rows(client.get("/?days=7").text)
        assert [r[0] for r in rows] == [
            (d1 + timedelta(days=i)).strftime("%d.%m.%Y") for i in range(7)
        ]
        assert rows[0] == [d1.strftime("%d.%m.%Y"), "134", "86", "71", "2"]
        assert rows[1] == [(d1 + timedelta(days=1)).strftime("%d.%m.%Y"), "—", "—", "—", "0"]
        assert rows[6] == [today.strftime("%d.%m.%Y"), "120", "80", "—", "1"]

    def test_total_row(self, app_client, settings) -> None:
        client, user_id, *_ = app_client
        _register(client)
        today = _today()
        for hour in range(8, 17):  # день с 9 замерами весит как день с одним
            _add(settings, user_id, today - timedelta(days=1), 160, 100, pulse=None, hour=hour)
        _add(settings, user_id, today, 120, 80, pulse=70)
        html = client.get("/?days=7").text
        assert _total(html) == ["Среднее за день", "140", "90", "70", ""]
        assert '<tr class="daily-total">' in _table(html)

    def test_total_without_pulse(self, app_client, settings) -> None:
        client, user_id, *_ = app_client
        _register(client)
        _add(settings, user_id, _today(), 120, 80, pulse=None)
        assert _total(client.get("/?days=7").text)[3] == "—"

    def test_zone_highlight(self, app_client, settings) -> None:
        client, user_id, *_ = app_client
        _register(client)
        today = _today()
        _add(settings, user_id, today - timedelta(days=2), 154, 84)  # красная
        _add(settings, user_id, today - timedelta(days=1), 120, 87)  # жёлтая
        _add(settings, user_id, today, 120, 80)  # норма
        html = client.get("/?days=7").text
        tags = _row_tags(html)
        assert "zone-row-high" in tags[4] and 'title="САД: ≥ 140"' in tags[4]
        assert "zone-row-elevated" in tags[5] and 'title="ДАД: 85–89"' in tags[5]
        assert tags[6] == "<tr>"
        assert tags[0] == "<tr>"  # пустой день — без подсветки
        assert "САД 130–139 · ДАД 85–89" in _block(html)  # легенда

    def test_30_days(self, app_client, settings) -> None:
        client, user_id, *_ = app_client
        _register(client)
        _add(settings, user_id, _today(), 120, 80)
        assert len(_rows(client.get("/?days=30").text)) == 30

    def test_92_days_table_93_text(self, app_client, settings) -> None:
        client, user_id, *_ = app_client
        _register(client)
        today = _today()
        _add(settings, user_id, today, 120, 80)
        assert DAILY_AVERAGES_MAX_DAYS == 92
        start = today - timedelta(days=91)
        html = client.get(f"/?days=custom&date_from={start}&date_to={today}").text
        assert len(_rows(html)) == 92
        assert TOO_LONG_TEXT not in html
        start = today - timedelta(days=92)
        html = client.get(f"/?days=custom&date_from={start}&date_to={today}").text
        block = _block(html)
        assert TOO_LONG_TEXT in block and "<table" not in block
        assert "Систолическое" in html  # сводка работает как прежде

    def test_no_records_no_block(self, app_client, settings) -> None:
        client, user_id, other_id, _ = app_client
        _register(client)
        _add(settings, other_id, _today(), 199, 120)  # чужая запись
        html = client.get("/?days=7").text
        assert 'id="daily-averages"' not in html
        assert "199" not in html

    def test_local_day_of_patient(self, app_client, settings) -> None:
        """23:30 по Минску — тот же день, хотя в UTC это 20:30 (а 01:30 — уже следующий)."""
        client, user_id, *_ = app_client
        _register(client)
        yesterday = _today() - timedelta(days=1)
        _add(settings, user_id, yesterday, 150, 95, hour=23)
        _add(settings, user_id, yesterday, 120, 80, hour=0)
        rows = _rows(client.get("/?days=7").text)
        assert rows[5] == [yesterday.strftime("%d.%m.%Y"), "135", "88", "64", "2"]

    def test_only_own_data(self, app_client, settings) -> None:
        client, user_id, other_id, _ = app_client
        _register(client)
        _add(settings, user_id, _today(), 120, 80)
        _add(settings, other_id, _today(), 200, 120)
        rows = _rows(client.get("/?days=7").text)
        assert rows[-1][1:] == ["120", "80", "64", "1"]

    def test_css_styles_total(self, app_client) -> None:
        client, *_ = app_client
        css = client.get("/static/style.css").text
        assert ".daily-table tfoot td" in css
        assert ".daily-table tfoot {" in css


# ─────────────────────────── Кабинет врача ───────────────────────────


class TestDoctor:
    def test_block_for_active_patient(self, app_client, settings) -> None:
        client, user_id, _, doctor_id = app_client
        _link(settings, doctor_id, user_id)
        today = _today()
        _add(settings, user_id, today - timedelta(days=1), 154, 84, pulse=70)
        _login_doctor(client, doctor_id)
        html = client.get(f"/doctor/patient/{user_id}?days=7").text
        rows = _rows(html)
        assert len(rows) == 7
        assert rows[5] == [(today - timedelta(days=1)).strftime("%d.%m.%Y"), "154", "84", "70", "1"]
        assert "zone-row-high" in _row_tags(html)[5]
        assert _total(html) == ["Среднее за день", "154", "84", "70", ""]
        summary = html[html.index('id="summary"') :]
        assert summary.index("Средние по дням") > summary.index("Тренд")

    def test_too_long_text(self, app_client, settings) -> None:
        client, user_id, _, doctor_id = app_client
        _link(settings, doctor_id, user_id)
        today = _today()
        _add(settings, user_id, today, 120, 80)
        _login_doctor(client, doctor_id)
        start = today - timedelta(days=120)
        html = client.get(f"/doctor/patient/{user_id}?days=custom&date_from={start}&date_to={today}").text
        assert TOO_LONG_TEXT in _block(html)

    @pytest.mark.parametrize("status", ["pending", "revoked"])
    def test_requires_active_link(self, app_client, settings, status) -> None:
        client, user_id, _, doctor_id = app_client
        _link(settings, doctor_id, user_id, status=status)
        _add(settings, user_id, _today(), 154, 84)
        _login_doctor(client, doctor_id)
        response = client.get(f"/doctor/patient/{user_id}")
        assert response.status_code == 303
        assert response.headers["location"] == "/doctor?error=denied"

    def test_other_patient_denied(self, app_client, settings) -> None:
        client, user_id, other_id, doctor_id = app_client
        _link(settings, doctor_id, user_id)
        _add(settings, other_id, _today(), 154, 84)
        _login_doctor(client, doctor_id)
        response = client.get(f"/doctor/patient/{other_id}")
        assert response.status_code == 303
        assert response.headers["location"] == "/doctor?error=denied"
