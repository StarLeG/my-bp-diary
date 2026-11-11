"""Засев демо-данными локального стенда (кабинет + бот, общая SQLite).

Запуск внутри контейнера:  python /tmp/seed_demo.py
Скрипт идемпотентен: демо-пользователь (telegram_id=900001) удаляется
со всеми данными (CASCADE) и создаётся заново.

Демо-вход в кабинет: demo@example.org / demopass123
"""
from __future__ import annotations

import random
import sqlite3
from datetime import date, datetime, time, timedelta
from pathlib import Path

from argon2 import PasswordHasher

from bot.services.consent import CONSENT_VERSION, DOCTOR_CONSENT_VERSION

DB_PATH = Path("/app/data/bot.sqlite3")
TZ = "Europe/Minsk"
TELEGRAM_ID = 900001
EMAIL = "demo@example.org"
PASSWORD = "demopass123"
DOCTOR_EMAIL = "doctor.sidorova@example.org"

TODAY = date(2026, 11, 11)  # локальный «сегодня» стенда
UTC = "%Y-%m-%d %H:%M:%S"


def utc(day: date, hour: int, minute: int = 0) -> str:
    """Локальное время Минска (UTC+3) → строка UTC для БД."""
    return (datetime.combine(day, time(hour, minute)) - timedelta(hours=3)).strftime(UTC)


