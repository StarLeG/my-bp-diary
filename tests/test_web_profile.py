"""Данные пациента в кабинете (SPEC.md, §6.12, §16.2, §21, §10.1).

Покрывается: форма «Мои данные» в ЛК (сохранение, ошибки ФИО / даты / 16+,
CSRF, удаление), строки карточки; у врача — подписи в списке (сортировка
по фамилии, «данные не заполнены»), заголовок карточки, шапка и имя файла
PDF, имя файла CSV; шапка PDF самого пациента.
"""
from __future__ import annotations

import io
import sqlite3
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader

from bot.config import Settings
from web.cabinet import auth, doctor_auth
from web.cabinet.app import DOCTOR_COOKIE, create_app

FAKE_TOKEN = f"123456:{'A' * 35}"
SECRET = "test-secret"
TG_ID = 111_111


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(bot_token=FAKE_TOKEN, web_secret_key=SECRET, db_path=str(tmp_path / "cab.sqlite3"))


def sql(settings: Settings, query: str, params: tuple = ()) -> list[tuple]:
    con = sqlite3.connect(settings.db_path)
    rows = con.execute(query, params).fetchall()
    con.commit()
    con.close()
    return rows


def add_user(settings: Settings, telegram_id: int, **fields) -> int:
    columns = ["telegram_id", "timezone", "target_systolic", "target_diastolic", "onboarded", "created_at"]
    values: list = [telegram_id, "Europe/Minsk", 130, 80, 1, "2026-01-01 00:00:00"]
    for key, value in fields.items():
        columns.append(key)
        values.append(value)
    sql(settings, f"INSERT INTO users ({', '.join(columns)}) VALUES ({', '.join('?' * len(values))})", tuple(values))
    return sql(settings, "SELECT id FROM users WHERE telegram_id = ?", (telegram_id,))[0][0]


def add_measurement(settings: Settings, user_id: int) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    sql(settings, "INSERT INTO measurements (user_id, timestamp, systolic, diastolic) VALUES (?, ?, 128, 82)", (user_id, now))


@pytest.fixture
def patient(settings: Settings):
    """ЛК пациента с сессией (согласие — по умолчанию, tests/conftest.py)."""
    app = create_app(settings)
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as client:
        add_user(settings, TG_ID)
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
        client.get("/")
        yield client


def form(client: TestClient, **overrides) -> dict:
    data = {
        "csrf_token": client.cookies.get("csrf_token", ""),
        "last_name": "Иванов",
        "first_name": "Иван",
        "middle_name": "Иванович",
        "birth_date": "1958-03-12",
        "sex": "m",
    }
    data.update(overrides)
    return data


class TestPatientForm:
    def test_empty_card(self, patient: TestClient) -> None:
        page = patient.get("/")
        assert "Мои данные" in page.text and "не заполнены" in page.text

    def test_save(self, patient: TestClient, settings: Settings) -> None:
        response = patient.post("/profile/personal", data=form(patient))
        assert response.headers["location"] == "/?saved=personal#personal"
        assert sql(settings, "SELECT last_name, middle_name, birth_date, sex FROM users") == [
            ("Иванов", "Иванович", "1958-03-12", "m")
        ]
        page = patient.get("/?saved=personal")
        assert "Данные сохранены." in page.text
        assert "Иванов Иван Иванович" in page.text and "12.03.1958" in page.text and "мужской" in page.text

    @pytest.mark.parametrize(
        ("overrides", "code", "message"),
        [
            ({"last_name": "Ив4н"}, "personal_name", "От 1 до 50 букв"),
            ({"sex": "x"}, "personal_name", "От 1 до 50 букв"),
            ({"birth_date": "не дата"}, "personal_birth", "Нужна дата в формате"),
            ({"birth_date": "1800-01-01"}, "personal_birth", "Нужна дата в формате"),
            ({"birth_date": "2015-01-01"}, "personal_young", "Сервис доступен с 16 лет"),
        ],
    )
    def test_errors(self, patient: TestClient, settings: Settings, overrides, code, message) -> None:
        response = patient.post("/profile/personal", data=form(patient, **overrides))
        assert response.headers["location"] == f"/?error={code}#personal"
        assert sql(settings, "SELECT last_name FROM users") == [(None,)]
        assert message in patient.get(f"/?error={code}").text

    def test_csrf(self, patient: TestClient) -> None:
        assert patient.post("/profile/personal", data=form(patient, csrf_token="bad")).status_code == 403

    def test_delete(self, patient: TestClient, settings: Settings) -> None:
        patient.post("/profile/personal", data=form(patient))
        response = patient.post("/profile/personal/delete", data={"csrf_token": patient.cookies.get("csrf_token", "")})
        assert response.headers["location"] == "/?saved=personal_deleted#personal"
        assert sql(settings, "SELECT last_name, sex FROM users") == [(None, None)]
        assert "Данные удалены." in patient.get("/?saved=personal_deleted").text

    def test_own_pdf_subject(self, patient: TestClient, settings: Settings) -> None:
        add_measurement(settings, sql(settings, "SELECT id FROM users")[0][0])
        plain = PdfReader(io.BytesIO(patient.get("/export/report.pdf").content)).pages[0].extract_text()
        assert "Дневник давления" in plain
        patient.post("/profile/personal", data=form(patient))
        response = patient.get("/export/report.pdf")
        text = PdfReader(io.BytesIO(response.content)).pages[0].extract_text()
        assert "Иванов Иван Иванович, 12.03.1958" in text
        assert 'filename="bp_diary_' in response.headers["content-disposition"]  # у пациента — прежнее имя


