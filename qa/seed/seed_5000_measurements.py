"""Сид измерений нагрузочных пациентов: 250 на пациента за 30 дней (SPEC.md, §22).

Полный профиль — 20 × 250 = 5000 записей. Расписание детерминированное:
запись i пациента n лежит в день `anchor − days + i·days // m` (m — записей
на пациента) с 06:00 шагом в 2 часа, минуты — по формуле, секунда —
`SEED_SECOND`. Значения в валидных диапазонах: САД 100–180, ДАД 60–110
(всегда ниже САД минимум на 20), пульс 50–110. У каждой третьей записи
заполнены самочувствие, заметка и флаг «Принял лекарство».

Идемпотентно: вставляются только моменты расписания, которых ещё нет у
пациента. Запуск — через `qa/seed/seed_all.py`.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import Measurement, User
from qa.seed import Scale, anchor_day, load_patients, seed_moment

#: Самочувствие «заполненных» записей по кругу (литералы модели).
_FEELINGS = ("normal", "headache", "weakness", "other")

#: Заметки «заполненных» записей; для «Другое» — текст самочувствия (§6.3).
_NOTES = ("после прогулки", "утром до кофе", "после работы", "немного шумит в ушах")


@dataclass(frozen=True)
class PlannedMeasurement:
    """Одна запись расписания сида."""

    timestamp: datetime
    systolic: int
    diastolic: int
    pulse: int
    feeling: Optional[str]
    note: Optional[str]
    medication_taken: Optional[bool]


def plan_measurements(n: int, user: User, scale: Scale) -> list[PlannedMeasurement]:
    """Расписание записей пациента `n` (чистая функция, без БД)."""
    count = scale.measurements_per_patient
    first_day = anchor_day(user.created_at, user.timezone) - timedelta(days=scale.days)
    plan: list[PlannedMeasurement] = []
    day_start = 0
    previous_day = -1
    for i in range(count):
        day_index = i * scale.days // count
        if day_index != previous_day:
            day_start, previous_day = i, day_index
        slot_in_day = i - day_start  # не больше 9 записей в день → 06:00…22:00
        rng = random.Random(n * 100_003 + i)
        systolic = rng.randint(100, 180)
        diastolic = rng.randint(60, min(110, systolic - 20))
        filled = i % 3 == 0
        feeling = _FEELINGS[(i // 3) % len(_FEELINGS)] if filled else None
        plan.append(
            PlannedMeasurement(
                timestamp=seed_moment(
                    first_day + timedelta(days=day_index),
                    6 + 2 * slot_in_day,
                    (n * 7 + i * 13) % 60,
                    user.timezone,
                ),
                systolic=systolic,
                diastolic=diastolic,
                pulse=rng.randint(50, 110),
                feeling=feeling,
                note=_NOTES[(i // 3) % len(_NOTES)] if filled else None,
                medication_taken=(i // 3) % 2 == 0 if filled else None,
            )
        )
    return plan


async def seed_measurements(session: AsyncSession, scale: Scale) -> dict[str, int]:
    """Дозаполнить расписание измерений всех пациентов профиля.

    Возвращает `{"created": …, "existing": …}`; коммит — у вызывающего кода.
    """
    created = existing = 0
    for n, user in await load_patients(session, scale):
        taken = set(
            (
                await session.execute(
                    select(Measurement.timestamp).where(Measurement.user_id == user.id)
                )
            ).scalars()
        )
        for planned in plan_measurements(n, user, scale):
            if planned.timestamp in taken:
                existing += 1
                continue
            session.add(
                Measurement(
                    user_id=user.id,
                    timestamp=planned.timestamp,
                    systolic=planned.systolic,
                    diastolic=planned.diastolic,
                    pulse=planned.pulse,
                    feeling=planned.feeling,
                    note=planned.note,
                    medication_taken=planned.medication_taken,
                )
            )
            created += 1
    await session.flush()
    return {"created": created, "existing": existing}
