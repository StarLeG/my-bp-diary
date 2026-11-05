"""Аутентификация личного кабинета (SPEC.md, раздел 16.2).

Компоненты безопасности:

- пароли — только Argon2id (`argon2-cffi`), в БД хранится лишь хеш;
- magic link — stateless-подпись `itsdangerous.URLSafeTimedSerializer`
  (срок действия 15 минут, одноразовость по `jti` в памяти процесса);
- сессии — HTTP-only cookie со случайным токеном, токен хранится в SQLite
  (`web_sessions`), время жизни 7 дней;
- CSRF — подписанный токен (double-submit: cookie + hidden field);
- rate limiting входа — не более 5 попыток в минуту на ключ (в памяти процесса).

Пароли и токены никогда не логируются.
"""
from __future__ import annotations

import re
import secrets
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from itsdangerous import BadSignature, URLSafeSerializer, URLSafeTimedSerializer
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import WebSession, WebUser

#: Срок действия magic link — 15 минут (SPEC.md, раздел 16.2).
MAGIC_LINK_MAX_AGE = 15 * 60

#: Время жизни сессионной cookie — 7 дней (SPEC.md, раздел 16.2).
SESSION_TTL = timedelta(days=7)

#: Ограничение попыток входа: не более 5 за минуту (SPEC.md, раздел 16.2).
LOGIN_RATE_LIMIT = 5
LOGIN_RATE_WINDOW = 60.0

#: Минимальная длина пароля учётной записи кабинета.
PASSWORD_MIN_LENGTH = 8

#: Максимальная длина пароля (защита от DoS-хеширования гигантского ввода).
PASSWORD_MAX_LENGTH = 128

#: Простая проверка e-mail (достаточно для формы кабинета).
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

#: Хешер Argon2id: соль генерируется автоматически (SPEC.md, раздел 4.6).
_password_hasher = PasswordHasher()


def utcnow() -> datetime:
    """Текущий момент в UTC (единая точка получения «сейчас»)."""
    return datetime.now(timezone.utc)


# ─────────────────────────── Пароли (Argon2id) ───────────────────────────


def hash_password(password: str) -> str:
    """Хеш пароля Argon2id; в открытом виде пароль нигде не хранится."""
    return _password_hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """Проверить пароль по хешу; любые повреждения хеша — False, не ошибка."""
    try:
        return _password_hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError, UnicodeEncodeError):
        return False


def valid_email(email: str) -> bool:
    """Достаточно ли корректен e-mail для формы кабинета."""
    return bool(_EMAIL_RE.match(email)) and len(email) <= 255


def valid_password(password: str) -> bool:
    """Длина пароля в допустимых пределах (SPEC.md, раздел 16.2 — без мелочей)."""
    return PASSWORD_MIN_LENGTH <= len(password) <= PASSWORD_MAX_LENGTH


# ─────────────────────────── Magic link ───────────────────────────


class MagicLink:
    """Подпись и проверка одноразовых ссылок входа (SPEC.md, раздел 16.2).

    Токен — stateless: подпись и время жизни, отдельная таблица не нужна.
    Одноразовость обеспечивается памятью процесса (`jti` в токене уже
    использованных ссылок отклоняются).
    """

    def __init__(self, secret_key: str) -> None:
        """Секрет подписи — `WEB_SECRET_KEY` из окружения."""
        self._serializer = URLSafeTimedSerializer(secret_key, salt="cabinet-login")
        #: Использованные `jti` с моментом истечения (для чистки памяти).
        self._used: dict[str, datetime] = {}

    def make(self, telegram_id: int) -> str:
        """Подписать ссылку входа для пользователя с данным `telegram_id`.

        В токен кладётся `telegram_id` (то, что знает бот), а не `users.id`:
        кабинет находит пользователя по `users.telegram_id`.
        """
        return self._serializer.dumps(
            {"uid": telegram_id, "jti": secrets.token_urlsafe(16)}
        )

    def verify(self, token: str, now: Optional[datetime] = None) -> Optional[int]:
        """Проверить токен без списания: подпись и срок 15 минут.

        Возвращает `telegram_id` или None (битый/просроченный/уже
        использованный токен). Страница регистрации показывает форму по
        токену, ещё не списанному: списывает только `consume`.
        """
        payload = self._load(token, now)
        if payload is None:
            return None
        user_id, jti, now = payload
        if jti in self._used:
            return None
        return user_id

    def consume(self, token: str, now: Optional[datetime] = None) -> Optional[int]:
        """Проверить токен и списать его (одноразовость — при реальном входе).

        Возвращает `telegram_id` или None. Повторное использование той же
        ссылки невозможно: `jti` навсегда (в пределах срока) помечается.
        """
        payload = self._load(token, now)
        if payload is None:
            return None
        user_id, jti, now = payload
        if jti in self._used:
            return None
        self._used[jti] = now + timedelta(seconds=MAGIC_LINK_MAX_AGE)
        return user_id

    def _load(
        self, token: str, now: Optional[datetime]
    ) -> Optional[tuple[int, str, datetime]]:
        """Распаковать токен: None, если подпись битая, срок истёк или полей нет."""
        now = now or utcnow()
        self._purge_used(now)
        try:
            payload = self._serializer.loads(token, max_age=MAGIC_LINK_MAX_AGE)
        except BadSignature:
            return None
        if not isinstance(payload, dict):
            return None
        user_id = payload.get("uid")
        jti = payload.get("jti")
        if not isinstance(user_id, int) or not isinstance(jti, str):
            return None
        return user_id, jti, now

    def _purge_used(self, now: datetime) -> None:
        """Выбросить истёкшие `jti`: память не растёт бесконечно."""
        expired = [jti for jti, until in self._used.items() if until <= now]
        for jti in expired:
            del self._used[jti]


