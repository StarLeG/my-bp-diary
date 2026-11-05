"""Сводка и тренд (SPEC.md, раздел 6.5).

Считается на лету из БД, без кэшей. Решения:
- тренд: период делится пополам по времени; разница средних систолических
  второй и первой половины: > +2 мм рт. ст. — «растёт», < −2 — «падает»,
  иначе «стабильно»; при < 4 измерениях — «мало данных для тренда»;
- вердикт по цели — по обоим средним с допуском 5 мм рт. ст. (среднее
  128/82 при цели 130/80 — «в пределах цели», как в примере §6.5);
- строка приверженности — только если настроены лекарства.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot import texts
from bot.models import Measurement, User
from bot.services import medications as medications_service
from bot.services.measurements import measurements_for_period

#: Порог «стабильности» тренда, мм рт. ст. (SPEC.md, раздел 6.5).
TREND_THRESHOLD = 2

#: Минимум измерений для оценки тренда.
TREND_MIN_MEASUREMENTS = 4

#: Допуск цели, мм рт. ст.: небольшое превышение — «в пределах цели».
GOAL_TOLERANCE = 5


@dataclass
class StatsSummary:
    """Числа сводки; None — если данных нет или поле не считается."""

    count: int
    sys_avg: Optional[int] = None
    sys_min: Optional[int] = None
    sys_max: Optional[int] = None
    dia_avg: Optional[int] = None
    dia_min: Optional[int] = None
    dia_max: Optional[int] = None
    pulse_avg: Optional[int] = None
    trend: str = texts.STATS_TREND_FEW
    goal_verdict: Optional[str] = None
    adherence: Optional[tuple[int, int]] = None  # (taken, planned); None — лекарств нет


def compute_trend(systolic_values: Sequence[int], timestamps: Sequence[datetime]) -> str:
    """Тренд по систолическому: период пополам по времени, порог ±2 мм рт. ст."""
    if len(systolic_values) < TREND_MIN_MEASUREMENTS:
        return texts.STATS_TREND_FEW
    start, end = min(timestamps), max(timestamps)
    mid = start + (end - start) / 2
    first = [value for value, ts in zip(systolic_values, timestamps) if ts < mid]
    second = [value for value, ts in zip(systolic_values, timestamps) if ts >= mid]
    if not first or not second:
        return texts.STATS_TREND_FEW
    delta = sum(second) / len(second) - sum(first) / len(first)
    if delta > TREND_THRESHOLD:
        return texts.STATS_TREND_UP
    if delta < -TREND_THRESHOLD:
        return texts.STATS_TREND_DOWN
    return texts.STATS_TREND_STABLE


def compute_goal(
    sys_avg: int, dia_avg: int, target_systolic: int, target_diastolic: int
) -> str:
    """Вердикт по цели: «в пределах» / «выше» / «ниже» (допуск ±GOAL_TOLERANCE)."""
    if (
        sys_avg > target_systolic + GOAL_TOLERANCE
        or dia_avg > target_diastolic + GOAL_TOLERANCE
    ):
        return texts.STATS_GOAL_ABOVE
    if sys_avg < target_systolic and dia_avg < target_diastolic:
        return texts.STATS_GOAL_BELOW
    return texts.STATS_GOAL_OK


async def summary(
    session: AsyncSession, user: User, start_day: date, end_day: date
) -> StatsSummary:
    """Сводка за период (локальные сутки пользователя); при 0 записей — count=0."""
    measurements = await measurements_for_period(session, user, start_day, end_day)
    count = len(measurements)
    if count == 0:
        return StatsSummary(count=0)

    systolic = [m.systolic for m in measurements]
    diastolic = [m.diastolic for m in measurements]
    pulses = [m.pulse for m in measurements if m.pulse is not None]
    timestamps = [m.timestamp for m in measurements]

    has_medications = await _has_medications(session, user)
    adherence_value: Optional[tuple[int, int]] = None
    if has_medications:
        adherence_value = await medications_service.adherence(session, user, start_day, end_day)

    sys_avg = round(sum(systolic) / count)
    dia_avg = round(sum(diastolic) / count)
    return StatsSummary(
        count=count,
        sys_avg=sys_avg,
        sys_min=min(systolic),
        sys_max=max(systolic),
        dia_avg=dia_avg,
        dia_min=min(diastolic),
        dia_max=max(diastolic),
        pulse_avg=round(sum(pulses) / len(pulses)) if pulses else None,
        trend=compute_trend(systolic, timestamps),
        goal_verdict=compute_goal(
            sys_avg, dia_avg, user.target_systolic, user.target_diastolic
        ),
        adherence=adherence_value,
    )


async def _has_medications(session: AsyncSession, user: User) -> bool:
    """Настроены ли лекарства (строка приверженности показывается только тогда)."""
    from bot.models import Medication

    row = (
        await session.execute(
            select(Medication.id).where(Medication.user_id == user.id).limit(1)
        )
    ).first()
    return row is not None