@pytest.fixture
def doctor(settings: Settings):
    """Кабинет врача: три пациента с доступом (согласия — по умолчанию)."""
    app = create_app(settings)
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as client:
        sql(settings, "INSERT INTO doctors (email, name, password_hash, created_at) VALUES ('d@example.org', 'Врач', 'x', '2026-01-01')")
        doctor_id = sql(settings, "SELECT id FROM doctors")[0][0]
        ids = {
            "anon": add_user(settings, 555_555),
            "named": add_user(settings, 666_666, telegram_name="Анна Иванова"),
            "yak": add_user(settings, 777_777, last_name="Яковлев", first_name="Пётр", birth_date="1980-05-01", sex="m"),
            "abr": add_user(settings, 888_888, last_name="Абрамова", first_name="Ольга", middle_name="Ивановна",
                            birth_date="1958-03-12", sex="f"),
        }
        for user_id in ids.values():
            sql(settings, "INSERT INTO doctor_patients (doctor_id, user_id, status, created_at) VALUES (?, ?, 'active', '2026-01-01')",
                (doctor_id, user_id))
            add_measurement(settings, user_id)
        client.cookies.set(DOCTOR_COOKIE, doctor_auth.DoctorSession(SECRET).make(doctor_id))
        yield client, ids


class TestDoctorViews:
    def test_list_sorted_with_labels(self, doctor) -> None:
        client, _ = doctor
        text = client.get("/doctor").text
        abr = text.index("Абрамова Ольга Ивановна · 12.03.1958 (")
        yak = text.index("Яковлев Пётр · 01.05.1980 (")
        named = text.index("Анна Иванова — данные не заполнены")
        anon = text.index("Пациент 555555 — данные не заполнены")
        assert abr < yak < min(named, anon)
        assert " · ж</a>" in text

    def test_card_header(self, doctor) -> None:
        client, ids = doctor
        filled = client.get(f"/doctor/patient/{ids['abr']}").text
        assert "Абрамова Ольга Ивановна, " in filled and "лет — сводка за период" in filled
        assert "Дата рождения: 12.03.1958 · Пол: женский" in filled
        empty = client.get(f"/doctor/patient/{ids['named']}").text
        assert "Анна Иванова — сводка за период" in empty and "Данные не заполнены" in empty

    def test_pdf_and_csv_names(self, doctor) -> None:
        client, ids = doctor
        pdf = client.get(f"/doctor/patient/{ids['abr']}/export.pdf")
        assert 'filename="Abramova_O_I_' in pdf.headers["content-disposition"]
        assert "Абрамова Ольга Ивановна, 12.03.1958" in PdfReader(io.BytesIO(pdf.content)).pages[0].extract_text()
        csv = client.get(f"/doctor/patient/{ids['abr']}/export.csv")
        assert 'filename="Abramova_O_I_' in csv.headers["content-disposition"]
        anon_pdf = client.get(f"/doctor/patient/{ids['anon']}/export.pdf")
        assert 'filename="bp_diary_' in anon_pdf.headers["content-disposition"]
        assert "Пациент 555555" in PdfReader(io.BytesIO(anon_pdf.content)).pages[0].extract_text()
        anon_csv = client.get(f"/doctor/patient/{ids['anon']}/export.csv")
        assert 'filename="bp_' in anon_csv.headers["content-disposition"]
