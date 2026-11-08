"""Кабинет врача: magic link, сессия и HTTP-сценарии (SPEC.md, раздел 21).

Покрывается: одноразовая ссылка для врача (15 минут, совместима с подписью
бота), stateless-сессия (7 дней), вход по паролю (Argon2id + rate limiting),
просмотр пациента только при `status='active'`, заметки врача (§4.10),
экспорт CSV/PDF.
"""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from itsdangerous import URLSafeTimedSerializer

from bot.config import Settings
from bot.db import create_engine, create_session_factory, init_db
from bot.models import UsedToken
from bot.handlers import doctor as doctor_handlers
from bot.services import pdf_report
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


def _open(client: TestClient, token: str):
    """Открыть ссылку как врач: страница подтверждения → «Открыть дневник»."""
    page = client.get(f"/doctor?token={token}")
    if page.status_code != 200:
        return page
    return client.post("/doctor/open", data={"csrf_token": _csrf(client), "token": token})


def _sql_count(settings: Settings, table: str) -> int:
    con = sqlite3.connect(settings.db_path)
    (count,) = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    con.close()
    return count


def _sql_exec(settings: Settings, query: str) -> None:
    con = sqlite3.connect(settings.db_path)
    con.execute(query)
    con.commit()
    con.close()


# ─────────────────────────── Magic link ───────────────────────────


class TestDoctorMagicLink:
    def test_make_and_load(self) -> None:
        magic = doctor_auth.DoctorMagicLink(SECRET)
        access = magic.load(magic.make(7, 42))
        assert access is not None
        assert (access.doctor_id, access.patient_user_id) == (7, 42)
        assert access.jti
        left = access.expires_at - doctor_auth.utcnow()
        assert timedelta(hours=71) < left <= timedelta(hours=72)

    def test_access_ttl_is_72_hours(self) -> None:
        assert doctor_auth.DOCTOR_ACCESS_MAX_AGE == 72 * 3600

    def test_expired_token_is_rejected(self, mocker) -> None:
        """Срок 72 часа проверяется по времени подписи (itsdangerous)."""
        magic = doctor_auth.DoctorMagicLink(SECRET)
        past = int(doctor_auth.utcnow().timestamp()) - (doctor_auth.DOCTOR_ACCESS_MAX_AGE + 60)
        mocker.patch("itsdangerous.timed.TimestampSigner.get_timestamp", return_value=past)
        token = magic.make(7, 42)
        mocker.stopall()
        assert magic.load(token) is None

    def test_token_alive_after_two_days(self, mocker) -> None:
        magic = doctor_auth.DoctorMagicLink(SECRET)
        past = int(doctor_auth.utcnow().timestamp()) - 48 * 3600
        mocker.patch("itsdangerous.timed.TimestampSigner.get_timestamp", return_value=past)
        token = magic.make(7, 42)
        mocker.stopall()
        assert magic.load(token) is not None

    def test_legacy_salt_is_rejected(self) -> None:
        """Ссылки до 0.4.11 (соль «doctor-login») не принимаются: иначе уже
        использованные 15-минутные ссылки ожили бы на 72 часа после деплоя."""
        token = URLSafeTimedSerializer(SECRET, salt=doctor_auth.LEGACY_DOCTOR_SALT).dumps(
            {"did": 7, "pid": 42, "jti": "j"}
        )
        assert doctor_auth.DoctorMagicLink(SECRET).load(token) is None

    def test_bot_and_cabinet_share_salt(self) -> None:
        assert doctor_handlers.DOCTOR_SALT == doctor_auth.DOCTOR_ACCESS_SALT == "doctor-access"

    def test_foreign_signature_is_rejected(self) -> None:
        token = doctor_auth.DoctorMagicLink(SECRET).make(7, 42)
        assert doctor_auth.DoctorMagicLink("other-secret").load(token) is None

    def test_garbage_is_rejected(self) -> None:
        magic = doctor_auth.DoctorMagicLink(SECRET)
        assert magic.load("просто не токен") is None
        assert magic.load("") is None

    def test_non_dict_payload_is_rejected(self) -> None:
        """Payload не-dict отклоняется без ошибки."""
        token = URLSafeTimedSerializer(SECRET, salt=doctor_auth.DOCTOR_ACCESS_SALT).dumps(
            ["did", "pid"]
        )
        assert doctor_auth.DoctorMagicLink(SECRET).load(token) is None

    @pytest.mark.parametrize(
        "payload",
        [
            {"did": "x", "pid": 42, "jti": "j"},  # типы полей
            {"did": 7, "pid": 42},  # нет jti
            {"did": 7, "pid": 42, "jti": 5},  # jti не строка
            {"did": 7, "pid": 42, "jti": ""},  # пустой jti
            {},
        ],
    )
    def test_invalid_payload_is_rejected(self, payload: dict) -> None:
        token = URLSafeTimedSerializer(SECRET, salt=doctor_auth.DOCTOR_ACCESS_SALT).dumps(
            payload
        )
        assert doctor_auth.DoctorMagicLink(SECRET).load(token) is None


