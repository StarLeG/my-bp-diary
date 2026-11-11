"""Демо-данные для скриншотов лендинга (SPEC.md, §16.1 «Как это выглядит»).

Только вымышленные данные и только БД тестового стенда (в имени файла —
«test-stand», `qa.seed.ensure_stand_db`): прод и `data/bot.sqlite3` не
трогаются. Состав:

- демо-пациент кабинета «Иван Петров» (`ivan.petrov@example.org`) — 30 дней
  истории по два замера в день, лекарства по слотам, журнал приёмов, связи
  с записями, напоминания утро/вечер;
- ещё три пациента с короткой историей — чтобы список у врача был не из одного;
- у всех четырёх — вымышленные ФИО, дата рождения и пол (§6.12): их видит врач;
- врач «Смирнова Елена Андреевна» (`smirnova@example.org`) с активным доступом ко
  всем четырём и заметкой для демо-пациента;
- пользователь бота с `--bot-tg-id` (аккаунт, под которым снимается Telegram
  Web) — та же вымышленная история, чтобы статистика и «· сегодня ✓» в кадрах
  бота были осмысленными; с `--visits` ему же сеются визиты и анализы
  (кадры 0.5.8), eve-напоминание приходит ближайшим тиком бота.

Пароли не хранятся: учётным записям ставится хеш случайного одноразового
секрета, а съёмка входит сессионной cookie (`shot_landing.py`).

Запуск (БД пересоздаётся):

    uv run python -m qa.screenshots.demo_data --db data/screens-test-stand.sqlite3 \\
        --bot-tg-id <telegram id аккаунта Telegram Web>
"""
from __future__ import annotations

import argparse
import asyncio
import random
import secrets
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from bot.db import create_engine, create_session_factory, init_db
from bot.models import (
    Appointment,
    Doctor,
    DoctorNote,
    DoctorPatient,
    Measurement,
    MeasurementMedication,
    Medication,
    MedicationLog,
    Reminder,
    User,
    WebUser,
)
from bot.services.reminders import next_slot_occurrence
from qa.seed.consents import ensure_doctor_consent, ensure_patient_consent
from qa.seed import ensure_stand_db
from web.cabinet import auth

TZ = "Europe/Minsk"
DAYS = 30

#: Демо-пациент кабинета (вымышленный): telegram_id, e-mail.
DEMO_PATIENT = (500_100_201, "ivan.petrov@example.org")
#: Остальные пациенты врача (вымышленные), короткая история.
OTHER_PATIENTS = (
    (500_100_202, "anna.k@example.org"),
    (500_100_203, "s.orlov@example.org"),
    (500_100_204, "m.volkova@example.org"),
)
#: Данные пациентов для врача (§6.12, вымышленные): ФИО, дата рождения, пол.
PERSONAL = {
    500_100_201: ("Петров", "Иван", "Сергеевич", date(1958, 3, 12), "m"),
    500_100_202: ("Ковалёва", "Анна", "Викторовна", date(1964, 7, 21), "f"),
    500_100_203: ("Орлов", "Сергей", "Петрович", date(1951, 11, 5), "m"),
    500_100_204: ("Волкова", "Мария", "Ивановна", date(1970, 2, 17), "f"),
}
DOCTOR = ("smirnova@example.org", "Смирнова Елена Андреевна", "Поликлиника № 1")

#: Лекарства демо-пациента: (название, дозировка, слот).
MEDICATIONS = (
    ("Лозартан", "50 мг", "morning"),
    ("Амлодипин", "5 мг", "evening"),
    ("Каптоприл", "25 мг", "crisis"),
)
SLOT_TIMES = {"morning": "08:00", "evening": "20:00"}

#: Визиты и анализы бот-пользователя (кадры 0.5.8):
#: (тип, название, смещение дней от «сегодня», время или None, заметка).
APPOINTMENTS = (
    ("doctor", "Кардиолог", 1, "10:00", "поликлиника № 5, каб. 212"),
    ("lab", "Общий анализ крови", 3, None, "натощак"),
    ("exam", "ЭКГ", 8, "09:30", None),
)
#: Прошедшее событие — кнопка «🗂 Прошедшие» в списке (статус «done», без тика).
APPOINTMENT_PAST = ("lab", "Биохимия", -6, "08:30", "холестерин и глюкоза")

DOCTOR_NOTE = (
    "Давление снижается — продолжайте Лозартан утром и Амлодипин вечером. "
    "Утренний замер — до приёма таблетки. Контроль через месяц."
)


def _utc(day: date, hhmm: time) -> datetime:
    """Локальные дата и время демо-пациента → aware UTC."""
    return datetime.combine(day, hhmm, tzinfo=ZoneInfo(TZ)).astimezone(timezone.utc)


