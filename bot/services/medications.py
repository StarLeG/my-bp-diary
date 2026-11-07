"""Лекарства: CRUD, расписание по слотам, лог приёмов, приверженность (SPEC.md, §6.6–6.7).

Решения:
- лекарство в двух слотах — две строки (`UNIQUE(user_id, name, slot)`); повторное
  добавление того же имени в тот же слот обновляет дозировку (идемпотентно);
- лекарство целиком — группа строк с одним `(user_id, name)`: изменение и
  удаление действуют по группе (§6.6); переживающие слоты при правке
  обновляются на месте — `medication_logs` не теряются;
- дозировка опциональна: пустая строка — «без дозировки»;
- слот `crisis` — препарат на случай кризиса: в расписании напоминаний не
  участвует и в приверженность не входит (SCHEDULED_SLOTS), только лог приёма;
- «Принял всё ✓» / «Частично…» / «Пропускаю» пишут по одной записи
  `medication_logs` на лекарство одной транзакцией; повторное действие за те же
  локальные сутки и слот новые логи не создаёт (двойное нажатие безопасно);
- мультивыбор «Какие лекарства приняли?» при записи давления (`log_manual_intake`)
  пишет по одной записи `medication_logs` (`status='taken'`, слот — из строки
  лекарства) на каждый отмеченный приём; идемпотентность — те же локальные сутки
  (§6.3, §7.2); «Готово» дополнительно связывает отмеченные приёмы с записью
  измерения (`link_measurement_intake` → `measurement_medications`, §4.11) —
  перечень для «Истории записей» кабинета (§16.2); связь идемпотентна
  (UNIQUE пары «запись–приём»);
- детальный трекинг — только здесь: «Приняли лекарство?» → «Да» без выбора
  лекарств в `medication_logs` ничего не пишет (только флаг
  `measurements.medication_taken`, §6.3).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Iterable, Mapping, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import exists, func, insert, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot import texts
from bot.models import Measurement, Medication, MedicationLog, MeasurementMedication, User
from bot.services.measurements import day_bounds_utc, utcnow

#: Все слоты приёма лекарств: слоты давления + препарат на случай кризиса.
SLOTS = ("morning", "noon", "evening", "crisis")

#: Слоты с расписанием (утро/обед/вечер) — только они участвуют в приверженности:
#: кризисный препарат принимают по факту, планировать его нельзя (§6.7).
SCHEDULED_SLOTS = ("morning", "noon", "evening")

_intake_lock: Optional[asyncio.Lock] = None
_intake_lock_loop: Optional[asyncio.AbstractEventLoop] = None


def _current_intake_lock() -> asyncio.Lock:
    """Лок отметок приёма текущего event loop (SPEC.md, раздел 7.2).

    Проверка «уже отмечено» и вставка должны быть атомарны относительно
    других отметок: двойное нажатие «Принял всё ✓» создаёт ровно один
    набор записей. В проде event loop один; в тестах лок пересоздаётся
    при смене loop (не путать с «bound to a different event loop»).
    """
    global _intake_lock, _intake_lock_loop
    loop = asyncio.get_running_loop()
    if _intake_lock is None or _intake_lock_loop is not loop:
        _intake_lock = asyncio.Lock()
        _intake_lock_loop = loop
    return _intake_lock


@dataclass
class SlotIntakeResult:
    """Итог отметки приёма за слот (для сообщения пользователю)."""

    taken: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    already_logged: bool = False


@dataclass
class ManualIntakeResult:
    """Итог ручной отметки приёмов (мультивыбор «Какие лекарства приняли?», §6.3).

    `taken` — подписи всех отмеченных приёмов (`fmt_med_item`, в порядке
    `list_medications`); `already_logged` — True, если новые записи не
    создавались (всё уже было отмечено за локальные сутки).
    """

    taken: list[str] = field(default_factory=list)
    already_logged: bool = False


def _as_utc(moment: Optional[datetime]) -> datetime:
    """Момент времени в UTC; наивное время трактуется как UTC."""
    moment = moment or utcnow()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


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


class MedicationNameConflictError(Exception):
    """Новое название занято другим лекарством пользователя (SPEC.md, §6.6)."""


@dataclass(frozen=True)
class MedicationGroup:
    """Лекарство целиком: одна кнопка выбора в изменении/удалении (§6.6).

    `id` — любой id строки группы (значение `callback_data`); слоты — в
    порядке `SLOTS`.
    """

    id: int
    name: str
    dosage: str
    slots: tuple[str, ...]


def _sort_by_slots(rows: list[Medication]) -> list[Medication]:
    """Строки лекарства в каноническом порядке слотов (`SLOTS`)."""
    return sorted(rows, key=lambda row: SLOTS.index(row.slot))


async def medications_by_name(
    session: AsyncSession, user: User, name: str
) -> list[Medication]:
    """Строки одного лекарства (группа `user_id` + `name`) в порядке `SLOTS`."""
    rows = (
        await session.execute(
            select(Medication).where(
                Medication.user_id == user.id, Medication.name == name
            )
        )
    ).scalars()
    return _sort_by_slots(list(rows))


async def list_medication_groups(
    session: AsyncSession, user: User
) -> list[MedicationGroup]:
    """Лекарства пользователя по группам (по названию), слоты в порядке `SLOTS`.

    Дозировка группы — дозировка первой строки в порядке слотов; если
    дозировки строк различаются (можно создать повторным добавлением в другой
    слот), правка унифицирует группу на одну дозировку.
    """
    rows = await list_medications(session, user)
    by_name: dict[str, list[Medication]] = {}
    for row in rows:
        by_name.setdefault(row.name, []).append(row)
    groups: list[MedicationGroup] = []
    for name, group_rows in by_name.items():
        group = _sort_by_slots(group_rows)
        groups.append(
            MedicationGroup(
                id=group[0].id,
                name=name,
                dosage=group[0].dosage,
                slots=tuple(row.slot for row in group),
            )
        )
    return groups


async def delete_medication_group(
    session: AsyncSession, user: User, name: str
) -> bool:
    """Удалить лекарство целиком: все строки группы (`user_id`, `name`).

    Логи приёмов удаляются каскадом — как у `delete_medication` (§6.6).
    Возвращает True, если удалена хотя бы одна строка; чужое или неизвестное
    имя — False (ничего не меняется).
    """
    rows = await medications_by_name(session, user, name)
    if not rows:
        return False
    for row in rows:
        await session.delete(row)
    await session.flush()
    return True


async def update_medication(
    session: AsyncSession,
    user: User,
    old_name: str,
    new_name: str,
    dosage: str,
    slots: Iterable[str],
) -> Optional[list[Medication]]:
    """Пересобрать лекарство (группу строк) по новым значениям одной транзакцией.

    Переживающие слоты обновляются на месте (id строк и история
    `medication_logs` сохраняются); снятые слоты удаляются (их логи каскадом);
    новые слоты — добавляются. Пустой набор слотов не проверяется — его
    отсекает вызывающая стор (как в `add_medication`).

    Возвращает итоговые строки группы или None, если группы уже нет (stale:
    лекарство удалили, пока шла правка). Бросает
    `MedicationNameConflictError`, если `new_name` занято другим лекарством
    пользователя (§6.6).
    """
    rows = await medications_by_name(session, user, old_name)
    if not rows:
        return None
    if new_name != old_name:
        taken = (
            await session.execute(
                select(Medication.id)
                .where(
                    Medication.user_id == user.id, Medication.name == new_name
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if taken is not None:
            raise MedicationNameConflictError(new_name)
    target = [slot for slot in SLOTS if slot in set(slots)]
    current = {row.slot: row for row in rows}
    # Снятые слоты удаляются первыми — освобождают (user, name, slot) до
    # переименования и вставок: переходных нарушений UNIQUE не бывает.
    for slot, row in current.items():
        if slot not in target:
            await session.delete(row)
    await session.flush()
    for slot in target:
        row = current.get(slot)
        if row is None:
            session.add(
                Medication(user_id=user.id, name=new_name, dosage=dosage, slot=slot)
            )
        else:
            row.name = new_name
            row.dosage = dosage
    await session.flush()
    return await medications_by_name(session, user, new_name)


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
    Проверка «уже отмечено» и вставка атомарны: под локом
    (`_current_intake_lock`) и запросом `INSERT … WHERE NOT EXISTS` —
    конкурентный проигравший получает `rowcount=0` и дубль не создаёт.
    """
    moment = _as_utc(timestamp)
    medications = await slot_medications(session, user, slot)
    by_id = {medication.id: medication for medication in medications}
    local_day = moment.astimezone(ZoneInfo(user.timezone)).date()
    day_start, day_end = day_bounds_utc(user.timezone, local_day)
    result = SlotIntakeResult()
    new_logs = 0
    async with _current_intake_lock():
        logged_today = await _logged_ids_today(session, user, slot, local_day)
        for med_id, status in decisions.items():
            medication = by_id.get(med_id)
            if medication is None:
                continue  # чужое или удалённое лекарство — тихо игнорируем
            if med_id in logged_today:
                continue
            duplicate = exists(
                select(literal(1)).where(
                    MedicationLog.user_id == user.id,
                    MedicationLog.medication_id == med_id,
                    MedicationLog.slot == slot,
                    MedicationLog.timestamp >= day_start,
                    MedicationLog.timestamp < day_end,
                )
            )
            stmt = insert(MedicationLog).from_select(
                ["user_id", "medication_id", "timestamp", "status", "slot"],
                select(
                    literal(user.id),
                    literal(med_id),
                    literal(moment),
                    literal(status),
                    literal(slot),
                ).where(~duplicate),
            )
            inserted = await session.execute(stmt)
            if inserted.rowcount == 0:
                continue  # конкурентно уже отметили — дубль не создаём
            label = texts.fmt_med_item(medication.name, medication.dosage)
            new_logs += 1
            if status == "taken":
                result.taken.append(label)
            else:
                result.skipped.append(label)
        await session.flush()
    result.already_logged = new_logs == 0
    return result


