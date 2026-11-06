"""Лекарства: CRUD, расписание по слотам, лог приёмов, приверженность (SPEC.md, §6.6–6.7).

Решения:
- лекарство в двух слотах — две строки (`UNIQUE(user_id, name, slot)`); повторное
  добавление того же имени в тот же слот обновляет дозировку (идемпотентно);
- дозировка опциональна: пустая строка — «без дозировки»;
- слот `crisis` — препарат на случай кризиса: в расписании напоминаний не
  участвует и в приверженность не входит (SCHEDULED_SLOTS), только лог приёма;
- «Принял всё ✓» / «Частично…» / «Пропускаю» пишут по одной записи
  `medication_logs` на лекарство одной транзакцией; повторное действие за те же
  локальные сутки и слот новые логи не создаёт (двойное нажатие безопасно);
- детальный трекинг — только здесь: «Приняли лекарство?» → «Да» при записи
  давления в `medication_logs` ничего не пишет (§6.3).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Iterable, Mapping, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot import texts
from bot.models import Medication, MedicationLog, User
from bot.services.measurements import day_bounds_utc, utcnow

#: Все слоты приёма лекарств: слоты давления + препарат на случай кризиса.
SLOTS = ("morning", "noon", "evening", "crisis")

#: Слоты с расписанием (утро/обед/вечер) — только они участвуют в приверженности:
#: кризисный препарат принимают по факту, планировать его нельзя (§6.7).
SCHEDULED_SLOTS = ("morning", "noon", "evening")


@dataclass
class SlotIntakeResult:
    """Итог отметки приёма за слот (для сообщения пользователю)."""

    taken: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    already_logged: bool = False


async def add_medication(
    session: AsyncSession,
    user: User,
    name: str,
    dosage: str,
    slots: Iterable[str],
) -> list[Medication]:
    """Добавить лекарство: по строке на каждый выбранный слот.

    Если лекарство с таким именем в слоте уже есть — обновляется дозировка
    (`UNIQUE(user_id, name, slot)` не нарушается, дублей строк нет).
    """
    result: list[Medication] = []
    for slot in slots:
        medication = (
            await session.execute(
                select(Medication).where(
                    Medication.user_id == user.id,
                    Medication.name == name,
                    Medication.slot == slot,
                )
            )
        ).scalar_one_or_none()
        if medication is None:
            medication = Medication(user_id=user.id, name=name, dosage=dosage, slot=slot)
            session.add(medication)
        else:
            medication.dosage = dosage
        result.append(medication)
    await session.flush()
    return result


async def delete_medication(
    session: AsyncSession, user: User, medication_id: int
) -> bool:
    """Удалить лекарство (с проверкой владельца); логи приёмов удаляются каскадом.

    Возвращает True, если запись была и удалена.
    """
    medication = (
        await session.execute(
            select(Medication).where(
                Medication.id == medication_id, Medication.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    if medication is None:
        return False
    await session.delete(medication)
    await session.flush()
    return True


async def list_medications(session: AsyncSession, user: User) -> list[Medication]:
    """Все лекарства пользователя: по названию, затем по слоту."""
    return list(
        (
            await session.execute(
                select(Medication)
                .where(Medication.user_id == user.id)
                .order_by(Medication.name, Medication.slot)
            )
        ).scalars()
    )


async def slot_medications(session: AsyncSession, user: User, slot: str) -> list[Medication]:
    """Лекарства конкретного слота (для перечня в напоминании)."""
    return list(
        (
            await session.execute(
                select(Medication)
                .where(Medication.user_id == user.id, Medication.slot == slot)
                .order_by(Medication.name)
            )
        ).scalars()
    )


async def _logged_ids_today(
    session: AsyncSession, user: User, slot: str, day: date
) -> set[int]:
    """Id лекарств, уже отмеченных в этом слоте за локальные сутки (идемпотентность)."""
    start, end = day_bounds_utc(user.timezone, day)
    rows = (
        await session.execute(
            select(MedicationLog.medication_id).where(
                MedicationLog.user_id == user.id,
                MedicationLog.slot == slot,
                MedicationLog.timestamp >= start,
                MedicationLog.timestamp < end,
            )
        )
    ).scalars()
    return set(rows)


async def log_slot_intake(
    session: AsyncSession,
    user: User,
    slot: str,
    decisions: Mapping[int, str],
    timestamp: Optional[datetime] = None,
) -> SlotIntakeResult:
    """Отметить приём лекарств слота одной транзакцией.

    `decisions` — {id лекарства: 'taken' | 'skipped' | 'partial'}.
    Уже отмеченные за сегодня в этом слоте лекарства повторно не логируются:
    двойное нажатие «Принял всё ✓» создаёт ровно один набор записей.
    """
    moment = timestamp or utcnow()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc)
    medications = await slot_medications(session, user, slot)
    by_id = {medication.id: medication for medication in medications}
    local_day = moment.astimezone(ZoneInfo(user.timezone)).date()
    logged_today = await _logged_ids_today(session, user, slot, local_day)
    result = SlotIntakeResult()
    new_logs = 0
    for med_id, status in decisions.items():
        medication = by_id.get(med_id)
        if medication is None:
            continue  # чужое или удалённое лекарство — тихо игнорируем
        label = texts.fmt_med_item(medication.name, medication.dosage)
        if med_id in logged_today:
            continue
        session.add(
            MedicationLog(
                user_id=user.id,
                medication_id=med_id,
                timestamp=moment,
                status=status,
                slot=slot,
            )
        )
        new_logs += 1
        if status == "taken":
            result.taken.append(label)
        else:
            result.skipped.append(label)
    await session.flush()
    result.already_logged = new_logs == 0
    return result


async def planned_intakes(
    session: AsyncSession, user: User, start_day: date, end_day: date
) -> int:
    """Число запланированных приёмов за период по текущему расписанию.

    Каждая строка `medications` — один приём в день в своём слоте:
    приёмов = строк × дней. Считаются только расписание слоты
    (`SCHEDULED_SLOTS`): кризисные препараты в план не входят. Расписание
    считается текущим (MVP без истории изменений списка лекарств).
    """
    days = (end_day - start_day).days + 1
    if days <= 0:
        return 0
    total = (
        await session.execute(
            select(func.count())
            .select_from(Medication)
            .where(
                Medication.user_id == user.id,
                Medication.slot.in_(SCHEDULED_SLOTS),
            )
        )
    ).scalar_one()
    return int(total) * days


async def adherence(
    session: AsyncSession, user: User, start_day: date, end_day: date
) -> tuple[int, int]:
    """Приверженность за период: (принято taken, всего запланировано).

    Считаются только расписание слоты: приёмы кризисных препаратов и в
    числитель, и в знаменатель не входят (§6.7).
    """
    start, _ = day_bounds_utc(user.timezone, start_day)
    _, end = day_bounds_utc(user.timezone, end_day)
    taken = (
        await session.execute(
            select(func.count())
            .select_from(MedicationLog)
            .where(
                MedicationLog.user_id == user.id,
                MedicationLog.status == "taken",
                MedicationLog.slot.in_(SCHEDULED_SLOTS),
                MedicationLog.timestamp >= start,
                MedicationLog.timestamp < end,
            )
        )
    ).scalar_one()
    planned = await planned_intakes(session, user, start_day, end_day)
    return int(taken), planned


async def logs_for_period(
    session: AsyncSession, user: User, start_day: date, end_day: date
) -> list[tuple[MedicationLog, str, str]]:
    """Логи приёмов за период с названием и дозировкой (для CSV, §10)."""
    start, end = day_bounds_utc(user.timezone, start_day)
    rows = await session.execute(
        select(MedicationLog, Medication.name, Medication.dosage)
        .join(Medication, Medication.id == MedicationLog.medication_id)
        .where(
            MedicationLog.user_id == user.id,
            MedicationLog.timestamp >= start,
            MedicationLog.timestamp < end,
        )
        .order_by(MedicationLog.timestamp)
    )
    return [(log, name, dosage) for log, name, dosage in rows.all()]


async def logs_all(
    session: AsyncSession, user: User
) -> list[tuple[MedicationLog, str, str]]:
    """Все логи приёмов пользователя (экспорт «всё время», §10)."""
    rows = await session.execute(
        select(MedicationLog, Medication.name, Medication.dosage)
        .join(Medication, Medication.id == MedicationLog.medication_id)
        .where(MedicationLog.user_id == user.id)
        .order_by(MedicationLog.timestamp)
    )
    return [(log, name, dosage) for log, name, dosage in rows.all()]
