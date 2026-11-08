"""Веб-аутентификация личного кабинета (SPEC.md, разделы 16.2 и 13).

Покрывается: хеширование/проверка Argon2id, сессионные cookie (SQLite,
HTTP-only, 7 дней), magic link (stateless-подпись, срок 15 минут,
одноразовость), CSRF (double-submit) и rate limiting входа (5 попыток/мин).
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from itsdangerous import URLSafeTimedSerializer
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.handlers import support
from bot.models import User, WebSession, WebUser
from web.cabinet import auth
from web.cabinet.app import SESSION_COOKIE, create_app
from web.cabinet.chart_zones import CHART_ZONES

FAKE_TOKEN = f"123456:{'A' * 35}"
SECRET = "test-secret"
#: Telegram ID пользователя в фикстуре: токен magic link подписывает именно его.
TG_ID = 111_111


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
    """TestClient с готовой схемой и пользователем бота в users.

    Пользователь вставляется синхронным sqlite3 после init_db (lifespan):
    так данные создаются без конфликта event loop'ов TestClient и pytest.
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
        con.commit()
        user_id = con.execute("SELECT id FROM users").fetchone()[0]
        con.close()
        yield client, user_id


def _csrf(client: TestClient) -> str:
    """CSRF-токен из cookie (его же клиент кладёт в hidden-поле формы)."""
    return client.cookies.get("csrf_token", "")


def _register(client: TestClient) -> None:
    """Завести учётную запись кабинета по magic link (email/пароль по умолчанию)."""
    magic = auth.MagicLink(SECRET)
    client.get(f"/register?token={magic.make(TG_ID)}")
    client.post(
        "/register",
        data={
            "csrf_token": _csrf(client),
            "token": magic.make(TG_ID),
            "email": "user@example.org",
            "password": "secret-pass",
            "password_confirm": "secret-pass",
        },
    )


# ─────────────────────────── Пароли (Argon2id) ───────────────────────────


class TestPasswords:
    """Хеширование и проверка паролей Argon2id."""

    def test_hash_and_verify_roundtrip(self) -> None:
        password_hash = auth.hash_password("correct horse")
        assert auth.verify_password(password_hash, "correct horse") is True

    def test_hash_is_not_plaintext_and_salted(self) -> None:
        first = auth.hash_password("same password")
        second = auth.hash_password("same password")
        assert "same password" not in first
        assert first != second  # соль генерируется автоматически

    def test_verify_wrong_password_is_false(self) -> None:
        password_hash = auth.hash_password("correct horse")
        assert auth.verify_password(password_hash, "wrong") is False

    def test_verify_broken_hash_is_false(self) -> None:
        assert auth.verify_password("не хеш", "whatever") is False

    @pytest.mark.parametrize(
        ("email", "expected"),
        [
            ("user@example.org", True),
            ("user.name+tag@mail.example", True),
            ("userexample.org", False),
            ("user @example.org", False),
            ("user@", False),
            ("", False),
        ],
    )
    def test_valid_email(self, email: str, expected: bool) -> None:
        assert auth.valid_email(email) is expected

    @pytest.mark.parametrize(
        ("password", "expected"),
        [
            ("1234567", False),
            ("12345678", True),
            ("x" * 128, True),
            ("x" * 129, False),
            ("", False),
        ],
    )
    def test_valid_password(self, password: str, expected: bool) -> None:
        assert auth.valid_password(password) is expected


# ─────────────────────────── Magic link ───────────────────────────


class TestMagicLink:
    """Подпись, срок действия 15 минут и одноразовость ссылки входа."""

    def test_make_verify_consume(self) -> None:
        magic = auth.MagicLink(SECRET)
        token = magic.make(42)
        assert magic.verify(token) == 42
        assert magic.consume(token) == 42

    def test_consume_is_single_use(self) -> None:
        magic = auth.MagicLink(SECRET)
        token = magic.make(42)
        assert magic.consume(token) == 42
        assert magic.consume(token) is None
        assert magic.verify(token) is None  # использованный токен не проверяется

    def test_verify_does_not_consume(self) -> None:
        """Страница регистрации показывает форму, не списывая токен."""
        magic = auth.MagicLink(SECRET)
        token = magic.make(42)
        assert magic.verify(token) == 42
        assert magic.verify(token) == 42
        assert magic.consume(token) == 42

    def test_expired_token_is_rejected(self, mocker) -> None:
        """Срок 15 минут проверяется по времени подписи (itsdangerous)."""
        magic = auth.MagicLink(SECRET)
        past = int(auth.utcnow().timestamp()) - (auth.MAGIC_LINK_MAX_AGE + 60)
        mocker.patch(
            "itsdangerous.timed.TimestampSigner.get_timestamp", return_value=past
        )
        token = magic.make(42)
        mocker.stopall()
        assert magic.verify(token) is None
        assert magic.consume(token) is None

    def test_foreign_signature_is_rejected(self) -> None:
        token = auth.MagicLink(SECRET).make(42)
        assert auth.MagicLink("other-secret").verify(token) is None

    def test_garbage_is_rejected(self) -> None:
        magic = auth.MagicLink(SECRET)
        assert magic.verify("просто не токен") is None
        assert magic.consume("") is None

    def test_used_jti_is_purged_after_expiry(self) -> None:
        """Память одноразовых токенов чистится по истечении срока."""
        magic = auth.MagicLink(SECRET)
        token = magic.make(42)
        magic.consume(token)
        assert len(magic._used) == 1
        later = auth.utcnow() + timedelta(seconds=auth.MAGIC_LINK_MAX_AGE + 1)
        magic.verify("что-то", now=later)
        assert len(magic._used) == 0


