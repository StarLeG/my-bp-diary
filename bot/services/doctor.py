"""Логика связей врача и пациента, генерация отчётов (SPEC.md, §4.8–4.10, §21).

Связи живут в `doctor_patients`: `pending` — пациент добавил врача, `active` —
врач открыл ссылку-приглашение (доступ к данным открыт), `revoked` — пациент
отозвал доступ. Врач видит только пациентов со статусом `active`.

Функции, меняющие данные, коммитят транзакцию сами: вставки защищены от
гонок ограничениями UNIQUE (`doctor_patients(doctor_id, user_id)`,
`doctors.email`) — проигравший получает уже существующую строку, а не ошибку.
"""
from __future__ import annotations

import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence
from zoneinfo import ZoneInfo

from argon2 import PasswordHasher
from fpdf import FPDF
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import Doctor, DoctorNote, DoctorPatient, Measurement, User

#: Проверка формата e-mail при добавлении врача (как в §16.2 для ЛК).
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

#: Хеш пароля врача — только Argon2id (CLAUDE.md, «Безопасность и платежи»).
_password_hasher = PasswordHasher()

#: Шрифт отчёта PDF: DejaVuSans покрывает кириллицу (лицензия свободная).
REPORT_FONT_PATH = Path(__file__).resolve().parent / "fonts" / "DejaVuSans.ttf"

#: Заголовки таблицы отчёта PDF.
_REPORT_COLUMNS = ("Дата", "Сист.", "Диаст.", "Пульс", "Лекарство", "Самочувствие")

#: Отметка приёма лекарства в отчёте PDF.
_REPORT_MED_LABELS = {True: "да", False: "нет", None: "—"}


def valid_email(email: str) -> bool:
    """Достаточно ли корректен e-mail врача (формат и длина)."""
    return bool(EMAIL_RE.match(email)) and len(email) <= 255


# ─────────────────────────── Врачи ───────────────────────────


async def find_or_create_doctor(session: AsyncSession, email: str) -> tuple[Doctor, bool]:
    """Врач по e-mail; при первом добавлении создаётся запись `doctors`.

    Пароль генерируется случайным и хранится только как хеш Argon2id —
    пациент добавляет врача по e-mail, врач входит по ссылке. Имя в MVP —
    локальная часть e-mail (ФИО боту не известно, поле NOT NULL).
    Возвращает (врач, создана ли новая запись); гонка двух добавлений
    отсекается UNIQUE по `doctors.email` (§7.2).
    """
    email = email.strip().lower()
    doctor = (
        await session.execute(select(Doctor).where(Doctor.email == email))
    ).scalar_one_or_none()
    if doctor is not None:
        return doctor, False
    doctor = Doctor(
        email=email,
        name=email.partition("@")[0][:100],
        password_hash=_password_hasher.hash(_random_password()),
    )
    session.add(doctor)
    try:
        await session.flush()
        await session.commit()
    except IntegrityError:
        await session.rollback()
        doctor = (
            await session.execute(select(Doctor).where(Doctor.email == email))
        ).scalar_one()
        return doctor, False
    return doctor, True


def _random_password() -> str:
    """Случайный пароль: в открытом виде нигде не хранится и не показывается."""
    return secrets.token_urlsafe(18)


# ─────────────────────────── Связи врач–пациент ───────────────────────────


async def link_patient(
    session: AsyncSession, doctor_id: int, user_id: int
) -> tuple[DoctorPatient, bool]:
    """Связать врача с пациентом: новая связь — `pending` (§21, п.2).

    Существующая связь не дублируется (UNIQUE по паре): `revoked` снова
    становится `pending` (пациент добавил врача заново), `pending`/`active`
    не меняются. Возвращает (связь, создана ли новая).
    """
    link = await link_for_pair(session, doctor_id, user_id)
    if link is not None:
        if link.status == "revoked":
            link.status = "pending"
            await session.commit()
        return link, False
    link = DoctorPatient(doctor_id=doctor_id, user_id=user_id, status="pending")
    session.add(link)
    try:
        await session.flush()
        await session.commit()
    except IntegrityError:
        # Гонка двух добавлений: конкурент уже связал пару (§7.2).
        await session.rollback()
        link = await link_for_pair(session, doctor_id, user_id)
        assert link is not None  # noqa: S101 — строка появилась от конкурента
        return link, False
    return link, True


async def link_for_pair(
    session: AsyncSession, doctor_id: int, user_id: int
) -> Optional[DoctorPatient]:
    """Связь конкретной пары врач–пациент (или None)."""
    return (
        await session.execute(
            select(DoctorPatient).where(
                DoctorPatient.doctor_id == doctor_id, DoctorPatient.user_id == user_id
            )
        )
    ).scalar_one_or_none()


async def links_for_patient(
    session: AsyncSession, user_id: int
) -> list[tuple[DoctorPatient, Doctor]]:
    """Все связи пациента с врачами (для экрана «Мой врач»)."""
    rows = await session.execute(
        select(DoctorPatient, Doctor)
        .join(Doctor, Doctor.id == DoctorPatient.doctor_id)
        .where(DoctorPatient.user_id == user_id)
        .order_by(DoctorPatient.id)
    )
    return list(rows.all())


