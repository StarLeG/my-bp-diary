"""Pydantic-схемы и парсеры ввода (SPEC.md, раздел 9).

Сообщения об ошибках — короткие и человеческие; те же правила продублированы
CHECK-ограничениями БД (см. `bot/models.py`).
"""
from __future__ import annotations

import re
from datetime import time
from typing import Literal, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, ValidationError, field_validator

#: Допустимые значения самочувствия (SPEC.md, раздел 4.2).
FeelingLiteral = Literal["normal", "headache", "weakness", "other"]

#: Время строго в формате ЧЧ:ММ (SPEC.md, раздел 9).
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

TIME_FORMAT_HINT = "Нужно время в формате ЧЧ:ММ (например, 08:30)"


class MeasurementInput(BaseModel):
    """Числа одного измерения давления."""

    systolic: int = Field(ge=70, le=260, description="систолическое")
    diastolic: int = Field(ge=40, le=160, description="диастолическое")
    pulse: Optional[int] = Field(default=None, ge=30, le=220, description="пульс; None = пропущено")


class NoteInput(BaseModel):
    """Заметка к измерению (1…500 символов)."""

    note: str = Field(min_length=1, max_length=500)

    @field_validator("note")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Заметка не может быть пустой")
        return value


class FeelingInput(BaseModel):
    """Самочувствие; при «other» — короткий текст в `other_text` (уйдёт в note)."""

    feeling: FeelingLiteral
    other_text: Optional[str] = Field(default=None, max_length=500)

    @field_validator("other_text")
    @classmethod
    def _strip(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = value.strip()
        return value or None


class MedicationNameInput(BaseModel):
    """Название лекарства (1…100 символов)."""

    name: str = Field(min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Название не может быть пустым")
        return value


class DosageInput(BaseModel):
    """Дозировка лекарства строкой (1…50 символов), например «10 мг»."""

    dosage: str = Field(min_length=1, max_length=50)

    @field_validator("dosage")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Дозировка не может быть пустой")
        return value


class TimeInput(BaseModel):
    """Время слота HH:MM."""

    value: time

    @field_validator("value", mode="before")
    @classmethod
    def _parse(cls, value: object) -> time:
        if isinstance(value, time):
            return value
        match = _TIME_RE.match(str(value).strip())
        if not match:
            raise ValueError(TIME_FORMAT_HINT)
        return time(int(match.group(1)), int(match.group(2)))


class TimezoneInput(BaseModel):
    """Часовой пояс IANA с проверкой через zoneinfo."""

    value: str

    @field_validator("value")
    @classmethod
    def _validate(cls, value: str) -> str:
        value = value.strip()
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
            raise ValueError(
                "Не знаю такой часовой пояс. Нужно IANA-имя, например Europe/Minsk"
            ) from exc
        return value


def validate_int(raw: str, lo: int, hi: int, label: str = "значение") -> int:
    """Разобрать свободный ввод числа с проверкой диапазона.

    Исключения:
        ValueError: не число или вне диапазона — с человеческим сообщением.
    """
    try:
        number = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Нужно целое число от {lo} до {hi}") from exc
    if not lo <= number <= hi:
        raise ValueError(f"Нужно {label} от {lo} до {hi}, а получилось {number}")
    return number


def _clean_error(exc: ValidationError) -> str:
    """Человеческий текст первой ошибки валидации (без форматирования Pydantic)."""
    first = exc.errors()[0]
    error = (first.get("ctx") or {}).get("error", first["msg"])
    return str(error)


def validate_time(raw: str) -> time:
    """Разобрать свободный ввод времени (строго ЧЧ:ММ).

    Исключения:
        ValueError: не время — с человечным сообщением.
    """
    try:
        return TimeInput(value=raw).value
    except ValidationError as exc:
        raise ValueError(_clean_error(exc)) from exc


def validate_timezone(raw: str) -> str:
    """Разобрать свободный ввод часового пояса (IANA).

    Исключения:
        ValueError: неизвестный пояс — с человечным сообщением.
    """
    try:
        return TimezoneInput(value=raw).value
    except ValidationError as exc:
        raise ValueError(_clean_error(exc)) from exc


def validate_note(raw: str) -> str:
    """Разобрать заметку (1…500 символов, §6.3).

    Исключения:
        ValueError: пусто или длиннее 500 — с человечным сообщением.
    """
    try:
        return NoteInput(note=raw).note
    except ValidationError as exc:
        raise ValueError(_clean_error(exc)) from exc


def validate_medication_name(raw: str) -> str:
    """Разобрать название лекарства (1…100 символов, §6.6).

    Исключения:
        ValueError: пусто или длиннее 100 — с человечным сообщением.
    """
    try:
        return MedicationNameInput(name=raw).name
    except ValidationError as exc:
        raise ValueError(_clean_error(exc)) from exc


def validate_dosage(raw: str) -> str:
    """Разобрать дозировку (строка 1…50 символов, §6.6).

    Исключения:
        ValueError: пусто или длиннее 50 — с человечным сообщением.
    """
    try:
        return DosageInput(dosage=raw).dosage
    except ValidationError as exc:
        raise ValueError(_clean_error(exc)) from exc
