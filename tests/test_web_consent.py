"""Согласие в кабинете (SPEC.md, §6.11: К1, К2, К3, К5).

Без триггеров согласия по умолчанию (`real_consent`). Покрывается:
пациент без согласия видит только `/consent` (страницы, экспорт и импорт —
редирект), «Согласен» / «Не согласен», строка согласия в карточке, отзыв в
кабинете (согласие закрыто, сессии удалены, удаление ждёт бот), страница
согласия врача (все пути входа, чекбокс, CSRF, безопасный `next`), пациент
без согласия на врача врачу не виден.
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from bot.config import Settings
from bot.services.consent import CONSENT_VERSION, DOCTOR_CONSENT_VERSION
from web.cabinet import auth, doctor_auth
from web.cabinet.app import DOCTOR_COOKIE, _safe_doctor_next, create_app

pytestmark = pytest.mark.real_consent

FAKE_TOKEN = f"123456:{'A' * 35}"
SECRET = "test-secret"
TG_ID = 111_111
PRIVACY = "https://bp-diary.example.org/privacy.html"


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        bot_token=FAKE_TOKEN,
        web_secret_key=SECRET,
        privacy_url=PRIVACY,
        db_path=str(tmp_path / "cabinet.sqlite3"),
    )


def _sql(settings: Settings, query: str, params: tuple = ()) -> list[tuple]:
    con = sqlite3.connect(settings.db_path)
    rows = con.execute(query, params).fetchall()
    con.commit()
    con.close()
    return rows


@pytest.fixture
def client(settings: Settings):
    """Кабинет с пациентом (без согласия) и его сессией."""
    app = create_app(settings)
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as client:
        _sql(
            settings,
            "INSERT INTO users (telegram_id, timezone, target_systolic, target_diastolic,"
            " med_remeasure_delay, onboarded, created_at)"
            " VALUES (?, 'Europe/Minsk', 130, 80, 60, 1, '2026-01-01 00:00:00')",
            (TG_ID,),
        )
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
        yield client


def _user_id(settings: Settings) -> int:
    return _sql(settings, "SELECT id FROM users")[0][0]


def _give(settings: Settings, kind: str = "processing", doctor_id: int | None = None) -> None:
    _sql(
        settings,
        "INSERT INTO consents (user_id, kind, doctor_id, version, channel, given_at)"
        " VALUES (?, ?, ?, ?, 'bot', '2026-11-10 09:00:00')",
        (_user_id(settings), kind, doctor_id, CONSENT_VERSION),
    )


class TestPatientConsent:
    @pytest.mark.parametrize("path", ["/", "/export/measurements.csv", "/export/report.pdf"])
    def test_pages_redirect_to_consent(self, client: TestClient, path: str) -> None:
        response = client.get(path)
        assert response.status_code == 303 and response.headers["location"] == "/consent"

    def test_consent_page(self, client: TestClient) -> None:
        page = client.get("/consent")
        assert page.status_code == 200
        assert "Нужно ваше согласие" in page.text
        assert "Данные хранятся на сервере в Германии" in page.text
        assert f"{PRIVACY}#consent" in page.text
        assert "Полный текст:" not in page.text
        assert "16 лет" in page.text

    def test_declined_page(self, client: TestClient) -> None:
        page = client.get("/consent?declined=1")
        assert "Удалить аккаунт и все данные можно в боте" in page.text

    def test_agree(self, client: TestClient, settings: Settings) -> None:
        client.get("/consent")
        response = client.post("/consent", data={"csrf_token": client.cookies.get("csrf_token", "")})
        assert response.status_code == 303 and response.headers["location"] == "/"
        assert _sql(settings, "SELECT kind, channel, version FROM consents") == [
            ("processing", "cabinet", CONSENT_VERSION)
        ]
        dashboard = client.get("/")
        assert dashboard.status_code == 200
        assert "Согласие на обработку данных" in dashboard.text and "Отозвать согласие" in dashboard.text
        # повторный заход на /consent — сразу в кабинет
        assert client.get("/consent").headers["location"] == "/"

    def test_agree_requires_csrf(self, client: TestClient) -> None:
        assert client.post("/consent", data={"csrf_token": "bad"}).status_code == 403

    def test_agree_clears_deletion_request(self, client: TestClient, settings: Settings) -> None:
        _sql(settings, "UPDATE users SET deletion_requested_at = '2026-11-10 09:00:00'")
        client.get("/consent")
        client.post("/consent", data={"csrf_token": client.cookies.get("csrf_token", "")})
        assert _sql(settings, "SELECT deletion_requested_at FROM users") == [(None,)]

    def test_withdraw(self, client: TestClient, settings: Settings) -> None:
        _give(settings)
        page = client.get("/consent/withdraw")
        assert "будут удалены в течение часа" in page.text
        response = client.post("/consent/withdraw", data={"csrf_token": client.cookies.get("csrf_token", "")})
        assert response.status_code == 200
        assert "Согласие отозвано" in response.text
        assert _sql(settings, "SELECT COUNT(*) FROM consents WHERE withdrawn_at IS NULL") == [(0,)]
        assert _sql(settings, "SELECT deletion_requested_at IS NOT NULL FROM users") == [(1,)]
        assert _sql(settings, "SELECT COUNT(*) FROM web_sessions") == [(0,)]
        assert client.get("/").headers["location"] == "/login"

    def test_withdraw_requires_consent(self, client: TestClient) -> None:
        assert client.get("/consent/withdraw").headers["location"] == "/consent"


@pytest.fixture
def doctor_client(settings: Settings):
    """Кабинет с пациентом и врачом (по e-mail, без согласия) и связью `active`."""
    app = create_app(settings)
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as client:
        _sql(
            settings,
            "INSERT INTO users (telegram_id, timezone, target_systolic, target_diastolic,"
            " med_remeasure_delay, onboarded, created_at)"
            " VALUES (?, 'Europe/Minsk', 130, 80, 60, 1, '2026-01-01 00:00:00')",
            (TG_ID,),
        )
        _sql(
            settings,
            "INSERT INTO doctors (email, name, password_hash, created_at)"
            " VALUES ('doc@example.org', 'Врач', 'x', '2026-01-01 00:00:00')",
        )
        doctor_id = _sql(settings, "SELECT id FROM doctors")[0][0]
        _sql(
            settings,
            "INSERT INTO doctor_patients (doctor_id, user_id, status, created_at)"
            " VALUES (?, ?, 'active', '2026-01-01 00:00:00')",
            (doctor_id, _user_id(settings)),
        )
        client.cookies.set(DOCTOR_COOKIE, doctor_auth.DoctorSession(SECRET).make(doctor_id))
        yield client, doctor_id


class TestDoctorConsent:
    def test_list_redirects(self, doctor_client) -> None:
        client, _ = doctor_client
        assert client.get("/doctor").headers["location"] == "/doctor/consent"

    def test_patient_page_redirects_with_next(self, doctor_client, settings) -> None:
        client, _ = doctor_client
        uid = _user_id(settings)
        response = client.get(f"/doctor/patient/{uid}")
        assert response.headers["location"] == f"/doctor/consent?next=%2Fdoctor%2Fpatient%2F{uid}"

    def test_consent_page_and_submit(self, doctor_client, settings) -> None:
        client, doctor_id = doctor_client
        page = client.get("/doctor/consent?next=/doctor/patient/1")
        assert page.status_code == 200
        assert "Согласен на обработку моих данных" in page.text
        assert f"{PRIVACY}#policy" in page.text
        csrf = client.cookies.get("csrf_token", "")
        missing = client.post("/doctor/consent", data={"csrf_token": csrf, "next": "/doctor/patient/1"})
        assert "error=agree" in missing.headers["location"]
        done = client.post(
            "/doctor/consent", data={"csrf_token": csrf, "next": "/doctor/patient/1", "agree": "1"}
        )
        assert done.headers["location"] == "/doctor/patient/1"
        assert _sql(settings, "SELECT consent_version, consent_channel FROM doctors") == [
            (DOCTOR_CONSENT_VERSION, "cabinet")
        ]
        assert client.get("/doctor/consent").headers["location"] == "/doctor"

    def test_consent_error_message(self, doctor_client) -> None:
        client, _ = doctor_client
        assert "Отметьте согласие" in client.get("/doctor/consent?error=agree").text

    def test_consent_without_login(self, settings) -> None:
        app = create_app(settings)
        with TestClient(app, base_url="https://testserver", follow_redirects=False) as client:
            assert client.get("/doctor/consent").headers["location"] == "/doctor"
            client.get("/doctor")
            response = client.post("/doctor/consent", data={"csrf_token": client.cookies.get("csrf_token", ""), "agree": "1"})
            assert response.headers["location"] == "/doctor"

    def test_patient_without_doctor_consent_hidden(self, doctor_client, settings) -> None:
        client, _ = doctor_client
        _sql(
            settings,
            "UPDATE doctors SET consent_version = ?, consent_at = '2026-11-10 09:00:00'",
            (DOCTOR_CONSENT_VERSION,),
        )
        _give(settings)  # основное согласие есть, на врача — нет (Д4)
        uid = _user_id(settings)
        page = client.get("/doctor")
        assert page.status_code == 200 and f"/doctor/patient/{uid}" not in page.text
        assert client.get(f"/doctor/patient/{uid}").headers["location"] == "/doctor?error=denied"
        _give(settings, "doctor", _sql(settings, "SELECT id FROM doctors")[0][0])
        assert f"/doctor/patient/{uid}" in client.get("/doctor").text
        assert client.get(f"/doctor/patient/{uid}").status_code == 200


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("/doctor", "/doctor"),
        ("/doctor/patient/5", "/doctor/patient/5"),
        ("https://evil.example.org", "/doctor"),
        ("//evil.example.org", "/doctor"),
        ("/doctor//evil", "/doctor"),
        ("/doctor/\\evil", "/doctor"),
        ("/login", "/doctor"),
    ],
)
def test_safe_doctor_next(url: str, expected: str) -> None:
    assert _safe_doctor_next(url) == expected
