"""Модели SQLAlchemy (SPEC.md, раздел 4).

Все моменты времени хранятся в UTC. Ограничения CHECK и UNIQUE продублированы
на уровне БД — защита данных при конкурентном доступе.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    """Текущий момент в UTC (единая точка получения «сейчас»)."""
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """Момент времени: в БД — UTC без tz (SQLite так умеет), наружу — aware UTC.

    SQLite не хранит tzinfo, поэтому столбцы `UTCDateTime()` при
    чтении теряют его, а сравнения с aware-значениями становятся ненадёжными.
    Декоратор конвертирует всё в UTC на входе и возвращает aware UTC на выходе;
    naive-значение трактуется как UTC.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class Base(DeclarativeBase):
    """Базовый класс декларативных моделей."""


class User(Base):
    """Пользователь Telegram (SPEC.md, раздел 4.1)."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Europe/Minsk")
    target_systolic: Mapped[int] = mapped_column(Integer, nullable=False, default=130)
    target_diastolic: Mapped[int] = mapped_column(Integer, nullable=False, default=80)
    med_remeasure_delay: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True, default=60
    )
    onboarded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_support_shown_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=_utcnow
    )

    __table_args__ = (
        CheckConstraint(
            "med_remeasure_delay IS NULL OR med_remeasure_delay IN (30, 60, 120)",
            name="ck_users_med_remeasure_delay",
        ),
    )


class Measurement(Base):
    """Измерение давления (SPEC.md, раздел 4.2)."""

    __tablename__ = "measurements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    systolic: Mapped[int] = mapped_column(Integer, nullable=False)
    diastolic: Mapped[int] = mapped_column(Integer, nullable=False)
    pulse: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    medication_taken: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    feeling: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_measurements_user_timestamp", "user_id", "timestamp"),
        CheckConstraint("systolic BETWEEN 70 AND 260", name="ck_measurements_systolic"),
        CheckConstraint("diastolic BETWEEN 40 AND 160", name="ck_measurements_diastolic"),
        CheckConstraint(
            "pulse IS NULL OR pulse BETWEEN 30 AND 220", name="ck_measurements_pulse"
        ),
        CheckConstraint(
            "feeling IS NULL OR feeling IN ('normal', 'headache', 'weakness', 'other')",
            name="ck_measurements_feeling",
        ),
        CheckConstraint(
            "note IS NULL OR length(note) <= 500", name="ck_measurements_note_length"
        ),
    )


class Reminder(Base):
    """Напоминание-слот или повтор замера (SPEC.md, раздел 4.3).

    Строк с типом `snooze` нет: «Через 30 мин» перезаписывает
    `next_trigger_time` существующей строки слота.
    """

    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    local_time: Mapped[Optional[str]] = mapped_column(String(5), nullable=True)
    next_trigger_time: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_sent_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    measurement_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("measurements.id", ondelete="CASCADE"), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "type IN ('morning', 'noon', 'evening', 'med_remeasure')",
            name="ck_reminders_type",
        ),
        CheckConstraint(
            "type = 'med_remeasure' OR local_time IS NOT NULL",
            name="ck_reminders_local_time",
        ),
        # Слоты: не более одной строки на (пользователь, слот).
        Index(
            "uq_reminders_user_slot",
            "user_id",
            "type",
            unique=True,
            sqlite_where=text("type != 'med_remeasure'"),
        ),
        # Повтор замера: не более одного на запись (идемпотентность).
        Index(
            "uq_reminders_measurement",
            "measurement_id",
            unique=True,
            sqlite_where=text("type = 'med_remeasure' AND measurement_id IS NOT NULL"),
        ),
    )


class Medication(Base):
    """Лекарство пользователя, привязанное к слоту (SPEC.md, раздел 4.4).

    Одно лекарство в двух слотах — две строки (UNIQUE по имени и слоту).
    """

    __tablename__ = "medications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    dosage: Mapped[str] = mapped_column(String(50), nullable=False)
    slot: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=_utcnow
    )

    __table_args__ = (
        UniqueConstraint("user_id", "name", "slot", name="uq_medications_user_name_slot"),
        CheckConstraint(
            "slot IN ('morning', 'noon', 'evening')", name="ck_medications_slot"
        ),
        CheckConstraint("length(name) BETWEEN 1 AND 100", name="ck_medications_name_length"),
        CheckConstraint(
            "length(dosage) BETWEEN 1 AND 50", name="ck_medications_dosage_length"
        ),
    )


class MedicationLog(Base):
    """Отметка приёма лекарства (SPEC.md, раздел 4.5).

    Детальный трекинг приёмов ведётся только здесь; `measurements.medication_taken`
    — быстрый флаг конкретного замера.
    """

    __tablename__ = "medication_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    medication_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("medications.id", ondelete="CASCADE"), nullable=False
    )
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    slot: Mapped[str] = mapped_column(String(16), nullable=False)

    __table_args__ = (
        Index("ix_medication_logs_user_timestamp", "user_id", "timestamp"),
        CheckConstraint(
            "status IN ('taken', 'skipped', 'partial')", name="ck_medication_logs_status"
        ),
    )


class WebUser(Base):
    """Учётная запись личного кабинета (SPEC.md, раздел 4.6).

    Пароль хранится только как хеш Argon2id.
    """

    __tablename__ = "web_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=_utcnow
    )


class WebSession(Base):
    """Сессия личного кабинета (SPEC.md, раздел 16.2).

    Сессионный токен хранится в SQLite и живёт 7 дней; сам токен —
    случайная строка, в HTTP-only cookie.
    """

    __tablename__ = "web_sessions"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    web_user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("web_users.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=_utcnow
    )
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