# ─────────────────────────── CSRF ───────────────────────────


    def test_non_dict_payload_is_rejected(self) -> None:
        """Payload не-dict (не формат uid/jti) отклоняется без ошибки."""
        token = URLSafeTimedSerializer(SECRET, salt="cabinet-login").dumps(7)
        magic = auth.MagicLink(SECRET)
        assert magic.verify(token) is None
        assert magic.consume(token) is None


class TestCabinetLinkContract:
    """Контракт бот ↔ кабинет: токен из бота принимается веб-частью (§16.2)."""

    def test_bot_link_verifies_in_cabinet(self) -> None:
        settings = Settings(
            bot_token=FAKE_TOKEN,
            web_secret_key=SECRET,
            cabinet_url="https://cabinet.example.org",
        )
        token = support.make_cabinet_link(settings, 42).partition("token=")[2]
        magic = auth.MagicLink(SECRET)
        assert magic.verify(token) == 42
        assert magic.consume(token) == 42
        assert magic.consume(token) is None  # одноразовость сохраняется

    def test_foreign_secret_is_rejected(self) -> None:
        settings = Settings(
            bot_token=FAKE_TOKEN,
            web_secret_key="other-secret",
            cabinet_url="https://cabinet.example.org",
        )
        token = support.make_cabinet_link(settings, 42).partition("token=")[2]
        assert auth.MagicLink(SECRET).verify(token) is None

    def test_full_flow_from_bot_link(self, app_client, settings) -> None:
        """Полный цикл: ссылка из бота → создание пароля → вход (§16.2).

        Главная ловушка рассинхрона: бот подписывает `telegram_id`, а не
        `users.id` — здесь токен проходит весь путь через БД.
        """
        client, _ = app_client
        token = support.make_cabinet_link(settings, TG_ID).partition("token=")[2]
        response = client.get(f"/login?token={token}")
        assert response.status_code == 303
        assert response.headers["location"] == f"/register?token={token}"
        client.get(f"/register?token={token}")
        response = client.post(
            "/register",
            data={
                "csrf_token": _csrf(client),
                "token": token,
                "email": "user@example.org",
                "password": "secret-pass",
                "password_confirm": "secret-pass",
            },
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/"
        # Повторная ссылка бота — уже вход, без регистрации.
        token = support.make_cabinet_link(settings, TG_ID).partition("token=")[2]
        client.cookies.delete(SESSION_COOKIE)
        response = client.get(f"/login?token={token}")
        assert response.status_code == 303
        assert response.headers["location"] == "/"

    def test_unknown_telegram_id_is_rejected(self, app_client) -> None:
        """Токен на telegram_id без строки в `users` — ссылка недействительна."""
        client, _ = app_client
        token = auth.MagicLink(SECRET).make(999_999)
        response = client.get(f"/login?token={token}")
        assert response.status_code == 303
        assert response.headers["location"] == "/login?error=link_expired"
        client.get("/login")
        response = client.post(
            "/register",
            data={
                "csrf_token": _csrf(client),
                "token": token,
                "email": "user@example.org",
                "password": "secret-pass",
                "password_confirm": "secret-pass",
            },
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/login?error=link_expired"

    def test_failed_validation_keeps_token_alive(self, app_client) -> None:
        """Опечатка в форме не списывает токен — ссылка остаётся рабочей."""
        client, _ = app_client
        token = auth.MagicLink(SECRET).make(TG_ID)
        client.get(f"/register?token={token}")
        response = client.post(
            "/register",
            data={
                "csrf_token": _csrf(client),
                "token": token,
                "email": "user@example.org",
                "password": "short",
                "password_confirm": "short",
            },
        )
        assert response.status_code == 400
        response = client.post(
            "/register",
            data={
                "csrf_token": _csrf(client),
                "token": token,
                "email": "user@example.org",
                "password": "secret-pass",
                "password_confirm": "secret-pass",
            },
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/"


class TestCsrf:
    """Подписанный double-submit токен (cookie + hidden field)."""

    def test_issue_and_validate(self) -> None:
        csrf = auth.Csrf(SECRET)
        token = csrf.issue()
        assert csrf.validate(token, token) is True

    def test_mismatch_with_cookie_is_rejected(self) -> None:
        csrf = auth.Csrf(SECRET)
        assert csrf.validate(csrf.issue(), "другой") is False

    def test_forged_token_is_rejected(self) -> None:
        csrf = auth.Csrf(SECRET)
        forged = auth.Csrf("other-secret").issue()
        assert csrf.validate(forged, forged) is False

    def test_empty_tokens_are_rejected(self) -> None:
        csrf = auth.Csrf(SECRET)
        assert csrf.validate("", "") is False


# ─────────────────────────── Сессии (SQLite) ───────────────────────────


@pytest.fixture
async def web_user(session: AsyncSession, user: User) -> WebUser:
    """Учётная запись кабинета, привязанная к пользователю бота."""
    record = WebUser(
        user_id=user.id, email="user@example.org", password_hash=auth.hash_password("secret-pass")
    )
    session.add(record)
    await session.commit()
    return record


class TestSessionsDb:
    """Сессионный токен в SQLite: срок жизни 7 дней, удаление, чистка."""

    async def test_create_and_get(self, session: AsyncSession, web_user: WebUser) -> None:
        token = await auth.create_web_session(session, web_user.id)
        await session.commit()
        found = await auth.get_web_user_by_token(session, token)
        assert found is not None
        assert found.id == web_user.id

    async def test_create_commits_immediately(
        self, session: AsyncSession, web_user: WebUser
    ) -> None:
        """Сессия сохраняется сразу, без отложенного коммита хендлера.

        Регрессия: `create_web_session` делал только `flush`, а `get_db`
        коммитил в teardown после отправки ответа. Мгновенный `GET /` после
        редиректа входа не находил сессию и откатывал на `/login`.
        """
        token = await auth.create_web_session(session, web_user.id)
        await session.rollback()  # «откат» не должен стирать выданную сессию
        assert await auth.get_web_user_by_token(session, token) is not None

    async def test_unknown_or_empty_token(self, session: AsyncSession) -> None:
        assert await auth.get_web_user_by_token(session, "нет-такого") is None
        assert await auth.get_web_user_by_token(session, "") is None

    async def test_expired_session_is_invisible(
        self, session: AsyncSession, web_user: WebUser
    ) -> None:
        token = await auth.create_web_session(session, web_user.id)
        row = await session.get(WebSession, token)
        row.expires_at = auth.utcnow() - timedelta(seconds=1)
        await session.commit()
        assert await auth.get_web_user_by_token(session, token) is None

    async def test_delete_session(self, session: AsyncSession, web_user: WebUser) -> None:
        token = await auth.create_web_session(session, web_user.id)
        await session.commit()
        await auth.delete_web_session(session, token)
        await session.commit()
        assert await auth.get_web_user_by_token(session, token) is None

    async def test_create_purges_expired_rows(
        self, session: AsyncSession, web_user: WebUser
    ) -> None:
        stale = WebSession(
            token="stale-token",
            web_user_id=web_user.id,
            created_at=auth.utcnow() - timedelta(days=8),
            expires_at=auth.utcnow() - timedelta(days=1),
        )
        session.add(stale)
        await session.commit()
        await auth.create_web_session(session, web_user.id)
        await session.commit()
        assert await session.get(WebSession, "stale-token") is None


# ─────────────────────────── Rate limiting ───────────────────────────


class TestRateLimiter:
    """Не более 5 попыток входа в минуту на ключ."""

    def test_sixth_attempt_is_blocked(self) -> None:
        limiter = auth.RateLimiter()
        now = auth.utcnow()
        for _ in range(auth.LOGIN_RATE_LIMIT):
            assert limiter.allow("ip:mail", now=now) is True
            limiter.hit("ip:mail", now=now)
        assert limiter.allow("ip:mail", now=now) is False

    def test_window_slides(self) -> None:
        limiter = auth.RateLimiter()
        now = auth.utcnow()
        for _ in range(auth.LOGIN_RATE_LIMIT):
            limiter.hit("key", now=now)
        assert limiter.allow("key", now=now) is False
        later = now + timedelta(seconds=auth.LOGIN_RATE_WINDOW + 1)
        assert limiter.allow("key", now=later) is True

    def test_keys_are_independent(self) -> None:
        limiter = auth.RateLimiter()
        now = auth.utcnow()
        for _ in range(auth.LOGIN_RATE_LIMIT):
            limiter.hit("a", now=now)
        assert limiter.allow("a", now=now) is False
        assert limiter.allow("b", now=now) is True

    def test_reset_clears_counter(self) -> None:
        limiter = auth.RateLimiter()
        now = auth.utcnow()
        for _ in range(auth.LOGIN_RATE_LIMIT):
            limiter.hit("key", now=now)
        limiter.reset("key")
        assert limiter.allow("key", now=now) is True


# ─────────────────────────── HTTP-сценарии ───────────────────────────


class TestLoginRoutes:
    """Вход e-mail + пароль: CSRF, ошибки, rate limiting, cookie."""

    def test_login_page_renders(self, app_client) -> None:
        client, _ = app_client
        response = client.get("/login")
        assert response.status_code == 200
        assert "Личный кабинет" in response.text
        assert client.cookies.get("csrf_token")

    def test_login_without_csrf_is_rejected(self, app_client) -> None:
        client, _ = app_client
        response = client.post(
            "/login", data={"email": "user@example.org", "password": "x" * 8}
        )
        assert response.status_code == 403

    def test_login_wrong_password_redirects(self, app_client) -> None:
        client, _ = app_client
        client.get("/login")  # пользователь открыл форму — есть CSRF-cookie
        response = client.post(
            "/login",
            data={
                "csrf_token": _csrf(client),
                "email": "user@example.org",
                "password": "wrong-password",
            },
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/login?error=wrong"

    def test_login_rate_limit(self, app_client) -> None:
        client, _ = app_client
        client.get("/login")
        for _ in range(auth.LOGIN_RATE_LIMIT):
            response = client.post(
                "/login",
                data={
                    "csrf_token": _csrf(client),
                    "email": "user@example.org",
                    "password": "wrong-password",
                },
            )
            assert response.status_code == 303
        response = client.post(
            "/login",
            data={
                "csrf_token": _csrf(client),
                "email": "user@example.org",
                "password": "wrong-password",
            },
        )
        assert response.status_code == 429

    def test_login_success_sets_cookie(self, app_client) -> None:
        client, user_id = app_client
        magic = auth.MagicLink(SECRET)
        client.get(f"/register?token={magic.make(TG_ID)}")
        response = client.post(
            "/register",
            data={
                "csrf_token": _csrf(client),
                "token": magic.make(TG_ID),
                "email": "user@example.org",
                "password": "secret-pass",
                "password_confirm": "secret-pass",
            },
        )
        assert response.status_code == 303
        client.cookies.delete(SESSION_COOKIE)
        client.cookies.delete("csrf_token")
        client.get("/login")
        response = client.post(
            "/login",
            data={
                "csrf_token": _csrf(client),
                "email": "user@example.org",
                "password": "secret-pass",
            },
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/"
        raw_cookie = [
            value
            for value in response.headers.get_list("set-cookie")
            if value.startswith(f"{SESSION_COOKIE}=")
        ][0]
        assert "HttpOnly" in raw_cookie
        assert "Secure" in raw_cookie
        assert "Max-Age=604800" in raw_cookie


class TestRobotsTxt:
    """Кабинет закрыт от индексации (SPEC.md, §16.2)."""

    def test_disallow_all_without_auth(self, app_client) -> None:
        client, _ = app_client
        response = client.get("/robots.txt")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")
        assert response.text == "User-agent: *\nDisallow: /\n"


class TestSecureCookieFlag:
    """Secure-флаг cookie следует схеме доступа (§16.2, локальный деплой по HTTP)."""

    def test_http_local_registration_works(self, settings: Settings) -> None:
        """Регрессия: по http:// Secure-куки клиент не возвращает — формы падали с 403."""
        app = create_app(settings)
        with TestClient(app, base_url="http://testserver", follow_redirects=False) as client:
            con = sqlite3.connect(settings.db_path)
            con.execute(
                "INSERT INTO users (telegram_id, timezone, target_systolic, target_diastolic,"
                " med_remeasure_delay, onboarded, created_at)"
                " VALUES (?, 'Europe/Minsk', 130, 80, 60, 1, '2026-01-01 00:00:00')",
                (TG_ID,),
            )
            con.commit()
            con.close()
            token = auth.MagicLink(SECRET).make(TG_ID)
            response = client.get(f"/register?token={token}")
            assert response.status_code == 200
            raw = [
                value
                for value in response.headers.get_list("set-cookie")
                if value.startswith("csrf_token=")
            ][0]
            assert "Secure" not in raw
            response = client.post(
                "/register",
                data={
                    "csrf_token": _csrf(client),
                    "token": token,
                    "email": "user@example.org",
                    "password": "secret-pass",
                    "password_confirm": "secret-pass",
                },
            )
            assert response.status_code == 303
            assert response.headers["location"] == "/"

    def test_https_cabinet_url_keeps_secure_flag(self, tmp_path) -> None:
        """Продакшен (`CABINET_URL=https://…`): Secure ставится и по http-запросу."""
        prod_settings = Settings(
            bot_token=FAKE_TOKEN,
            web_secret_key=SECRET,
            cabinet_url="https://cabinet.example.org",
            db_path=str(tmp_path / "prod.sqlite3"),
        )
        app = create_app(prod_settings)
        with TestClient(app, base_url="http://testserver", follow_redirects=False) as client:
            response = client.get("/login")
            raw = [
                value
                for value in response.headers.get_list("set-cookie")
                if value.startswith("csrf_token=")
            ][0]
            assert "Secure" in raw


class TestMagicRoutes:
    """Magic link из бота: вход и первичная привязка пароля."""

    def test_bad_token_redirects_to_login(self, app_client) -> None:
        client, _ = app_client
        response = client.get("/magic?token=нечто")
        assert response.status_code == 303
        assert response.headers["location"] == "/login?error=link_expired"

    def test_first_visit_offers_registration(self, app_client) -> None:
        client, user_id = app_client
        token = auth.MagicLink(SECRET).make(TG_ID)
        response = client.get(f"/magic?token={token}")
        assert response.status_code == 303
        assert response.headers["location"] == f"/register?token={token}"

    def test_magic_logs_in_registered_user(self, app_client) -> None:
        client, user_id = app_client
        magic = auth.MagicLink(SECRET)
        client.get(f"/register?token={magic.make(TG_ID)}")
        client.post(
            "/register",
            data={
                "csrf_token": _csrf(client),
                "token": magic.make(TG_ID),
                "email": "user@example.org",
                "password": "secret-pass",
                "password_confirm": "secret-pass",
            },
        )
        client.cookies.delete(SESSION_COOKIE)
        response = client.get(f"/magic?token={magic.make(TG_ID)}")
        assert response.status_code == 303
        assert response.headers["location"] == "/"

    def test_register_page_requires_valid_token(self, app_client) -> None:
        client, _ = app_client
        response = client.get("/register")
        assert response.status_code == 303
        assert response.headers["location"] == "/login?error=link_expired"

    def test_login_with_token_bad_token_redirects(self, app_client) -> None:
        """`/login?token=` — тот же magic link, что и `/magic` (ссылка из бота)."""
        client, _ = app_client
        response = client.get("/login?token=нечто")
        assert response.status_code == 303
        assert response.headers["location"] == "/login?error=link_expired"

    def test_login_with_token_offers_registration(self, app_client) -> None:
        client, user_id = app_client
        token = auth.MagicLink(SECRET).make(TG_ID)
        response = client.get(f"/login?token={token}")
        assert response.status_code == 303
        assert response.headers["location"] == f"/register?token={token}"

    def test_login_with_token_logs_in_registered_user(self, app_client) -> None:
        client, user_id = app_client
        magic = auth.MagicLink(SECRET)
        _register(client)
        client.cookies.delete(SESSION_COOKIE)
        response = client.get(f"/login?token={magic.make(TG_ID)}")
        assert response.status_code == 303
        assert response.headers["location"] == "/"


class TestRegisterRoutes:
    """Регистрация e-mail + пароль по magic link (Argon2id)."""

    def test_register_page_renders(self, app_client) -> None:
        client, user_id = app_client
        response = client.get(f"/register?token={auth.MagicLink(SECRET).make(TG_ID)}")
        assert response.status_code == 200
        assert "Создание пароля" in response.text

    def test_register_success_and_single_use_token(self, app_client) -> None:
        client, user_id = app_client
        magic = auth.MagicLink(SECRET)
        token = magic.make(TG_ID)
        client.get(f"/register?token={token}")
        response = client.post(
            "/register",
            data={
                "csrf_token": _csrf(client),
                "token": token,
                "email": "user@example.org",
                "password": "secret-pass",
                "password_confirm": "secret-pass",
            },
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/"
        # Тот же токен повторно — уже использован.
        response = client.post(
            "/register",
            data={
                "csrf_token": _csrf(client),
                "token": token,
                "email": "second@example.org",
                "password": "secret-pass",
                "password_confirm": "secret-pass",
            },
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/login?error=link_expired"

    @pytest.mark.parametrize(
        ("data_patch", "needle"),
        [
            ({"email": "нет-почты"}, "корректный e-mail"),
            ({"password": "123", "password_confirm": "123"}, "от 8 до 128"),
            ({"password_confirm": "other-pass"}, "не совпадают"),
        ],
    )
    def test_register_validation_errors(self, app_client, data_patch, needle) -> None:
        client, user_id = app_client
        token = auth.MagicLink(SECRET).make(TG_ID)
        client.get(f"/register?token={token}")
        data = {
            "csrf_token": _csrf(client),
            "token": token,
            "email": "user@example.org",
            "password": "secret-pass",
            "password_confirm": "secret-pass",
        }
        data.update(data_patch)
        response = client.post("/register", data=data)
        assert response.status_code == 400
        assert needle in response.text

    def test_register_existing_email_is_rejected(self, app_client) -> None:
        client, user_id = app_client
        magic = auth.MagicLink(SECRET)
        data = {
            "email": "user@example.org",
            "password": "secret-pass",
            "password_confirm": "secret-pass",
        }
        client.get(f"/register?token={magic.make(TG_ID)}")
        client.post(
            "/register",
            data={**data, "csrf_token": _csrf(client), "token": magic.make(TG_ID)},
        )
        response = client.post(
            "/register",
            data={**data, "csrf_token": _csrf(client), "token": magic.make(TG_ID)},
        )
        assert response.status_code == 400
        assert "уже есть" in response.text


class TestSessionRoutes:
    """Сессия на dashboard: cookie, выход, защита страниц."""

    def test_dashboard_requires_login(self, app_client) -> None:
        client, _ = app_client
        response = client.get("/")
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    def test_dashboard_shows_card(self, app_client) -> None:
        client, user_id = app_client
        _register(client)
        response = client.get("/")
        assert response.status_code == 200
        assert "Личная карточка" in response.text
        assert "user@example.org" in response.text

    def test_logout_revokes_session(self, app_client) -> None:
        client, user_id = app_client
        _register(client)
        response = client.post("/logout", data={"csrf_token": _csrf(client)})
        assert response.status_code == 303
        assert response.headers["location"] == "/login"
        assert client.cookies.get(SESSION_COOKIE) is None
        response = client.get("/")
        assert response.status_code == 303

    def test_logout_requires_csrf(self, app_client) -> None:
        client, user_id = app_client
        _register(client)
        response = client.post("/logout")
        assert response.status_code == 403
        response = client.get("/")
        assert response.status_code == 200  # сессия осталась


class TestDashboardRoutes:
    """Содержимое кабинета: профиль, история, лекарства, напоминания, CSV."""

    def _sql(self, settings: Settings, query: str, args: tuple = ()) -> list[tuple]:
        con = sqlite3.connect(settings.db_path)
        rows = con.execute(query, args).fetchall()
        con.close()
        return rows

    def test_profile_saves_goals_and_timezone(self, app_client, settings) -> None:
        client, user_id = app_client
        _register(client)
        response = client.post(
            "/profile",
            data={
                "csrf_token": _csrf(client),
                "target_systolic": "125",
                "target_diastolic": "75",
                "timezone": "Europe/Moscow",
            },
        )
        assert response.status_code == 303
        rows = self._sql(
            settings,
            "SELECT target_systolic, target_diastolic, timezone FROM users WHERE id = ?",
            (user_id,),
        )
        assert rows == [(125, 75, "Europe/Moscow")]

    def test_profile_rejects_bad_timezone(self, app_client) -> None:
        client, user_id = app_client
        _register(client)
        response = client.post(
            "/profile",
            data={
                "csrf_token": _csrf(client),
                "target_systolic": "125",
                "target_diastolic": "75",
                "timezone": "Mars/Olympus",
            },
        )
        assert response.status_code == 303
        assert "error=profile" in response.headers["location"]

    def test_profile_changes_password(self, app_client) -> None:
        client, user_id = app_client
        _register(client)
        response = client.post(
            "/profile",
            data={
                "csrf_token": _csrf(client),
                "target_systolic": "130",
                "target_diastolic": "80",
                "timezone": "Europe/Minsk",
                "new_password": "brand-new-pass",
            },
        )
        assert response.status_code == 303
        client.post("/logout", data={"csrf_token": _csrf(client)})
        client.get("/login")
        response = client.post(
            "/login",
            data={
                "csrf_token": _csrf(client),
                "email": "user@example.org",
                "password": "brand-new-pass",
            },
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/"

    def test_medication_crud(self, app_client, settings) -> None:
        client, user_id = app_client
        _register(client)
        response = client.post(
            "/medications",
            data={
                "csrf_token": _csrf(client),
                "name": "Лозартан",
                "dosage": "50 мг",
                "slot_morning": "1",
            },
        )
        assert response.status_code == 303
        rows = self._sql(
            settings,
            "SELECT id, name, dosage, slot FROM medications WHERE user_id = ?",
            (user_id,),
        )
        assert rows[0][1:] == ("Лозартан", "50 мг", "morning")
        med_id = rows[0][0]

        response = client.post(
            f"/medications/{med_id}",
            data={
                "csrf_token": _csrf(client),
                "name": "Лозартан",
                "dosage": "100 мг",
                "slot": "evening",
            },
        )
        assert response.status_code == 303
        rows = self._sql(
            settings,
            "SELECT dosage, slot FROM medications WHERE id = ?",
            (med_id,),
        )
        assert rows == [("100 мг", "evening")]

        response = client.post(
            f"/medications/{med_id}/delete", data={"csrf_token": _csrf(client)}
        )
        assert response.status_code == 303
        assert self._sql(settings, "SELECT id FROM medications") == []

    def test_medication_update_conflict_is_rejected(self, app_client, settings) -> None:
        client, user_id = app_client
        _register(client)
        for name, slot in (("Лозартан", "morning"), ("Амлодипин", "noon")):
            client.post(
                "/medications",
                data={
                    "csrf_token": _csrf(client),
                    "name": name,
                    "dosage": "5 мг",
                    f"slot_{slot}": "1",
                },
            )
        rows = self._sql(
            settings,
            "SELECT id, name FROM medications WHERE user_id = ? ORDER BY id",
            (user_id,),
        )
        response = client.post(
            f"/medications/{rows[0][0]}",
            data={
                "csrf_token": _csrf(client),
                "name": rows[1][1],  # имя уже занято в слоте noon
                "dosage": "5 мг",
                "slot": "noon",
            },
        )
        assert response.status_code == 303
        assert "error=medication" in response.headers["location"]

    def test_medication_without_dosage_and_crisis_slot(self, app_client, settings) -> None:
        """Дозировка опциональна, слот «Кризис» доступен — и при добавлении, и при правке."""
        client, user_id = app_client
        _register(client)
        response = client.post(
            "/medications",
            data={
                "csrf_token": _csrf(client),
                "name": "Каптоприл",
                "dosage": "",
                "slot_crisis": "1",
            },
        )
        assert response.status_code == 303
        rows = self._sql(
            settings,
            "SELECT id, name, dosage, slot FROM medications WHERE user_id = ?",
            (user_id,),
        )
        assert rows[0][1:] == ("Каптоприл", "", "crisis")
        med_id = rows[0][0]

        response = client.post(
            f"/medications/{med_id}",
            data={
                "csrf_token": _csrf(client),
                "name": "Каптоприл",
                "dosage": "",
                "slot": "crisis",
            },
        )
        assert response.status_code == 303
        rows = self._sql(
            settings, "SELECT dosage, slot FROM medications WHERE id = ?", (med_id,)
        )
        assert rows == [("", "crisis")]

    def test_medication_update_bad_slot_is_rejected(self, app_client, settings) -> None:
        client, user_id = app_client
        _register(client)
        client.post(
            "/medications",
            data={
                "csrf_token": _csrf(client),
                "name": "Лозартан",
                "dosage": "50 мг",
                "slot_morning": "1",
            },
        )
        med_id = self._sql(
            settings, "SELECT id FROM medications WHERE user_id = ?", (user_id,)
        )[0][0]
        response = client.post(
            f"/medications/{med_id}",
            data={
                "csrf_token": _csrf(client),
                "name": "Лозартан",
                "dosage": "50 мг",
                "slot": "night",
            },
        )
        assert response.status_code == 303
        assert "error=medication" in response.headers["location"]

    def test_settings_create_and_disable_slot(self, app_client, settings) -> None:
        client, user_id = app_client
        _register(client)
        response = client.post(
            "/settings",
            data={
                "csrf_token": _csrf(client),
                "slot_morning_enabled": "1",
                "slot_morning_time": "08:30",
                "slot_noon_time": "13:00",
                "slot_evening_time": "21:00",
                "med_remeasure_delay": "60",
            },
        )
        assert response.status_code == 303
        rows = self._sql(
            settings,
            "SELECT type, local_time, enabled FROM reminders WHERE user_id = ?",
            (user_id,),
        )
        assert rows == [("morning", "08:30", 1)]

        response = client.post(
            "/settings",
            data={
                "csrf_token": _csrf(client),
                "slot_morning_time": "08:30",
                "slot_noon_time": "13:00",
                "slot_evening_time": "21:00",
                "med_remeasure_delay": "",
            },
        )
        assert response.status_code == 303
        rows = self._sql(
            settings,
            "SELECT enabled FROM reminders WHERE user_id = ?",
            (user_id,),
        )
        assert rows == [(0,)]
        rows = self._sql(
            settings,
            "SELECT med_remeasure_delay FROM users WHERE id = ?",
            (user_id,),
        )
        assert rows == [(None,)]

    def test_settings_reject_bad_time(self, app_client) -> None:
        client, user_id = app_client
        _register(client)
        response = client.post(
            "/settings",
            data={
                "csrf_token": _csrf(client),
                "slot_morning_enabled": "1",
                "slot_morning_time": "25:99",
                "med_remeasure_delay": "60",
            },
        )
        assert response.status_code == 303
        assert "error=reminder" in response.headers["location"]

    def test_settings_single_digit_hour_normalized(self, app_client, settings) -> None:
        """«2:30» — то же, что «02:30»: принимается и сохраняется нормализованным (§2)."""
        client, user_id = app_client
        _register(client)
        response = client.post(
            "/settings",
            data={
                "csrf_token": _csrf(client),
                "slot_morning_enabled": "1",
                "slot_morning_time": "8:30",
                "med_remeasure_delay": "60",
            },
        )
        assert response.status_code == 303
        rows = self._sql(
            settings,
            "SELECT local_time FROM reminders WHERE user_id = ?",
            (user_id,),
        )
        assert rows == [("08:30",)]

    def test_history_and_csv_export(self, app_client, settings) -> None:
        client, user_id = app_client
        _register(client)
        con = sqlite3.connect(settings.db_path)
        con.execute(
            "INSERT INTO measurements (user_id, timestamp, systolic, diastolic, pulse,"
            " medication_taken, feeling, note)"
            " VALUES (?, '2026-01-02 07:05:00', 128, 82, 64, 1, 'normal', 'после сна')",
            (user_id,),
        )
        con.commit()
        con.close()

        response = client.get(
            "/?days=custom&date_from=2026-01-01&date_to=2026-01-03"
        )
        assert response.status_code == 200
        assert "128" in response.text
        assert "после сна" in response.text

        response = client.get("/export/measurements.csv?days=custom&date_from=2026-01-01&date_to=2026-01-03")
        assert response.status_code == 200
        assert response.content.startswith(b"\xef\xbb\xbf")  # UTF-8 с BOM
        assert b"128" in response.content
        assert "attachment" in response.headers["content-disposition"]

        response = client.get("/export/medications.csv?days=7")
        assert response.status_code == 200

    def test_history_shows_medication_intake(self, app_client, settings) -> None:
        """Колонка «Лекарство» — перечень отмеченных приёмов записи (§16.2, §4.11).

        Есть `measurement_medications` — «Принято: Лозартан 50 мг»; без связей —
        статус флага «Принято» / «Пропущено» / «—».
        """
        client, user_id = app_client
        _register(client)
        con = sqlite3.connect(settings.db_path)
        hours = {1: "07", 0: "08", None: "09"}
        first_id = None
        for medication_taken, hour in hours.items():
            con.execute(
                "INSERT INTO measurements (user_id, timestamp, systolic, diastolic,"
                " medication_taken, note)"
                " VALUES (?, ?, 128, 82, ?, '')",
                (user_id, f"2026-01-02 {hour}:05:00", medication_taken),
            )
            if first_id is None:
                first_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        con.execute(
            "INSERT INTO medications (user_id, name, dosage, slot, created_at)"
            " VALUES (?, 'Лозартан', '50 мг', 'morning', '2026-01-01 00:00:00')",
            (user_id,),
        )
        medication_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        con.execute(
            "INSERT INTO measurement_medications (measurement_id, medication_id)"
            " VALUES (?, ?)",
            (first_id, medication_id),
        )
        con.commit()
        con.close()

        response = client.get(
            "/?days=custom&date_from=2026-01-01&date_to=2026-01-03"
        )
        assert response.status_code == 200
        text = response.text
        assert 'data-label="Лекарство">Принято: Лозартан 50 мг<' in text
        assert 'data-label="Лекарство">Пропущено<' in text
        assert 'data-label="Лекарство">—<' in text


class TestDashboardCharts:
    """Графики сводки: три панели и цветные зоны АД (SPEC.md, раздел 16.2)."""

    def _add_measurement(self, settings: Settings, user_id: int) -> None:
        con = sqlite3.connect(settings.db_path)
        con.execute(
            "INSERT INTO measurements (user_id, timestamp, systolic, diastolic, pulse,"
            " medication_taken, feeling, note)"
            " VALUES (?, '2026-01-02 07:05:00', 128, 82, 64, 1, 'normal', 'после сна')",
            (user_id,),
        )
        con.commit()
        con.close()

    def test_summary_has_three_chart_panels(self, app_client, settings) -> None:
        client, user_id = app_client
        _register(client)
        self._add_measurement(settings, user_id)
        response = client.get("/?days=custom&date_from=2026-01-01&date_to=2026-01-03")
        assert response.status_code == 200
        text = response.text
        assert 'id="bp-chart"' in text
        assert 'id="bp-chart-dia"' in text
        assert 'id="bp-chart-pulse"' in text
        # E2E-локатор qa/test_web_e2e.py: «bp-chart» — ровно один на страницу
        # (закрывающая кавычка не даёт совпасть с bp-chart-dia).
        assert text.count('id="bp-chart"') == 1

    def test_chart_zone_thresholds_rendered(self, app_client, settings) -> None:
        client, user_id = app_client
        _register(client)
        self._add_measurement(settings, user_id)
        response = client.get("/?days=custom&date_from=2026-01-01&date_to=2026-01-03")
        assert response.status_code == 200
        match = re.search(
            r'<script id="bp-chart-zones" type="application/json">(.*?)</script>',
            response.text,
            re.DOTALL,
        )
        assert match, "нет JSON-острова #bp-chart-zones"
        zones = json.loads(match.group(1))
        assert [band["to"] for band in zones["systolic"]] == [130, 140, None]
        assert [band["to"] for band in zones["diastolic"]] == [85, 90, None]
        for key in ("systolic", "diastolic"):
            assert len(zones[key]) == 3
            for band in zones[key]:
                assert band["label"]
                assert band["fill"].startswith("rgba(")

    def test_chart_zone_constants(self) -> None:
        # Единый источник истины — константы web/cabinet/chart_zones.py:
        # границы ESC/ESH + ВОЗ (SPEC.md, раздел 16.2), у «Пульса» зон нет.
        assert "pulse" not in CHART_ZONES
        expected = {"systolic": [130, 140], "diastolic": [85, 90]}
        for key, thresholds in expected.items():
            bands = CHART_ZONES[key]
            assert [band["to"] for band in bands[:-1]] == thresholds
            assert bands[-1]["to"] is None
            bounds = [band["to"] for band in bands if band["to"] is not None]
            assert bounds == sorted(bounds)
            for band in bands:
                assert band["fill"].startswith("rgba(")
                if band["to"] is None:
                    assert band["line"] is None
                else:
                    assert band["line"].startswith("rgba(")

    def test_medications_wide_block(self, app_client, settings) -> None:
        client, user_id = app_client
        _register(client)
        for name, dosage, slot_field in (
            ("Лозартан", "50 мг", "slot_morning"),
            ("Моксонидин", "", "slot_crisis"),
        ):
            response = client.post(
                "/medications",
                data={
                    "csrf_token": _csrf(client),
                    "name": name,
                    "dosage": dosage,
                    slot_field: "1",
                },
            )
            assert response.status_code == 303
        response = client.get("/")
        assert response.status_code == 200
        text = response.text
        # «Лекарства» — на всю ширину ряда, группы по слотам (§16.2).
        assert "facts-wide" in text
        assert "med-groups" in text
        assert '<span class="med-group-title">Утро</span>' in text
        assert '<span class="med-group-title">Кризис</span>' in text
        # Пустые группы («Обед», «Вечер») не показываются.
        assert '<span class="med-group-title">Обед</span>' not in text
        assert '<span class="med-group-title">Вечер</span>' not in text
        assert "<li>Лозартан (50 мг)</li>" in text
        assert "<li>Моксонидин</li>" in text


# ─────────────────── Argon2id вне event loop (§7.2 п.7) ───────────────────


class TestPasswordHashingOffLoop:
    """Хеширование и проверка пароля не останавливают кабинет для остальных."""

    def test_login_verify_runs_in_thread(self, app_client, monkeypatch, loop_gate) -> None:
        client, _ = app_client
        _register(client)
        client.post("/logout", data={"csrf_token": _csrf(client)})
        client.get("/login")
        csrf_token = _csrf(client)
        gate = loop_gate(True)
        monkeypatch.setattr(auth, "verify_password", gate)
        login, page = gate.assert_loop_free(
            heavy=lambda: client.post(
                "/login",
                data={"csrf_token": csrf_token, "email": "user@example.org", "password": "secret-pass"},
            ),
            light=lambda: client.get("/login"),
        )
        assert login.status_code == 303 and login.headers["location"] == "/"
        assert page.status_code in (200, 303)

    def test_register_hash_runs_in_thread(self, app_client, monkeypatch, loop_gate) -> None:
        client, _ = app_client
        magic = auth.MagicLink(SECRET)
        client.get(f"/register?token={magic.make(TG_ID)}")
        csrf_token = _csrf(client)
        gate = loop_gate(auth.hash_password("secret-pass"))
        monkeypatch.setattr(auth, "hash_password", gate)
        register, page = gate.assert_loop_free(
            heavy=lambda: client.post(
                "/register",
                data={
                    "csrf_token": csrf_token,
                    "token": magic.make(TG_ID),
                    "email": "user@example.org",
                    "password": "secret-pass",
                    "password_confirm": "secret-pass",
                },
            ),
            light=lambda: client.get("/login"),
        )
        assert register.status_code == 303 and register.headers["location"] == "/"
        assert page.status_code in (200, 303)

    def test_profile_password_hash_runs_in_thread(self, app_client, monkeypatch, loop_gate) -> None:
        client, _ = app_client
        _register(client)
        csrf_token = _csrf(client)
        gate = loop_gate(auth.hash_password("brand-new-pass"))
        monkeypatch.setattr(auth, "hash_password", gate)
        profile, page = gate.assert_loop_free(
            heavy=lambda: client.post(
                "/profile",
                data={
                    "csrf_token": csrf_token,
                    "target_systolic": "130",
                    "target_diastolic": "80",
                    "timezone": "Europe/Minsk",
                    "new_password": "brand-new-pass",
                },
            ),
            light=lambda: client.get("/login"),
        )
        assert profile.status_code == 303
        assert gate.calls == 1
