"""Кабинет врача: magic link, сессия и HTTP-сценарии (SPEC.md, раздел 21).

Покрывается: одноразовая ссылка для врача (15 минут, совместима с подписью
бота), stateless-сессия (7 дней), вход по паролю (Argon2id + rate limiting),
просмотр пациента только при `status='active'`, заметки врача (§4.10),
экспорт CSV/PDF.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from itsdangerous import URLSafeTimedSerializer

from bot.config import Settings
from bot.handlers import doctor as doctor_handlers
from web.cabinet import auth, doctor_auth
from web.cabinet.app import DOCTOR_COOKIE, create_app

FAKE_TOKEN = f"123456:{'A' * 35}"
SECRET = "test-secret"
CABINET_URL = "https://cabinet.example.org"
#: Telegram ID пользователя в фикстуре.
TG_ID = 111_111
DOCTOR_EMAIL = "doc@example.org"
DOCTOR_PASSWORD = "doctor-pass"
#: Хеш Argon2id считается один раз на модуль (дорого на каждый тест).
DOCTOR_PASSWORD_HASH = auth.hash_password(DOCTOR_PASSWORD)


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
    """TestClient со схемой, пациентом и врачом (пароль для /doctor/login).

    Данные вставляются синхронным sqlite3 после init_db (lifespan): так они
    создаются без конфликта event loop'ов TestClient и pytest.
    """
    app = create_app(settings)
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as client:
        con = sqlite3.connect(settings.db_path)
        con.execute(
            "INSERT INTO users (telegram_id, timezone, target_systolic, target_diastolic,"
            " med_remeasure_delay, onboarded, created_at)"
            " VALUES (?, 'Europe/Minsk', 130, 80, 60, 1, '2026-01-01 00:00:00')",
            (TG_ID,),
        )
        user_id = con.execute("SELECT id FROM users").fetchone()[0]
        con.execute(
            "INSERT INTO doctors (email, name, password_hash, created_at)"
            " VALUES (?, 'Врач', ?, '2026-01-01 00:00:00')",
            (DOCTOR_EMAIL, DOCTOR_PASSWORD_HASH),
        )
        doctor_id = con.execute("SELECT id FROM doctors").fetchone()[0]
        con.commit()
        con.close()
        yield client, user_id, doctor_id


def _csrf(client: TestClient) -> str:
    """CSRF-токен из cookie (его же клиент кладёт в hidden-поле формы)."""
    return client.cookies.get("csrf_token", "")


def _link(settings: Settings, doctor_id: int, user_id: int, status: str = "active") -> None:
    """Связь врач–пациент нужного статуса (синхронная вставка)."""
    con = sqlite3.connect(settings.db_path)
    con.execute(
        "INSERT INTO doctor_patients (doctor_id, user_id, status, created_at)"
        " VALUES (?, ?, ?, '2026-01-01 00:00:00')",
        (doctor_id, user_id, status),
    )
    con.commit()
    con.close()


def _status(settings: Settings, doctor_id: int, user_id: int) -> str:
    con = sqlite3.connect(settings.db_path)
    row = con.execute(
        "SELECT status FROM doctor_patients WHERE doctor_id = ? AND user_id = ?",
        (doctor_id, user_id),
    ).fetchone()
    con.close()
    return row[0]


def _add_measurement(settings: Settings, user_id: int) -> None:
    """Измерение «сейчас» — попадает в период по умолчанию (7 дней)."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    con = sqlite3.connect(settings.db_path)
    con.execute(
        "INSERT INTO measurements (user_id, timestamp, systolic, diastolic, pulse,"
        " medication_taken, feeling, note)"
        " VALUES (?, ?, 128, 82, 64, 1, 'normal', 'после сна')",
        (user_id, now),
    )
    con.commit()
    con.close()


def _login_doctor(client: TestClient, doctor_id: int) -> None:
    """Поставить сессионную cookie врача напрямую (без формы входа)."""
    client.cookies.set(DOCTOR_COOKIE, doctor_auth.DoctorSession(SECRET).make(doctor_id))


def _magic_token(doctor_id: int, user_id: int) -> str:
    return doctor_auth.DoctorMagicLink(SECRET).make(doctor_id, user_id)


# ─────────────────────────── Magic link ───────────────────────────


