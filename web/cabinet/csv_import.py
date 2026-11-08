"""Парсинг и валидация CSV-импорта архива (SPEC.md, раздел 16.2).

Формат — как у CSV-экспорта (§10): UTF-8 (BOM допустим), разделитель `;`,
первая строка — заголовок `timestamp_local;systolic;diastolic;pulse;`
`medication_taken;feeling;note`; обязательны `timestamp_local`, `systolic`,
`diastolic`, остальные колонки опциональны. `timestamp_local` — «ДД.ММ.ГГГГ
ЧЧ:ММ» в таймзоне пользователя; `medication_taken` — «да» / «нет» / пусто;
`feeling` — по-русски, как в экспорте («норма», «головная боль», …).

Разбор (`parse`) — синхронная чистая функция без БД, вызывается через
`asyncio.to_thread` (§7.2 п.7). Вставка (`import_rows`) — одной транзакцией,
дубли за минуту (существующие записи и внутри файла) пропускаются: двойная
отправка формы не создаёт дублей (§7.2 п.4). Отчёт — «Импортировано: N ·
Пропущено дублей: M · Ошибок: K»; ошибочные строки перечисляются до 10.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from typing import Mapping, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import insert, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot import schemas, texts
from bot.models import Measurement, User
from bot.services import csv_export
from bot.services.measurements import (
    _current_save_lock,
    duplicate_in_minute,
    minute_window,
)

#: Лимит строк данных на файл (§16.2); больше — «Слишком много строк…».
MAX_IMPORT_ROWS = 5000

#: Лимит размера файла в байтах: 5000 строк с заметкой в 500 символов
#: кириллицей (~1,1 КБ на строку) с запасом. Больше — тот же ответ, что и при
#: превышении лимита строк; файл не читается в память целиком.
MAX_IMPORT_BYTES = 8 * 1024 * 1024

#: Сколько ошибочных строк перечисляется в отчёте (§16.2).
MAX_ERRORS_SHOWN = 10

#: Тексты ошибок всего файла (показываются вместо отчёта).
FILE_NOT_UTF8 = "Файл должен быть в кодировке UTF-8"
FILE_NO_HEADER = "В файле нет строки заголовка"
FILE_TOO_MANY_ROWS = "Слишком много строк, разделите файл"

#: Обязательные колонки заголовка (§16.2).
_REQUIRED_COLUMNS = ("timestamp_local", "systolic", "diastolic")

#: `timestamp_local` — «ДД.ММ.ГГГГ ЧЧ:ММ» (§10); день, месяц и час — 1–2 цифры:
#: «1.3.2025 8:30» = «01.03.2025 08:30» (минимум трения, §2).
_TIMESTAMP_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})\s+([01]?\d|2[0-3]):([0-5]\d)$")

#: Короткие причины ошибок строк отчёта («Строка 5: время в будущем»).
_ERR_TIMESTAMP = "дата и время не распознаны"
_ERR_FUTURE = "время в будущем"
_ERR_MED_FLAG = "флаг лекарства — «да», «нет» или пусто"
_ERR_FEELING = "самочувствие не распознано"
_ERR_NOTE = "заметка длиннее 500 символов"

#: Тексты колонки `medication_taken` (зеркало `_MED_FLAG_TEXT` экспорта).
_MED_FLAG_VALUES = {"да": True, "нет": False, "": None}

#: Тексты колонки `feeling` → литералы модели (зеркало `_FEELING_TEXT` экспорта).
_FEELING_VALUES = {label: key for key, label in texts.FEELING_LABELS.items()}


@dataclass(frozen=True)
class ImportRow:
    """Валидная строка импорта: момент в таймзоне пользователя + поля записи."""

    moment: datetime
    systolic: int
    diastolic: int
    pulse: Optional[int]
    medication_taken: Optional[bool]
    feeling: Optional[str]
    note: Optional[str]


@dataclass(frozen=True)
class ParsedImport:
    """Разбор файла: валидные строки, дубли внутри файла и ошибки строк."""

    rows: list[ImportRow]
    duplicates: int
    errors: list[str]  # «Строка N: причина», не более MAX_ERRORS_SHOWN
    error_count: int  # всего ошибочных строк (включая не показанные)


@dataclass(frozen=True)
class ImportReport:
    """Отчёт импорта (§16.2): «Импортировано: N · Пропущено дублей: M · Ошибок: K»."""

    imported: int
    duplicates: int
    errors: int
    error_lines: list[str] = field(default_factory=list)

    def text(self) -> str:
        """Единая строка отчёта для страницы кабинета."""
        return (
            f"Импортировано: {self.imported} · "
            f"Пропущено дублей: {self.duplicates} · "
            f"Ошибок: {self.errors}"
        )


def _parse_row(record: Mapping[str, str], tz_name: str, current: datetime) -> ImportRow:
    """Разобрать одну строку; ошибки — `ValueError` с короткой причиной (§16.2)."""
    match = _TIMESTAMP_RE.match(record.get("timestamp_local", "").strip())
    if not match:
        raise ValueError(_ERR_TIMESTAMP)
    day_s, month_s, year_s, hour_s, minute_s = match.groups()
    try:
        day = date(int(year_s), int(month_s), int(day_s))
        moment_time = time(int(hour_s), int(minute_s))
    except ValueError:
        raise ValueError(_ERR_TIMESTAMP) from None
    try:
        moment = schemas.combine_moment(day, moment_time, tz_name, now=current)
    except ValueError:  # единственная причина — момент в будущем (§9)
        raise ValueError(_ERR_FUTURE) from None
    systolic = schemas.validate_int(record.get("systolic", ""), 70, 260, "систолическое")
    diastolic = schemas.validate_int(record.get("diastolic", ""), 40, 160, "диастолическое")
    schemas.validate_pair(systolic, diastolic)
    pulse_raw = record.get("pulse", "").strip()
    pulse = schemas.validate_int(pulse_raw, 30, 220, "пульс") if pulse_raw else None
    med_raw = record.get("medication_taken", "").strip().lower()
    if med_raw not in _MED_FLAG_VALUES:
        raise ValueError(_ERR_MED_FLAG)
    feeling_raw = record.get("feeling", "").strip().lower()
    if not feeling_raw:
        feeling = None
    elif feeling_raw in _FEELING_VALUES:
        feeling = _FEELING_VALUES[feeling_raw]
    else:
        raise ValueError(_ERR_FEELING)
    note_raw = record.get("note", "").strip()
    try:
        note = schemas.validate_note(note_raw) if note_raw else None
    except ValueError:
        raise ValueError(_ERR_NOTE) from None
    return ImportRow(
        moment=moment,
        systolic=systolic,
        diastolic=diastolic,
        pulse=pulse,
        medication_taken=_MED_FLAG_VALUES[med_raw],
        feeling=feeling,
        note=note,
    )


def parse(
    data: bytes | str, tz_name: str, *, now: Optional[datetime] = None
) -> ParsedImport:
    """Разобрать CSV-файл импорта (чистая функция, без БД; §16.2).

    Строки с ошибками валидации пропускаются и перечисляются (до 10);
    дубли за минуту внутри файла пропускаются и считаются отдельно.

    Исключения:
        ValueError: ошибка всего файла — кодировка, заголовок, лимит строк.
    """
    if isinstance(data, bytes):
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError(FILE_NOT_UTF8) from exc
    else:
        text = data.lstrip("﻿")
    reader = csv.reader(io.StringIO(text), delimiter=csv_export.CSV_DELIMITER)
    try:
        header = next(reader)
    except StopIteration as exc:
        raise ValueError(FILE_NO_HEADER) from exc
    columns = [name.strip().lower() for name in header]
    missing = [name for name in _REQUIRED_COLUMNS if name not in columns]
    if missing:
        raise ValueError("В заголовке нет колонок: " + ", ".join(missing))
    data_rows = [
        raw
        for raw in reader
        if raw and any(cell.strip() for cell in raw)
    ]
    if len(data_rows) > MAX_IMPORT_ROWS:
        raise ValueError(FILE_TOO_MANY_ROWS)
    tz = ZoneInfo(tz_name)
    current = now if now is not None else datetime.now(tz)
    if current.tzinfo is None:
        current = current.replace(tzinfo=tz)
    else:
        current = current.astimezone(tz)
    rows: list[ImportRow] = []
    errors: list[str] = []
    error_count = 0
    duplicates = 0
    seen: set[datetime] = set()
    for index, raw in enumerate(data_rows):
        number = index + 2  # строка 1 — заголовок
        record = {
            name: (raw[i] if i < len(raw) else "")
            for i, name in enumerate(columns)
        }
        try:
            row = _parse_row(record, tz_name, current)
        except ValueError as exc:
            error_count += 1
            if len(errors) < MAX_ERRORS_SHOWN:
                errors.append(f"Строка {number}: {exc}")
            continue
        bucket = row.moment.astimezone(timezone.utc).replace(second=0, microsecond=0)
        if bucket in seen:
            duplicates += 1
            continue
        seen.add(bucket)
        rows.append(row)
    return ParsedImport(
        rows=rows,
        duplicates=duplicates,
        errors=errors,
        error_count=error_count,
    )


async def import_rows(
    db: AsyncSession, user: User, parsed: ParsedImport
) -> ImportReport:
    """Вставить валидные строки одной транзакцией; дубли пропускаются (§16.2).

    Дубль — запись за ту же минуту, уже существующая в БД; коммитит вызывающий
    код. Дубли внутри файла уже посчитаны при разборе (`parsed.duplicates`).
    Каждая строка — `INSERT … SELECT … WHERE NOT EXISTS` под общим локом
    сохранений (как `save_measurement`, §7.2): проверка дубля и вставка
    атомарны, поэтому и параллельная двойная отправка формы дублей не создаёт.
    """
    duplicates = parsed.duplicates
    imported = 0
    async with _current_save_lock():
        for row in parsed.rows:
            moment_utc, minute_start, minute_end = minute_window(row.moment)
            duplicate = duplicate_in_minute(user.id, minute_start, minute_end)
            result = await db.execute(
                insert(Measurement).from_select(
                    [
                        "user_id", "timestamp", "systolic", "diastolic", "pulse",
                        "medication_taken", "feeling", "note",
                    ],
                    select(
                        literal(user.id),
                        literal(moment_utc),
                        literal(row.systolic),
                        literal(row.diastolic),
                        literal(row.pulse),
                        literal(row.medication_taken),
                        literal(row.feeling),
                        literal(row.note),
                    ).where(~duplicate),
                )
            )
            if result.rowcount == 0:
                duplicates += 1
            else:
                imported += 1
        await db.flush()
    return ImportReport(
        imported=imported,
        duplicates=duplicates,
        errors=parsed.error_count,
        error_lines=list(parsed.errors),
    )
