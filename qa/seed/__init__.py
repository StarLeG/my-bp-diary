"""Сид нагрузочного тестового стенда (SPEC.md, §22, «Сид данных»).

Общее для всех сидов и нагрузочных тестов: защита от запуска не на стенде,
диапазоны Telegram ID нагрузочных пользователей, профили масштаба
(`full` / `smoke`) и чтение пароля нагрузочных аккаунтов.

Защита стенда: нагрузочный код работает только с БД, в имени файла которой
есть `test-stand` (`data/test-stand.sqlite3` или `test-stand.sqlite3` во
временной папке теста). Обхода нет: прод-БД (`bot.sqlite3`) и БД флешмоба
(`flashmob.sqlite3`) отклоняются всегда.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Union

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

SCALES: dict[str, Scale] = {FULL.name: FULL, SMOKE.name: SMOKE}


def load_password_from_env() -> str:
    """Пароль нагрузочных аккаунтов из `LOAD_PASSWORD` (пусто — не задан).

    В коде пароля нет (CLAUDE.md); сиды и Locust без него не запускаются,
    pytest генерирует случайный пароль на сессию (`qa/conftest.py`).
    """
    return os.environ.get("LOAD_PASSWORD", "")