class TestDoctorMagicLink:
    def test_make_verify_consume(self) -> None:
        magic = doctor_auth.DoctorMagicLink(SECRET)
        token = magic.make(7, 42)
        assert magic.verify(token) == (7, 42)  # verify не списывает
        assert magic.verify(token) == (7, 42)
        assert magic.consume(token) == (7, 42)

    def test_consume_is_single_use(self) -> None:
        magic = doctor_auth.DoctorMagicLink(SECRET)
        token = magic.make(7, 42)
        assert magic.consume(token) == (7, 42)
        assert magic.consume(token) is None
        assert magic.verify(token) is None  # использованный токен не проверяется

    def test_expired_token_is_rejected(self, mocker) -> None:
        """Срок 15 минут проверяется по времени подписи (itsdangerous)."""
        magic = doctor_auth.DoctorMagicLink(SECRET)
        past = int(doctor_auth.utcnow().timestamp()) - (doctor_auth.DOCTOR_LINK_MAX_AGE + 60)
        mocker.patch("itsdangerous.timed.TimestampSigner.get_timestamp", return_value=past)
        token = magic.make(7, 42)
        mocker.stopall()
        assert magic.verify(token) is None
        assert magic.consume(token) is None

    def test_foreign_signature_is_rejected(self) -> None:
        token = doctor_auth.DoctorMagicLink(SECRET).make(7, 42)
        assert doctor_auth.DoctorMagicLink("other-secret").verify(token) is None

    def test_garbage_is_rejected(self) -> None:
        magic = doctor_auth.DoctorMagicLink(SECRET)
        assert magic.verify("просто не токен") is None
        assert magic.consume("") is None

    def test_non_dict_payload_is_rejected(self) -> None:
        """Payload не-dict отклоняется без ошибки."""
        token = URLSafeTimedSerializer(SECRET, salt=doctor_auth.DOCTOR_LOGIN_SALT).dumps(
            ["did", "pid"]
        )
        magic = doctor_auth.DoctorMagicLink(SECRET)
        assert magic.verify(token) is None

    @pytest.mark.parametrize(
        "payload",
        [
            {"did": "x", "pid": 42, "jti": "j"},  # типы полей
            {"did": 7, "pid": 42},  # нет jti
            {"did": 7, "pid": 42, "jti": 5},  # jti не строка
            {},
        ],
    )
    def test_invalid_payload_is_rejected(self, payload: dict) -> None:
        token = URLSafeTimedSerializer(SECRET, salt=doctor_auth.DOCTOR_LOGIN_SALT).dumps(
            payload
        )
        magic = doctor_auth.DoctorMagicLink(SECRET)
        assert magic.verify(token) is None
        assert magic.consume(token) is None

    def test_used_jti_is_purged_after_expiry(self) -> None:
        """Память одноразовых токенов чистится по истечении срока."""
        magic = doctor_auth.DoctorMagicLink(SECRET)
        magic.consume(magic.make(7, 42))
        assert len(magic._used) == 1
        later = doctor_auth.utcnow() + timedelta(seconds=doctor_auth.DOCTOR_LINK_MAX_AGE + 1)
        magic.verify("что-то", now=later)
        assert len(magic._used) == 0

    def test_token_contract_with_bot(self) -> None:
        """Ссылка из бота читается веб-частью: единый salt «doctor-login»."""
        settings = Settings(
            bot_token=FAKE_TOKEN, web_secret_key=SECRET, cabinet_url=CABINET_URL
        )
        url = doctor_handlers.make_doctor_link(settings, 7, 42)
        token = url.split("token=", 1)[1]
        assert doctor_auth.DoctorMagicLink(SECRET).verify(token) == (7, 42)


# ─────────────────────────── Сессия врача ───────────────────────────


class TestDoctorSession:
    def test_make_verify(self) -> None:
        sessions = doctor_auth.DoctorSession(SECRET)
        assert sessions.verify(sessions.make(7)) == 7

    def test_foreign_signature_is_rejected(self) -> None:
        token = doctor_auth.DoctorSession(SECRET).make(7)
        assert doctor_auth.DoctorSession("other-secret").verify(token) is None

    def test_garbage_is_rejected(self) -> None:
        sessions = doctor_auth.DoctorSession(SECRET)
        assert sessions.verify("") is None
        assert sessions.verify("нет токена") is None

    def test_invalid_payload_is_rejected(self) -> None:
        token = URLSafeTimedSerializer(
            SECRET, salt=doctor_auth.DOCTOR_SESSION_SALT
        ).dumps({"did": "x"})
        assert doctor_auth.DoctorSession(SECRET).verify(token) is None


