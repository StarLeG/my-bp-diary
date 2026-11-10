"""Сводка и тренд (SPEC.md, раздел 6.5).

Считается на лету из БД, без кэшей. Решения:
- тренд: период делится пополам по времени; разница средних систолических
  второй и первой половины: > +2 мм рт. ст. — «растёт», < −2 — «падает»,
  иначе «стабильно»; при < 4 измерениях — «мало данных для тренда»;
- вердикт по цели — по обоим средним с допуском 5 мм рт. ст. (среднее
  128/82 при цели 130/80 — «в пределах цели», как в примере §6.5);
- строка приверженности — только если настроены лекарства;
- средние по дням (`daily_averages`, 0.5.3): по локальным суткам пациента,
  итог «Среднее за день» — среднее из неокруглённых средних дней с
  измерениями (каждый день весит одинаково), округление в конце.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable, Optional, Sequence
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot import texts
from bot.models import Measurement, User
from bot.services import medications as medications_service
from bot.services.measurements import measurements_for_period
from web.cabinet.chart_zones import CHART_ZONES, band_index

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


# ─────────────────────────── Средние по дням (0.5.3) ───────────────────────────


@dataclass(frozen=True)
class DayAverage:
    """Средние за одни локальные сутки; у дня без измерений значения — None.

    `zone` — худшая из зон округлённых средних САД и ДАД (0 — зелёная,
    1 — жёлтая, 2 — красная, пороги `chart_zones`); None — измерений нет.
    """

    day: date
    count: int
    systolic: Optional[int] = None
    diastolic: Optional[int] = None
    pulse: Optional[int] = None
    zone: Optional[int] = None


@dataclass(frozen=True)
class DailyAverages:
    """Средние по каждому дню периода и итог «Среднее за день» (§6.5)."""

    days: list[DayAverage]
    systolic: Optional[int] = None
    diastolic: Optional[int] = None
    pulse: Optional[int] = None

    @property
    def measured_days(self) -> list[DayAverage]:
        """Только дни с измерениями (список в боте, §6.5)."""
        return [d for d in self.days if d.count]


def day_zone(systolic: int, diastolic: int) -> int:
    """Худшая из зон САД и ДАД: 0 — зелёная, 1 — жёлтая, 2 — красная."""
    return max(
        band_index(systolic, CHART_ZONES["systolic"]),
        band_index(diastolic, CHART_ZONES["diastolic"]),
    )


def _mean(values: Sequence[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def _rounded(value: Optional[float]) -> Optional[int]:
    return round(value) if value is not None else None


def daily_averages(
    measurements: Iterable[Measurement], tz_name: str, start_day: date, end_day: date
) -> DailyAverages:
    """Средние по каждому дню [start_day, end_day] (локальные сутки `tz_name`).

    Без БД и ORM-запросов: принимает уже выбранные записи (их же рисует
    гистограмма, §7.2 п.7). Записи вне периода игнорируются.
    """
    tz = ZoneInfo(tz_name)
    buckets: dict[date, list[Measurement]] = {}
    for m in measurements:
        buckets.setdefault(m.timestamp.astimezone(tz).date(), []).append(m)

    days: list[DayAverage] = []
    sys_means: list[float] = []
    dia_means: list[float] = []
    pulse_means: list[float] = []
    day = start_day
    while day <= end_day:
        rows = buckets.get(day, [])
        if not rows:
            days.append(DayAverage(day=day, count=0))
        else:
            sys_mean = _mean([m.systolic for m in rows])
            dia_mean = _mean([m.diastolic for m in rows])
            pulse_mean = _mean([m.pulse for m in rows if m.pulse is not None])
            assert sys_mean is not None and dia_mean is not None  # noqa: S101
            sys_means.append(sys_mean)
            dia_means.append(dia_mean)
            if pulse_mean is not None:
                pulse_means.append(pulse_mean)
            systolic, diastolic = round(sys_mean), round(dia_mean)
            days.append(
                DayAverage(
                    day=day,
                    count=len(rows),
                    systolic=systolic,
                    diastolic=diastolic,
                    pulse=_rounded(pulse_mean),
                    zone=day_zone(systolic, diastolic),
                )
            )
        day += timedelta(days=1)
    return DailyAverages(
        days=days,
        systolic=_rounded(_mean(sys_means)),
        diastolic=_rounded(_mean(dia_means)),
        pulse=_rounded(_mean(pulse_means)),
    )
