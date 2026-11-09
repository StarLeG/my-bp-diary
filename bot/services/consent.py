"""Согласия на обработку персональных данных (SPEC.md, §4.15, §6.11).

Основание — Закон РБ № 99-З: данные о здоровье — специальные (ст. 8), их
обработка — только с согласия; запись согласия — доказательство (ст. 5 п. 7).

- **Основное согласие** пациента (`processing`) — на ведение дневника. Без
  действующего согласия бот и кабинет показывают только экран согласия,
  напоминания не отправляются, врач пациента не видит.
- **Согласие на врача** (`doctor`) — отдельно на каждое приглашение. У
  приглашения по ссылке врач становится известен только при «Принять»: до
  этого запись открыта с `doctor_id = NULL` и используется повторно.
- **Согласие врача** на обработку его данных — поля `doctors.consent_*`
  (у врача по e-mail нет записи `users`).

Функции, меняющие данные, коммитят сами. Двойное «Согласен» не создаёт вторую
действующую запись: частичный уникальный индекс `uq_consents_open` (§7.2) —
проигравший получает уже существующую запись.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import and_, exists, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from bot.models import Consent, Doctor, DoctorInvite, DoctorPatient, User

#: Версия согласия пациента — дата редакции раздела `#consent` (`ГГГГ-ММ-ДД`).
#: Меняется только при существенных изменениях (новые цели или перечень
#: данных, C10-Б): тогда бот и кабинет снова спрашивают согласие у всех.
CONSENT_VERSION = "2026-11-10"

#: Дата редакции разделов `#policy` и `#rights` страницы «Персональные данные».
POLICY_DATE = "2026-11-10"

#: Версия согласия врача (тексты Б8 / К2) — меняется независимо от пациентского.
DOCTOR_CONSENT_VERSION = "2026-11-10"

#: Виды согласия пациента (§4.15).
KIND_PROCESSING = "processing"
KIND_DOCTOR = "doctor"

#: Где дано согласие.
CHANNEL_BOT = "bot"
CHANNEL_CABINET = "cabinet"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def display_date(version: str) -> str:
    """`2026-11-10` → `10.11.2026` (дата редакции на странице и в боте)."""
    year, month, day = version.split("-")
    return f"{day}.{month}.{year}"


# ─────────────────────────── Основное согласие ───────────────────────────


async def current_processing(session: AsyncSession, user_id: int) -> Optional[Consent]:
    """Действующее основное согласие текущей версии (или None)."""
    return (
        await session.execute(
            select(Consent).where(
                Consent.user_id == user_id,
                Consent.kind == KIND_PROCESSING,
                Consent.withdrawn_at.is_(None),
                Consent.version == CONSENT_VERSION,
            )
        )
    ).scalar_one_or_none()


def processing_ok_clause(user_id_column):
    """SQL-условие «у пациента есть действующее основное согласие» (для выборок)."""
    return exists().where(
        Consent.user_id == user_id_column,
        Consent.kind == KIND_PROCESSING,
        Consent.withdrawn_at.is_(None),
        Consent.version == CONSENT_VERSION,
    )


async def has_processing(session: AsyncSession, user_id: int) -> bool:
    """Есть ли действующее основное согласие и не запрошено ли удаление (§6.11)."""
    user = await session.get(User, user_id)
    if user is None or user.deletion_requested_at is not None:
        return False
    return await current_processing(session, user_id) is not None


async def give_processing(
    session: AsyncSession, user_id: int, channel: str, now: Optional[datetime] = None
) -> Consent:
    """«Согласен»: основное согласие текущей версии (идемпотентно).

    Действующая запись прежней версии закрывается `withdrawn_at`, новая
    вставляется той же транзакцией (§4.15). Повторное или параллельное
    «Согласен» возвращает уже существующую запись.
    """
    now = now or _utcnow()
    existing = await current_processing(session, user_id)
    if existing is not None:
        return existing
    await session.execute(
        update(Consent)
        .where(
            Consent.user_id == user_id,
            Consent.kind == KIND_PROCESSING,
            Consent.withdrawn_at.is_(None),
        )
        .values(withdrawn_at=now)
    )
    consent = Consent(
        user_id=user_id,
        kind=KIND_PROCESSING,
        version=CONSENT_VERSION,
        channel=channel,
        given_at=now,
    )
    session.add(consent)
    try:
        await session.flush()
        await session.commit()
    except IntegrityError:
        # Параллельное «Согласен» уже вставило запись (§7.2).
        await session.rollback()
        consent = await current_processing(session, user_id)
        assert consent is not None  # noqa: S101 — запись появилась от конкурента
    return consent


async def request_deletion(
    session: AsyncSession, user_id: int, now: Optional[datetime] = None
) -> None:
    """Отзыв в кабинете (К5): согласие закрыто, аккаунт ждёт удаления ботом (§19).

    Одной транзакцией: основное согласие и согласия на врачей закрываются,
    `users.deletion_requested_at` ставится. Сессии кабинета удаляет вызывающий.
    """
    now = now or _utcnow()
    await session.execute(
        update(Consent)
        .where(Consent.user_id == user_id, Consent.withdrawn_at.is_(None))
        .values(withdrawn_at=now)
    )
    await session.execute(
        update(User)
        .where(User.id == user_id, User.deletion_requested_at.is_(None))
        .values(deletion_requested_at=now)
    )
    await session.commit()


async def users_to_delete(session: AsyncSession) -> list[User]:
    """Аккаунты, отозвавшие согласие в кабинете: их удаляет бот (§19)."""
    return list(
        (
            await session.execute(
                select(User)
                .where(User.deletion_requested_at.is_not(None))
                .order_by(User.id)
            )
        ).scalars()
    )


async def count_with_processing(session: AsyncSession) -> int:
    """Сколько пользователей дали действующее основное согласие (админка, §18.2)."""
    from sqlalchemy import func

    return int(
        (
            await session.execute(
                select(func.count(User.id)).where(
                    User.deletion_requested_at.is_(None), processing_ok_clause(User.id)
                )
            )
        ).scalar_one()
    )


# ─────────────────────────── Согласие на врача ───────────────────────────


async def _open_doctor_consent(
    session: AsyncSession, user_id: int, doctor_id: Optional[int]
) -> Optional[Consent]:
    """Открытая запись согласия на врача (`doctor_id` или «врач ещё неизвестен»)."""
    condition = (
        Consent.doctor_id.is_(None) if doctor_id is None else Consent.doctor_id == doctor_id
    )
    return (
        await session.execute(
            select(Consent).where(
                Consent.user_id == user_id,
                Consent.kind == KIND_DOCTOR,
                Consent.withdrawn_at.is_(None),
                condition,
            )
        )
    ).scalar_one_or_none()


async def give_doctor(
    session: AsyncSession,
    user_id: int,
    doctor_id: Optional[int],
    channel: str = CHANNEL_BOT,
    now: Optional[datetime] = None,
) -> Consent:
    """«Согласен, пригласить» / «Оставить доступ»: согласие на врача (идемпотентно).

    `doctor_id=None` — приглашение по ссылке, врач ещё неизвестен: открытая
    запись без врача одна и используется повторно (§4.15).
    """
    now = now or _utcnow()
    existing = await _open_doctor_consent(session, user_id, doctor_id)
    if existing is not None:
        return existing
    consent = Consent(
        user_id=user_id,
        kind=KIND_DOCTOR,
        doctor_id=doctor_id,
        version=CONSENT_VERSION,
        channel=channel,
        given_at=now,
    )
    session.add(consent)
    try:
        await session.flush()
        await session.commit()
    except IntegrityError:
        await session.rollback()
        consent = await _open_doctor_consent(session, user_id, doctor_id)
        assert consent is not None  # noqa: S101 — запись появилась от конкурента
    return consent


async def has_invite_consent(session: AsyncSession, user_id: int) -> bool:
    """Дано ли согласие на приглашение нового врача (Б7, запись без врача)."""
    return await _open_doctor_consent(session, user_id, None) is not None


async def attach_doctor(session: AsyncSession, user_id: int, doctor_id: int) -> bool:
    """Врач стал известен (e-mail, «Принять»): открытой записи без врача — `doctor_id`.

    Уже есть открытая запись на этого врача — ничего не меняется. Записи без
    врача нет (приглашение выдано до 0.5.0) — False: доступ откроется только
    после подтверждения пациентом (Д4).
    """
    if await _open_doctor_consent(session, user_id, doctor_id) is not None:
        return True
    pending = await _open_doctor_consent(session, user_id, None)
    if pending is None:
        return False
    pending.doctor_id = doctor_id
    await session.commit()
    return True


async def withdraw_doctor(
    session: AsyncSession,
    user_id: int,
    doctor_id: Optional[int],
    now: Optional[datetime] = None,
) -> None:
    """Закрыть согласие на врача: «Отозвать доступ», «Отклонить» у врача (§6.11).

    `doctor_id=None` — закрыть открытую запись без врача (приглашение по
    ссылке отклонено).
    """
    now = now or _utcnow()
    condition = (
        Consent.doctor_id.is_(None) if doctor_id is None else Consent.doctor_id == doctor_id
    )
    await session.execute(
        update(Consent)
        .where(
            Consent.user_id == user_id,
            Consent.kind == KIND_DOCTOR,
            Consent.withdrawn_at.is_(None),
            condition,
        )
        .values(withdrawn_at=now)
    )
    await session.commit()


async def links_to_confirm(
    session: AsyncSession, user_id: int
) -> list[tuple[DoctorPatient, Doctor]]:
    """Связи `pending` / `active` без согласия на этого врача — экран Д4."""
    rows = await session.execute(
        select(DoctorPatient, Doctor)
        .join(Doctor, Doctor.id == DoctorPatient.doctor_id)
        .where(
            DoctorPatient.user_id == user_id,
            DoctorPatient.status.in_(("pending", "active")),
            ~exists().where(
                Consent.user_id == DoctorPatient.user_id,
                Consent.kind == KIND_DOCTOR,
                Consent.doctor_id == DoctorPatient.doctor_id,
                Consent.withdrawn_at.is_(None),
            ),
        )
        .order_by(DoctorPatient.id)
    )
    return list(rows.all())


async def cancel_pending_invites(
    session: AsyncSession, user_id: int, now: Optional[datetime] = None
) -> None:
    """«Отменить приглашение» (Д4): неиспользованные коды `doctor_invites` списываются."""
    now = now or _utcnow()
    await session.execute(
        update(DoctorInvite)
        .where(DoctorInvite.user_id == user_id, DoctorInvite.used_at.is_(None))
        .values(used_at=now)
    )
    await session.commit()


# ─────────────────────────── Согласие врача ───────────────────────────


def doctor_ok(doctor: Doctor) -> bool:
    """Действующее согласие врача: дано и текущей версии (§4.8)."""
    return doctor.consent_at is not None and doctor.consent_version == DOCTOR_CONSENT_VERSION


async def give_doctor_self(
    session: AsyncSession, doctor: Doctor, channel: str, now: Optional[datetime] = None
) -> None:
    """Согласие врача (Б8 / К2): версия, момент и канал — в `doctors`."""
    if doctor_ok(doctor):
        return
    doctor.consent_version = DOCTOR_CONSENT_VERSION
    doctor.consent_at = now or _utcnow()
    doctor.consent_channel = channel
    await session.commit()


# ─────────────────────────── Доступ врача к пациенту ───────────────────────────


_patient = aliased(User)


def access_clause(doctor_id: int):
    """SQL-условие доступа врача к пациенту `DoctorPatient.user_id` (§6.11, без
    согласия самого врача — оно проверяется по объекту `Doctor`).

    Связь `active`, действующее согласие пациента на этого врача, действующее
    основное согласие пациента и не запрошено удаление.
    """
    return and_(
        DoctorPatient.doctor_id == doctor_id,
        DoctorPatient.status == "active",
        exists().where(
            Consent.user_id == DoctorPatient.user_id,
            Consent.kind == KIND_DOCTOR,
            Consent.doctor_id == doctor_id,
            Consent.withdrawn_at.is_(None),
        ),
        processing_ok_clause(DoctorPatient.user_id),
        # Псевдоним: внешний запрос бывает соединён с `users` (список пациентов),
        # и без него подзапрос схлопнулся бы автокорреляцией.
        exists().where(
            _patient.id == DoctorPatient.user_id, _patient.deletion_requested_at.is_(None)
        ),
    )


async def doctor_can_see(session: AsyncSession, doctor: Doctor, user_id: int) -> bool:
    """Видит ли врач пациента: три условия §6.11 (связь, согласие пациента на
    врача, согласие врача) плюс действующее основное согласие пациента."""
    if not doctor_ok(doctor):
        return False
    found = (
        await session.execute(
            select(DoctorPatient.id).where(
                access_clause(doctor.id), DoctorPatient.user_id == user_id
            )
        )
    ).first()
    return found is not None


async def visible_patients(
    session: AsyncSession, doctor: Doctor
) -> list[tuple[DoctorPatient, User]]:
    """Пациенты, которых врач видит (кабинет и «Мои пациенты» бота, §6.11)."""
    if not doctor_ok(doctor):
        return []
    rows = await session.execute(
        select(DoctorPatient, User)
        .join(User, User.id == DoctorPatient.user_id)
        .where(access_clause(doctor.id))
        .order_by(DoctorPatient.id)
    )
    return list(rows.all())