# ─────────────────────────── Вход по ссылке ───────────────────────────


class TestMagicLogin:
    def test_token_logs_in_and_activates(self, app_client, settings) -> None:
        client, user_id, doctor_id = app_client
        _link(settings, doctor_id, user_id, status="pending")
        response = client.get(f"/doctor?token={_magic_token(doctor_id, user_id)}")
        assert response.status_code == 303
        assert response.headers["location"] == f"/doctor/patient/{user_id}"
        assert client.cookies.get(DOCTOR_COOKIE)
        assert _status(settings, doctor_id, user_id) == "active"

    def test_token_is_single_use(self, app_client, settings) -> None:
        client, user_id, doctor_id = app_client
        _link(settings, doctor_id, user_id)
        token = _magic_token(doctor_id, user_id)
        assert client.get(f"/doctor?token={token}").status_code == 303
        response = client.get(f"/doctor?token={token}")
        assert response.status_code == 303
        assert response.headers["location"] == "/doctor?error=link_expired"

    def test_revoked_link_is_not_activated(self, app_client, settings) -> None:
        """Пациент отозвал доступ до открытия ссылки — статус не меняется."""
        client, user_id, doctor_id = app_client
        _link(settings, doctor_id, user_id, status="revoked")
        response = client.get(f"/doctor?token={_magic_token(doctor_id, user_id)}")
        assert response.status_code == 303
        assert response.headers["location"] == "/doctor?error=denied"
        assert _status(settings, doctor_id, user_id) == "revoked"

    def test_unknown_doctor_is_rejected(self, app_client) -> None:
        client, user_id, doctor_id = app_client
        response = client.get(f"/doctor?token={_magic_token(doctor_id + 99, user_id)}")
        assert response.status_code == 303
        assert response.headers["location"] == "/doctor?error=link_expired"


# ─────────────────────────── Список пациентов ───────────────────────────


class TestDoctorList:
    def test_without_cookie_shows_login(self, app_client) -> None:
        client, _, _ = app_client
        response = client.get("/doctor")
        assert response.status_code == 200
        assert "Кабинет врача" in response.text
        assert "/doctor/login" in response.text

    def test_active_patient_is_listed(self, app_client, settings) -> None:
        client, user_id, doctor_id = app_client
        _link(settings, doctor_id, user_id, status="active")
        _login_doctor(client, doctor_id)
        response = client.get("/doctor")
        assert response.status_code == 200
        assert f"/doctor/patient/{user_id}" in response.text

    def test_revoked_patient_is_hidden(self, app_client, settings) -> None:
        client, user_id, doctor_id = app_client
        _link(settings, doctor_id, user_id, status="revoked")
        _login_doctor(client, doctor_id)
        response = client.get("/doctor")
        assert response.status_code == 200
        assert f"/doctor/patient/{user_id}" not in response.text

    def test_pending_patient_is_hidden(self, app_client, settings) -> None:
        client, user_id, doctor_id = app_client
        _link(settings, doctor_id, user_id, status="pending")
        _login_doctor(client, doctor_id)
        response = client.get("/doctor")
        assert f"/doctor/patient/{user_id}" not in response.text


# ─────────────────────────── Карточка пациента ───────────────────────────