async def active_links_for_doctor(
    session: AsyncSession, doctor_id: int
) -> list[tuple[DoctorPatient, User]]:
    """Пациенты врача: только те, кто дал доступ (`status='active'`, §21)."""
    rows = await session.execute(
        select(DoctorPatient, User)
        .join(User, User.id == DoctorPatient.user_id)
        .where(DoctorPatient.doctor_id == doctor_id, DoctorPatient.status == "active")
        .order_by(DoctorPatient.id)
    )
    return list(rows.all())


async def revoke_access(session: AsyncSession, user_id: int, doctor_id: int) -> bool:
    """Отозвать доступ врача (`status='revoked'`, §21). True — связь была."""
    link = await link_for_pair(session, doctor_id, user_id)
    if link is None:
        return False
    if link.status != "revoked":
        link.status = "revoked"
        await session.commit()
    return True


async def activate_link(session: AsyncSession, doctor_id: int, user_id: int) -> bool:
    """Открытие врачом ссылки: `pending` → `active` (доступ к данным открыт).

    `revoked` не активируется: пациент отозвал доступ. True — доступ открыт.
    """
    link = await link_for_pair(session, doctor_id, user_id)
    if link is None or link.status == "revoked":
        return False
    if link.status == "pending":
        link.status = "active"
        await session.commit()
    return True


# ─────────────────────────── Заметки врача ───────────────────────────

#: Максимальная длина заметки врача (SPEC.md, раздел 4.10).
NOTE_MAX_LENGTH = 1000


async def add_note(
    session: AsyncSession, doctor_id: int, user_id: int, text: str
) -> DoctorNote:
    """Сохранить заметку врача пациенту (§21: видна пациенту в его ЛК).

    `ValueError` — пустой текст или длиннее 1000 символов (§4.10).
    """
    text = text.strip()
    if not text or len(text) > NOTE_MAX_LENGTH:
        raise ValueError("некорректный текст заметки")
    note = DoctorNote(doctor_id=doctor_id, user_id=user_id, text=text)
    session.add(note)
    await session.flush()
    await session.commit()
    return note


async def notes_for_patient(
    session: AsyncSession, user_id: int
) -> list[tuple[DoctorNote, Doctor]]:
    """Заметки врача для пациента: новые сверху (с именем врача)."""
    rows = await session.execute(
        select(DoctorNote, Doctor)
        .join(Doctor, Doctor.id == DoctorNote.doctor_id)
        .where(DoctorNote.user_id == user_id)
        .order_by(DoctorNote.created_at.desc(), DoctorNote.id.desc())
    )
    return list(rows.all())


# ─────────────────────────── Отчёт PDF ───────────────────────────


def build_report_pdf(
    patient_label: str,
    measurements: Sequence[Measurement],
    tz_name: str = "Europe/Minsk",
    generated_at: Optional[datetime] = None,
) -> bytes:
    """PDF-отчёт для врача: сводка и таблица измерений (§21).

    Кириллица — встроенным шрифтом DejaVuSans (`REPORT_FONT_PATH`).
    """
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_font("DejaVu", fname=str(REPORT_FONT_PATH))
    pdf.add_page()
    pdf.set_font("DejaVu", size=14)
    pdf.cell(text="Отчёт по давлению", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("DejaVu", size=10)
    moment = (generated_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    pdf.cell(
        text=f"{patient_label} · сформирован {moment:%d.%m.%Y %H:%M} UTC",
        new_x="LMARGIN",
        new_y="NEXT",
    )
    pdf.ln(2)
    pdf.multi_cell(w=0, text=_summary_text(measurements), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    _report_table(pdf, measurements, tz_name)
    return bytes(pdf.output())


def _summary_text(measurements: Sequence[Measurement]) -> str:
    """Сводка отчёта: количество и средние значения."""
    if not measurements:
        return "Записей нет."
    sys_avg = sum(m.systolic for m in measurements) / len(measurements)
    dia_avg = sum(m.diastolic for m in measurements) / len(measurements)
    pulses = [m.pulse for m in measurements if m.pulse is not None]
    lines = [
        f"Измерений: {len(measurements)}",
        f"Систолическое: в среднем {sys_avg:.0f}",
        f"Диастолическое: в среднем {dia_avg:.0f}",
    ]
    if pulses:
        lines.append(f"Пульс: в среднем {sum(pulses) / len(pulses):.0f}")
    return "\n".join(lines)


def _report_table(pdf: FPDF, measurements: Sequence[Measurement], tz_name: str) -> None:
    """Таблица измерений отчёта (по локальному времени пациента)."""
    widths = (38, 18, 18, 18, 28, 50)
    pdf.set_font("DejaVu", size=9)
    for title, width in zip(_REPORT_COLUMNS, widths):
        pdf.cell(width, 7, text=title, border=1)
    pdf.ln()
    tz = ZoneInfo(tz_name)
    for m in measurements:
        moment: datetime = m.timestamp.astimezone(tz)
        values = (
            f"{moment:%d.%m.%Y %H:%M}",
            str(m.systolic),
            str(m.diastolic),
            str(m.pulse) if m.pulse is not None else "—",
            _REPORT_MED_LABELS.get(m.medication_taken, "—"),
            (m.feeling or "—")[:20],
        )
        for value, width in zip(values, widths):
            pdf.cell(width, 6, text=value, border=1)
        pdf.ln()