# ─────────────────────────── CSRF ───────────────────────────


class Csrf:
    """Подписанный CSRF-токен (double-submit: cookie + hidden field)."""

    def __init__(self, secret_key: str) -> None:
        self._serializer = URLSafeSerializer(secret_key, salt="cabinet-csrf")

    def issue(self) -> str:
        """Выпустить новый токен (случайный nonce под общей подписью)."""
        return self._serializer.dumps({"nonce": secrets.token_urlsafe(16)})

    def validate(self, form_token: str, cookie_token: str) -> bool:
        """Токен из формы совпадает с cookie и имеет корректную подпись."""
        if not form_token or form_token != cookie_token:
            return False
        try:
            payload = self._serializer.loads(form_token)
        except BadSignature:
            return False
        return isinstance(payload, dict) and "nonce" in payload


# ─────────────────────────── Сессии (SQLite) ───────────────────────────


async def create_web_session(db: AsyncSession, web_user_id: int) -> str:
    """Создать сессию в SQLite, вернуть сессионный токен (для cookie).

    Заодно удаляются просроченные сессии — таблица не растёт бесконечно.
    """
    now = utcnow()
    await db.execute(delete(WebSession).where(WebSession.expires_at <= now))
    token = secrets.token_urlsafe(32)
    db.add(
        WebSession(
            token=token,
            web_user_id=web_user_id,
            created_at=now,
            expires_at=now + SESSION_TTL,
        )
    )
    await db.flush()
    return token


async def get_web_user_by_token(db: AsyncSession, token: str) -> Optional[WebUser]:
    """Найти учётную запись по сессионному токену; просроченный — как нет."""
    if not token:
        return None
    row = (
        await db.execute(
            select(WebUser)
            .join(WebSession, WebSession.web_user_id == WebUser.id)
            .where(WebSession.token == token, WebSession.expires_at > utcnow())
        )
    ).scalar_one_or_none()
    return row


async def delete_web_session(db: AsyncSession, token: str) -> None:
    """Удалить сессию (выход из кабинета)."""
    await db.execute(delete(WebSession).where(WebSession.token == token))
    await db.flush()


# ─────────────────────────── Rate limiting ───────────────────────────


class RateLimiter:
    """Ограничитель попыток входа: не более `max_attempts` за окно.

    Счётчики живут в памяти процесса (SPEC.md, раздел 16.2: 5 попыток/мин).
    """

    def __init__(
        self, max_attempts: int = LOGIN_RATE_LIMIT, window_seconds: float = LOGIN_RATE_WINDOW
    ) -> None:
        self._max_attempts = max_attempts
        self._window = timedelta(seconds=window_seconds)
        self._attempts: dict[str, list[datetime]] = defaultdict(list)

    def allow(self, key: str, now: Optional[datetime] = None) -> bool:
        """Можно ли делать попытку с этим ключом (проверка без учёта)."""
        now = now or utcnow()
        recent = [ts for ts in self._attempts[key] if now - ts < self._window]
        self._attempts[key] = recent
        return len(recent) < self._max_attempts

    def hit(self, key: str, now: Optional[datetime] = None) -> None:
        """Засчитать попытку (успех тоже засчитывается — окно скользит)."""
        now = now or utcnow()
        self._attempts[key].append(now)

    def reset(self, key: str) -> None:
        """Сбросить счётчик (например, после успешного входа)."""
        self._attempts.pop(key, None)