class TestPatientCard:
    def test_requires_session(self, app_client, settings) -> None:
        client, user_id, doctor_id = app_client
        _link(settings, doctor_id, user_id)
        response = client.get(f"/doctor/patient/{user_id}")
        assert response.status_code == 303
        assert response.headers["location"] == "/doctor"

    def test_requires_active_link(self, app_client, settings) -> None:
        client, user_id, doctor_id = app_client
        _link(settings, doctor_id, user_id, status="pending")
        _login_doctor(client, doctor_id)
        response = client.get(f"/doctor/patient/{user_id}")
        assert response.status_code == 303
        assert response.headers["location"] == "/doctor?error=denied"

    def test_revoked_link_is_denied(self, app_client, settings) -> None:
        client, user_id, doctor_id = app_client
        _link(settings, doctor_id, user_id, status="revoked")
        _login_doctor(client, doctor_id)
        response = client.get(f"/doctor/patient/{user_id}")
        assert response.status_code == 303
        assert response.headers["location"] == "/doctor?error=denied"

    def test_other_doctor_is_denied(self, app_client, settings) -> None:
        """Связь есть у другого врача — доступа нет."""
        client, user_id, doctor_id = app_client
        con = sqlite3.connect(settings.db_path)
        con.execute(
            "INSERT INTO doctors (email, name, password_hash, created_at)"
            " VALUES ('other@example.org', 'Другой', ?, '2026-01-01 00:00:00')",
            (DOCTOR_PASSWORD_HASH,),
        )
        other_id = con.execute("SELECT MAX(id) FROM doctors").fetchone()[0]
        con.commit()
        con.close()
        _link(settings, doctor_id, user_id)
        _login_doctor(client, other_id)
        response = client.get(f"/doctor/patient/{user_id}")
        assert response.status_code == 303
        assert response.headers["location"] == "/doctor?error=denied"

    def test_card_renders(self, app_client, settings) -> None:
        client, user_id, doctor_id = app_client
        _link(settings, doctor_id, user_id)
        _add_measurement(settings, user_id)
        _login_doctor(client, doctor_id)
        response = client.get(f"/doctor/patient/{user_id}")
        assert response.status_code == 200
        assert f"Пациент {TG_ID}" in response.text
        assert "128" in response.text  # измерение в таблице
        assert "Заметки врача" in response.text

    def test_card_without_measurements(self, app_client, settings) -> None:
        client, user_id, doctor_id = app_client
        _link(settings, doctor_id, user_id)
        _login_doctor(client, doctor_id)
        response = client.get(f"/doctor/patient/{user_id}")
        assert response.status_code == 200
        assert "За этот период записей нет." in response.text


# ─────────────────────────── Заметки врача ───────────────────────────


