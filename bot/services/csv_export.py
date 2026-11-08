"""CSV для врача: измерения и приёмы лекарств (SPEC.md, раздел 10).

- UTF-8 с BOM (кириллица в Excel), разделитель `;`, перевод строки CRLF;
- `timestamp_local` — «ДД.ММ.ГГГГ ЧЧ:ММ» в таймзоне пользователя;
- `medication_taken` — «да» / «нет» / пусто (не спрашивали);
- `feeling` — по-русски («норма», «головная боль», …);
- лимит 5000 записей применяется к экспорту «всё время» (хендлер передаёт
  `limit=5000` для «всё время» и None для 7/30 дней);
- генерация — через `asyncio.to_thread`, в поток уходят только данные (id/значения).
"""
from __future__ import annotations

import asyncio
import csv
import io
from datetime import date, datetime
from typing import Optional, Sequence
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import Measurement, MedicationLog, User
from bot.services import measurements as measurements_service
from bot.services import medications as medications_service

#: Разделитель колонок (Excel с русской локалью).
CSV_DELIMITER = ";"

#: Лимит записей для экспорта «всё время».
ALL_TIME_LIMIT = 5000

#: Колонки файла измерений.
MEASUREMENTS_HEADER = (
    "timestamp_local",
    "systolic",
    "diastolic",
    "pulse",
    "medication_taken",
    "feeling",
    "note",
)

#: Колонки файла приёмов лекарств.
MEDS_HEADER = ("timestamp_local", "medication", "dosage", "status")

_MED_FLAG_TEXT = {True: "да", False: "нет", None: ""}

_FEELING_TEXT = {
    "normal": "норма",
    "headache": "головная боль",
    "weakness": "слабость",
    "other": "другое",
}


def _format_local(timestamp: datetime, tz_name: str) -> str:
    """Момент в текст «ДД.ММ.ГГГГ ЧЧ:ММ» локальной таймзоны пользователя."""
    return timestamp.astimezone(ZoneInfo(tz_name)).strftime("%d.%m.%Y %H:%M")


def _render(header: Sequence[str], rows: Sequence[Sequence[object]]) -> bytes:
    """CSV: UTF-8 с BOM, разделитель `;`, CRLF — готовые байты файла."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=CSV_DELIMITER, lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")


def build_measurements_csv(measurements: Sequence[Measurement], tz_name: str) -> bytes:
    """Файл измерений (синхронная генерация — вызывать через `asyncio.to_thread`)."""
    rows = [
        (
            _format_local(m.timestamp, tz_name),
            m.systolic,
            m.diastolic,
            m.pulse if m.pulse is not None else "",
            _MED_FLAG_TEXT[m.medication_taken],
            _FEELING_TEXT.get(m.feeling or "", m.feeling or ""),
            m.note or "",
        )
        for m in measurements
    ]
    return _render(MEASUREMENTS_HEADER, rows)


def build_meds_csv(
    logs: Sequence[tuple[MedicationLog, str, str]], tz_name: str
) -> bytes:
    """Файл приёмов лекарств: timestamp_local, medication, dosage, status."""
    rows = [
        (_format_local(log.timestamp, tz_name), name, dosage, log.status)
        for log, name, dosage in logs
    ]
    return _render(MEDS_HEADER, rows)


def file_name(kind: str, start_day: date, end_day: date, ext: str = "csv") -> str:
    """Имя файла: `bp_diary_<дата1>-<дата2>.csv` (даты ГГГГММДД).

    `ext` — расширение: тем же правилом называется PDF-отчёт (§10.1).
    """
    return f"{kind}_{start_day:%Y%m%d}-{end_day:%Y%m%d}.{ext}"


async def export_measurements(
    session: AsyncSession,
    user: User,
    start_day: Optional[date],
    end_day: Optional[date],
    limit: Optional[int] = None,
) -> Optional[bytes]:
    """CSV измерений за период; `start_day=None` — «всё время».

    Возвращает None, если записей больше `limit` (хендлер покажет
    «Слишком много записей, выберите 7 или 30 дней»).
    """
    if start_day is None or end_day is None:
        measurements = await measurements_service.measurements_all(session, user)
    else:
        measurements = await measurements_service.measurements_for_period(
            session, user, start_day, end_day
        )
    if limit is not None and len(measurements) > limit:
        return None
    return await asyncio.to_thread(build_measurements_csv, measurements, user.timezone)


async def export_medications(
    session: AsyncSession,
    user: User,
    start_day: Optional[date],
    end_day: Optional[date],
    limit: Optional[int] = None,
) -> Optional[bytes]:
    """CSV приёмов лекарств за период; `start_day=None` — «всё время»."""
    if start_day is None or end_day is None:
        logs = await medications_service.logs_all(session, user)
    else:
        logs = await medications_service.logs_for_period(session, user, start_day, end_day)
    if limit is not None and len(logs) > limit:
        return None
    return await asyncio.to_thread(build_meds_csv, logs, user.timezone)
