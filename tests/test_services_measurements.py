"""Тесты сервиса измерений (bot/services/measurements.py)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import Measurement, User
from bot.services import measurements as service

UTC = timezone.utc
MINSK_NOW = datetime(2026, 11, 4, 8, 30)  # «сейчас» для детерминированных тестов


def aware(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


class TestDayBounds:
    def test_minsk_day_bounds(self) -> None:
        start, end = service.day_bounds_utc("Europe/Minsk", date(2026, 11, 4))
        # Минск — UTC+3 (зима): день начинается в 21:00 UTC предыдущего дня.
        assert start == aware(2026, 11, 3, 21, 0)
        assert end == aware(2026, 11, 4, 21, 0)

    def test_utc_day_bounds(self) -> None:
        start, end = service.day_bounds_utc("UTC", date(2026, 11, 4))
        assert start == aware(2026, 11, 4)
        assert end == aware(2026, 11, 5)

    def test_bounds_are_half_open(self) -> None:
        start, end = service.day_bounds_utc("Europe/Minsk", date(2026, 11, 4))
        assert end - start == timedelta(days=1)


class TestGetOrCreateUser:
    async def test_creates_with_defaults(self, session: AsyncSession) -> None:
        user = await service.get_or_create_user(session, 42)
        await session.commit()
        assert user.telegram_id == 42
        assert user.timezone == "Europe/Minsk"
        assert user.med_remeasure_delay == 60
        assert user.onboarded is False

    async def test_returns_existing(self, session: AsyncSession) -> None:
        first = await service.get_or_create_user(session, 42)
        await session.commit()
        second = await service.get_or_create_user(session, 42)
        assert first.id == second.id
        count = (await session.execute(select(User))).scalars().all()
        assert len(count) == 1

    async def test_custom_default_timezone(self, session: AsyncSession) -> None:
        user = await service.get_or_create_user(session, 42, default_timezone="UTC")
        assert user.timezone == "UTC"

    async def test_race_falls_back_to_existing(
        self, session: AsyncSession, session_factory, mocker
    ) -> None:
        # Гонка двух /start: первый SELECT не видит уже вставленную строку,
        # INSERT получает IntegrityError → функция возвращает существующую.
        existing = User(telegram_id=42)
        other = session_factory()
        try:
            other.add(existing)
            await other.commit()
        finally:
            await other.close()

        class _EmptyResult:
            def scalar_one_or_none(self):
                return None

        real_execute = session.execute
        calls = {"n": 0}

        async def fake_execute(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return _EmptyResult()
            return await real_execute(*args, **kwargs)

        mocker.patch.object(session, "execute", side_effect=fake_execute)
        user = await service.get_or_create_user(session, 42)
        await session.commit()
        assert user.id == existing.id
        # SELECT (мок → «нет») → INSERT через flush (IntegrityError) → SELECT.
        assert calls["n"] == 2


class TestSaveMeasurement:
    async def test_saves_and_returns(self, session: AsyncSession, user: User) -> None:
        saved = await service.save_measurement(
            session, user, 135, 85, 72, timestamp=aware(2026, 11, 4, 8, 30)
        )
        await session.commit()
        assert saved.id is not None
        assert saved.systolic == 135
        assert saved.pulse == 72
        assert saved.timestamp == aware(2026, 11, 4, 8, 30)

    async def test_pulse_optional(self, session: AsyncSession, user: User) -> None:
        saved = await service.save_measurement(session, user, 135, 85)
        assert saved.pulse is None

    async def test_naive_timestamp_treated_as_utc(
        self, session: AsyncSession, user: User
    ) -> None:
        saved = await service.save_measurement(
            session, user, 135, 85, timestamp=datetime(2026, 11, 4, 8, 30)
        )
        assert saved.timestamp == aware(2026, 11, 4, 8, 30)

    async def test_duplicate_same_minute_rejected(
        self, session: AsyncSession, user: User
    ) -> None:
        await service.save_measurement(
            session, user, 135, 85, timestamp=aware(2026, 11, 4, 8, 30)
        )
        with pytest.raises(service.DuplicateMeasurementError):
            await service.save_measurement(
                session, user, 140, 90, timestamp=aware(2026, 11, 4, 8, 30, 45)
            )

    async def test_different_minutes_ok(self, session: AsyncSession, user: User) -> None:
        await service.save_measurement(
            session, user, 135, 85, timestamp=aware(2026, 11, 4, 8, 30)
        )
        await service.save_measurement(
            session, user, 140, 90, timestamp=aware(2026, 11, 4, 8, 31)
        )

    async def test_same_minute_other_user_ok(self, session: AsyncSession, user: User) -> None:
        other = User(telegram_id=222_222)
        session.add(other)
        await session.flush()
        await service.save_measurement(
            session, user, 135, 85, timestamp=aware(2026, 11, 4, 8, 30)
        )
        await service.save_measurement(
            session, other, 140, 90, timestamp=aware(2026, 11, 4, 8, 30)
        )

    async def test_parallel_saves_create_one_row(
        self, session_factory, user: User
    ) -> None:
        # Два конкурентных сохранения с одним временем — ровно одна запись.
        import asyncio

        async def save() -> bool:
            async with session_factory() as s:
                u = (
                    await s.execute(select(User).where(User.telegram_id == user.telegram_id))
                ).scalar_one()
                try:
                    await service.save_measurement(
                        s, u, 135, 85, timestamp=aware(2026, 11, 4, 8, 30)
                    )
                    await s.commit()
                    return True
                except service.DuplicateMeasurementError:
                    await s.rollback()
                    return False

        results = await asyncio.gather(save(), save())
        assert sorted(results) == [False, True]


class TestOptionalSteps:
    async def _saved(self, session: AsyncSession, user: User) -> Measurement:
        return await service.save_measurement(
            session, user, 135, 85, 72, timestamp=aware(2026, 11, 4, 8, 30)
        )

    async def test_add_note(self, session: AsyncSession, user: User) -> None:
        saved = await self._saved(session, user)
        updated = await service.add_note(session, user, saved.id, "после кофе")
        await session.commit()
        assert updated is not None
        assert updated.note == "после кофе"

    async def test_add_note_other_user_returns_none(
        self, session: AsyncSession, user: User
    ) -> None:
        saved = await self._saved(session, user)
        other = User(telegram_id=222_222)
        session.add(other)
        await session.flush()
        assert await service.add_note(session, other, saved.id, "x") is None

    async def test_set_medication_taken(self, session: AsyncSession, user: User) -> None:
        saved = await self._saved(session, user)
        updated = await service.set_medication_taken(session, user, saved.id, True)
        assert updated is not None
        assert updated.medication_taken is True
        updated = await service.set_medication_taken(session, user, saved.id, None)
        assert updated is not None
        assert updated.medication_taken is None

    async def test_set_medication_taken_unknown_id(
        self, session: AsyncSession, user: User
    ) -> None:
        assert await service.set_medication_taken(session, user, 999, True) is None

    async def test_set_feeling(self, session: AsyncSession, user: User) -> None:
        saved = await self._saved(session, user)
        updated = await service.set_feeling(session, user, saved.id, "headache")
        assert updated is not None
        assert updated.feeling == "headache"

    async def test_set_feeling_other_appends_note(
        self, session: AsyncSession, user: User
    ) -> None:
        saved = await self._saved(session, user)
        await service.add_note(session, user, saved.id, "после кофе")
        updated = await service.set_feeling(session, user, saved.id, "other", "тошнота")
        assert updated is not None
        assert updated.note == "после кофе\nтошнота"

    async def test_set_feeling_other_without_text(
        self, session: AsyncSession, user: User
    ) -> None:
        saved = await self._saved(session, user)
        updated = await service.set_feeling(session, user, saved.id, "other")
        assert updated is not None
        assert updated.note is None

    async def test_set_feeling_unknown_id(self, session: AsyncSession, user: User) -> None:
        assert await service.set_feeling(session, user, 999, "normal") is None


class TestQueries:
    async def _add(
        self,
        session: AsyncSession,
        user: User,
        stamp: datetime,
        systolic: int = 135,
        pulse: int = 72,
    ) -> Measurement:
        return await service.save_measurement(
            session, user, systolic, 85, pulse, timestamp=stamp
        )

    async def test_measurements_for_day_minsk(
        self, session: AsyncSession, user: User
    ) -> None:
        # 04.11 в Минске (UTC+3): 01:00 UTC 04.11 … 20:59 UTC 04.11.
        await self._add(session, user, aware(2026, 11, 4, 0, 59))  # 03:59 по Минску — 04.11
        await self._add(session, user, aware(2026, 11, 4, 20, 59))  # 23:59 по Минску — 04.11
        await self._add(session, user, aware(2026, 11, 4, 21, 0))  # 00:00 05.11 — уже нет
        rows = await service.measurements_for_day(session, user, date(2026, 11, 4))
        assert len(rows) == 2
        assert rows[0].timestamp == aware(2026, 11, 4, 0, 59)

    async def test_measurements_for_period(
        self, session: AsyncSession, user: User
    ) -> None:
        await self._add(session, user, aware(2026, 11, 1, 12, 0))
        await self._add(session, user, aware(2026, 11, 3, 12, 0))
        await self._add(session, user, aware(2026, 11, 5, 12, 0))
        rows = await service.measurements_for_period(session, user, date(2026, 11, 1), date(2026, 11, 3))
        assert [m.timestamp for m in rows] == [aware(2026, 11, 1, 12, 0), aware(2026, 11, 3, 12, 0)]

    async def test_measurements_all(self, session: AsyncSession, user: User) -> None:
        await self._add(session, user, aware(2026, 11, 3, 12, 0))
        await self._add(session, user, aware(2026, 11, 1, 12, 0))
        rows = await service.measurements_all(session, user)
        assert [m.timestamp for m in rows] == [aware(2026, 11, 1, 12, 0), aware(2026, 11, 3, 12, 0)]

    async def test_count_measurements(self, session: AsyncSession, user: User) -> None:
        await self._add(session, user, aware(2026, 11, 1, 12, 0))
        await self._add(session, user, aware(2026, 11, 2, 12, 0))
        assert await service.count_measurements(session, user) == 2

    async def test_count_isolated_per_user(self, session: AsyncSession, user: User) -> None:
        other = User(telegram_id=222_222)
        session.add(other)
        await session.flush()
        await self._add(session, user, aware(2026, 11, 1, 12, 0))
        await self._add(session, other, aware(2026, 11, 1, 12, 0))
        assert await service.count_measurements(session, user) == 1
        assert await service.count_measurements(session, other) == 1

    async def test_medication_taken_count(self, session: AsyncSession, user: User) -> None:
        first = await self._add(session, user, aware(2026, 11, 1, 12, 0))
        second = await self._add(session, user, aware(2026, 11, 2, 12, 0))
        assert await service.medication_taken_count(session, user) == 0
        await service.set_medication_taken(session, user, first.id, True)
        await service.set_medication_taken(session, user, second.id, False)
        # Считаются только «Да»: FALSE и NULL не в счёт.
        assert await service.medication_taken_count(session, user) == 1


class TestHasRecentMeasurement:
    async def test_recent_within_window(self, session: AsyncSession, user: User) -> None:
        now = aware(2026, 11, 4, 12, 0)
        await service.save_measurement(
            session, user, 135, 85, timestamp=now - timedelta(hours=2)
        )
        assert await service.has_recent_measurement(session, user, now) is True

    async def test_old_outside_window(self, session: AsyncSession, user: User) -> None:
        now = aware(2026, 11, 4, 12, 0)
        await service.save_measurement(
            session, user, 135, 85, timestamp=now - timedelta(hours=4)
        )
        assert await service.has_recent_measurement(session, user, now) is False

    async def test_no_measurements(self, session: AsyncSession, user: User) -> None:
        assert await service.has_recent_measurement(session, user, aware(2026, 11, 4, 12, 0)) is False

    async def test_other_user_ignored(self, session: AsyncSession, user: User) -> None:
        other = User(telegram_id=222_222)
        session.add(other)
        await session.flush()
        now = aware(2026, 11, 4, 12, 0)
        await service.save_measurement(
            session, other, 135, 85, timestamp=now - timedelta(minutes=5)
        )
        assert await service.has_recent_measurement(session, user, now) is False

    async def test_naive_now_treated_as_utc(self, session: AsyncSession, user: User) -> None:
        await service.save_measurement(
            session, user, 135, 85, timestamp=aware(2026, 11, 4, 11, 59)
        )
        assert (
            await service.has_recent_measurement(
                session, user, datetime(2026, 11, 4, 12, 0)
            )
            is True
        )
