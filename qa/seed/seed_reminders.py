"""Сид активных напоминаний нагрузочных пациентов (SPEC.md, §22).

Полный профиль — 100 напоминаний: 60 слотовых (morning, noon, evening у
каждого из 20 пациентов; больше трёх на пользователя схема не допускает,
`uq_reminders_user_slot`) и 40 `med_remeasure` — по 2 на пациента, к двум
последним записям его истории (`uq_reminders_measurement`).

`next_trigger_time` — в ближайший час от момента сида (у каждого напоминания
своя минута); `local_time` слота совпадает с этим моментом в таймзоне
пользователя, так что строка согласована с собственным расписанием.

Идемпотентно: существующие слоты и повторы не трогаются. Нужны пациенты и
их измерения (`seed_load_users`, `seed_5000_measurements`). Запуск — через
`qa/seed/seed_all.py`.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import Measurement, Reminder
from qa.seed import Scale, load_patients

#: Слоты напоминаний в порядке заполнения (дымовой профиль берёт первые).
_SLOTS = ("morning", "noon", "evening")


async def seed_reminders(
    session: AsyncSession, scale: Scale, now: Optional[datetime] = None
) -> dict[str, int]:
    """Дозаполнить слотовые напоминания и повторы замера пациентов профиля.

    `now` — момент сида (UTC; по умолчанию текущий). Возвращает счётчики;
    коммит — у вызывающего кода.
    """
    moment = (now or datetime.now(timezone.utc)).replace(second=0, microsecond=0)
    stats = {"slots_created": 0, "slots_existing": 0, "remeasure_created": 0, "remeasure_existing": 0}
    for n, user in await load_patients(session, scale):
        reminders = list(
            (await session.execute(select(Reminder).where(Reminder.user_id == user.id))).scalars()
        )
        slots = {r.type for r in reminders if r.type != "med_remeasure"}
        for index, slot in enumerate(_SLOTS[: scale.slot_reminders_per_patient]):
            if slot in slots:
                stats["slots_existing"] += 1
                continue
            trigger = moment + timedelta(minutes=(n * 2 + index) % 55 + 1)
            session.add(
                Reminder(
                    user_id=user.id,
                    type=slot,
                    local_time=trigger.astimezone(ZoneInfo(user.timezone)).strftime("%H:%M"),
                    next_trigger_time=trigger,
                    enabled=True,
                )
            )
            stats["slots_created"] += 1

        remeasure_rows = [r for r in reminders if r.type == "med_remeasure"]
        # NULL в NOT IN обнулил бы выборку — в исключения только реальные id.
        remeasure = {r.measurement_id for r in remeasure_rows if r.measurement_id is not None}
        # Норма — по количеству: свежие записи от нагрузки (Locust) не должны
        # порождать лишние повторы при повторном сиде.
        missing = scale.remeasure_per_patient - len(remeasure_rows)
        stats["remeasure_existing"] += min(len(remeasure_rows), scale.remeasure_per_patient)
        if missing <= 0:
            continue
        latest = list(
            (
                await session.execute(
                    select(Measurement.id)
                    .where(
                        Measurement.user_id == user.id,
                        Measurement.id.not_in(remeasure),
                    )
                    .order_by(Measurement.timestamp.desc(), Measurement.id.desc())
                    .limit(missing)
                )
            ).scalars()
        )
        for index, measurement_id in enumerate(latest):
            session.add(
                Reminder(
                    user_id=user.id,
                    type="med_remeasure",
                    local_time=None,
                    next_trigger_time=moment + timedelta(minutes=(n * 2 + 3 + index) % 55 + 1),
                    enabled=True,
                    measurement_id=measurement_id,
                )
            )
            stats["remeasure_created"] += 1
    await session.flush()
    return stats