async def logged_intake_ids_today(
    session: AsyncSession, user: User, timestamp: Optional[datetime] = None
) -> set[int]:
    """Id лекарств с отметкой приёма за локальные сутки («✓» в мультивыборе, §6.3).

    Учитывается любая отметка (`taken`/`skipped`/`partial`) во всех слотах —
    повторная отметка того же приёма за те же сутки записи не создаёт.
    """
    moment = _as_utc(timestamp)
    local_day = moment.astimezone(ZoneInfo(user.timezone)).date()
    start, end = day_bounds_utc(user.timezone, local_day)
    rows = (
        await session.execute(
            select(MedicationLog.medication_id).where(
                MedicationLog.user_id == user.id,
                MedicationLog.timestamp >= start,
                MedicationLog.timestamp < end,
            )
        )
    ).scalars()
    return set(rows)


async def log_manual_intake(
    session: AsyncSession,
    user: User,
    medication_ids: Iterable[int],
    *,
    timestamp: Optional[datetime] = None,
) -> ManualIntakeResult:
    """Отметить приём выбранных лекарств вручную («Готово» мультивыбора, §6.3).

    По одной записи `medication_logs` (`status='taken'`, слот — из строки
    лекарства) на каждый отмеченный приём. Повторная отметка того же приёма за
    те же локальные сутки новые записи не создаёт (§7.2): проверка «уже
    отмечено» и вставка атомарны — под `_current_intake_lock` и запросом
    `INSERT … WHERE NOT EXISTS`. Чужие и удалённые id тихо игнорируются.

    Возвращает подписи всех отмеченных (для «💊 Принято: …») и признак того,
    что всё уже было отмечено (`already_logged`).
    """
    wanted = set(medication_ids)
    if not wanted:
        return ManualIntakeResult()
    moment = _as_utc(timestamp)
    medications = list(
        (
            await session.execute(
                select(Medication)
                .where(Medication.user_id == user.id, Medication.id.in_(wanted))
                .order_by(Medication.name, Medication.slot)
            )
        ).scalars()
    )
    local_day = moment.astimezone(ZoneInfo(user.timezone)).date()
    day_start, day_end = day_bounds_utc(user.timezone, local_day)
    result = ManualIntakeResult()
    new_logs = 0
    async with _current_intake_lock():
        logged_today = await logged_intake_ids_today(session, user, timestamp=moment)
        for medication in medications:
            if medication.id not in logged_today:
                duplicate = exists(
                    select(literal(1)).where(
                        MedicationLog.user_id == user.id,
                        MedicationLog.medication_id == medication.id,
                        MedicationLog.slot == medication.slot,
                        MedicationLog.timestamp >= day_start,
                        MedicationLog.timestamp < day_end,
                    )
                )
                stmt = insert(MedicationLog).from_select(
                    ["user_id", "medication_id", "timestamp", "status", "slot"],
                    select(
                        literal(user.id),
                        literal(medication.id),
                        literal(moment),
                        literal("taken"),
                        literal(medication.slot),
                    ).where(~duplicate),
                )
                inserted = await session.execute(stmt)
                if inserted.rowcount:
                    new_logs += 1
            result.taken.append(texts.fmt_med_item(medication.name, medication.dosage))
        await session.flush()
    result.already_logged = new_logs == 0
    return result


