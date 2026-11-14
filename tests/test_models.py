"""Тесты моделей SQLAlchemy (bot/models.py): CHECK, UNIQUE, nullable, CASCADE."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import (
    DiaryEvent,
    Doctor,
    DoctorNote,
    DoctorPatient,
    Measurement,
    Medication,
    MedicationLog,
    Reminder,
    User,
    WebSession,
    WebUser,
)

UTC = timezone.utc


def make_user(telegram_id: int = 111_111) -> User:
    """Пользователь с настройками по умолчанию."""
    return User(telegram_id=telegram_id, timezone="Europe/Minsk")


def make_measurement(user_id: int, **overrides) -> Measurement:
    """Валидное измерение с возможностью подменить поля."""
    data = {
        "user_id": user_id,
        "timestamp": datetime(2026, 11, 4, 8, 30, tzinfo=UTC),
        "systolic": 120,
        "diastolic": 80,
        "pulse": 60,
    }
    data.update(overrides)
    return Measurement(**data)


def make_doctor(email: str = "doc@example.org", **overrides) -> Doctor:
    """Валидная запись врача с возможностью подменить поля (§4.8)."""
    data = {"email": email, "name": "Врач", "password_hash": "$argon2id$fake"}
    data.update(overrides)
    return Doctor(**data)


async def _flush_fails(session: AsyncSession) -> None:
    """Ожидать IntegrityError при сохранении текущих изменений."""
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


class TestUTCDateTime:
    """Поведение UTCDateTime: в БД — naive UTC, наружу — aware UTC."""

    def _type(self):
        from bot.models import UTCDateTime

        return UTCDateTime()

    def test_bind_none(self) -> None:
        assert self._type().process_bind_param(None, None) is None

    def test_bind_naive_kept(self) -> None:
        value = datetime(2026, 11, 4, 8, 30)
        assert self._type().process_bind_param(value, None) == value

    def test_bind_aware_converted_to_utc_naive(self) -> None:
        from datetime import timedelta, timezone as tz

        value = datetime(2026, 11, 4, 11, 30, tzinfo=tz(timedelta(hours=3)))
        assert self._type().process_bind_param(value, None) == datetime(2026, 11, 4, 8, 30)

    def test_result_none(self) -> None:
        assert self._type().process_result_value(None, None) is None

    def test_result_naive_becomes_utc(self) -> None:
        result = self._type().process_result_value(datetime(2026, 11, 4, 8, 30), None)
        assert result == datetime(2026, 11, 4, 8, 30, tzinfo=UTC)
        assert result.tzinfo is not None

    def test_result_aware_converted_to_utc(self) -> None:
        from datetime import timedelta, timezone as tz

        value = datetime(2026, 11, 4, 11, 30, tzinfo=tz(timedelta(hours=3)))
        result = self._type().process_result_value(value, None)
        assert result == datetime(2026, 11, 4, 8, 30, tzinfo=UTC)

    async def test_roundtrip_with_non_utc_timestamp(self, session: AsyncSession, user: User) -> None:
        # Запись с локальным временем (+3) хранится в UTC и читается aware UTC.
        from datetime import timedelta, timezone as tz

        local = datetime(2026, 11, 4, 11, 30, tzinfo=tz(timedelta(hours=3)))
        session.add(make_measurement(user.id, timestamp=local))
        await session.commit()
        row = (
            await session.execute(select(Measurement))
        ).scalars().one()
        assert row.timestamp == datetime(2026, 11, 4, 8, 30, tzinfo=UTC)


class TestUser:
    async def test_defaults(self, session: AsyncSession) -> None:
        user = make_user()
        session.add(user)
        await session.commit()
        assert user.timezone == "Europe/Minsk"
        assert user.target_systolic == 130
        assert user.target_diastolic == 80
        assert user.med_remeasure_delay == 60
        assert user.onboarded is False
        assert user.last_support_shown_at is None
        assert user.created_at.tzinfo is not None

    async def test_telegram_id_unique(self, session: AsyncSession) -> None:
        session.add(make_user(1))
        await session.commit()
        session.add(make_user(1))
        await _flush_fails(session)

    async def test_telegram_id_other_user_ok(self, session: AsyncSession) -> None:
        session.add(make_user(1))
        session.add(make_user(2))
        await session.commit()

    @pytest.mark.parametrize("delay", [30, 60, 120, None])
    async def test_med_remeasure_delay_valid(self, session: AsyncSession, delay) -> None:
        session.add(make_user(1))
        user = make_user(2)
        user.med_remeasure_delay = delay
        session.add(user)
        await session.commit()

    @pytest.mark.parametrize("delay", [0, 45, 90, 180, -60])
    async def test_med_remeasure_delay_invalid(self, session: AsyncSession, delay: int) -> None:
        user = make_user()
        user.med_remeasure_delay = delay
        session.add(user)
        await _flush_fails(session)


class TestMeasurement:
    async def test_insert_valid(self, session: AsyncSession, user: User) -> None:
        session.add(make_measurement(user.id))
        await session.commit()

    @pytest.mark.parametrize("systolic", [70, 260])
    async def test_systolic_boundaries_ok(self, session: AsyncSession, user: User, systolic: int) -> None:
        session.add(make_measurement(user.id, systolic=systolic))
        await session.commit()

    @pytest.mark.parametrize("systolic", [69, 261, 0])
    async def test_systolic_out_of_range(self, session: AsyncSession, user: User, systolic: int) -> None:
        session.add(make_measurement(user.id, systolic=systolic))
        await _flush_fails(session)

    @pytest.mark.parametrize("diastolic", [40, 160])
    async def test_diastolic_boundaries_ok(self, session: AsyncSession, user: User, diastolic: int) -> None:
        session.add(make_measurement(user.id, diastolic=diastolic))
        await session.commit()

    @pytest.mark.parametrize("diastolic", [39, 161])
    async def test_diastolic_out_of_range(self, session: AsyncSession, user: User, diastolic: int) -> None:
        session.add(make_measurement(user.id, diastolic=diastolic))
        await _flush_fails(session)

    async def test_pulse_null_ok(self, session: AsyncSession, user: User) -> None:
        session.add(make_measurement(user.id, pulse=None))
        await session.commit()

    @pytest.mark.parametrize("pulse", [30, 220])
    async def test_pulse_boundaries_ok(self, session: AsyncSession, user: User, pulse: int) -> None:
        session.add(make_measurement(user.id, pulse=pulse))
        await session.commit()

    @pytest.mark.parametrize("pulse", [29, 221])
    async def test_pulse_out_of_range(self, session: AsyncSession, user: User, pulse: int) -> None:
        session.add(make_measurement(user.id, pulse=pulse))
        await _flush_fails(session)

    @pytest.mark.parametrize("feeling", ["normal", "headache", "weakness", "other", None])
    async def test_feeling_valid(self, session: AsyncSession, user: User, feeling) -> None:
        session.add(make_measurement(user.id, feeling=feeling))
        await session.commit()

    @pytest.mark.parametrize("feeling", ["great", "NORMAL", ""])
    async def test_feeling_invalid(self, session: AsyncSession, user: User, feeling: str) -> None:
        session.add(make_measurement(user.id, feeling=feeling))
        await _flush_fails(session)

    async def test_note_length_limits(self, session: AsyncSession, user: User) -> None:
        session.add(make_measurement(user.id, note="x" * 500))
        await session.commit()
        session.add(make_measurement(user.id, note="y" * 501))
        await _flush_fails(session)

    async def test_medication_taken_nullable(self, session: AsyncSession, user: User) -> None:
        # None = не спрашивали / пропустил.
        session.add(make_measurement(user.id, medication_taken=None))
        session.add(make_measurement(user.id, medication_taken=True))
        session.add(make_measurement(user.id, medication_taken=False))
        await session.commit()

    async def test_cascade_delete_user(self, session: AsyncSession, user: User) -> None:
        session.add(make_measurement(user.id))
        await session.commit()
        await session.delete(user)
        await session.commit()
        count = (
            await session.execute(
                Measurement.__table__.select().with_only_columns(Measurement.id)
            )
        ).all()
        assert count == []


class TestReminder:
    async def _slot(self, user_id: int, slot: str = "morning") -> Reminder:
        return Reminder(
            user_id=user_id,
            type=slot,
            local_time="08:00",
            next_trigger_time=datetime(2026, 11, 5, 6, 0, tzinfo=UTC),
        )

    async def test_slot_valid(self, session: AsyncSession, user: User) -> None:
        session.add(await self._slot(user.id))
        await session.commit()

    @pytest.mark.parametrize("rtype", ["morning", "noon", "evening"])
    async def test_slot_types_ok(self, session: AsyncSession, user: User, rtype: str) -> None:
        session.add(await self._slot(user.id, rtype))
        await session.commit()

    @pytest.mark.parametrize("rtype", ["snooze", "daily", ""])
    async def test_type_invalid(self, session: AsyncSession, user: User, rtype: str) -> None:
        session.add(await self._slot(user.id, rtype))
        await _flush_fails(session)

    async def test_slot_requires_local_time(self, session: AsyncSession, user: User) -> None:
        reminder = await self._slot(user.id)
        reminder.local_time = None
        session.add(reminder)
        await _flush_fails(session)

    async def test_med_remeasure_allows_null_local_time(self, session: AsyncSession, user: User) -> None:
        session.add(
            Reminder(
                user_id=user.id,
                type="med_remeasure",
                local_time=None,
                next_trigger_time=datetime(2026, 11, 4, 9, 30, tzinfo=UTC),
            )
        )
        await session.commit()

    async def test_one_row_per_user_slot(self, session: AsyncSession, user: User) -> None:
        session.add(await self._slot(user.id, "morning"))
        await session.commit()
        session.add(await self._slot(user.id, "morning"))
        await _flush_fails(session)

    async def test_same_slot_different_users_ok(self, session: AsyncSession, user: User) -> None:
        other = make_user(222_222)
        session.add(other)
        await session.commit()
        session.add(await self._slot(user.id, "morning"))
        session.add(await self._slot(other.id, "morning"))
        await session.commit()

    async def test_med_remeasure_unique_per_measurement(
        self, session: AsyncSession, user: User
    ) -> None:
        measurement = make_measurement(user.id)
        session.add(measurement)
        await session.commit()
        for _ in range(2):
            session.add(
                Reminder(
                    user_id=user.id,
                    type="med_remeasure",
                    local_time=None,
                    next_trigger_time=datetime(2026, 11, 4, 9, 30, tzinfo=UTC),
                    measurement_id=measurement.id,
                )
            )
        await _flush_fails(session)

    async def test_med_remeasure_without_measurement_ok(
        self, session: AsyncSession, user: User
    ) -> None:
        # Несколько строк med_remeasure без measurement_id допустимы.
        for hour in (9, 10):
            session.add(
                Reminder(
                    user_id=user.id,
                    type="med_remeasure",
                    local_time=None,
                    next_trigger_time=datetime(2026, 11, 4, hour, 30, tzinfo=UTC),
                    measurement_id=None,
                )
            )
        await session.commit()

    async def test_default_enabled(self, session: AsyncSession, user: User) -> None:
        reminder = await self._slot(user.id)
        session.add(reminder)
        await session.commit()
        assert reminder.enabled is True
        assert reminder.last_sent_date is None

    async def test_last_sent_date_type(self, session: AsyncSession, user: User) -> None:
        reminder = await self._slot(user.id)
        reminder.last_sent_date = date(2026, 11, 4)
        session.add(reminder)
        await session.commit()


class TestMedication:
    def _med(self, user_id: int, name: str = "Лозартан", slot: str = "morning") -> Medication:
        return Medication(user_id=user_id, name=name, dosage="50 мг", slot=slot)

    async def test_insert_valid(self, session: AsyncSession, user: User) -> None:
        session.add(self._med(user.id))
        await session.commit()

    async def test_unique_user_name_slot(self, session: AsyncSession, user: User) -> None:
        session.add(self._med(user.id))
        await session.commit()
        session.add(self._med(user.id))
        await _flush_fails(session)

    async def test_same_name_different_slots_ok(self, session: AsyncSession, user: User) -> None:
        # Лекарство в двух слотах — две строки.
        session.add(self._med(user.id, slot="morning"))
        session.add(self._med(user.id, slot="evening"))
        await session.commit()

    async def test_same_name_other_user_ok(self, session: AsyncSession, user: User) -> None:
        other = make_user(222_222)
        session.add(other)
        await session.commit()
        session.add(self._med(user.id))
        session.add(self._med(other.id))
        await session.commit()

    @pytest.mark.parametrize("slot", ["night", "morning ", ""])
    async def test_slot_invalid(self, session: AsyncSession, user: User, slot: str) -> None:
        session.add(self._med(user.id, slot=slot))
        await _flush_fails(session)

    async def test_slot_crisis_ok(self, session: AsyncSession, user: User) -> None:
        """Слот «Кризис» — препарат на случай кризиса (§4.4)."""
        session.add(self._med(user.id, slot="crisis"))
        await session.commit()

    @pytest.mark.parametrize("name", ["", "x" * 101])
    async def test_name_length_invalid(self, session: AsyncSession, user: User, name: str) -> None:
        session.add(self._med(user.id, name=name))
        await _flush_fails(session)

    async def test_name_length_boundary_ok(self, session: AsyncSession, user: User) -> None:
        session.add(self._med(user.id, name="x" * 100))
        await session.commit()

    @pytest.mark.parametrize("dosage", ["x" * 51])
    async def test_dosage_length_invalid(self, session: AsyncSession, user: User, dosage: str) -> None:
        session.add(Medication(user_id=user.id, name="Амлодипин", dosage=dosage, slot="morning"))
        await _flush_fails(session)

    async def test_dosage_empty_ok(self, session: AsyncSession, user: User) -> None:
        """Дозировка опциональна: пустая строка — «без дозировки» (§4.4)."""
        session.add(Medication(user_id=user.id, name="Амлодипин", dosage="", slot="morning"))
        await session.commit()

    async def test_dosage_length_boundary_ok(self, session: AsyncSession, user: User) -> None:
        session.add(Medication(user_id=user.id, name="Амлодипин", dosage="x" * 50, slot="morning"))
        await session.commit()


class TestMedicationLog:
    async def _log_kwargs(self, user_id: int, medication_id: int, **overrides) -> dict:
        data = {
            "user_id": user_id,
            "medication_id": medication_id,
            "timestamp": datetime(2026, 11, 4, 8, 35, tzinfo=UTC),
            "status": "taken",
            "slot": "morning",
        }
        data.update(overrides)
        return data

    async def _medication(self, session: AsyncSession, user: User) -> Medication:
        med = Medication(user_id=user.id, name="Лозартан", dosage="50 мг", slot="morning")
        session.add(med)
        await session.commit()
        return med

    @pytest.mark.parametrize("status", ["taken", "skipped", "partial"])
    async def test_status_valid(self, session: AsyncSession, user: User, status: str) -> None:
        med = await self._medication(session, user)
        session.add(MedicationLog(**await self._log_kwargs(user.id, med.id, status=status)))
        await session.commit()

    @pytest.mark.parametrize("status", ["missed", ""])
    async def test_status_invalid(self, session: AsyncSession, user: User, status: str) -> None:
        med = await self._medication(session, user)
        session.add(MedicationLog(**await self._log_kwargs(user.id, med.id, status=status)))
        await _flush_fails(session)

    async def test_cascade_delete_medication(self, session: AsyncSession, user: User) -> None:
        med = await self._medication(session, user)
        session.add(MedicationLog(**await self._log_kwargs(user.id, med.id)))
        await session.commit()
        await session.delete(med)
        await session.commit()
        logs = (await session.execute(MedicationLog.__table__.select())).all()
        assert logs == []


class TestWebUser:
    async def test_insert_valid(self, session: AsyncSession, user: User) -> None:
        session.add(WebUser(user_id=user.id, email="a@example.org", password_hash="$argon2id$fake"))
        await session.commit()

    async def test_user_id_unique(self, session: AsyncSession, user: User) -> None:
        session.add(WebUser(user_id=user.id, email="a@example.org", password_hash="h1"))
        await session.commit()
        session.add(WebUser(user_id=user.id, email="b@example.org", password_hash="h2"))
        await _flush_fails(session)

    async def test_email_unique(self, session: AsyncSession, user: User) -> None:
        other = make_user(222_222)
        session.add(other)
        await session.commit()
        session.add(WebUser(user_id=user.id, email="a@example.org", password_hash="h1"))
        await session.commit()
        session.add(WebUser(user_id=other.id, email="a@example.org", password_hash="h2"))
        await _flush_fails(session)

    async def test_cascade_delete_user(self, session: AsyncSession, user: User) -> None:
        session.add(WebUser(user_id=user.id, email="a@example.org", password_hash="h"))
        await session.commit()
        await session.delete(user)
        await session.commit()
        rows = (await session.execute(WebUser.__table__.select())).all()
        assert rows == []


class TestWebSession:
    """Сессии личного кабинета: хранятся в SQLite, каскадное удаление (§16.2)."""

    async def _make_web_user(self, session: AsyncSession, user: User) -> WebUser:
        record = WebUser(user_id=user.id, email="a@example.org", password_hash="h")
        session.add(record)
        await session.commit()
        return record

    async def test_insert_valid(self, session: AsyncSession, user: User) -> None:
        web_user = await self._make_web_user(session, user)
        now = datetime.now(UTC)
        session.add(
            WebSession(
                token="t" * 32,
                web_user_id=web_user.id,
                created_at=now,
                expires_at=now + timedelta(days=7),
            )
        )
        await session.commit()

    async def test_cascade_delete_web_user(self, session: AsyncSession, user: User) -> None:
        web_user = await self._make_web_user(session, user)
        now = datetime.now(UTC)
        session.add(
            WebSession(
                token="t" * 32,
                web_user_id=web_user.id,
                created_at=now,
                expires_at=now + timedelta(days=7),
            )
        )
        await session.commit()
        await session.delete(web_user)
        await session.commit()
        rows = (await session.execute(WebSession.__table__.select())).all()
        assert rows == []


class TestDoctor:
    """Врачи (SPEC.md, раздел 4.8): UNIQUE e-mail, длина имени и клиники."""

    async def test_insert_valid(self, session: AsyncSession) -> None:
        doctor = make_doctor()
        session.add(doctor)
        await session.commit()
        assert doctor.created_at.tzinfo is not None
        assert doctor.clinic is None

    async def test_email_unique(self, session: AsyncSession) -> None:
        session.add(make_doctor("a@example.org"))
        await session.commit()
        session.add(make_doctor("a@example.org", name="Другой"))
        await _flush_fails(session)

    async def test_email_other_doctor_ok(self, session: AsyncSession) -> None:
        session.add(make_doctor("a@example.org"))
        session.add(make_doctor("b@example.org"))
        await session.commit()

    @pytest.mark.parametrize("name", ["", "x" * 101])
    async def test_name_length_invalid(self, session: AsyncSession, name: str) -> None:
        session.add(make_doctor(name=name))
        await _flush_fails(session)

    async def test_name_length_boundary_ok(self, session: AsyncSession) -> None:
        session.add(make_doctor(name="x" * 100))
        await session.commit()

    async def test_clinic_null_ok(self, session: AsyncSession) -> None:
        session.add(make_doctor(clinic=None))
        await session.commit()

    async def test_clinic_length_boundary_ok(self, session: AsyncSession) -> None:
        session.add(make_doctor(clinic="x" * 100))
        await session.commit()

    async def test_clinic_too_long(self, session: AsyncSession) -> None:
        session.add(make_doctor(clinic="x" * 101))
        await _flush_fails(session)


class TestDoctorPatient:
    """Связи врач–пациент (SPEC.md, раздел 4.9): статусы и UNIQUE пары."""

    async def _doctor(self, session: AsyncSession) -> Doctor:
        doctor = make_doctor()
        session.add(doctor)
        await session.commit()
        return doctor

    def _link(self, doctor_id: int, user_id: int, **overrides) -> DoctorPatient:
        data = {"doctor_id": doctor_id, "user_id": user_id}
        data.update(overrides)
        return DoctorPatient(**data)

    async def test_default_status_pending(self, session: AsyncSession, user: User) -> None:
        doctor = await self._doctor(session)
        link = self._link(doctor.id, user.id)
        session.add(link)
        await session.commit()
        assert link.status == "pending"
        assert link.created_at.tzinfo is not None

    @pytest.mark.parametrize("status", ["pending", "active", "revoked"])
    async def test_status_valid(
        self, session: AsyncSession, user: User, status: str
    ) -> None:
        doctor = await self._doctor(session)
        session.add(self._link(doctor.id, user.id, status=status))
        await session.commit()

    @pytest.mark.parametrize("status", ["deleted", "PENDING", ""])
    async def test_status_invalid(
        self, session: AsyncSession, user: User, status: str
    ) -> None:
        doctor = await self._doctor(session)
        session.add(self._link(doctor.id, user.id, status=status))
        await _flush_fails(session)

    async def test_unique_pair(self, session: AsyncSession, user: User) -> None:
        doctor = await self._doctor(session)
        session.add(self._link(doctor.id, user.id))
        await session.commit()
        session.add(self._link(doctor.id, user.id))
        await _flush_fails(session)

    async def test_same_doctor_other_patient_ok(self, session: AsyncSession, user: User) -> None:
        other = make_user(222_222)
        session.add(other)
        await session.commit()
        doctor = await self._doctor(session)
        session.add(self._link(doctor.id, user.id))
        session.add(self._link(doctor.id, other.id))
        await session.commit()

    async def test_cascade_delete_user(self, session: AsyncSession, user: User) -> None:
        doctor = await self._doctor(session)
        session.add(self._link(doctor.id, user.id))
        await session.commit()
        await session.delete(user)
        await session.commit()
        rows = (await session.execute(DoctorPatient.__table__.select())).all()
        assert rows == []

    async def test_cascade_delete_doctor(self, session: AsyncSession, user: User) -> None:
        doctor = await self._doctor(session)
        session.add(self._link(doctor.id, user.id))
        await session.commit()
        await session.delete(doctor)
        await session.commit()
        rows = (await session.execute(DoctorPatient.__table__.select())).all()
        assert rows == []


class TestDoctorNote:
    """Заметки врача (SPEC.md, раздел 4.10): длина текста и каскады."""

    async def _doctor(self, session: AsyncSession) -> Doctor:
        doctor = make_doctor()
        session.add(doctor)
        await session.commit()
        return doctor

    def _note(self, doctor_id: int, user_id: int, text: str = "Продолжайте измерения") -> DoctorNote:
        return DoctorNote(doctor_id=doctor_id, user_id=user_id, text=text)

    async def test_insert_valid(self, session: AsyncSession, user: User) -> None:
        doctor = await self._doctor(session)
        note = self._note(doctor.id, user.id)
        session.add(note)
        await session.commit()
        assert note.created_at.tzinfo is not None

    async def test_text_empty_invalid(self, session: AsyncSession, user: User) -> None:
        doctor = await self._doctor(session)
        session.add(self._note(doctor.id, user.id, text=""))
        await _flush_fails(session)

    async def test_text_boundary_ok(self, session: AsyncSession, user: User) -> None:
        doctor = await self._doctor(session)
        session.add(self._note(doctor.id, user.id, text="x" * 1000))
        await session.commit()

    async def test_text_too_long(self, session: AsyncSession, user: User) -> None:
        doctor = await self._doctor(session)
        session.add(self._note(doctor.id, user.id, text="x" * 1001))
        await _flush_fails(session)

    async def test_cascade_delete_user(self, session: AsyncSession, user: User) -> None:
        doctor = await self._doctor(session)
        session.add(self._note(doctor.id, user.id))
        await session.commit()
        await session.delete(user)
        await session.commit()
        rows = (await session.execute(DoctorNote.__table__.select())).all()
        assert rows == []

    async def test_cascade_delete_doctor(self, session: AsyncSession, user: User) -> None:
        doctor = await self._doctor(session)
        session.add(self._note(doctor.id, user.id))
        await session.commit()
        await session.delete(doctor)
        await session.commit()
        rows = (await session.execute(DoctorNote.__table__.select())).all()
        assert rows == []


class TestDiaryEvent:
    """Событие дневника (SPEC.md, §4.18; 0.6.0): CHECK, nullable, каскады."""

    @staticmethod
    def _event(user_id: int, **overrides) -> DiaryEvent:
        data = {
            "user_id": user_id,
            "type": "food",
            "moment": datetime(2026, 11, 14, 13, 20, tzinfo=UTC),
            "payload": '{"marks": ["salt"]}',
        }
        data.update(overrides)
        return DiaryEvent(**data)

    async def test_insert_valid(self, session: AsyncSession, user: User) -> None:
        event = self._event(user.id)
        session.add(event)
        await session.commit()
        assert event.created_at.tzinfo is not None
        assert event.measurement_id is None

    @pytest.mark.parametrize(
        "bad_type", ["meal", "", "FOOD", "note ", None]
    )
    async def test_type_invalid(self, session: AsyncSession, user: User, bad_type) -> None:
        session.add(self._event(user.id, type=bad_type))
        await _flush_fails(session)

    @pytest.mark.parametrize("good_type", ["food", "stress", "sleep", "activity", "note"])
    async def test_type_valid(self, session: AsyncSession, user: User, good_type: str) -> None:
        session.add(self._event(user.id, type=good_type))
        await session.commit()

    async def test_note_boundary_ok(self, session: AsyncSession, user: User) -> None:
        session.add(self._event(user.id, note="х" * 500))
        await session.commit()

    async def test_note_too_long(self, session: AsyncSession, user: User) -> None:
        session.add(self._event(user.id, note="х" * 501))
        await _flush_fails(session)

    async def test_cascade_delete_user(self, session: AsyncSession, user: User) -> None:
        session.add(self._event(user.id))
        await session.commit()
        await session.delete(user)
        await session.commit()
        rows = (await session.execute(DiaryEvent.__table__.select())).all()
        assert rows == []

    async def test_measurement_set_null(self, session: AsyncSession, user: User) -> None:
        """Удаление измерения снимает привязку, событие остаётся (§4.18)."""
        measurement = make_measurement(user.id)
        session.add(measurement)
        await session.commit()
        event = self._event(user.id, measurement_id=measurement.id, type="stress")
        session.add(event)
        await session.commit()
        await session.delete(measurement)
        await session.commit()
        session.expire_all()  # SET NULL делает БД — перечитать объекты из identity map
        rows = (
            await session.execute(
                select(DiaryEvent).execution_options(populate_existing=True)
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].measurement_id is None