def main() -> None:
    random.seed(42)  # воспроизводимый «шум» показателей
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("DELETE FROM users WHERE telegram_id = ?", (TELEGRAM_ID,))

    cur = con.execute(
        "INSERT INTO users (telegram_id, timezone, target_systolic, target_diastolic,"
        " med_remeasure_delay, onboarded, followup_enabled, created_at,"
        " last_name, first_name, middle_name, birth_date, sex)"
        " VALUES (?, ?, 130, 80, 60, 1, 1, ?, 'Тестов', 'Иван', 'Иванович', '1958-03-12', 'm')",
        (TELEGRAM_ID, TZ, utc(TODAY - timedelta(days=90), 12)),
    )
    user_id = cur.lastrowid

    con.execute(
        "INSERT INTO consents (user_id, kind, doctor_id, version, channel, given_at)"
        " VALUES (?, 'processing', NULL, ?, 'bot', ?)",
        (user_id, CONSENT_VERSION, utc(TODAY - timedelta(days=90), 12)),
    )
    con.execute(
        "INSERT INTO web_users (user_id, email, password_hash, created_at) VALUES (?, ?, ?, ?)",
        (user_id, EMAIL, PasswordHasher().hash(PASSWORD), utc(TODAY - timedelta(days=90), 12)),
    )

    meds: dict[str, int] = {}
    for name, dosage, slot in (
        ("Лозартан", "50 мг", "morning"),
        ("Амлодипин", "5 мг", "evening"),
        ("Каптоприл", "25 мг", "crisis"),
    ):
        cur = con.execute(
            "INSERT INTO medications (user_id, name, dosage, slot, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (user_id, name, dosage, slot, utc(TODAY - timedelta(days=60), 12)),
        )
        meds[name] = cur.lastrowid

    # ── Измерения за три месяца: утро ежедневно, вечер через день ──
    note_pool = ["после кофе", "мало спал", "после прогулки", "дождь, голова"]
    for back in range(90, -1, -1):
        day = TODAY - timedelta(days=back)
        base_sys = random.randint(120, 138)
        base_dia = random.randint(76, 88)
        if back in (3, 17):  # «красные» дни
            base_sys, base_dia = 148, 94
        if back in (8, 22):  # «жёлтые» дни
            base_sys, base_dia = 136, 86
        rows = [(utc(day, 7, 30), base_sys, base_dia, random.randint(58, 72), 1)]
        if back % 2 == 0:
            rows.append(
                (
                    utc(day, 21, 0),
                    base_sys + random.randint(-6, 8),
                    base_dia + random.randint(-4, 5),
                    random.randint(60, 78),
                    1,
                )
            )
        for ts, sys_, dia, pulse, taken in rows:
            feeling = "headache" if (back, sys_) == (17, 148) else None
            note = note_pool[back % len(note_pool)] if back in (3, 8, 17, 22) else ""
            cur = con.execute(
                "INSERT INTO measurements (user_id, timestamp, systolic, diastolic, pulse,"
                " medication_taken, feeling, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, ts, sys_, dia, pulse, taken, feeling, note),
            )
            measurement_id = cur.lastrowid
            if taken:
                con.execute(
                    "INSERT INTO measurement_medications (measurement_id, medication_id)"
                    " VALUES (?, ?)",
                    (measurement_id, meds["Лозартан"]),
                )
                con.execute(
                    "INSERT INTO medication_logs (user_id, medication_id, timestamp, status, slot)"
                    " VALUES (?, ?, ?, 'taken', 'morning')",
                    (user_id, meds["Лозартан"], ts),
                )

    # ── Визиты и анализы: все состояния нового UI ──
    appt = (
        "INSERT INTO appointments (user_id, kind, title, starts_on, starts_at, note, status,"
        " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
    )
    created = utc(TODAY - timedelta(days=40), 12)
    upcoming = [
        ("doctor", "Кардиолог", TODAY + timedelta(days=2), "10:30", "каб. 212\nвторой этаж, вход со двора"),
        ("lab", "Общий анализ крови", TODAY + timedelta(days=5), "07:30", "натощак, с собой паспорт"),
        ("exam", "ЭКГ", TODAY + timedelta(days=9), None, ""),
    ]
    for kind, title, day, at, note in upcoming:
        con.execute(appt, (user_id, kind, title, day.isoformat(), at, note, "planned", created))
    # «Ждёт отметки» (вчера) и «без отметки» (5 дней назад)
    con.execute(
        appt,
        (user_id, "doctor", "Терапевт", (TODAY - timedelta(days=1)).isoformat(), "11:00", "", "planned", created),
    )
    con.execute(
        appt,
        (user_id, "exam", "Окулист", (TODAY - timedelta(days=5)).isoformat(), "14:00", "проверка зрения", "planned", created),
    )
    # Прошедшие (прошлый месяц + этот)
    last_month_day = TODAY.replace(day=1) - timedelta(days=3)
    past = [
        ("exam", "УЗИ сердца", TODAY - timedelta(days=10), "09:00", "по направлению Кардиолога", "done"),
        ("lab", "Общий анализ крови", TODAY - timedelta(days=15), "07:00", "", "missed"),
        ("doctor", "Кардиолог", TODAY - timedelta(days=20), "10:00", "итоги обследования", "done"),
        ("doctor", "Невролог", TODAY - timedelta(days=25), "16:00", "", "cancelled"),
        ("doctor", "Нефролог", last_month_day, "12:00", "раз в полгода", "done"),
        ("exam", "СМАД", TODAY - timedelta(days=60), None, "суточный мониторинг", "done"),
        ("doctor", "Кардиолог", TODAY - timedelta(days=70), "10:00", "первичный приём", "done"),
    ]
    for kind, title, day, at, note, status in past:
        con.execute(appt, (user_id, kind, title, day.isoformat(), at, note, status, created))

    # ── Врач (кабинет врача): вход doctor.sidorova@example.org / demopass123 ──
    con.execute("DELETE FROM doctors WHERE email = ?", (DOCTOR_EMAIL,))
    cur = con.execute(
        "INSERT INTO doctors (email, telegram_id, telegram_username, name, clinic,"
        " password_hash, created_at, consent_version, consent_at, consent_channel)"
        " VALUES (?, NULL, NULL, 'Сидорова Анна Петровна', 'Поликлиника №2', ?, ?, ?, ?, 'cabinet')",
        (
            DOCTOR_EMAIL,
            PasswordHasher().hash(PASSWORD),
            created,
            DOCTOR_CONSENT_VERSION,
            created,
        ),
    )
    doctor_id = cur.lastrowid
    con.execute(
        "INSERT INTO doctor_patients (doctor_id, user_id, status, created_at)"
        " VALUES (?, ?, 'active', ?)",
        (doctor_id, user_id, created),
    )
    # Согласие пациента на этого врача (kind='doctor') — без него кабинет врача
    # пациента не показывает (access_clause).
    con.execute(
        "INSERT INTO consents (user_id, kind, doctor_id, version, channel, given_at)"
        " VALUES (?, 'doctor', ?, ?, 'cabinet', ?)",
        (user_id, doctor_id, CONSENT_VERSION, created),
    )
    for text, days_ago in (
        ("Контроль давления утром и вечером, дневник — на приём.", 12),
        ("Отменить Амлодипин до следующего визита не нужно — доза прежняя.", 4),
    ):
        con.execute(
            "INSERT INTO doctor_notes (doctor_id, user_id, text, created_at)"
            " VALUES (?, ?, ?, ?)",
            (doctor_id, user_id, text, utc(TODAY - timedelta(days=days_ago), 15)),
        )

    con.commit()
    counts = {
        table: con.execute(f"SELECT COUNT(*) FROM {table} WHERE user_id = ?", (user_id,)).fetchone()[0]
        for table in ("measurements", "medications", "appointments", "web_users", "consents")
    }
    con.close()
    print(f"user_id={user_id}, {counts}")


if __name__ == "__main__":
    main()
