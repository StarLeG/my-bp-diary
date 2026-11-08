"""Сид нагрузочных врачей и их связей с пациентами (SPEC.md, §22).

Врач n — `doctor{n}@load.test` с Argon2id-хешем пароля из `LOAD_PASSWORD`,
связан 1:1 с пациентом n (`doctor_patients.status = 'active'`). Основной
вход в нагрузке — magic link; пароль позволяет войти и по e-mail.

Идемпотентно: существующие врачи не меняются, связь доводится до `active`
(отозванная на стенде связь восстанавливается). Нужны пациенты
(`seed_load_users`). Запуск — через `qa/seed/seed_all.py`.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import Doctor, DoctorPatient
from qa.seed import Scale, doctor_email, load_patients


async def seed_doctors(session: AsyncSession, scale: Scale, password_hash: str) -> dict[str, int]:
    """Создать недостающих врачей профиля и активировать связи с пациентами.

    Возвращает счётчики врачей и связей; коммит — у вызывающего кода.
    """
    stats = {"doctors_created": 0, "doctors_existing": 0, "links_created": 0, "links_activated": 0}
    patients = dict(await load_patients(session, scale))
    for n in range(1, scale.doctors + 1):
        email = doctor_email(n)
        doctor = (
            await session.execute(select(Doctor).where(Doctor.email == email))
        ).scalar_one_or_none()
        if doctor is None:
            doctor = Doctor(
                email=email,
                name=f"Врач нагрузки {n}",
                clinic="Нагрузочный стенд",
                password_hash=password_hash,
            )
            session.add(doctor)
            await session.flush()
            stats["doctors_created"] += 1
        else:
            stats["doctors_existing"] += 1
        patient = patients.get(n)
        if patient is None:
            continue
        link = (
            await session.execute(
                select(DoctorPatient).where(
                    DoctorPatient.doctor_id == doctor.id, DoctorPatient.user_id == patient.id
                )
            )
        ).scalar_one_or_none()
        if link is None:
            session.add(DoctorPatient(doctor_id=doctor.id, user_id=patient.id, status="active"))
            stats["links_created"] += 1
        elif link.status != "active":
            link.status = "active"
            stats["links_activated"] += 1
    await session.flush()
    return stats