async def link_measurement_intake(
    session: AsyncSession,
    user: User,
    measurement_id: int,
    medication_ids: Iterable[int],
) -> None:
    """Связать отмеченные лекарства с записью измерения (§4.11, §6.3).

    По одной строке `measurement_medications` на отмеченный приём — перечень
    для «Истории записей» кабинета (§16.2). Идемпотентно (§7.2): повторное
    «Готово» дублей не создаёт — `INSERT … WHERE NOT EXISTS` по паре
    (measurement_id, medication_id) атомарно под `_current_intake_lock`,
    UNIQUE пары продублирован на уровне БД. Чужие и удалённые id лекарств,
    а также чужая или удалённая запись измерения, тихо игнорируются.
    """
    wanted = set(medication_ids)
    if not wanted:
        return
    owner = (
        await session.execute(
            select(Measurement.id).where(
                Measurement.id == measurement_id, Measurement.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    if owner is None:
        return  # чужая или удалённая запись
    med_ids = list(
        (
            await session.execute(
                select(Medication.id)
                .where(Medication.user_id == user.id, Medication.id.in_(wanted))
                .order_by(Medication.name, Medication.slot)
            )
        ).scalars()
    )
    async with _current_intake_lock():
        for med_id in med_ids:
            duplicate = exists(
                select(literal(1)).where(
                    MeasurementMedication.measurement_id == measurement_id,
                    MeasurementMedication.medication_id == med_id,
                )
            )
            stmt = insert(MeasurementMedication).from_select(
                ["measurement_id", "medication_id"],
                select(literal(measurement_id), literal(med_id)).where(~duplicate),
            )
            await session.execute(stmt)
        await session.flush()


async def intake_labels_by_measurement(
    session: AsyncSession,
    user: User,
    measurement_ids: Iterable[int],
) -> dict[int, list[str]]:
    """Подписи отмеченных приёмов по записям измерения (§4.11, §16.2).

    {id записи: [«Лозартан 50 мг», …]} в порядке `list_medications` (по
    названию и слоту). Записи без отмеченных приёмов в результат не
    попадают; чужие связи отсекаются по владельцу лекарства.
    """
    wanted = set(measurement_ids)
    if not wanted:
        return {}
    rows = await session.execute(
        select(
            MeasurementMedication.measurement_id, Medication.name, Medication.dosage
        )
        .join(Medication, Medication.id == MeasurementMedication.medication_id)
        .where(
            MeasurementMedication.measurement_id.in_(wanted),
            Medication.user_id == user.id,
        )
        .order_by(
            MeasurementMedication.measurement_id, Medication.name, Medication.slot
        )
    )
    result: dict[int, list[str]] = {}
    for measurement_id, name, dosage in rows.all():
        result.setdefault(measurement_id, []).append(texts.fmt_med_item(name, dosage))
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
