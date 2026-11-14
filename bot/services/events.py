"""События дневника (SPEC.md, §4.18, §6.14; 0.6.0).

Ключевые свойства:
- `payload` — канонический JSON (детерминированная сериализация, `marks`
  отсортированы): идемпотентность «➕ Фактор» сравнивает payload строками,
  иначе двойной тап создал бы дубль (§7.2);
- «➕ Фактор» — одна транзакция: метки одной группы собираются в одно событие,
  разные группы — по событию на тип (B6, R29 = А);
- `upsert_sleep` — не более одной оценки сна на локальные сутки: повторный ввод
  обновляет существующее событие (C23);
- правка/удаление — по id с проверкой владельца, чужое выглядит как
  отсутствующее (§7.1).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable, Literal, Optional

from sqlalchemy import exists, insert, literal, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import DiaryEvent, Measurement, MedicationLog, User
from bot.services.measurements import day_bounds_utc

#: Типы событий (§4.18).
EVENT_TYPES = ("food", "stress", "sleep", "activity", "note")

#: Метки питания (§6.14): соль, кофе, алкоголь.
FOOD_MARKS = ("salt", "coffee", "alcohol")
#: Метки «➕ Фактор» (§6.3): питание + стресс + недосып.
FACTOR_MARKS = ("salt", "coffee", "alcohol", "stress", "sleep")

#: Варианты «Стресс / самочувствие» (§6.14).
STRESS_VARIANTS = ("stress", "anxiety", "headache", "weakness", "dizziness", "other")
#: Оценки сна (§6.14).
SLEEP_RATINGS = ("little", "normal", "much")
#: Пресеты длительности прогулки, минуты (§6.14, R17 = Б).
ACTIVITY_DURATIONS = (10, 20, 30, 60)


class EventPayloadError(ValueError):
    """Поля `payload` не соответствуют типу события (§4.18)."""


def dumps_payload(payload: dict) -> str:
    """Канонический JSON: сортировка ключей, без пробелов, UTF-8 без escape.

    Детерминированная сериализация — часть идемпотентности (§7.2).
    """
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def event_payload(type_: str, **fields: object) -> Optional[str]:
    """Собрать и проверить `payload` события; `None` — событие без полей.

    Поля дословно по §4.18: `food` — `marks` (1–3 отсортированных метки),
    `stress` — `variant`, `sleep` — `rating`, `activity` — `duration_min`,
    `note` — без полей.

    Исключения:
        EventPayloadError: неизвестный тип, лишние или недостающие поля.
    """
    if type_ == "note":
        if fields:
            raise EventPayloadError("у события note нет полей payload")
        return None
    if type_ == "food":
        marks = fields.pop("marks", None)
        if fields or not isinstance(marks, (set, frozenset, list, tuple)):
            raise EventPayloadError("food: ожидается только поле marks")
        unknown = set(marks) - set(FOOD_MARKS)
        if unknown or not 1 <= len(marks) <= 3:
            raise EventPayloadError("food: marks — 1–3 из salt/coffee/alcohol")
        return dumps_payload({"marks": sorted(marks)})
    if type_ == "stress":
        variant = fields.pop("variant", None)
        if fields or variant not in STRESS_VARIANTS:
            raise EventPayloadError("stress: ожидается variant из STRESS_VARIANTS")
        return dumps_payload({"variant": variant})
    if type_ == "sleep":
        rating = fields.pop("rating", None)
        if fields or rating not in SLEEP_RATINGS:
            raise EventPayloadError("sleep: ожидается rating из SLEEP_RATINGS")
        return dumps_payload({"rating": rating})
    if type_ == "activity":
        duration = fields.pop("duration_min", None)
        if fields or duration not in ACTIVITY_DURATIONS:
            raise EventPayloadError("activity: duration_min — 10/20/30/60")
        return dumps_payload({"duration_min": duration})
    raise EventPayloadError(f"неизвестный тип события: {type_}")


def factor_groups(marks: Iterable[str]) -> list[tuple[str, Optional[str]]]:
    """Разложить метки «➕ Фактор» по типам событий (B6, R29 = А).

    Метки питания собираются в одно событие `food`, «Стресс» и «Недосып» —
    отдельные события `stress` / `sleep`; порядок групп фиксирован.
    """
    chosen = set(marks)
    unknown = chosen - set(FACTOR_MARKS)
    if unknown:
        raise EventPayloadError(f"неизвестные метки факторов: {sorted(unknown)}")
    groups: list[tuple[str, Optional[str]]] = []
    food = chosen & set(FOOD_MARKS)
    if food:
        groups.append(("food", event_payload("food", marks=food)))
    if "stress" in chosen:
        groups.append(("stress", event_payload("stress", variant="stress")))
    if "sleep" in chosen:
        groups.append(("sleep", event_payload("sleep", rating="little")))
    return groups


async def get_event(
    session: AsyncSession, user: User, event_id: int
) -> Optional[DiaryEvent]:
    """Событие по id с проверкой владельца: чужое выглядит как отсутствующее (§7.1)."""
    return (
        await session.execute(
            select(DiaryEvent).where(
                DiaryEvent.id == event_id, DiaryEvent.user_id == user.id
            )
        )
    ).scalar_one_or_none()


async def create_event(
    session: AsyncSession,
    user: User,
    type_: str,
    moment: datetime,
    *,
    note: Optional[str] = None,
    payload: Optional[str] = None,
    measurement_id: Optional[int] = None,
) -> DiaryEvent:
    """Создать событие дневника (валидация полей — на уровне хендлеров, §9)."""
    event = DiaryEvent(
        user_id=user.id,
        type=type_,
        moment=moment,
        note=note,
        payload=payload,
        measurement_id=measurement_id,
    )
    session.add(event)
    await session.flush()
    return event


async def create_factor_events(
    session: AsyncSession,
    user: User,
    measurement_id: int,
    marks: Iterable[str],
    moment: datetime,
) -> list[DiaryEvent]:
    """«➕ Фактор»: события групп по меткам с привязкой к измерению (§6.3, B6).

    Одна транзакция вызывающего; идемпотентно (§7.2): `INSERT … WHERE NOT EXISTS`
    по `(user_id, measurement_id, type, payload)` — повторное «Готово» дописывает
    недостающие события, дублей не создаёт (payload канонический, `marks`
    отсортированы — `dumps_payload`). Чужая запись измерения (`measurement_id`
    не принадлежит пользователю) — события создаются без привязки (§4.18:
    журнал и связи живут независимо; привязка — только к своей записи).
    """
    groups = factor_groups(marks)
    if not groups:
        return []
    own = (
        await session.execute(
            select(literal(1)).where(
                Measurement.id == measurement_id, Measurement.user_id == user.id
            )
        )
    ).first()
    link_id = measurement_id if own is not None else None
    created: list[DiaryEvent] = []
    for type_, payload in groups:
        duplicate = exists(
            select(literal(1)).where(
                DiaryEvent.user_id == user.id,
                DiaryEvent.measurement_id == link_id,
                DiaryEvent.type == type_,
                DiaryEvent.payload == payload,
            )
        )
        stmt = (
            insert(DiaryEvent)
            .from_select(
                ["user_id", "type", "moment", "payload", "measurement_id"],
                select(
                    literal(user.id),
                    literal(type_),
                    literal(moment),
                    literal(payload),
                    literal(link_id),
                ).where(~duplicate),
            )
            .returning(DiaryEvent.id)
        )
        result = await session.execute(stmt)
        row = result.first()
        if row is not None:
            created.append(
                (
                    await session.execute(
                        select(DiaryEvent).where(DiaryEvent.id == row[0])
                    )
                ).scalar_one()
            )
    await session.flush()
    return created


async def update_event(
    session: AsyncSession,
    user: User,
    event_id: int,
    *,
    moment: Optional[datetime] = None,
    note: Optional[str] = None,
    payload: Optional[str] = None,
) -> Optional[DiaryEvent]:
    """Правка события по id (владелец проверяется, §7.1).

    Все переданные поля перезаписываются одним UPDATE (§7.2); не переданные не
    меняются. Чужое или удалённое событие — `None`.
    """
    values: dict = {}
    if moment is not None:
        values["moment"] = moment
    if note is not None:
        values["note"] = note
    if payload is not None:
        values["payload"] = payload
    if values:
        result = await session.execute(
            update(DiaryEvent)
            .where(DiaryEvent.id == event_id, DiaryEvent.user_id == user.id)
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount == 0:
            return None
    event = await get_event(session, user, event_id)
    if event is not None and values:
        await session.refresh(event)
    return event


async def delete_event(session: AsyncSession, user: User, event_id: int) -> bool:
    """Удалить событие по id с проверкой владельца; чужое или уже удалённое — False."""
    event = await get_event(session, user, event_id)
    if event is None:
        return False
    await session.delete(event)
    await session.flush()
    return True


async def events_for_day(
    session: AsyncSession, user: User, day: date
) -> list[DiaryEvent]:
    """События за локальные сутки (границы дня — в таймзоне пользователя)."""
    start, end = day_bounds_utc(user.timezone, day)
    return list(
        (
            await session.execute(
                select(DiaryEvent)
                .where(
                    DiaryEvent.user_id == user.id,
                    DiaryEvent.moment >= start,
                    DiaryEvent.moment < end,
                )
                .order_by(DiaryEvent.moment, DiaryEvent.id)
            )
        ).scalars()
    )


async def upsert_sleep(
    session: AsyncSession,
    user: User,
    day: date,
    rating: str,
    moment: datetime,
) -> DiaryEvent:
    """Оценка сна: не более одной на локальные сутки (§6.14, C23).

    Повторный ввод тех же суток обновляет существующее событие (оценка и
    момент), новое не создаётся.
    """
    if rating not in SLEEP_RATINGS:
        raise EventPayloadError("sleep: rating — little/normal/much")
    payload = event_payload("sleep", rating=rating)
    existing = (
        await session.execute(
            select(DiaryEvent)
            .where(
                DiaryEvent.user_id == user.id,
                DiaryEvent.type == "sleep",
                DiaryEvent.moment >= day_bounds_utc(user.timezone, day)[0],
                DiaryEvent.moment < day_bounds_utc(user.timezone, day)[1],
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.payload = payload
        existing.moment = moment
        await session.flush()
        return existing
    return await create_event(session, user, "sleep", moment, payload=payload)


@dataclass(frozen=True)
class FeedItem:
    """Строка ленты «Дневник за сегодня» (§6.4): измерение, приём или событие."""

    kind: Literal["bp", "med", "event"]
    moment: datetime
    measurement: Optional[Measurement] = None
    log: Optional[MedicationLog] = None
    event: Optional[DiaryEvent] = None


async def day_feed(session: AsyncSession, user: User, day: date) -> list[FeedItem]:
    """Лента дня: измерения + отметки приёмов + события, по времени (§6.4, R23)."""
    start, end = day_bounds_utc(user.timezone, day)
    measurements = (
        await session.execute(
            select(Measurement)
            .where(
                Measurement.user_id == user.id,
                Measurement.timestamp >= start,
                Measurement.timestamp < end,
            )
            .order_by(Measurement.timestamp, Measurement.id)
        )
    ).scalars()
    logs = (
        await session.execute(
            select(MedicationLog)
            .where(
                MedicationLog.user_id == user.id,
                MedicationLog.timestamp >= start,
                MedicationLog.timestamp < end,
            )
            .order_by(MedicationLog.timestamp, MedicationLog.id)
        )
    ).scalars()
    events = await events_for_day(session, user, day)
    items = [FeedItem("bp", m.timestamp, measurement=m) for m in measurements]
    items += [FeedItem("med", log.timestamp, log=log) for log in logs]
    items += [FeedItem("event", e.moment, event=e) for e in events]
    items.sort(key=lambda item: (item.moment, item.kind))
    return items