class TestUsedTokens:
    """Одноразовость ссылки — в БД (`used_tokens`, §4.14), а не в памяти."""

    async def test_consume_once(self, session) -> None:
        magic = doctor_auth.DoctorMagicLink(SECRET)
        access = magic.load(magic.make(7, 42))
        assert not await doctor_auth.is_used(session, access.jti)
        assert await doctor_auth.consume(session, access) is True
        assert await doctor_auth.is_used(session, access.jti)
        assert await doctor_auth.consume(session, access) is False

    async def test_parallel_consume_one_wins(self, tmp_path) -> None:
        """Два одновременных нажатия «Открыть дневник» — проходит одно (§7.2)."""
        engine = create_engine(str(tmp_path / "used.sqlite3"))
        await init_db(engine)
        factory = create_session_factory(engine)
        magic = doctor_auth.DoctorMagicLink(SECRET)
        access = magic.load(magic.make(7, 42))

        async def attempt() -> bool:
            async with factory() as session:
                ok = await doctor_auth.consume(session, access)
                await session.commit()
                return ok

        try:
            results = await asyncio.gather(attempt(), attempt())
        finally:
            await engine.dispose()
        assert sorted(results) == [False, True]

    async def test_purge_removes_only_expired(self, session) -> None:
        now = doctor_auth.utcnow()
        session.add_all(
            [
                UsedToken(jti="old", expires_at=now - timedelta(minutes=1)),
                UsedToken(jti="fresh", expires_at=now + timedelta(hours=1)),
            ]
        )
        await session.commit()
        assert await doctor_auth.purge_used(session, now) == 1
        await session.commit()
        assert not await doctor_auth.is_used(session, "old")
        assert await doctor_auth.is_used(session, "fresh")

    def test_token_contract_with_bot(self) -> None:
        """Ссылка из бота читается веб-частью: единый salt «doctor-login»."""
        settings = Settings(
            bot_token=FAKE_TOKEN, web_secret_key=SECRET, cabinet_url=CABINET_URL
        )
        url = doctor_handlers.make_doctor_link(settings, 7, 42)
        token = url.split("token=", 1)[1]
        access = doctor_auth.DoctorMagicLink(SECRET).load(token)
        assert (access.doctor_id, access.patient_user_id) == (7, 42)


# ─────────────────────────── Сессия врача ───────────────────────────