def _pressure(rng: random.Random, progress: float, evening: bool) -> tuple[int, int, int]:
    """Правдоподобное давление: снижение на лечении от ~150/95 к ~132/84 с разбросом."""
    systolic = 150 - 18 * progress + rng.gauss(0, 6) + (3 if evening else 0)
    diastolic = 95 - 11 * progress + rng.gauss(0, 4)
    pulse = 72 + rng.gauss(0, 5)
    systolic = max(112, min(178, round(systolic)))
    diastolic = max(68, min(108, round(diastolic)))
    return systolic, min(diastolic, systolic - 25), max(54, min(96, round(pulse)))


def _tz_for_eve(now: datetime) -> str:
    """Часовой пояс, где сейчас только что наступили 20:00 (локальное время).

    Eve-напоминание «Завтра визит» — в 20:00 накануне (§6.13): подбор пояса
    делает его «только что прошедшим» в момент съёмки, тик бота шлёт его
    ближайшей минутой (окно catch-up 6 часов). В кадре пояс не виден.
    """
    utc_hour = now.astimezone(timezone.utc).hour
    offset = (20 - utc_hour) % 24
    if offset > 14:  # сдвиги Etc/GMT — от −12 до +14; ночь UTC — прошлый день
        offset -= 24
    if offset == 0:
        return "UTC"
    # Знак в имени Etc/GMT обратный: Etc/GMT-6 — это UTC+6.
    return f"Etc/GMT{'-' if offset > 0 else '+'}{abs(offset)}"


async def _visits(session, user: User, now: datetime) -> None:
    """Визиты и анализы бот-пользователя (кадры «Визиты и анализы», 0.5.8).

    Часовой пояс пользователя подбирается так, чтобы локальные 20:00 только что
    наступили: eve-напоминание «Завтра визит» срабатывает ближайшим тиком бота —
    кадр «Напоминание о визите» снимается сразу, а не дожидаясь вечера. Бота
    нужно запустить в течение 6 часов после сборки БД (окно catch-up).
    """
    tz_name = _tz_for_eve(now)
    user.timezone = tz_name
    today = now.astimezone(ZoneInfo(tz_name)).date()
    for kind, title, offset, hhmm, note in APPOINTMENTS:
        session.add(
            Appointment(
                user_id=user.id, kind=kind, title=title,
                starts_on=today + timedelta(days=offset), starts_at=hhmm,
                note=note, status="planned", created_at=now,
            )
        )
    kind, title, offset, hhmm, note = APPOINTMENT_PAST
    session.add(
        Appointment(
            user_id=user.id, kind=kind, title=title,
            starts_on=today + timedelta(days=offset), starts_at=hhmm,
            note=note, status="done", created_at=now,
        )
    )
    await session.flush()


async def _patient(
    session,
    telegram_id: int,
    *,
    days: int,
    rng: random.Random,
    now: datetime,
    with_meds: bool,
    remind_in_min: int | None = None,
) -> User:
    """Пациент с историей замеров; при `with_meds` — лекарства, журнал, связи, напоминания.

    `remind_in_min` (пользователь бота): сегодняшний утренний приём — «принято»
    (пометка «· сегодня ✓» в кадре мультивыбора), вечернее напоминание —
    через столько минут от `now` (кадр «Пора измерить давление»).
    """
    user = User(
        telegram_id=telegram_id,
        timezone=TZ,
        target_systolic=135,
        target_diastolic=85,
        med_remeasure_delay=60,
        onboarded=True,
    )
    if telegram_id in PERSONAL:
        (user.last_name, user.first_name, user.middle_name,
         user.birth_date, user.sex) = PERSONAL[telegram_id]
    session.add(user)
    await session.flush()
    await ensure_patient_consent(session, user.id)
    meds: dict[str, Medication] = {}
    if with_meds:
        for name, dosage, slot in MEDICATIONS:
            medication = Medication(user_id=user.id, name=name, dosage=dosage, slot=slot)
            session.add(medication)
            meds[slot] = medication
        for slot, local_time in SLOT_TIMES.items():
            next_time = next_slot_occurrence(local_time, TZ, now)
            if remind_in_min is not None and slot == "evening":
                next_time = now.replace(second=0, microsecond=0) + timedelta(minutes=remind_in_min)
                local_time = next_time.astimezone(ZoneInfo(TZ)).strftime("%H:%M")
            session.add(
                Reminder(
                    user_id=user.id,
                    type=slot,
                    local_time=local_time,
                    next_trigger_time=next_time,
                    enabled=True,
                    # Кадр «Напоминания» (0.5.6): утро — «Только лекарства».
                    mode="meds" if slot == "morning" else "both",
                )
            )
        await session.flush()

    today = now.astimezone(ZoneInfo(TZ)).date()
    crisis_day = today - timedelta(days=days - 9)
    for offset in range(days, -1, -1):
        day = today - timedelta(days=offset)
        progress = 1 - offset / days
        for evening in (False, True):
            hhmm = time(20, rng.randint(5, 50)) if evening else time(7, rng.randint(20, 55))
            moment = _utc(day, hhmm)
            if moment > now - timedelta(minutes=30):
                continue  # только прошедшие замеры
            systolic, diastolic, pulse = _pressure(rng, progress, evening)
            note = None
            feeling = None
            if with_meds and day == crisis_day and evening:
                systolic, diastolic, pulse, feeling, note = 176, 104, 88, "headache", "после стресса на работе"
            elif rng.random() < 0.12:
                feeling = rng.choice(("normal", "weakness"))
            slot = "evening" if evening else "morning"
            taken = with_meds and rng.random() < 0.9
            if remind_in_min is not None and day == today and not evening:
                taken = True  # «· сегодня ✓» у утреннего лекарства
            measurement = Measurement(
                user_id=user.id,
                timestamp=moment,
                systolic=systolic,
                diastolic=diastolic,
                pulse=pulse,
                medication_taken=True if taken else None,
                feeling=feeling,
                note=note,
            )
            session.add(measurement)
            await session.flush()
            if taken:
                taken_meds = [meds[slot]]
                if feeling == "headache":
                    taken_meds.append(meds["crisis"])
                for medication in taken_meds:
                    session.add(
                        MedicationLog(
                            user_id=user.id,
                            medication_id=medication.id,
                            timestamp=moment + timedelta(minutes=5),
                            status="taken",
                            slot=medication.slot,
                        )
                    )
                    session.add(
                        MeasurementMedication(
                            measurement_id=measurement.id, medication_id=medication.id
                        )
                    )
            elif with_meds:
                session.add(
                    MedicationLog(
                        user_id=user.id,
                        medication_id=meds[slot].id,
                        timestamp=moment + timedelta(minutes=5),
                        status="skipped",
                        slot=slot,
                    )
                )
    await session.flush()
    return user


