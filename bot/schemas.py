"""Pydantic-схемы и парсеры ввода (SPEC.md, раздел 9).

Сообщения об ошибках — короткие и человеческие; те же правила продублированы
CHECK-ограничениями БД (см. `bot/models.py`).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Literal, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, ValidationError, field_validator

from bot import texts

#: Допустимые значения самочувствия (SPEC.md, раздел 4.2).
FeelingLiteral = Literal["normal", "headache", "weakness", "other"]

#: Время в формате ЧЧ:ММ (SPEC.md, раздел 9); час — 1–2 цифры: «2:30» = «02:30».
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

#: Дата архива `ДД.ММ[.ГГГГ]` (SPEC.md, раздел 9, §6.10); день и месяц — 1–2 цифры.
_DATE_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?$")

#: Строка быстрого ввода архива `ДД.ММ[.ГГГГ] ЧЧ:ММ САД/ДАД [пульс]` (§6.10);
#: день, месяц и час — 1–2 цифры: «2.3 2:30 120/80» = «02.03 02:30 120/80».
_QUICK_RE = re.compile(
    r"^(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?\s+([01]?\d|2[0-3]):([0-5]\d)"
    r"\s+(\d{1,3})/(\d{1,3})(?:\s+(\d{1,3}))?$"
)

TIME_FORMAT_HINT = "Нужно время в формате ЧЧ:ММ (например, 08:30)"


@dataclass(frozen=True)
class ArchiveQuick:
    """Разобранная строка быстрого ввода архива (SPEC.md, §6.10)."""

    moment: datetime  # осведомлённый момент, в таймзоне пользователя
    systolic: int
    diastolic: int
    pulse: Optional[int]


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


def validate_pair(systolic: int, diastolic: int) -> None:
    """Проверить пару давлений: систолическое строго выше диастолического (§9).

    Минимальная разница (пульсовое давление) не ограничивается.

    Исключения:
        ValueError: диастолическое не ниже систолического.
    """
    if diastolic >= systolic:
        raise ValueError(texts.DIA_NOT_LOWER)


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


def parse_archive_date(raw: str, today: date) -> date:
    """Дата архива `ДД.ММ[.ГГГГ]` (год — текущий), не позже сегодняшней (§9).

    Исключения:
        ValueError: не дата — «Нужна дата в формате…»; будущее —
            «Время измерения не может быть в будущем».
    """
    match = _DATE_RE.match(str(raw).strip())
    if not match:
        raise ValueError(texts.ARCHIVE_DATE_FORMAT_HINT)
    day_s, month_s, year_s = match.groups()
    year = int(year_s) if year_s else today.year
    try:
        day = date(year, int(month_s), int(day_s))
    except ValueError as exc:
        raise ValueError(texts.ARCHIVE_DATE_FORMAT_HINT) from exc
    if day > today:
        raise ValueError(texts.ARCHIVE_FUTURE_ERROR)
    return day


def combine_moment(
    day: date, moment_time: time, tz_name: str, *, now: Optional[datetime] = None
) -> datetime:
    """Момент измерения (дата + время в таймзоне пользователя), ≤ текущего (§9).

    Исключения:
        ValueError: будущее — «Время измерения не может быть в будущем».
    """
    tz = ZoneInfo(tz_name)
    moment = datetime(
        day.year, day.month, day.day, moment_time.hour, moment_time.minute, tzinfo=tz
    )
    current = now if now is not None else datetime.now(tz)
    if current.tzinfo is None:
        current = current.replace(tzinfo=tz)
    if moment > current:
        raise ValueError(texts.ARCHIVE_FUTURE_ERROR)
    return moment


def parse_archive_quick(
    raw: str, tz_name: str, *, now: Optional[datetime] = None
) -> ArchiveQuick:
    """Строка быстрого ввода архива: `ДД.ММ[.ГГГГ] ЧЧ:ММ САД/ДАД [пульс]` (§6.10).

    Порядок проверок — как в пошаговом вводе: сначала момент (дата, время,
    запрет будущего), затем значения (диапазоны §9 и пара давлений).

    Исключения:
        ValueError: не распознано — «Не понял формат…»; иначе — сообщения
            валидации полей (§9).
    """
    match = _QUICK_RE.match(str(raw).strip())
    if not match:
        raise ValueError(texts.ARCHIVE_QUICK_FORMAT_HINT)
    day_s, month_s, year_s, hour_s, minute_s, sys_s, dia_s, pulse_s = match.groups()
    tz = ZoneInfo(tz_name)
    current = now if now is not None else datetime.now(tz)
    if current.tzinfo is None:
        current = current.replace(tzinfo=tz)
    else:
        current = current.astimezone(tz)
    year = int(year_s) if year_s else current.date().year
    try:
        day = date(year, int(month_s), int(day_s))
        moment_time = time(int(hour_s), int(minute_s))
    except ValueError as exc:
        raise ValueError(texts.ARCHIVE_QUICK_FORMAT_HINT) from exc
    moment = combine_moment(day, moment_time, tz_name, now=current)
    systolic = validate_int(sys_s, 70, 260, "систолическое")
    diastolic = validate_int(dia_s, 40, 160, "диастолическое")
    validate_pair(systolic, diastolic)
    pulse = validate_int(pulse_s, 30, 220, "пульс") if pulse_s else None
    return ArchiveQuick(
        moment=moment, systolic=systolic, diastolic=diastolic, pulse=pulse
    )


# ─────────────────────────── Данные пациента (§6.12) ───────────────────────────

#: Длина поля ФИО (§4.1).
PERSON_NAME_MAX = 50

#: Буквы (любой алфавит), пробел, дефис, апостроф; начинается с буквы.
_PERSON_NAME_RE = re.compile(r"^[^\W\d_]+(?:[ '’-][^\W\d_]+)*$")

#: Минимальный возраст пациента (ст. 5 п. 9 Закона № 99-З, §6.12).
MIN_AGE = 16

#: Самая ранняя допустимая дата рождения.
MIN_BIRTH_DATE = date(1900, 1, 1)


class TooYoungError(ValueError):
    """Пациенту меньше 16 лет — данные не сохраняются (§6.12, П5)."""


def validate_person_name(raw: str) -> str:
    """Фамилия / имя / отчество: 1–50 символов, буквы, пробел, дефис, апостроф.

    Лишние пробелы убираются, регистр сохраняется. `ValueError` — не подходит.
    """
    value = " ".join((raw or "").split())
    value = re.sub(r"\s*([-'’])\s*", r"\1", value)
    if not 1 <= len(value) <= PERSON_NAME_MAX or not _PERSON_NAME_RE.match(value):
        raise ValueError("ФИО: от 1 до 50 букв, пробел, дефис, апостроф")
    return value


def age_on(birth: date, today: date) -> int:
    """Полных лет на дату `today`."""
    return today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))


def parse_birth_date(raw: str, today: date) -> date:
    """`ДД.ММ.ГГГГ` → дата рождения (§6.12).

    `ValueError` — не дата, раньше 1900 года или в будущем;
    `TooYoungError` — младше 16 лет на `today` (сегодня пациента).
    """
    match = re.fullmatch(r"\s*(\d{1,2})\.(\d{1,2})\.(\d{4})\s*", raw or "")
    if match is None:
        raise ValueError("нужна дата ДД.ММ.ГГГГ")
    try:
        birth = date(int(match.group(3)), int(match.group(2)), int(match.group(1)))
    except ValueError as exc:
        raise ValueError("нет такой даты") from exc
    return validate_birth_date(birth, today)


def validate_birth_date(birth: date, today: date) -> date:
    """Дата рождения из формы ЛК (`type=date`): те же правила, что в боте."""
    if birth < MIN_BIRTH_DATE or birth > today:
        raise ValueError("дата рождения вне диапазона")
    if age_on(birth, today) < MIN_AGE:
        raise TooYoungError("младше 16 лет")
    return birth
