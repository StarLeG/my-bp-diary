"""Сид визитов и заключений нагрузочных пациентов (SPEC.md, §22, §6.13).

Профиль «три года» (`--scale three-years`, `doc/LOAD-CRITERIA-QUESTIONS.md`,
В1/В2): 30 событий на пациента (600) в окне **3 года назад … сегодня + 2
года** (§6.13: визит не дальше чем на 2 года вперёд; 730 дней вперёд за
календарные два года не выходит). Названия — подсказки шага 2 (§6.13),
заметки и заключения — вымышленные фразы; реальных данных нет. События
детерминированные (seeded RNG): повторный запуск даёт те же строки.

Прошедшие события (`starts_on` раньше локального «сегодня» пациента) — со
статусами `done` / `missed` / `moved` / `cancelled` по кругу (чаще `done`),
будущие — `planned`. Заключения (§4.17) — только к прошедшим `done`, 1–2 на
событие (жёсткий потолок §4.17 — 5, `CONCLUSIONS_MAX`), тексты ≤ 4000
символов. Флаги уведомлений `sent_*_at` — как при сохранении события в
момент сида (`fresh_flags`, §6.13): прошедшие моменты обработаны, будущие —
NULL. `web_sessions` и `invite_log` не создаются вовсе (не накапливаются).

Идемпотентно: события дедуплицируются по `(kind, starts_on, starts_at,
title)`, заключения — по `(appointment_id, created_at)`. Заключения пишутся
пачками (`executemany`). Запуск — через `qa/seed/seed_all.py`.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import Appointment, AppointmentConclusion, User
from bot.services.appointments import fresh_flags
from qa.seed import Scale, anchor_day, load_patients, seed_moment

#: Вперёд от «сегодня» пациента: 730 дней не выходят за календарные
#: «сегодня + 2 года» (§6.13), даже с високосным днём внутри окна.
FORWARD_DAYS = 730

#: Потолок заключений на событие (§4.17, §6.13); сид кладёт 1–2.
CONCLUSIONS_MAX = 5

#: Типы событий (§6.13).
_KINDS = ("doctor", "lab", "exam")

#: Названия — подсказки шага 2 (§6.13): типовые, без имён и организаций.
_TITLES = {
    "doctor": ("Терапевт", "Кардиолог", "Эндокринолог", "Невролог", "Окулист", "Нефролог"),
    "lab": ("Общий анализ крови", "Биохимия", "Липидный профиль", "Анализ мочи"),
    "exam": ("ЭКГ", "УЗИ сердца", "СМАД", "УЗИ почек"),
}

#: Заметки событий (вымышленные); пустая строка — без заметки.
_NOTES = ("", "поликлиника № 5, каб. 212", "натощак", "с полисом и паспортом", "каб. 214")

#: Часы начала (сетка шага 4, §6.13: 07:00…18:00) и минуты.
_HOURS = tuple(range(7, 19))
_MINUTES = ("00", "30")

#: Доля событий с указанным времени (остальные — «без времени»).
_TIME_SHARE = 0.75

#: Статусы прошедших событий по кругу: чаще `done` (SPEC §22).
_PAST_STATUSES = ("done", "done", "missed", "done", "moved", "done", "done", "cancelled")

#: Заключения врача (вымышленные короткие фразы, ≤ 4000 символов — §4.17).
_CONCLUSION_TEXTS = (
    "Рекомендовано продолжить приём препарата в прежней дозе. "
    "Контроль АД дважды в день, дневник давления — через 2 недели.",
    "Давление в целевом диапазоне. Продолжаем текущую схему лечения, контроль через месяц.",
    "Сдать общий анализ крови и биохимию, результаты принести на следующий приём.",
    "Добавлен препарат на вечер. Понаблюдать за самочувствием две недели.",
    "Самочувствие в норме. Дневник давления — через 2 недели, дальше посмотрим.",
    "ЭКГ без динамики. Повторим обследование через полгода.",
    "Дозу немного увеличили. Контроль давления утром и вечером.",
    "Жалоб нет, давление стабильное. К следующему визиту — недельный дневник.",
    "Рекомендовано больше гулять и уменьшить соль в рационе. Контроль через месяц.",
    "Анализы в пределах нормы. Продолжаем лечение без изменений.",
    "Хорошая динамика — продолжайте в том же режиме. Следующий визит через 3 месяца.",
    "Утренние показатели лучше вечерних. Оставляем одну таблетку утром.",
)

#: Размер пачки `executemany` для заключений (строк за один вызов).
_BATCH = 1000


@dataclass(frozen=True)
class PlannedConclusion:
    """Одно заключение расписания сида."""

    text: str
    created_at: datetime


@dataclass(frozen=True)
class PlannedAppointment:
    """Одно событие расписания сида (визит, анализы или обследование)."""

    kind: str
    title: str
    starts_on: date
    starts_at: Optional[str]
    note: str
    status: str
    conclusions: tuple[PlannedConclusion, ...]


def _plan_conclusions(
    rng: random.Random, n: int, i: int, day: date, user: User
) -> tuple[PlannedConclusion, ...]:
    """Заключения прошедшего визита: 1–2 штуки, тексты из пула без повторов."""
    count = 1 + rng.randrange(2)
    first = rng.randrange(len(_CONCLUSION_TEXTS))
    return tuple(
        PlannedConclusion(
            text=_CONCLUSION_TEXTS[(first + k) % len(_CONCLUSION_TEXTS)],
            created_at=seed_moment(
                day + timedelta(days=1), 12, (n * 7 + i * 13 + k * 11) % 60, user.timezone
            ),
        )
        for k in range(count)
    )


def plan_appointments(n: int, user: User, scale: Scale) -> list[PlannedAppointment]:
    """Расписание визитов пациента `n` (чистая функция, без БД).

    Событие i лежит в день `anchor − days + i·span // count` (span — всё окно
    `days + FORWARD_DAYS`): ~60% — прошедшие, остальные — «Скоро» (их меньше
    лимита 30 событий, §6.13). Моментов дальше `anchor + FORWARD_DAYS` нет.
    """
    anchor = anchor_day(user.created_at, user.timezone)
    count = scale.appointments_per_patient
    span = scale.days + FORWARD_DAYS
    plan: list[PlannedAppointment] = []
    past_index = 0
    for i in range(count):
        day = anchor - timedelta(days=scale.days) + timedelta(days=i * span // count)
        rng = random.Random(n * 300_007 + i)
        kind = _KINDS[rng.randrange(len(_KINDS))]
        starts_at = (
            f"{_HOURS[rng.randrange(len(_HOURS))]:02d}:{_MINUTES[rng.randrange(len(_MINUTES))]}"
            if rng.random() < _TIME_SHARE
            else None
        )
        past = day < anchor
        status = "planned"
        conclusions: tuple[PlannedConclusion, ...] = ()
        if past:
            status = _PAST_STATUSES[past_index % len(_PAST_STATUSES)]
            past_index += 1
            if status == "done":
                conclusions = _plan_conclusions(rng, n, i, day, user)
        plan.append(
            PlannedAppointment(
                kind=kind,
                title=_TITLES[kind][rng.randrange(len(_TITLES[kind]))],
                starts_on=day,
                starts_at=starts_at,
                note=_NOTES[rng.randrange(len(_NOTES))],
                status=status,
                conclusions=conclusions,
            )
        )
    return plan


async def seed_appointments(session: AsyncSession, scale: Scale) -> dict[str, int]:
    """Дозаполнить визиты и заключения всех пациентов профиля.

    Возвращает счётчики событий и заключений; коммит — у вызывающего кода.
    """
    stats = {
        "created": 0,
        "existing": 0,
        "conclusions_created": 0,
        "conclusions_existing": 0,
    }
    if scale.appointments_per_patient == 0:
        return stats
    for n, user in await load_patients(session, scale):
        existing = {
            (kind, starts_on, starts_at, title): appointment_id
            for appointment_id, kind, starts_on, starts_at, title in await session.execute(
                select(
                    Appointment.id,
                    Appointment.kind,
                    Appointment.starts_on,
                    Appointment.starts_at,
                    Appointment.title,
                ).where(Appointment.user_id == user.id)
            )
        }
        have_conclusions = set(
            await session.execute(
                select(
                    AppointmentConclusion.appointment_id, AppointmentConclusion.created_at
                ).where(
                    AppointmentConclusion.appointment_id.in_(
                        select(Appointment.id).where(Appointment.user_id == user.id)
                    )
                )
            )
        )
        plans = plan_appointments(n, user, scale)
        new_rows: dict[tuple, Appointment] = {}
        for planned in plans:
            key = (planned.kind, planned.starts_on, planned.starts_at, planned.title)
            if key in existing:
                stats["existing"] += 1
                continue
            new_rows[key] = Appointment(
                user_id=user.id,
                kind=planned.kind,
                title=planned.title,
                starts_on=planned.starts_on,
                starts_at=planned.starts_at,
                note=planned.note,
                status=planned.status,
                created_at=user.created_at,
                **fresh_flags(planned.starts_on, planned.starts_at, user.timezone, user.created_at),
            )
            session.add(new_rows[key])
            stats["created"] += 1
        await session.flush()

        conclusion_rows: list[dict] = []
        for planned in plans:
            key = (planned.kind, planned.starts_on, planned.starts_at, planned.title)
            appointment_id = new_rows[key].id if key in new_rows else existing[key]
            for conclusion in planned.conclusions:
                if (appointment_id, conclusion.created_at) in have_conclusions:
                    stats["conclusions_existing"] += 1
                    continue
                conclusion_rows.append(
                    {
                        "appointment_id": appointment_id,
                        "text": conclusion.text,
                        "created_at": conclusion.created_at,
                    }
                )
                stats["conclusions_created"] += 1
        for start in range(0, len(conclusion_rows), _BATCH):
            await session.execute(
                insert(AppointmentConclusion), conclusion_rows[start : start + _BATCH]
            )
    return stats