async def build(
    db_path: Path, bot_tg_id: int, seed: int = 20261108, remind_in_min: int = 6,
    visits: bool = False,
) -> None:
    """Пересоздать демо-БД стенда (файл и его WAL/SHM удаляются).

    `visits` (кадры 0.5.8): бот-пользователю сеются визиты и анализы, а его
    часовой пояс подбирается так, чтобы eve-напоминание «Завтра визит»
    сработало ближайшим тиком бота (`_visits`).
    """
    ensure_stand_db(db_path)
    for suffix in ("", "-wal", "-shm"):
        Path(f"{db_path}{suffix}").unlink(missing_ok=True)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(str(db_path))
    await init_db(engine)
    factory = create_session_factory(engine)
    now = datetime.now(timezone.utc)
    unused_secret = auth.hash_password(secrets.token_urlsafe(24))
    async with factory() as session:
        demo = await _patient(
            session, DEMO_PATIENT[0], days=DAYS, rng=random.Random(seed), now=now, with_meds=True
        )
        session.add(WebUser(user_id=demo.id, email=DEMO_PATIENT[1], password_hash=unused_secret))
        patients = [demo]
        for index, (telegram_id, email) in enumerate(OTHER_PATIENTS, start=1):
            other = await _patient(
                session, telegram_id, days=7 + 4 * index, rng=random.Random(seed + index),
                now=now, with_meds=False,
            )
            session.add(WebUser(user_id=other.id, email=email, password_hash=unused_secret))
            patients.append(other)
        bot_user = await _patient(
            session, bot_tg_id, days=DAYS, rng=random.Random(seed), now=now, with_meds=True,
            remind_in_min=remind_in_min,
        )
        if visits:
            await _visits(session, bot_user, now)
        doctor = Doctor(
            email=DOCTOR[0], name=DOCTOR[1], clinic=DOCTOR[2], password_hash=unused_secret
        )
        ensure_doctor_consent(doctor)
        session.add(doctor)
        await session.flush()
        for days_ago, patient in zip((40, 26, 12, 5), patients):
            await ensure_patient_consent(session, patient.id, doctor.id)
            session.add(
                DoctorPatient(
                    doctor_id=doctor.id,
                    user_id=patient.id,
                    status="active",
                    created_at=now - timedelta(days=days_ago),
                )
            )
        session.add(
            DoctorNote(
                doctor_id=doctor.id,
                user_id=demo.id,
                text=DOCTOR_NOTE,
                created_at=now - timedelta(days=2),
            )
        )
        await session.commit()
    await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default="data/screens-test-stand.sqlite3")
    parser.add_argument("--bot-tg-id", type=int, required=True)
    parser.add_argument(
        "--remind-in", type=int, default=6,
        help="через сколько минут после сборки сработает вечернее напоминание бота",
    )
    parser.add_argument(
        "--visits", action="store_true",
        help="кадры «Визиты и анализы» (0.5.8): eve-напоминание «Завтра визит» сработает "
             "ближайшим тиком бота; слотовое напоминание отодвигается на 600 минут, чтобы не мешать",
    )
    args = parser.parse_args()
    asyncio.run(
        build(
            Path(args.db), args.bot_tg_id,
            remind_in_min=600 if args.visits else args.remind_in,
            visits=args.visits,
        )
    )
    print(f"Демо-БД готова: {args.db}")


if __name__ == "__main__":
    main()
