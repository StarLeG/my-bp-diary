"""Тесты сервиса лекарств (bot/services/medications.py)."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import Medication, MedicationLog, User
from bot.services import medications as service

UTC = timezone.utc


def aware(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


async def _add_med(
    session: AsyncSession, user: User, name: str = "Лозартан", slots=("morning",)
) -> list[Medication]:
    return await service.add_medication(session, user, name, "50 мг", slots)


class TestAddMedication:
    async def test_creates_row_per_slot(self, session: AsyncSession, user: User) -> None:
        meds = await _add_med(session, user, slots=("morning", "evening"))
        await session.commit()
        assert len(meds) == 2
        assert {m.slot for m in meds} == {"morning", "evening"}
        assert all(m.name == "Лозартан" and m.dosage == "50 мг" for m in meds)

    async def test_empty_dosage_stored(self, session: AsyncSession, user: User) -> None:
        """«Без дозировки» — пустая строка сохраняется как есть (§4.4)."""
        meds = await service.add_medication(session, user, "Лозартан", "", ("morning",))
        await session.commit()
        assert meds[0].dosage == ""

    async def test_crisis_slot_stored(self, session: AsyncSession, user: User) -> None:
        """Слот «Кризис» — препарат SOS, строка создаётся как обычно (§4.4)."""
        meds = await service.add_medication(session, user, "Каптоприл", "25 мг", ("crisis",))
        await session.commit()
        assert len(meds) == 1
        assert meds[0].slot == "crisis"

    async def test_same_name_slot_updates_dosage(self, session: AsyncSession, user: User) -> None:
        await _add_med(session, user)
        await session.commit()
        meds = await service.add_medication(session, user, "Лозартан", "100 мг", ("morning",))
        await session.commit()
        assert len(meds) == 1
        assert meds[0].dosage == "100 мг"
        rows = (await session.execute(select(Medication))).scalars().all()
        assert len(rows) == 1

    async def test_same_name_other_user_isolated(self, session: AsyncSession, user: User) -> None:
        other = User(telegram_id=222_222)
        session.add(other)
        await session.flush()
        await _add_med(session, user)
        await _add_med(session, other)
        await session.commit()
        assert await service.list_medications(session, user) != []
        assert len(await service.list_medications(session, other)) == 1


class TestDeleteMedication:
    async def test_deletes_with_logs(self, session: AsyncSession, user: User) -> None:
        meds = await _add_med(session, user)
        await session.flush()
        await service.log_slot_intake(
            session, user, "morning", {meds[0].id: "taken"}, timestamp=aware(2026, 11, 4, 8, 5)
        )
        await session.commit()
        assert await service.delete_medication(session, user, meds[0].id) is True
        await session.commit()
        assert await service.list_medications(session, user) == []
        logs = (await session.execute(select(MedicationLog))).scalars().all()
        assert logs == []

    async def test_other_user_returns_false(self, session: AsyncSession, user: User) -> None:
        meds = await _add_med(session, user)
        await session.flush()
        other = User(telegram_id=222_222)
        session.add(other)
        await session.flush()
        assert await service.delete_medication(session, other, meds[0].id) is False

    async def test_unknown_id_returns_false(self, session: AsyncSession, user: User) -> None:
        assert await service.delete_medication(session, user, 999) is False


class TestLists:
    async def test_list_ordered(self, session: AsyncSession, user: User) -> None:
        await _add_med(session, user, "Амлодипин", slots=("evening",))
        await _add_med(session, user, "Лозартан", slots=("morning",))
        await session.commit()
        names = [m.name for m in await service.list_medications(session, user)]
        assert names == ["Амлодипин", "Лозартан"]

    async def test_slot_medications(self, session: AsyncSession, user: User) -> None:
        await _add_med(session, user, "Лозартан", slots=("morning", "evening"))
        await session.commit()
        morning = await service.slot_medications(session, user, "morning")
        assert [m.name for m in morning] == ["Лозартан"]
        assert morning[0].slot == "morning"


class TestLogSlotIntake:
    async def _setup(self, session: AsyncSession, user: User) -> list[Medication]:
        meds = await service.add_medication(
            session, user, "Лозартан", "50 мг", ("morning",)
        )
        meds += await service.add_medication(
            session, user, "Амлодипин", "5 мг", ("morning",)
        )
        await session.flush()
        return meds

    async def test_take_all(self, session: AsyncSession, user: User) -> None:
        meds = await self._setup(session, user)
        result = await service.log_slot_intake(
            session,
            user,
            "morning",
            {m.id: "taken" for m in meds},
            timestamp=aware(2026, 11, 4, 8, 5),
        )
        await session.commit()
        assert result.already_logged is False
        assert sorted(result.taken) == ["Амлодипин 5 мг", "Лозартан 50 мг"]
        assert result.skipped == []
        logs = (await session.execute(select(MedicationLog))).scalars().all()
        assert len(logs) == 2
        assert all(log.status == "taken" and log.slot == "morning" for log in logs)

    async def test_partial(self, session: AsyncSession, user: User) -> None:
        meds = await self._setup(session, user)
        taken_id, skipped_id = meds[0].id, meds[1].id
        result = await service.log_slot_intake(
            session,
            user,
            "morning",
            {taken_id: "taken", skipped_id: "skipped"},
            timestamp=aware(2026, 11, 4, 8, 5),
        )
        assert result.taken == ["Лозартан 50 мг"]
        assert result.skipped == ["Амлодипин 5 мг"]

    async def test_skip_all(self, session: AsyncSession, user: User) -> None:
        meds = await self._setup(session, user)
        result = await service.log_slot_intake(
            session,
            user,
            "morning",
            {m.id: "skipped" for m in meds},
            timestamp=aware(2026, 11, 4, 8, 5),
        )
        assert result.taken == []
        assert len(result.skipped) == 2

    async def test_repeat_same_day_is_idempotent(
        self, session: AsyncSession, user: User
    ) -> None:
        meds = await self._setup(session, user)
        decisions = {m.id: "taken" for m in meds}
        await service.log_slot_intake(
            session, user, "morning", decisions, timestamp=aware(2026, 11, 4, 8, 5)
        )
        second = await service.log_slot_intake(
            session, user, "morning", decisions, timestamp=aware(2026, 11, 4, 8, 6)
        )
        await session.commit()
        assert second.already_logged is True
        assert second.taken == [] and second.skipped == []
        logs = (await session.execute(select(MedicationLog))).scalars().all()
        assert len(logs) == 2  # ровно один набор, как при одиночном нажатии

    async def test_new_day_logs_again(self, session: AsyncSession, user: User) -> None:
        meds = await self._setup(session, user)
        decisions = {m.id: "taken" for m in meds}
        await service.log_slot_intake(
            session, user, "morning", decisions, timestamp=aware(2026, 11, 4, 8, 5)
        )
        await service.log_slot_intake(
            session, user, "morning", decisions, timestamp=aware(2026, 11, 5, 8, 5)
        )
        await session.commit()
        logs = (await session.execute(select(MedicationLog))).scalars().all()
        assert len(logs) == 4

    async def test_other_slot_logs_separately(self, session: AsyncSession, user: User) -> None:
        meds = await service.add_medication(
            session, user, "Лозартан", "50 мг", ("morning", "evening")
        )
        await session.flush()
        await service.log_slot_intake(
            session,
            user,
            "morning",
            {meds[0].id: "taken"},
            timestamp=aware(2026, 11, 4, 8, 5),
        )
        await service.log_slot_intake(
            session,
            user,
            "evening",
            {meds[1].id: "taken"},
            timestamp=aware(2026, 11, 4, 21, 5),
        )
        await session.commit()
        logs = (await session.execute(select(MedicationLog))).scalars().all()
        assert len(logs) == 2

    async def test_unknown_medication_ignored(self, session: AsyncSession, user: User) -> None:
        result = await service.log_slot_intake(
            session, user, "morning", {999: "taken"}, timestamp=aware(2026, 11, 4, 8, 5)
        )
        assert result.already_logged is True
        assert result.taken == []

    async def test_naive_timestamp_treated_as_utc(
        self, session: AsyncSession, user: User
    ) -> None:
        meds = await self._setup(session, user)
        result = await service.log_slot_intake(
            session,
            user,
            "morning",
            {meds[0].id: "taken"},
            timestamp=datetime(2026, 11, 4, 8, 5),  # naive = UTC
        )
        assert result.taken == ["Лозартан 50 мг"]
        repeat = await service.log_slot_intake(
            session,
            user,
            "morning",
            {meds[0].id: "taken"},
            timestamp=datetime(2026, 11, 4, 8, 6),
        )
        assert repeat.already_logged is True

    async def test_isolated_between_users(self, session: AsyncSession, user: User) -> None:
        meds = await self._setup(session, user)
        other = User(telegram_id=222_222)
        session.add(other)
        await session.flush()
        # Чужой medication_id не попадает в логи другого пользователя.
        result = await service.log_slot_intake(
            session,
            other,
            "morning",
            {meds[0].id: "taken"},
            timestamp=aware(2026, 11, 4, 8, 5),
        )
        assert result.already_logged is True


class TestAdherence:
    async def test_counts_taken_vs_planned(self, session: AsyncSession, user: User) -> None:
        meds = await service.add_medication(
            session, user, "Лозартан", "50 мг", ("morning", "evening")
        )
        await session.flush()
        # 7 дней × 2 приёма = 14 запланированных; принято 10.
        for day in range(1, 8):
            await service.log_slot_intake(
                session,
                user,
                "morning",
                {meds[0].id: "taken"},
                timestamp=aware(2026, 11, day, 8, 5),
            )
        for day in range(1, 6):
            await service.log_slot_intake(
                session,
                user,
                "evening",
                {meds[1].id: "taken"},
                timestamp=aware(2026, 11, day, 21, 5),
            )
        await session.commit()
        taken, planned = await service.adherence(
            session, user, date(2026, 11, 1), date(2026, 11, 7)
        )
        assert planned == 14
        assert taken == 12

    async def test_skipped_not_counted(self, session: AsyncSession, user: User) -> None:
        meds = await service.add_medication(session, user, "Лозартан", "50 мг", ("morning",))
        await session.flush()
        await service.log_slot_intake(
            session,
            user,
            "morning",
            {meds[0].id: "skipped"},
            timestamp=aware(2026, 11, 4, 8, 5),
        )
        await session.commit()
        taken, planned = await service.adherence(
            session, user, date(2026, 11, 4), date(2026, 11, 4)
        )
        assert taken == 0
        assert planned == 1

    async def test_crisis_excluded_from_plan(self, session: AsyncSession, user: User) -> None:
        """Кризисный препарат не в плане и не в «принято» (§6.7)."""
        crisis = await service.add_medication(session, user, "Каптоприл", "25 мг", ("crisis",))
        await service.add_medication(session, user, "Лозартан", "50 мг", ("morning",))
        await session.flush()
        await service.log_slot_intake(
            session,
            user,
            "crisis",
            {crisis[0].id: "taken"},
            timestamp=aware(2026, 11, 4, 15, 5),
        )
        await session.commit()
        taken, planned = await service.adherence(
            session, user, date(2026, 11, 4), date(2026, 11, 4)
        )
        # Приём кризиса не принят в зачёт: 0 taken / 1 planned (только утро).
        assert (taken, planned) == (0, 1)

    async def test_crisis_only_not_planned(self, session: AsyncSession, user: User) -> None:
        """Только кризисные препараты — план пуст, приверженность не считается."""
        await service.add_medication(session, user, "Каптоприл", "25 мг", ("crisis",))
        await session.commit()
        taken, planned = await service.adherence(
            session, user, date(2026, 11, 1), date(2026, 11, 7)
        )
        assert (taken, planned) == (0, 0)

    async def test_no_medications(self, session: AsyncSession, user: User) -> None:
        taken, planned = await service.adherence(
            session, user, date(2026, 11, 1), date(2026, 11, 7)
        )
        assert (taken, planned) == (0, 0)

    async def test_empty_period(self, session: AsyncSession, user: User) -> None:
        await _add_med(session, user)
        await session.flush()
        assert await service.planned_intakes(
            session, user, date(2026, 11, 7), date(2026, 11, 1)
        ) == 0


class TestLogsExport:
    async def test_logs_for_period_and_all(self, session: AsyncSession, user: User) -> None:
        meds = await service.add_medication(session, user, "Лозартан", "50 мг", ("morning",))
        await session.flush()
        for day in (1, 5):
            await service.log_slot_intake(
                session,
                user,
                "morning",
                {meds[0].id: "taken"},
                timestamp=aware(2026, 11, day, 8, 5),
            )
        await session.commit()
        period = await service.logs_for_period(
            session, user, date(2026, 11, 1), date(2026, 11, 3)
        )
        assert len(period) == 1
        log, name, dosage = period[0]
        assert (name, dosage, log.status) == ("Лозартан", "50 мг", "taken")
        all_logs = await service.logs_all(session, user)
        assert len(all_logs) == 2
        assert [entry[0].timestamp for entry in all_logs] == [
            aware(2026, 11, 1, 8, 5),
            aware(2026, 11, 5, 8, 5),
        ]
