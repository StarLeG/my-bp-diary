"""Аутентификация кабинета врача (SPEC.md, раздел 21).

Компоненты безопасности:

- вход по magic link из бота — stateless-подпись `itsdangerous`
  (срок действия 15 минут, одноразовость по `jti` в памяти процесса);
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
from datetime import datetime, timedelta, timezone
from typing import Optional

from itsdangerous import BadSignature, URLSafeTimedSerializer

#: Соль подписи ссылки для врача — как `DOCTOR_SALT` в `bot/handlers/doctor.py`.
DOCTOR_LOGIN_SALT = "doctor-login"

#: Соль подписи сессионной cookie врача.
DOCTOR_SESSION_SALT = "doctor-session"

#: Срок действия ссылки для врача — 15 минут (SPEC.md, раздел 21).
DOCTOR_LINK_MAX_AGE = 15 * 60

#: Время жизни сессии врача — 7 дней (как сессия кабинета).
DOCTOR_SESSION_MAX_AGE = 7 * 24 * 3600


def utcnow() -> datetime:
    """Текущий момент в UTC (единая точка получения «сейчас»)."""
    return datetime.now(timezone.utc)


class DoctorMagicLink:
    """Подпись и проверка одноразовых ссылок для врача (SPEC.md, раздел 21).

    Токен — stateless: подпись и время жизни, отдельная таблица не нужна.
    Payload — `did` (врач), `pid` (пациент, `users.id`) и `jti` для
    одноразовости; формат совпадает с `bot/handlers/doctor.py::make_doctor_link`
    (закреплено контрактным тестом `tests/test_web_doctor.py`).
    """

    def __init__(self, secret_key: str) -> None:
        """Секрет подписи — `WEB_SECRET_KEY` из окружения."""
        self._serializer = URLSafeTimedSerializer(secret_key, salt=DOCTOR_LOGIN_SALT)
        #: Использованные `jti` с моментом истечения (для чистки памяти).
        self._used: dict[str, datetime] = {}

    def make(self, doctor_id: int, patient_user_id: int) -> str:
        """Подписать ссылку для врача: доступ к данным конкретного пациента."""
        return self._serializer.dumps(
            {"did": doctor_id, "pid": patient_user_id, "jti": secrets.token_urlsafe(16)}
        )

    def verify(self, token: str, now: Optional[datetime] = None) -> Optional[tuple[int, int]]:
        """Проверить токен без списания: подпись и срок 15 минут.

        Возвращает `(doctor_id, patient_user_id)` или None (битый/
        просроченный/уже использованный токен).
        """
        payload = self._load(token, now)
        if payload is None:
            return None
        doctor_id, patient_user_id, jti, _ = payload
        if jti in self._used:
            return None
        return doctor_id, patient_user_id

    def consume(
        self, token: str, now: Optional[datetime] = None
    ) -> Optional[tuple[int, int]]:
        """Проверить токен и списать его (одноразовость — при реальном входе).

        Возвращает `(doctor_id, patient_user_id)` или None. Повторное
        использование той же ссылки невозможно: `jti` помечается навсегда
        (в пределах срока действия токена).
        """
        payload = self._load(token, now)
        if payload is None:
            return None
        doctor_id, patient_user_id, jti, now = payload
        if jti in self._used:
            return None
        self._used[jti] = now + timedelta(seconds=DOCTOR_LINK_MAX_AGE)
        return doctor_id, patient_user_id

    def _load(
        self, token: str, now: Optional[datetime]
    ) -> Optional[tuple[int, int, str, datetime]]:
        """Распаковать токен: None, если подпись битая, срок истёк или полей нет."""
        now = now or utcnow()
        self._purge_used(now)
        try:
            payload = self._serializer.loads(token, max_age=DOCTOR_LINK_MAX_AGE)
        except BadSignature:
            return None
        if not isinstance(payload, dict):
            return None
        doctor_id = payload.get("did")
        patient_user_id = payload.get("pid")
        jti = payload.get("jti")
        if not isinstance(doctor_id, int) or not isinstance(patient_user_id, int):
            return None
        if not isinstance(jti, str):
            return None
        return doctor_id, patient_user_id, jti, now

    def _purge_used(self, now: datetime) -> None:
        """Выбросить истёкшие `jti`: память не растёт бесконечно."""
        expired = [jti for jti, until in self._used.items() if until <= now]
        for jti in expired:
            del self._used[jti]


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
