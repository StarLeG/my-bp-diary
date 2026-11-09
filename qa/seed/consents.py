"""Согласия на стенде (SPEC.md, §6.11): без них пациенты видят только экран
согласия, а врачи — пустой список пациентов.

Сиды и демо-данные дают согласия так же, как это сделали бы люди: основное
согласие пациента, согласие на врача по каждой связи и согласие врача.
Идемпотентно: действующая запись не дублируется.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import Consent, Doctor
from bot.services.consent import CONSENT_VERSION, DOCTOR_CONSENT_VERSION


async def ensure_patient_consent(
    session: AsyncSession, user_id: int, doctor_id: int | None = None
) -> None:
    """Основное согласие (`doctor_id=None`) или согласие на врача."""
    kind = "processing" if doctor_id is None else "doctor"
    query = select(Consent.id).where(
        Consent.user_id == user_id,
        Consent.kind == kind,
        Consent.withdrawn_at.is_(None),
        Consent.version == CONSENT_VERSION if doctor_id is None else Consent.doctor_id == doctor_id,
    )
    if (await session.execute(query)).first() is None:
        session.add(
            Consent(
                user_id=user_id,
                kind=kind,
                doctor_id=doctor_id,
                version=CONSENT_VERSION,
                channel="bot",
                given_at=datetime.now(timezone.utc),
            )
        )
        await session.flush()


def ensure_doctor_consent(doctor: Doctor) -> None:
    """Согласие врача на обработку его данных (`doctors.consent_*`)."""
    if doctor.consent_version != DOCTOR_CONSENT_VERSION or doctor.consent_at is None:
        doctor.consent_version = DOCTOR_CONSENT_VERSION
        doctor.consent_at = datetime.now(timezone.utc)
        doctor.consent_channel = "cabinet"
