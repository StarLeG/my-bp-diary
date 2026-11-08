"""Сид лекарств и журнала приёмов нагрузочных пациентов (SPEC.md, §22).

Полный профиль: 5 названий на пациента (100 названий; строк `medications` —
100–200, по строке на слот, §4.4), из них ровно одно `crisis`; 25 записей
`medication_logs` на пациента (500), статусы `taken` / `skipped` /
`partial` по кругу. Дублей по `(user_id, slot, локальная дата,
medication_id)` нет — каждая плановая запись лежит в своём дне; записи по
`crisis` под это правило не подпадают (SOS-препарат можно принять дважды).

Идемпотентно: недостающие строки лекарств добавляются по `(name, slot)`,
записи журнала — по `(medication_id, timestamp)`. Запуск — через
`qa/seed/seed_all.py`.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import Medication, MedicationLog, User
from qa.seed import Scale, anchor_day, load_patients, seed_moment

#: Плановые лекарства (название, дозировка); последнее место — у `crisis`.
_REGULAR = (
    ("Лозартан", "50 мг"),
    ("Амлодипин", "5 мг"),
    ("Бисопролол", "2,5 мг"),
    ("Индапамид", "1,5 мг"),
)

#: SOS-препарат: ровно один на пациента, слот `crisis`.
_CRISIS = ("Каптоприл", "25 мг")

#: Слоты плановых лекарств: у каждого 1–2 слота, набор сдвигается по пациенту.
_SLOT_PATTERNS = (("morning",), ("morning", "evening"), ("noon",), ("evening", "noon"))

#: Час отметки приёма по слоту (в таймзоне пользователя).
_SLOT_HOURS = {"morning": 8, "noon": 13, "evening": 20, "crisis": 16}

#: Статусы журнала по кругу (SPEC §22: все три, включая `partial`).
_STATUSES = ("taken", "skipped", "partial")

#: Каждая десятая запись журнала — приём `crisis`.
_CRISIS_EVERY = 10


def plan_medications(n: int, scale: Scale) -> list[tuple[str, str, str]]:
    """Строки лекарств пациента `n`: (название, дозировка, слот)."""
    regular_count = scale.medications_per_patient - 1  # одно место — у crisis
    rows: list[tuple[str, str, str]] = []
    for k in range(regular_count):
        name, dosage = _REGULAR[k % len(_REGULAR)]
        for slot in _SLOT_PATTERNS[(k + n) % len(_SLOT_PATTERNS)]:
            rows.append((name, dosage, slot))
    rows.append((*_CRISIS, "crisis"))
    return rows


async def _medication_rows(session: AsyncSession, user: User) -> list[Medication]:
    """Строки лекарств пациента в стабильном порядке (по id)."""
    return list(
        (
            await session.execute(
                select(Medication).where(Medication.user_id == user.id).order_by(Medication.id)
            )
        ).scalars()
    )


async def seed_medications(session: AsyncSession, scale: Scale) -> dict[str, int]:
    """Дозаполнить лекарства и журнал приёмов всех пациентов профиля.

    Возвращает счётчики строк лекарств и записей журнала; коммит — у
    вызывающего кода.
    """
    stats = {"medications_created": 0, "medications_existing": 0, "logs_created": 0, "logs_existing": 0}
    for n, user in await load_patients(session, scale):
        have = {(med.name, med.slot) for med in await _medication_rows(session, user)}
        for name, dosage, slot in plan_medications(n, scale):
            if (name, slot) in have:
                stats["medications_existing"] += 1
                continue
            session.add(Medication(user_id=user.id, name=name, dosage=dosage, slot=slot))
            stats["medications_created"] += 1
        await session.flush()

        rows = await _medication_rows(session, user)
        regular = [med for med in rows if med.slot != "crisis"]
        crisis = next(med for med in rows if med.slot == "crisis")
        logged = {
            (medication_id, moment)
            for medication_id, moment in await session.execute(
                select(MedicationLog.medication_id, MedicationLog.timestamp).where(
                    MedicationLog.user_id == user.id
                )
            )
        }
        last_day = anchor_day(user.created_at, user.timezone) - timedelta(days=1)
        for j in range(scale.logs_per_patient):
            medication = (
                crisis if j % _CRISIS_EVERY == _CRISIS_EVERY - 1 else regular[j % len(regular)]
            )
            moment = seed_moment(
                last_day - timedelta(days=j),
                _SLOT_HOURS[medication.slot],
                j % 60,
                user.timezone,
            )
            if (medication.id, moment) in logged:
                stats["logs_existing"] += 1
                continue
            session.add(
                MedicationLog(
                    user_id=user.id,
                    medication_id=medication.id,
                    timestamp=moment,
                    status=_STATUSES[j % len(_STATUSES)],
                    slot=medication.slot,
                )
            )
            stats["logs_created"] += 1
    await session.flush()
    return stats
