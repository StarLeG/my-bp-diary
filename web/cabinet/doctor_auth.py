"""Аутентификация кабинета врача (SPEC.md, раздел 21).

Компоненты безопасности:

- ссылка доступа к пациенту — подпись `itsdangerous` (72 часа), одноразовость
  по `jti` в таблице `used_tokens` (переживает перезапуск); списывается не
  при открытии, а по кнопке на странице подтверждения — почтовые сканеры,
  открывающие ссылки из писем, её не сжигают;
- сессия врача — подписанная cookie (без таблицы в БД), 7 дней;
- вход e-mail + пароль — для врачей с установленным паролем: Argon2id
  (`argon2-cffi`, переиспользует `auth.hash_password` / `auth.verify_password`)
  и rate limiting как у входа в кабинет.

Сессия stateless: таблицы `doctor_sessions` в схеме §4 нет. Отзыв доступа
пациентом не отключает cookie, но и не даёт доступа: каждый запрос данных
пациента проверяет `doctor_patients.status='active'` (CLAUDE.md, B5).
Пароли и токены никогда не логируются.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import UsedToken

#: Соль подписи ссылки доступа — как `DOCTOR_SALT` в `bot/handlers/doctor.py`.
#: Сменена с «doctor-login» в 0.4.12: ссылки до 0.4.11 (15 минут, одноразовость
#: в памяти процесса) при сроке 72 часа снова стали бы рабочими.
DOCTOR_ACCESS_SALT = "doctor-access"
#: Прежняя соль ссылок врача — только для теста, что старые ссылки отклоняются.
LEGACY_DOCTOR_SALT = "doctor-login"

#: Соль ссылки входа врача из бота (§21): своя, не совпадает со ссылкой доступа.
DOCTOR_SIGNIN_SALT = "doctor-signin"

#: Срок ссылки входа врача из меню врача в боте — 15 минут (§21).
DOCTOR_SIGNIN_MAX_AGE = 15 * 60

#: Соль подписи сессионной cookie врача.
DOCTOR_SESSION_SALT = "doctor-session"

#: Срок ссылки доступа врача к пациенту — 72 часа (SPEC.md, §21): письмо и
#: пересылка — один и тот же токен.
DOCTOR_ACCESS_MAX_AGE = 72 * 3600

#: Время жизни сессии врача — 7 дней (как сессия кабинета).
DOCTOR_SESSION_MAX_AGE = 7 * 24 * 3600


def utcnow() -> datetime:
    """Текущий момент в UTC (единая точка получения «сейчас»)."""
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class AccessToken:
    """Распакованная ссылка доступа: врач, пациент, `jti` и конец срока."""

    doctor_id: int
    patient_user_id: int
    jti: str
    expires_at: datetime


class DoctorMagicLink:
    """Подпись и проверка ссылок доступа врача к пациенту (SPEC.md, §21).

    Подпись и срок (72 часа) — stateless (`itsdangerous`); одноразовость —
    по `jti` в таблице `used_tokens` (§4.14): списание переживает перезапуск
    кабинета. Payload — `did` (врач), `pid` (пациент, `users.id`) и `jti`;
    формат совпадает с `bot/handlers/doctor.py::make_doctor_link`
    (закреплено контрактным тестом `tests/test_web_doctor.py`).
    """

    def __init__(self, secret_key: str) -> None:
        """Секрет подписи — `WEB_SECRET_KEY` из окружения."""
        self._serializer = URLSafeTimedSerializer(secret_key, salt=DOCTOR_ACCESS_SALT)

    def make(self, doctor_id: int, patient_user_id: int) -> str:
        """Подписать ссылку доступа врача к данным конкретного пациента."""
        return self._serializer.dumps(
            {"did": doctor_id, "pid": patient_user_id, "jti": secrets.token_urlsafe(16)}
        )

    def load(self, token: str, now: Optional[datetime] = None) -> Optional[AccessToken]:
        """Проверить подпись и срок без списания; None — битый или просроченный токен.

        Списана ли ссылка, решает БД (`is_used` / `consume`).
        """
        now = now or utcnow()
        try:
            payload, signed_at = self._serializer.loads(
                token, max_age=DOCTOR_ACCESS_MAX_AGE, return_timestamp=True
            )
        except BadSignature:
            return None
        if not isinstance(payload, dict):
            return None
        doctor_id = payload.get("did")
        patient_user_id = payload.get("pid")
        jti = payload.get("jti")
        if not isinstance(doctor_id, int) or not isinstance(patient_user_id, int):
            return None
        if not isinstance(jti, str) or not jti:
            return None
        expires_at = signed_at.astimezone(timezone.utc) + timedelta(
            seconds=DOCTOR_ACCESS_MAX_AGE
        )
        return AccessToken(doctor_id, patient_user_id, jti, expires_at)


@dataclass(frozen=True)
class SignInToken:
    """Распакованная ссылка входа: врач, `jti`, конец срока и пациент (или None)."""

    doctor_id: int
    jti: str
    expires_at: datetime
    patient_user_id: Optional[int] = None


class DoctorSignInLink:
    """Ссылка входа врача из меню врача в боте (SPEC.md, §21).

    15 минут, одноразовая (`used_tokens`, общий с ссылкой доступа). Payload —
    `did` (врач), `jti` и необязательный `pid`: «Открыть в кабинете» ведёт
    в карточку пациента. Связей не меняет: доступ к пациенту — только при
    `status='active'`. Формат совпадает с `bot/handlers/doctor_menu.py`.
    """

    def __init__(self, secret_key: str) -> None:
        """Секрет подписи — `WEB_SECRET_KEY` из окружения."""
        self._serializer = URLSafeTimedSerializer(secret_key, salt=DOCTOR_SIGNIN_SALT)

    def make(self, doctor_id: int, patient_user_id: Optional[int] = None) -> str:
        """Подписать ссылку входа врача (с пациентом — сразу в его карточку)."""
        payload: dict = {"did": doctor_id, "jti": secrets.token_urlsafe(16)}
        if patient_user_id is not None:
            payload["pid"] = patient_user_id
        return self._serializer.dumps(payload)

    def load(self, token: str) -> Optional[SignInToken]:
        """Проверить подпись и срок без списания; None — битый или просроченный."""
        try:
            payload, signed_at = self._serializer.loads(
                token, max_age=DOCTOR_SIGNIN_MAX_AGE, return_timestamp=True
            )
        except BadSignature:
            return None
        if not isinstance(payload, dict):
            return None
        doctor_id = payload.get("did")
        jti = payload.get("jti")
        patient_user_id = payload.get("pid")
        if not isinstance(doctor_id, int) or not isinstance(jti, str) or not jti:
            return None
        if patient_user_id is not None and not isinstance(patient_user_id, int):
            return None
        expires_at = signed_at.astimezone(timezone.utc) + timedelta(
            seconds=DOCTOR_SIGNIN_MAX_AGE
        )
        return SignInToken(doctor_id, jti, expires_at, patient_user_id)


async def is_used(session: AsyncSession, jti: str) -> bool:
    """Списана ли ссылка (`used_tokens`, §4.14)."""
    row = await session.execute(select(UsedToken.jti).where(UsedToken.jti == jti))
    return row.first() is not None


async def consume(session: AsyncSession, token: "AccessToken | SignInToken") -> bool:
    """Списать ссылку атомарно: True — списана сейчас, False — уже была списана.

    `INSERT OR IGNORE` по первичному ключу `jti`: из двух одновременных
    открытий одной ссылки выигрывает ровно одно (§7.2).
    """
    result = await session.execute(
        sqlite_insert(UsedToken)
        .values(jti=token.jti, expires_at=token.expires_at)
        .on_conflict_do_nothing(index_elements=["jti"])
    )
    return result.rowcount == 1


async def purge_used(session: AsyncSession, now: Optional[datetime] = None) -> int:
    """Удалить просроченные списания (при старте кабинета); число удалённых строк."""
    result = await session.execute(
        delete(UsedToken).where(UsedToken.expires_at < (now or utcnow()))
    )
    return result.rowcount or 0


class DoctorSession:
    """Stateless-сессия врача: подписанная cookie (SPEC.md, раздел 21).

    В отличие от сессии кабинета (`web_sessions`) токен в SQLite не
    хранится: таблицы `doctor_sessions` в схеме нет, а доступ к данным
    защищён проверкой `status='active'` на каждый запрос.
    """

    def __init__(self, secret_key: str) -> None:
        """Секрет подписи — `WEB_SECRET_KEY` из окружения."""
        self._serializer = URLSafeTimedSerializer(secret_key, salt=DOCTOR_SESSION_SALT)

    def make(self, doctor_id: int) -> str:
        """Подписать сессионную cookie для врача."""
        return self._serializer.dumps({"did": doctor_id})

    def verify(self, token: str) -> Optional[int]:
        """Вернуть `doctor_id` из cookie или None (нет/битая/просроченная)."""
        if not token:
            return None
        try:
            payload = self._serializer.loads(token, max_age=DOCTOR_SESSION_MAX_AGE)
        except BadSignature:
            return None
        if not isinstance(payload, dict):
            return None
        doctor_id = payload.get("did")
        if not isinstance(doctor_id, int):
            return None
        return doctor_id
