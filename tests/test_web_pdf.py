"""PDF-отчёт в личном кабинете и в кабинете врача (SPEC.md, §10.1, §16.2, §21).

Покрывается: `GET /export/report.pdf` (только вошедший пациент и только его
данные, шапка «Дневник давления» без telegram_id, период фильтра, имя файла,
пустой период и лимит — сообщение на странице, кнопка «Скачать PDF» на
дашборде, генерация вне event loop) и `GET /doctor/patient/{id}/export.pdf`
(шапка «Пациент {telegram_id}», пустой период, доступ только при
`status='active'`).
"""
from __future__ import annotations

import io
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader

from bot.config import Settings
from bot.services import pdf_report
from web.cabinet import auth, doctor_auth
from web.cabinet.app import DOCTOR_COOKIE, create_app

FAKE_TOKEN = f"123456:{'A' * 35}"
SECRET = "test-secret"
TG_ID = 111_111
OTHER_TG_ID = 222_222
TZ = "Europe/Minsk"


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


def _add(settings: Settings, user_id: int, systolic: int, ago: timedelta = timedelta(hours=1), note: str = "") -> None:
    """Измерение `ago` назад (UTC в формате БД)."""
    moment = (datetime.now(timezone.utc) - ago).strftime("%Y-%m-%d %H:%M:%S")
    _sql(
        settings,
        "INSERT INTO measurements (user_id, timestamp, systolic, diastolic, pulse,"
        " medication_taken, feeling, note) VALUES (?, ?, ?, 82, 64, 1, 'normal', ?)",
        (user_id, moment, systolic, note),
    )


def _register(client: TestClient) -> None:
    """Учётная запись кабинета по magic link — клиент остаётся залогиненным."""
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


def _text(payload: bytes) -> str:
    return "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(payload)).pages)


def _today() -> str:
    return datetime.now(ZoneInfo(TZ)).date().strftime("%Y%m%d")


# ─────────────────────────── Личный кабинет ───────────────────────────


class TestCabinetPdf:
    def test_requires_login(self, app_client) -> None:
        client, *_ = app_client
        response = client.get("/export/report.pdf")
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    def test_report(self, app_client, settings) -> None:
        client, user_id, other_id, _ = app_client
        _register(client)
        _add(settings, user_id, 154, note="после прогулки")
        _add(settings, other_id, 199)
        response = client.get("/export/report.pdf?days=7")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/pdf"
        start = (datetime.now(ZoneInfo(TZ)).date() - timedelta(days=6)).strftime("%Y%m%d")
        assert response.headers["content-disposition"] == (
            f'attachment; filename="bp_diary_{start}-{_today()}.pdf"'
        )
        text = _text(response.content)
        assert "Дневник давления · сформирован" in text
        assert str(TG_ID) not in text  # файл пациента без telegram_id
        assert "154" in text and "после прогулки" in text and "Принято" in text
        assert "199" not in text  # чужие записи не попадают

    def test_custom_period(self, app_client, settings) -> None:
        client, user_id, *_ = app_client
        _register(client)
        _add(settings, user_id, 131, ago=timedelta(days=20))
        _add(settings, user_id, 145)
        day = (datetime.now(ZoneInfo(TZ)).date() - timedelta(days=20)).isoformat()
        response = client.get(f"/export/report.pdf?days=custom&date_from={day}&date_to={day}")
        assert response.status_code == 200
        text = _text(response.content)
        assert "131" in text and "145" not in text

    def test_empty_period(self, app_client, settings) -> None:
        client, user_id, *_ = app_client
        _register(client)
        _add(settings, user_id, 131, ago=timedelta(days=20))
        response = client.get("/export/report.pdf?days=7")
        assert response.status_code == 303
        assert response.headers["location"] == "/?days=7&date_from=&date_to=&error=empty"
        page = client.get(response.headers["location"])
        assert "За этот период записей нет." in page.text

    def test_too_many(self, app_client, settings, monkeypatch) -> None:
        client, user_id, *_ = app_client
        _register(client)
        _add(settings, user_id, 131, ago=timedelta(hours=2))
        _add(settings, user_id, 132)
        monkeypatch.setattr(pdf_report, "REPORT_LIMIT", 1)
        response = client.get("/export/report.pdf?days=30")
        assert response.status_code == 303
        assert response.headers["location"] == "/?days=30&date_from=&date_to=&error=too_many"
        page = client.get(response.headers["location"])
        assert "Слишком много записей, выберите период поменьше." in page.text

    def test_dashboard_button(self, app_client) -> None:
        client, *_ = app_client
        _register(client)
        page = client.get("/?days=30")
        assert 'href="/export/report.pdf?days=30&date_from=&date_to="' in page.text
        assert "Скачать PDF" in page.text

    def test_pdf_runs_in_thread(self, app_client, settings, monkeypatch, loop_gate) -> None:
        client, user_id, *_ = app_client
        _register(client)
        _add(settings, user_id, 131)
        gate = loop_gate(b"%PDF-gate")
        monkeypatch.setattr(pdf_report, "build_pdf", gate)
        pdf, page = gate.assert_loop_free(
            heavy=lambda: client.get("/export/report.pdf"),
            light=lambda: client.get("/robots.txt"),
        )
        assert pdf.status_code == 200 and pdf.content == b"%PDF-gate"
        assert page.status_code == 200
        assert gate.calls == 1


# ─────────────────────────── Кабинет врача ───────────────────────────


class TestDoctorPdf:
    def test_report_with_patient_label(self, app_client, settings) -> None:
        client, user_id, _, doctor_id = app_client
        _link(settings, doctor_id, user_id)
        _add(settings, user_id, 154)
        _login_doctor(client, doctor_id)
        response = client.get(f"/doctor/patient/{user_id}/export.pdf?days=30")
        assert response.status_code == 200
        start = (datetime.now(ZoneInfo(TZ)).date() - timedelta(days=29)).strftime("%Y%m%d")
        assert response.headers["content-disposition"] == (
            f'attachment; filename="bp_diary_{start}-{_today()}.pdf"'
        )
        text = _text(response.content)
        assert f"Пациент {TG_ID} · сформирован" in text
        assert "Сводка" in text and "154" in text

    def test_empty_period(self, app_client, settings) -> None:
        client, user_id, _, doctor_id = app_client
        _link(settings, doctor_id, user_id)
        _login_doctor(client, doctor_id)
        response = client.get(f"/doctor/patient/{user_id}/export.pdf")
        assert response.status_code == 303
        location = response.headers["location"]
        assert location == f"/doctor/patient/{user_id}?days=7&date_from=&date_to=&error=empty"
        assert "За этот период записей нет." in client.get(location).text

    @pytest.mark.parametrize("status", ["pending", "revoked"])
    def test_requires_active_link(self, app_client, settings, status) -> None:
        client, user_id, _, doctor_id = app_client
        _link(settings, doctor_id, user_id, status=status)
        _add(settings, user_id, 154)
        _login_doctor(client, doctor_id)
        response = client.get(f"/doctor/patient/{user_id}/export.pdf")
        assert response.status_code == 303
        assert response.headers["location"] == "/doctor?error=denied"

    def test_other_patient_denied(self, app_client, settings) -> None:
        client, user_id, other_id, doctor_id = app_client
        _link(settings, doctor_id, user_id)
        _add(settings, other_id, 154)
        _login_doctor(client, doctor_id)
        response = client.get(f"/doctor/patient/{other_id}/export.pdf")
        assert response.status_code == 303
        assert response.headers["location"] == "/doctor?error=denied"
