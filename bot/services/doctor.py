"""Логика связей врача и пациента (SPEC.md, §4.8–4.10, §21).

PDF-отчёт для врача — общий генератор `bot/services/pdf_report.py` (§10.1).

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
from datetime import datetime, timedelta, timezone
from typing import Optional

from argon2 import PasswordHasher
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import Doctor, DoctorInvite, DoctorNote, DoctorPatient, User

#: Проверка формата e-mail при добавлении врача (как в §16.2 для ЛК).
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

#: Хеш пароля врача — только Argon2id (CLAUDE.md, «Безопасность и платежи»).
_password_hasher = PasswordHasher()


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


# ─────────────────── Врач в Telegram и приглашения (§4.13, §21) ───────────────────

#: Срок ссылки-приглашения в бота (§4.13).
INVITE_TTL = timedelta(hours=72)

#: Максимальная длина имени пациента в приглашении (§4.13).
PATIENT_NAME_MAX = 64

#: Максимальная длина ФИО врача (§4.8).
DOCTOR_NAME_MAX = 100

#: Минимальная длина ФИО врача в `/iamdoctor` (§21).
DOCTOR_NAME_MIN = 2

#: Имя пользователя Telegram: 5–32 латинских букв, цифр и «_», «@» — по желанию.
USERNAME_RE = re.compile(r"^@?([A-Za-z0-9_]{5,32})$")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_username(raw: Optional[str]) -> Optional[str]:
    """`@Ivanova_Doc` → `ivanova_doc`; неверный формат — None (§21)."""
    match = USERNAME_RE.match((raw or "").strip())
    return match.group(1).lower() if match else None


def doctor_label(doctor: Doctor) -> str:
    """Строка врача у пациента: ФИО → @username → e-mail (§21, C3).

    Врач из Telegram — ФИО (из профиля или `/iamdoctor`); врач по e-mail —
    адрес: его «ФИО» — лишь часть адреса до «@».
    """
    if doctor.telegram_id is not None:
        if doctor.name:
            return doctor.name
        if doctor.telegram_username:
            return f"@{doctor.telegram_username}"
    return doctor.email or doctor.name


def full_name(first_name: Optional[str], last_name: Optional[str]) -> str:
    """«Имя Фамилия» из профиля Telegram, не длиннее 100 символов (§21, N4)."""
    name = " ".join(part for part in (first_name, last_name) if part).strip()
    return (name or "Врач")[:DOCTOR_NAME_MAX]


async def doctor_by_telegram(session: AsyncSession, telegram_id: int) -> Optional[Doctor]:
    """Врач по Telegram-аккаунту (или None — человек не врач)."""
    return (
        await session.execute(select(Doctor).where(Doctor.telegram_id == telegram_id))
    ).scalar_one_or_none()


async def doctor_by_username(session: AsyncSession, username: str) -> Optional[Doctor]:
    """Поиск по @username — только среди врачей с Telegram (§21, T3).

    Пациенты не ищутся никогда: по боту нельзя проверить, пользуется ли им
    человек.
    """
    return (
        await session.execute(
            select(Doctor).where(
                Doctor.telegram_username == username.lower(),
                Doctor.telegram_id.is_not(None),
            )
        )
    ).scalars().first()


async def get_or_create_telegram_doctor(
    session: AsyncSession, telegram_id: int, username: Optional[str], name: str
) -> tuple[Doctor, bool]:
    """Врач по Telegram-аккаунту; новый — с именем из профиля (§21, N4).

    Гонка двух созданий отсекается UNIQUE по `doctors.telegram_id` (§7.2).
    Возвращает (врач, создан ли новый).
    """
    doctor = await doctor_by_telegram(session, telegram_id)
    if doctor is not None:
        return doctor, False
    doctor = Doctor(
        telegram_id=telegram_id,
        telegram_username=(username or "").lower() or None,
        name=name[:DOCTOR_NAME_MAX],
        password_hash=_password_hasher.hash(_random_password()),
    )
    session.add(doctor)
    try:
        await session.flush()
        await session.commit()
    except IntegrityError:
        await session.rollback()
        doctor = await doctor_by_telegram(session, telegram_id)
        assert doctor is not None  # noqa: S101 — строка появилась от конкурента
        return doctor, False
    return doctor, True


async def register_doctor(
    session: AsyncSession, telegram_id: int, username: Optional[str], name: str
) -> Doctor:
    """`/iamdoctor`: зарегистрировать врача или сменить ФИО (§21, T4)."""
    doctor, created = await get_or_create_telegram_doctor(session, telegram_id, username, name)
    if not created and doctor.name != name:
        doctor.name = name
        await session.commit()
    return doctor


async def touch_username(
    session: AsyncSession, telegram_id: int, username: Optional[str]
) -> bool:
    """Обновить username врача, если он изменился (§21, T10). True — обновлён.

    Вызывается на каждое сообщение боту, поэтому сначала чтение: запись (и
    блокировка SQLite на запись) — только если человек врач и username
    действительно сменился.
    """
    value = (username or "").lower() or None
    doctor = await doctor_by_telegram(session, telegram_id)
    if doctor is None or doctor.telegram_username == value:
        return False
    doctor.telegram_username = value
    await session.commit()
    return True


async def create_invite(
    session: AsyncSession, user_id: int, patient_name: str, now: Optional[datetime] = None
) -> DoctorInvite:
    """Новая ссылка-приглашение пациента; прежние неиспользованные списываются (§4.13)."""
    now = now or _utcnow()
    await session.execute(
        update(DoctorInvite)
        .where(DoctorInvite.user_id == user_id, DoctorInvite.used_at.is_(None))
        .values(used_at=now)
    )
    invite = DoctorInvite(
        code=secrets.token_urlsafe(16),
        user_id=user_id,
        patient_name=(patient_name or "Пациент")[:PATIENT_NAME_MAX],
        created_at=now,
        expires_at=now + INVITE_TTL,
    )
    session.add(invite)
    await session.commit()
    return invite


async def current_invite(
    session: AsyncSession, user_id: int, patient_name: str, now: Optional[datetime] = None
) -> DoctorInvite:
    """Действующее приглашение пациента или новое (§4.13).

    Повторный заход на экран «Через Telegram» или поиск по @username не
    списывают ссылку, которую пациент, возможно, уже переслал врачу.
    """
    now = now or _utcnow()
    invite = (
        await session.execute(
            select(DoctorInvite)
            .where(
                DoctorInvite.user_id == user_id,
                DoctorInvite.used_at.is_(None),
                DoctorInvite.expires_at > now,
            )
            .order_by(DoctorInvite.created_at.desc())
        )
    ).scalars().first()
    if invite is not None:
        return invite
    return await create_invite(session, user_id, patient_name, now)


async def active_invite(
    session: AsyncSession, code: str, now: Optional[datetime] = None
) -> Optional[DoctorInvite]:
    """Действующее приглашение: не списано и не просрочено (или None)."""
    now = now or _utcnow()
    return (
        await session.execute(
            select(DoctorInvite).where(
                DoctorInvite.code == code,
                DoctorInvite.used_at.is_(None),
                DoctorInvite.expires_at > now,
            )
        )
    ).scalar_one_or_none()


async def close_invite(
    session: AsyncSession, code: str, now: Optional[datetime] = None
) -> Optional[DoctorInvite]:
    """Списать приглашение атомарно («Принять» / «Отклонить», §4.13).

    `UPDATE … WHERE used_at IS NULL AND expires_at > now`: из двух
    одновременных нажатий проходит одно (§7.2). Возвращает приглашение
    или None — уже списано, просрочено или не существует.
    """
    now = now or _utcnow()
    result = await session.execute(
        update(DoctorInvite)
        .where(
            DoctorInvite.code == code,
            DoctorInvite.used_at.is_(None),
            DoctorInvite.expires_at > now,
        )
        .values(used_at=now)
    )
    if result.rowcount != 1:
        await session.rollback()
        return None
    await session.commit()
    # populate_existing: объект из identity map иначе остался бы с used_at=None
    return await session.get(DoctorInvite, code, populate_existing=True)


async def grant_access(session: AsyncSession, doctor_id: int, user_id: int) -> DoctorPatient:
    """Принятое приглашение: связь врач–пациент сразу `active` (§21, п.5).

    Отозванная ранее связь открывается снова — пациент пригласил заново.
    """
    link, _ = await link_patient(session, doctor_id, user_id)
    if link.status != "active":
        link.status = "active"
        await session.commit()
    return link


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