class TestDoctorNotes:
    def test_note_is_saved_and_visible(self, app_client, settings) -> None:
        client, user_id, doctor_id = app_client
        _link(settings, doctor_id, user_id)
        _login_doctor(client, doctor_id)
        client.get("/doctor")  # получить CSRF-куку
        response = client.post(
            f"/doctor/patient/{user_id}/note",
            data={"csrf_token": _csrf(client), "text": "Продолжайте измерения"},
        )
        assert response.status_code == 303
        assert response.headers["location"] == (
            f"/doctor/patient/{user_id}?saved=note#notes"
        )
        con = sqlite3.connect(settings.db_path)
        row = con.execute("SELECT doctor_id, user_id, text FROM doctor_notes").fetchone()
        con.close()
        assert row == (doctor_id, user_id, "Продолжайте измерения")
        page = client.get(f"/doctor/patient/{user_id}")
        assert "Продолжайте измерения" in page.text

    def test_note_requires_csrf(self, app_client, settings) -> None:
        client, user_id, doctor_id = app_client
        _link(settings, doctor_id, user_id)
        _login_doctor(client, doctor_id)
        client.get("/doctor")  # CSRF-кука есть, токен в форме — нет
        response = client.post(
            f"/doctor/patient/{user_id}/note", data={"text": "Без токена"}
        )
        assert response.status_code == 403

    def test_note_empty_is_rejected(self, app_client, settings) -> None:
        client, user_id, doctor_id = app_client
        _link(settings, doctor_id, user_id)
        _login_doctor(client, doctor_id)
        client.get("/doctor")  # получить CSRF-куку
        response = client.post(
            f"/doctor/patient/{user_id}/note",
            data={"csrf_token": _csrf(client), "text": "   "},
        )
        assert response.status_code == 303
        assert response.headers["location"] == f"/doctor/patient/{user_id}?error=note#notes"

    def test_note_too_long_is_rejected(self, app_client, settings) -> None:
        client, user_id, doctor_id = app_client
        _link(settings, doctor_id, user_id)
        _login_doctor(client, doctor_id)
        client.get("/doctor")  # получить CSRF-куку
        response = client.post(
            f"/doctor/patient/{user_id}/note",
            data={"csrf_token": _csrf(client), "text": "x" * 1001},
        )
        assert response.status_code == 303
        assert response.headers["location"] == f"/doctor/patient/{user_id}?error=note#notes"

    def test_note_requires_active_link(self, app_client, settings) -> None:
        client, user_id, doctor_id = app_client
        _link(settings, doctor_id, user_id, status="pending")
        _login_doctor(client, doctor_id)
        client.get("/doctor")  # получить CSRF-куку
        response = client.post(
            f"/doctor/patient/{user_id}/note",
            data={"csrf_token": _csrf(client), "text": "Не должно сохраниться"},
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/doctor?error=denied"
        con = sqlite3.connect(settings.db_path)
        count = con.execute("SELECT COUNT(*) FROM doctor_notes").fetchone()[0]
        con.close()
        assert count == 0


# ─────────────────────────── Экспорт ───────────────────────────


class TestExports:
    def test_csv_export(self, app_client, settings) -> None:
        client, user_id, doctor_id = app_client
        _link(settings, doctor_id, user_id)
        _add_measurement(settings, user_id)
        _login_doctor(client, doctor_id)
        response = client.get(f"/doctor/patient/{user_id}/export.csv")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        assert "128" in response.text

    def test_pdf_export(self, app_client, settings) -> None:
        client, user_id, doctor_id = app_client
        _link(settings, doctor_id, user_id)
        _add_measurement(settings, user_id)
        _login_doctor(client, doctor_id)
        response = client.get(f"/doctor/patient/{user_id}/export.pdf")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/pdf"
        assert response.content.startswith(b"%PDF")

    def test_exports_require_active_link(self, app_client, settings) -> None:
        client, user_id, doctor_id = app_client
        _link(settings, doctor_id, user_id, status="pending")
        _login_doctor(client, doctor_id)
        for suffix in ("export.csv", "export.pdf"):
            response = client.get(f"/doctor/patient/{user_id}/{suffix}")
            assert response.status_code == 303
            assert response.headers["location"] == "/doctor?error=denied"


# ─────────────────────────── Вход e-mail + пароль ───────────────────────────


class TestDoctorLogin:
    def test_wrong_password(self, app_client) -> None:
        client, _, _ = app_client
        client.get("/doctor")
        response = client.post(
            "/doctor/login",
            data={
                "csrf_token": _csrf(client),
                "email": DOCTOR_EMAIL,
                "password": "wrong-pass",
            },
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/doctor?error=wrong"
        assert not client.cookies.get(DOCTOR_COOKIE)

    def test_correct_password_sets_cookie(self, app_client) -> None:
        client, _, _ = app_client
        client.get("/doctor")
        response = client.post(
            "/doctor/login",
            data={
                "csrf_token": _csrf(client),
                "email": DOCTOR_EMAIL,
                "password": DOCTOR_PASSWORD,
            },
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/doctor"
        assert client.cookies.get(DOCTOR_COOKIE)

    def test_login_requires_csrf(self, app_client) -> None:
        client, _, _ = app_client
        response = client.post(
            "/doctor/login",
            data={"email": DOCTOR_EMAIL, "password": DOCTOR_PASSWORD},
        )
        assert response.status_code == 403

    def test_rate_limit_blocks_sixth_attempt(self, app_client) -> None:
        client, _, _ = app_client
        client.get("/doctor")
        for attempt in range(auth.LOGIN_RATE_LIMIT):
            response = client.post(
                "/doctor/login",
                data={
                    "csrf_token": _csrf(client),
                    "email": DOCTOR_EMAIL,
                    "password": "wrong-pass",
                },
            )
            assert response.status_code == 303, attempt
        response = client.post(
            "/doctor/login",
            data={
                "csrf_token": _csrf(client),
                "email": DOCTOR_EMAIL,
                "password": "wrong-pass",
            },
        )
        assert response.status_code == 429

    def test_logout_clears_cookie(self, app_client, settings) -> None:
        client, user_id, doctor_id = app_client
        _link(settings, doctor_id, user_id)
        client.get(f"/doctor?token={_magic_token(doctor_id, user_id)}")
        assert client.cookies.get(DOCTOR_COOKIE)
        client.get("/doctor")  # получить CSRF-куку
        response = client.post(
            "/doctor/logout", data={"csrf_token": _csrf(client)}
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/doctor"
        assert not client.cookies.get(DOCTOR_COOKIE)