class TestDoctorSession:
    def test_make_verify(self) -> None:
        sessions = doctor_auth.DoctorSession(SECRET)
        assert sessions.verify(sessions.make(7)) == 7

    def test_legacy_salt_is_rejected(self) -> None:
        """Ссылки до 0.4.11 (соль «doctor-login») не принимаются: иначе уже
        использованные 15-минутные ссылки ожили бы на 72 часа после деплоя."""
        token = URLSafeTimedSerializer(SECRET, salt=doctor_auth.LEGACY_DOCTOR_SALT).dumps(
            {"did": 7, "pid": 42, "jti": "j"}
        )
        assert doctor_auth.DoctorMagicLink(SECRET).load(token) is None

    def test_bot_and_cabinet_share_salt(self) -> None:
        assert doctor_handlers.DOCTOR_SALT == doctor_auth.DOCTOR_ACCESS_SALT == "doctor-access"

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
    """Ссылка доступа: GET — страница подтверждения, списание — POST `/doctor/open` (§21)."""

    def test_get_shows_confirm_and_does_not_consume(self, app_client, settings) -> None:
        """Почтовый сканер открывает ссылку GET-запросом — она остаётся рабочей."""
        client, user_id, doctor_id = app_client
        _link(settings, doctor_id, user_id, status="pending")
        token = _magic_token(doctor_id, user_id)
        for _ in range(2):
            page = client.get(f"/doctor?token={token}")
            assert page.status_code == 200
            assert "Пациент открыл вам доступ к дневнику давления." in page.text
            assert "Открыть дневник" in page.text
            assert 'action="/doctor/open"' in page.text
        assert _status(settings, doctor_id, user_id) == "pending"
        assert not client.cookies.get(DOCTOR_COOKIE)
        assert _sql_count(settings, "used_tokens") == 0

    def test_open_logs_in_and_activates(self, app_client, settings) -> None:
        client, user_id, doctor_id = app_client
        _link(settings, doctor_id, user_id, status="pending")
        response = _open(client, _magic_token(doctor_id, user_id))
        assert response.status_code == 303
        assert response.headers["location"] == f"/doctor/patient/{user_id}"
        assert client.cookies.get(DOCTOR_COOKIE)
        assert _status(settings, doctor_id, user_id) == "active"
        assert _sql_count(settings, "used_tokens") == 1

    def test_token_is_single_use(self, app_client, settings) -> None:
        client, user_id, doctor_id = app_client
        _link(settings, doctor_id, user_id)
        token = _magic_token(doctor_id, user_id)
        assert _open(client, token).status_code == 303
        again = client.get(f"/doctor?token={token}")
        assert again.status_code == 303
        assert again.headers["location"] == "/doctor?error=link_expired"
        response = client.post(
            "/doctor/open", data={"csrf_token": _csrf(client), "token": token}
        )
        assert response.headers["location"] == "/doctor?error=link_expired"

    def test_used_token_survives_restart(self, app_client, settings) -> None:
        """Списание в БД: новый процесс кабинета (рестарт) ссылку не «оживляет»."""
        client, user_id, doctor_id = app_client
        _link(settings, doctor_id, user_id)
        token = _magic_token(doctor_id, user_id)
        assert _open(client, token).headers["location"] == f"/doctor/patient/{user_id}"
        with TestClient(
            create_app(settings), base_url="https://testserver", follow_redirects=False
        ) as restarted:
            response = restarted.get(f"/doctor?token={token}")
        assert response.headers["location"] == "/doctor?error=link_expired"

    def test_startup_purges_expired_used_tokens(self, app_client, settings) -> None:
        _sql_exec(
            settings,
            "INSERT INTO used_tokens (jti, expires_at) VALUES ('old', '2020-01-01 00:00:00'),"
            " ('fresh', '2999-01-01 00:00:00')",
        )
        with TestClient(create_app(settings), base_url="https://testserver"):
            pass
        assert _sql_count(settings, "used_tokens") == 1

    def test_open_requires_csrf(self, app_client, settings) -> None:
        client, user_id, doctor_id = app_client
        _link(settings, doctor_id, user_id)
        response = client.post(
            "/doctor/open", data={"csrf_token": "bad", "token": _magic_token(doctor_id, user_id)}
        )
        assert response.status_code == 403

    def test_open_with_garbage_token(self, app_client) -> None:
        client, _, _ = app_client
        client.get("/doctor")
        response = client.post("/doctor/open", data={"csrf_token": _csrf(client), "token": "x"})
        assert response.headers["location"] == "/doctor?error=link_expired"

    def test_get_with_garbage_token(self, app_client) -> None:
        client, _, _ = app_client
        response = client.get("/doctor?token=x")
        assert response.headers["location"] == "/doctor?error=link_expired"

    def test_revoked_link_is_not_activated(self, app_client, settings) -> None:
        """Пациент отозвал доступ до открытия ссылки — статус не меняется."""
        client, user_id, doctor_id = app_client
        _link(settings, doctor_id, user_id, status="revoked")
        response = _open(client, _magic_token(doctor_id, user_id))
        assert response.status_code == 303
        assert response.headers["location"] == "/doctor?error=denied"
        assert _status(settings, doctor_id, user_id) == "revoked"

    def test_unknown_doctor_is_rejected(self, app_client) -> None:
        client, user_id, doctor_id = app_client
        response = _open(client, _magic_token(doctor_id + 99, user_id))
        assert response.status_code == 303
        assert response.headers["location"] == "/doctor?error=link_expired"

    def test_login_page_mentions_72_hours(self, app_client) -> None:
        client, _, _ = app_client
        page = client.get("/doctor?error=link_expired")
        assert "новая ссылка действует 72 часа" in page.text
        assert "15 минут" not in page.text


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

    def test_card_has_three_chart_panels(self, app_client, settings) -> None:
        """Графики сводки: три панели (SPEC.md, разделы 21 и 16.2)."""
        client, user_id, doctor_id = app_client
        _link(settings, doctor_id, user_id)
        _add_measurement(settings, user_id)
        _login_doctor(client, doctor_id)
        response = client.get(f"/doctor/patient/{user_id}")
        assert response.status_code == 200
        text = response.text
        assert 'id="bp-chart"' in text
        assert 'id="bp-chart-dia"' in text
        assert 'id="bp-chart-pulse"' in text
        assert text.count('id="bp-chart"') == 1

    def test_card_without_measurements(self, app_client, settings) -> None:
        client, user_id, doctor_id = app_client
        _link(settings, doctor_id, user_id)
        _login_doctor(client, doctor_id)
        response = client.get(f"/doctor/patient/{user_id}")
        assert response.status_code == 200
        assert "За этот период записей нет." in response.text

    def test_card_shows_medication_intake(self, app_client, settings) -> None:
        """Колонка «Лекарство» — перечень отмеченных приёмов записи (§16.2, §4.11)."""
        client, user_id, doctor_id = app_client
        _link(settings, doctor_id, user_id)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        con = sqlite3.connect(settings.db_path)
        con.execute(
            "INSERT INTO measurements (user_id, timestamp, systolic, diastolic, pulse,"
            " medication_taken, feeling, note)"
            " VALUES (?, ?, 128, 82, 64, 1, 'normal', 'после сна')",
            (user_id, now),
        )
        measurement_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        con.execute(
            "INSERT INTO medications (user_id, name, dosage, slot, created_at)"
            " VALUES (?, 'Лозартан', '50 мг', 'morning', ?)",
            (user_id, now),
        )
        medication_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        con.execute(
            "INSERT INTO measurement_medications (measurement_id, medication_id)"
            " VALUES (?, ?)",
            (measurement_id, medication_id),
        )
        con.commit()
        con.close()
        _login_doctor(client, doctor_id)
        response = client.get(f"/doctor/patient/{user_id}")
        assert response.status_code == 200
        assert 'data-label="Лекарство">Принято: Лозартан 50 мг<' in response.text


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
        _open(client, _magic_token(doctor_id, user_id))
        assert client.cookies.get(DOCTOR_COOKIE)
        client.get("/doctor")  # получить CSRF-куку
        response = client.post(
            "/doctor/logout", data={"csrf_token": _csrf(client)}
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/doctor"
        assert not client.cookies.get(DOCTOR_COOKIE)


# ─────────────────── Тяжёлая работа вне event loop (§7.2 п.7) ───────────────────


class TestDoctorBlockingOffLoop:
    """PDF и Argon2id не останавливают кабинет: пока они идут, другие запросы отвечают."""

    def test_pdf_runs_in_thread(self, app_client, settings, monkeypatch, loop_gate) -> None:
        client, user_id, doctor_id = app_client
        _link(settings, doctor_id, user_id)
        _add_measurement(settings, user_id)
        _login_doctor(client, doctor_id)
        gate = loop_gate(b"%PDF-gate")
        monkeypatch.setattr(pdf_report, "build_pdf", gate)
        pdf, page = gate.assert_loop_free(
            heavy=lambda: client.get(f"/doctor/patient/{user_id}/export.pdf"),
            light=lambda: client.get("/doctor"),
        )
        assert pdf.status_code == 200 and pdf.content == b"%PDF-gate"
        assert page.status_code in (200, 303)
        assert gate.calls == 1

    def test_login_password_check_runs_in_thread(self, app_client, monkeypatch, loop_gate) -> None:
        client, _, _ = app_client
        client.get("/doctor")
        csrf_token = _csrf(client)
        gate = loop_gate(True)
        monkeypatch.setattr(auth, "verify_password", gate)
        login, page = gate.assert_loop_free(
            heavy=lambda: client.post(
                "/doctor/login",
                data={"csrf_token": csrf_token, "email": DOCTOR_EMAIL, "password": DOCTOR_PASSWORD},
            ),
            light=lambda: client.get("/login"),
        )
        assert login.status_code == 303 and login.headers["location"] == "/doctor"
        assert page.status_code == 200


class TestPatientCardZones:
    """У врача — та же подсветка строк истории и легенда, что в ЛК (§21, §16.2)."""

    def test_rows_and_legend(self, app_client, settings) -> None:
        client, user_id, doctor_id = app_client
        _link(settings, doctor_id, user_id)
        now = datetime.now(timezone.utc)
        con = sqlite3.connect(settings.db_path)
        for minutes, (systolic, diastolic) in enumerate(((132, 90), (128, 82))):
            con.execute(
                "INSERT INTO measurements (user_id, timestamp, systolic, diastolic)"
                " VALUES (?, ?, ?, ?)",
                (user_id, (now - timedelta(minutes=minutes + 1)).strftime("%Y-%m-%d %H:%M:%S"),
                 systolic, diastolic),
            )
        con.commit()
        con.close()
        _login_doctor(client, doctor_id)
        html = client.get(f"/doctor/patient/{user_id}").text
        assert html.count('class="zone-row zone-row-high"') == 1
        assert 'title="САД: 130–139, ДАД: ≥ 90"' in html
        assert "zone-row-elevated" not in html, "128/82 — норма, без подсветки"
        assert "САД ≥ 140 · ДАД ≥ 90" in html


def test_doctor_session_rejects_non_dict_payload() -> None:
    token = URLSafeTimedSerializer(SECRET, salt=doctor_auth.DOCTOR_SESSION_SALT).dumps([1])
    assert doctor_auth.DoctorSession(SECRET).verify(token) is None
