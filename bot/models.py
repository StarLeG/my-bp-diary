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
    deletion_requested_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True
    )
    """Согласие отозвано в личном кабинете (§6.11, К5): аккаунт удалит бот (§19)."""

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
    Дозировка опциональна: пустая строка — «без дозировки». Слот `crisis` —
    препарат на случай кризиса: напоминаний нет и в приверженности не
    участвует (§6.7).
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
            "slot IN ('morning', 'noon', 'evening', 'crisis')", name="ck_medications_slot"
        ),
        CheckConstraint("length(name) BETWEEN 1 AND 100", name="ck_medications_name_length"),
        CheckConstraint(
            "length(dosage) BETWEEN 0 AND 50", name="ck_medications_dosage_length"
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


class MeasurementMedication(Base):
    """Связь «измерение ↔ отмеченные лекарства» (SPEC.md, раздел 4.11).

    Создаётся «Готово» мультивыбора «Какие лекарства приняли?» (§6.3):
    перечень отмеченных приёмов показывается в «Истории записей» кабинета
    (§16.2). Журнал приёмов за сутки — отдельно, в `medication_logs` (§4.5).
    """

    __tablename__ = "measurement_medications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    measurement_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("measurements.id", ondelete="CASCADE"), nullable=False
    )
    medication_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("medications.id", ondelete="CASCADE"), nullable=False
    )

    __table_args__ = (
        # Один приём отмечается у записи один раз (идемпотентность «Готово»).
        UniqueConstraint(
            "measurement_id", "medication_id", name="uq_measurement_medications_pair"
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


class Doctor(Base):
    """Врач, который может просматривать данные пациентов (SPEC.md, раздел 4.8).

    Пароль хранится только как хеш Argon2id; запись создаётся ботом при
    добавлении врача пациентом (пароль — случайный, вход по ссылке).
    Врач опознаётся e-mail (приглашение по почте) или Telegram-аккаунтом
    (приглашение в бота, `/iamdoctor`) — хотя бы одним из них (CHECK).
    Записи одного врача, добавленного разными путями, не объединяются.
    """

    __tablename__ = "doctors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, unique=True)
    telegram_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, unique=True)
    telegram_username: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    clinic: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=_utcnow
    )
    consent_version: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    """Версия согласия врача (`DOCTOR_CONSENT_VERSION`, §4.8, §6.11); NULL — нет."""
    consent_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime(), nullable=True)
    """Когда врач дал согласие (UTC)."""
    consent_channel: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    """Где дано согласие врача: `bot` (Б8) или `cabinet` (К2)."""

    __table_args__ = (
        CheckConstraint("length(name) BETWEEN 1 AND 100", name="ck_doctors_name_length"),
        CheckConstraint(
            "consent_channel IS NULL OR consent_channel IN ('bot', 'cabinet')",
            name="ck_doctors_consent_channel",
        ),
        CheckConstraint(
            "clinic IS NULL OR length(clinic) <= 100", name="ck_doctors_clinic_length"
        ),
        CheckConstraint(
            "email IS NOT NULL OR telegram_id IS NOT NULL", name="ck_doctors_identity"
        ),
        Index("ix_doctors_telegram_username", "telegram_username"),
    )


class DoctorPatient(Base):
    """Связь врач–пациент: право доступа к данным (SPEC.md, раздел 4.9).

    Статусы: `pending` — пациент добавил врача, `active` — врач открыл
    ссылку-приглашение (доступ открыт), `revoked` — пациент отозвал доступ.
    """

    __tablename__ = "doctor_patients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    doctor_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("doctors.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=_utcnow
    )

    __table_args__ = (
        UniqueConstraint("doctor_id", "user_id", name="uq_doctor_patients_doctor_user"),
        CheckConstraint(
            "status IN ('pending', 'active', 'revoked')",
            name="ck_doctor_patients_status",
        ),
    )


class DoctorNote(Base):
    """Заметка врача пациенту (SPEC.md, раздел 4.10).

    Хранится в БД и видна пациенту в его личном кабинете.
    """

    __tablename__ = "doctor_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    doctor_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("doctors.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=_utcnow
    )

    __table_args__ = (
        CheckConstraint("length(text) BETWEEN 1 AND 1000", name="ck_doctor_notes_text_length"),
    )


class InviteLog(Base):
    """Учёт приглашений врача — лимиты и админка (SPEC.md, раздел 4.12).

    Письма (`channel='email'`) и приглашения по @username (`channel='telegram'`)
    — в одной таблице: лимиты 3 в сутки на пациента и 1 в час на `target`
    считаются по ней для обоих каналов (§21). Строки удаляются вместе с
    пациентом (§19).
    """

    __tablename__ = "invite_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    target: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=_utcnow
    )

    __table_args__ = (
        CheckConstraint("channel IN ('email', 'telegram')", name="ck_invite_log_channel"),
        CheckConstraint("kind IN ('invite', 'report')", name="ck_invite_log_kind"),
        CheckConstraint("length(target) BETWEEN 1 AND 255", name="ck_invite_log_target"),
        Index("ix_invite_log_user_created", "user_id", "created_at"),
        Index("ix_invite_log_target_created", "target", "created_at"),
    )


class UsedToken(Base):
    """Списанные одноразовые ссылки кабинета врача (SPEC.md, раздел 4.14).

    Списание в БД, а не в памяти процесса: ссылка доступа живёт 72 часа, и
    перезапуск кабинета (деплой) не должен снова делать её рабочей.
    Просроченные строки чистятся при старте кабинета.
    """

    __tablename__ = "used_tokens"

    jti: Mapped[str] = mapped_column(String(64), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class DoctorInvite(Base):
    """Ссылка-приглашение врача в бота `?start=doc_<code>` (SPEC.md, раздел 4.13).

    72 часа, одноразовая: `used_at` ставится при «Принять» / «Отклонить».
    Новое приглашение пациента списывает его прежние неиспользованные коды.
    `patient_name` — имя пациента из Telegram на момент приглашения: врач
    открывает ссылку позже, и имени пациента у бота в этот момент нет.
    """

    __tablename__ = "doctor_invites"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    patient_name: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=_utcnow
    )
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    used_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime(), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "length(patient_name) BETWEEN 1 AND 64", name="ck_doctor_invites_patient_name"
        ),
        Index("ix_doctor_invites_user", "user_id"),
    )


class Consent(Base):
    """Согласие пациента на обработку персональных данных (SPEC.md, §4.15, §6.11).

    `processing` — основное согласие (ведение дневника), `doctor` — на
    предоставление данных врачу. Запись — доказательство согласия (ст. 5 п. 7
    Закона № 99-З). Действующая — `withdrawn_at IS NULL`; частичный уникальный
    индекс не даёт двойному «Согласен» создать вторую действующую запись.
    Удаляется вместе с пациентом (§19).
    """

    __tablename__ = "consents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    doctor_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("doctors.id", ondelete="CASCADE"), nullable=True
    )
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    given_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=_utcnow)
    withdrawn_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime(), nullable=True)

    __table_args__ = (
        CheckConstraint("kind IN ('processing', 'doctor')", name="ck_consents_kind"),
        CheckConstraint("channel IN ('bot', 'cabinet')", name="ck_consents_channel"),
        CheckConstraint(
            "kind = 'doctor' OR doctor_id IS NULL", name="ck_consents_processing_no_doctor"
        ),
        Index(
            "uq_consents_open",
            "user_id",
            "kind",
            text("COALESCE(doctor_id, 0)"),
            unique=True,
            sqlite_where=text("withdrawn_at IS NULL"),
        ),
        Index("ix_consents_doctor", "doctor_id"),
    )
