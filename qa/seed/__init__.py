"""Сид нагрузочного тестового стенда (SPEC.md, §22, «Сид данных»).

Общее для всех сидов и нагрузочных тестов: защита от запуска не на стенде,
диапазоны Telegram ID нагрузочных пользователей, профили масштаба
(`full` / `smoke` / `three-years`) и чтение пароля нагрузочных аккаунтов.

Защита стенда: нагрузочный код работает только с БД, в имени файла которой
есть `test-stand` (`data/test-stand.sqlite3` или `test-stand.sqlite3` во
временной папке теста). Обхода нет: прод-БД (`bot.sqlite3`) и БД флешмоба
(`flashmob.sqlite3`) отклоняются всегда.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Union
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from bot.models import User

#: Метка, которая обязана быть в имени файла БД стенда (SPEC §22).
STAND_MARK = "test-stand"

#: БД стенда по умолчанию (переменная `LOAD_DB`, SPEC §3).
DEFAULT_STAND_DB = "data/test-stand.sqlite3"

#: Telegram ID нагрузочных пациентов: пациент n (с 1) — `PATIENT_TG_BASE + n`.
PATIENT_TG_BASE = 990_000

#: Весь блок ID пациентов стенда — его и только его чистит `--clean`.
PATIENT_TG_RANGE = range(PATIENT_TG_BASE + 1, PATIENT_TG_BASE + 1000)

#: Домен e-mail нагрузочных пациентов и врачей (`load{n}@…`, `doctor{n}@…`).
LOAD_EMAIL_DOMAIN = "load.test"

PathLike = Union[str, os.PathLike]


class StandGuardError(RuntimeError):
    """Попытка запустить нагрузочный код не на БД тестового стенда."""


def ensure_stand_db(path: PathLike) -> Path:
    """Проверить, что путь — БД тестового стенда, и вернуть его как `Path`.

    Исключения:
        StandGuardError: в имени файла нет `test-stand` (например, прод-БД
            `bot.sqlite3` или БД флешмоба `flashmob.sqlite3`).
    """
    db_path = Path(path)
    if STAND_MARK not in db_path.name:
        raise StandGuardError(
            f"Отказ: {db_path} — не БД тестового стенда. Нагрузочный код "
            f"работает только с файлом, в имени которого есть «{STAND_MARK}» "
            f"(по умолчанию {DEFAULT_STAND_DB}, SPEC.md §22)."
        )
    return db_path


def stand_db_path() -> Path:
    """БД стенда из `LOAD_DB` (по умолчанию `data/test-stand.sqlite3`), с проверкой."""
    return ensure_stand_db(os.environ.get("LOAD_DB", "").strip() or DEFAULT_STAND_DB)


def patient_tg_id(n: int) -> int:
    """Telegram ID нагрузочного пациента номер `n` (с 1)."""
    return PATIENT_TG_BASE + n


def patient_email(n: int) -> str:
    """E-mail кабинета нагрузочного пациента номер `n`."""
    return f"load{n}@{LOAD_EMAIL_DOMAIN}"


def doctor_email(n: int) -> str:
    """E-mail нагрузочного врача номер `n`."""
    return f"doctor{n}@{LOAD_EMAIL_DOMAIN}"


@dataclass(frozen=True)
class Scale:
    """Объёмы сида одного профиля (SPEC §22)."""

    name: str
    patients: int
    measurements_per_patient: int
    medications_per_patient: int
    logs_per_patient: int
    slot_reminders_per_patient: int
    remeasure_per_patient: int
    doctors: int
    days: int = 30
    appointments_per_patient: int = 0

    @property
    def measurements(self) -> int:
        """Всего измерений профиля."""
        return self.patients * self.measurements_per_patient

    @property
    def reminders(self) -> int:
        """Всего активных напоминаний профиля."""
        return self.patients * (self.slot_reminders_per_patient + self.remeasure_per_patient)


#: Полный профиль (`LOAD_TEST=1`, сид стенда): 20 пациентов, 5000 измерений,
#: 100 лекарств, 500 логов, 60 + 40 напоминаний, 20 врачей.
FULL = Scale(
    name="full",
    patients=20,
    measurements_per_patient=250,
    medications_per_patient=5,
    logs_per_patient=25,
    slot_reminders_per_patient=3,
    remeasure_per_patient=2,
    doctors=20,
)

#: Дымовой профиль (без флага): тот же сценарий в малом объёме, секунды.
SMOKE = Scale(
    name="smoke",
    patients=3,
    measurements_per_patient=25,
    medications_per_patient=2,
    logs_per_patient=5,
    slot_reminders_per_patient=1,
    remeasure_per_patient=1,
    doctors=3,
)

#: Профиль полного объёма авто-приёмки (`--scale three-years`, SPEC §22,
#: «Полный объём»; ответы — `doc/LOAD-CRITERIA-QUESTIONS.md`, В1/В2): 20
#: пациентов × 2500 измерений ≈ 50 000 за 3 года (~2–3 в день на пациента),
#: лекарства и логи приёма растут вместе с окном истории, визиты — 3 года
#: назад … сегодня + 2 года (§6.13) с заключениями к прошедшим. Не
#: масштабируются (нереалистичного роста нет): `reminders` (активные — 100,
#: как FULL), связи врач–пациент (20), `web_sessions` и `invite_log` не
#: создаются вовсе.
THREE_YEARS = Scale(
    name="three-years",
    patients=20,
    measurements_per_patient=2500,
    medications_per_patient=8,
    logs_per_patient=1095,
    slot_reminders_per_patient=3,
    remeasure_per_patient=2,
    doctors=20,
    days=1095,
    appointments_per_patient=30,
)

SCALES: dict[str, Scale] = {
    FULL.name: FULL,
    SMOKE.name: SMOKE,
    THREE_YEARS.name: THREE_YEARS,
}


#: Секунда-метка моментов сида: форма кабинета пишет время с точностью до
#: минуты (секунды = 0), поэтому сидированные записи стенда отличимы от
#: добавленных нагрузкой (E2: «сидированные 5000 на месте»).
SEED_SECOND = 17

#: Часовые пояса пациентов по кругу: моменты сида — в таймзоне пользователя.
PATIENT_TIMEZONES = ("Europe/Minsk", "Europe/Moscow", "Asia/Yekaterinburg")


def patient_timezone(n: int) -> str:
    """Часовой пояс нагрузочного пациента номер `n`."""
    return PATIENT_TIMEZONES[(n - 1) % len(PATIENT_TIMEZONES)]


def anchor_day(created_at: datetime, tz_name: str) -> date:
    """Последний день окна сида (не включается): локальная дата создания пациента.

    Расписание сида привязано к `users.created_at`, а не к «сегодня»: повторный
    запуск в другой день даёт те же моменты и ничего не дублирует. Окно —
    `[anchor − days, anchor)`, поэтому моментов в будущем не бывает.
    """
    return created_at.astimezone(ZoneInfo(tz_name)).date()


def seed_moment(day: date, hour: int, minute: int, tz_name: str) -> datetime:
    """Момент сида: локальные дата и время пользователя с секундой-меткой, в UTC."""
    local = datetime(
        day.year, day.month, day.day, hour, minute, SEED_SECOND, tzinfo=ZoneInfo(tz_name)
    )
    return local.astimezone(timezone.utc)


async def load_patients(session: "AsyncSession", scale: Scale) -> list[tuple[int, "User"]]:
    """Нагрузочные пациенты профиля, которые уже есть в БД: пары (n, User)."""
    from sqlalchemy import select

    from bot.models import User

    ids = {patient_tg_id(n): n for n in range(1, scale.patients + 1)}
    users = (
        await session.execute(select(User).where(User.telegram_id.in_(ids)))
    ).scalars()
    return sorted(((ids[user.telegram_id], user) for user in users), key=lambda pair: pair[0])


def load_password_from_env() -> str:
    """Пароль нагрузочных аккаунтов из `LOAD_PASSWORD` (пусто — не задан).

    В коде пароля нет (CLAUDE.md); сиды и Locust без него не запускаются,
    pytest генерирует случайный пароль на сессию (`qa/conftest.py`).
    """
    return os.environ.get("LOAD_PASSWORD", "")
