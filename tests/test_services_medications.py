"""Тесты сервиса лекарств (bot/services/medications.py)."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import Measurement, Medication, MedicationLog, MeasurementMedication, User
from bot.services import medications as service

UTC = timezone.utc


def aware(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


async def _add_med(
    session: AsyncSession, user: User, name: str = "Лозартан", slots=("morning",)
) -> list[Medication]:
    return await service.add_medication(session, user, name, "50 мг", slots)


async def _add_measurement(session: AsyncSession, user: User) -> int:
    """Запись измерения для привязки отмеченных лекарств (§4.11)."""
    row = Measurement(
        user_id=user.id,
        timestamp=aware(2026, 11, 4, 8, 5),
        systolic=135,
        diastolic=85,
    )
    session.add(row)
    await session.flush()
    return row.id


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


class TestMedicationGroups:
    async def test_medications_by_name_groups_rows(self, session: AsyncSession, user: User) -> None:
        """Группа (user_id, name) — строки во всех слотах, порядок SLOTS (§4.4)."""
        await service.add_medication(
            session, user, "Лозартан", "50 мг", ("evening", "crisis", "morning")
        )
        await session.commit()
        rows = await service.medications_by_name(session, user, "Лозартан")
        assert [row.slot for row in rows] == ["morning", "evening", "crisis"]

    async def test_medications_by_name_unknown(self, session: AsyncSession, user: User) -> None:
        assert await service.medications_by_name(session, user, "Нет такого") == []

    async def test_list_groups_by_name(self, session: AsyncSession, user: User) -> None:
        await service.add_medication(session, user, "Лозартан", "50 мг", ("morning", "evening"))
        await service.add_medication(session, user, "Каптоприл", "25 мг", ("crisis",))
        await session.commit()
        groups = await service.list_medication_groups(session, user)
        assert [group.name for group in groups] == ["Каптоприл", "Лозартан"]
        by_name = {group.name: group for group in groups}
        assert by_name["Лозартан"].slots == ("morning", "evening")
        assert by_name["Каптоприл"].slots == ("crisis",)
        # id кнопки — id первой строки группы в порядке слотов.
        rows = await service.medications_by_name(session, user, "Лозартан")
        assert by_name["Лозартан"].id == rows[0].id
        assert by_name["Лозартан"].dosage == "50 мг"

    async def test_list_groups_mixed_dosage_takes_first(
        self, session: AsyncSession, user: User
    ) -> None:
        """Смешанные дозировки (артефакт add-flow) → берётся первая по слотам."""
        await service.add_medication(session, user, "Лозартан", "100 мг", ("evening",))
        await service.add_medication(session, user, "Лозартан", "25 мг", ("morning",))
        await session.commit()
        groups = await service.list_medication_groups(session, user)
        assert len(groups) == 1
        assert groups[0].dosage == "25 мг"  # morning раньше evening в SLOTS

    async def test_delete_group_removes_all_rows_with_logs(
        self, session: AsyncSession, user: User
    ) -> None:
        meds = await service.add_medication(
            session, user, "Лозартан", "50 мг", ("morning", "evening")
        )
        await service.add_medication(session, user, "Каптоприл", "25 мг", ("crisis",))
        await session.flush()
        await service.log_slot_intake(
            session, user, "morning", {meds[0].id: "taken"}, timestamp=aware(2026, 11, 4, 8, 5)
        )
        await service.log_slot_intake(
            session, user, "evening", {meds[1].id: "taken"}, timestamp=aware(2026, 11, 4, 21, 5)
        )
        await session.commit()
        assert await service.delete_medication_group(session, user, "Лозартан") is True
        await session.commit()
        names = [m.name for m in await service.list_medications(session, user)]
        assert names == ["Каптоприл"]
        logs = (await session.execute(select(MedicationLog))).scalars().all()
        assert logs == []  # каскадом сняты обе группы логов

    async def test_delete_group_unknown_and_foreign_returns_false(
        self, session: AsyncSession, user: User
    ) -> None:
        await service.add_medication(session, user, "Лозартан", "50 мг", ("morning",))
        other = User(telegram_id=222_222)
        session.add(other)
        await session.flush()
        await session.commit()
        assert await service.delete_medication_group(session, user, "Нет такого") is False
        assert await service.delete_medication_group(session, other, "Лозартан") is False
        assert len(await service.list_medications(session, user)) == 1  # ничего не удалено


class TestUpdateMedication:
    async def test_rename_keeps_row_ids_and_logs(
        self, session: AsyncSession, user: User
    ) -> None:
        """Инвариант §6.6: правка не теряет medication_logs, id строк сохраняются."""
        meds = await service.add_medication(
            session, user, "Лозартан", "50 мг", ("morning", "evening")
        )
        await session.flush()
        await service.log_slot_intake(
            session, user, "morning", {meds[0].id: "taken"}, timestamp=aware(2026, 11, 4, 8, 5)
        )
        await session.commit()
        ids_before = {m.id for m in meds}
        updated = await service.update_medication(
            session, user, "Лозартан", "Лозап", "50 мг", ["morning", "evening"]
        )
        await session.commit()
        assert updated is not None
        assert {row.id for row in updated} == ids_before  # UPDATE на месте
        assert all(row.name == "Лозап" for row in updated)
        logs = (await session.execute(select(MedicationLog))).scalars().all()
        assert len(logs) == 1
        assert logs[0].medication_id in ids_before

    async def test_update_dosage_for_whole_group(
        self, session: AsyncSession, user: User
    ) -> None:
        await service.add_medication(session, user, "Лозартан", "50 мг", ("morning", "evening"))
        await session.commit()
        updated = await service.update_medication(
            session, user, "Лозартан", "Лозартан", "100 мг", ["morning", "evening"]
        )
        await session.commit()
        assert {row.dosage for row in updated or []} == {"100 мг"}
        assert len(await service.list_medications(session, user)) == 2

    async def test_removed_slot_cascades_only_its_logs(
        self, session: AsyncSession, user: User
    ) -> None:
        """Снятый слот удаляется с логами каскадом; логи переживающего целы (§6.6)."""
        meds = await service.add_medication(
            session, user, "Лозартан", "50 мг", ("morning", "evening")
        )
        await session.flush()
        await service.log_slot_intake(
            session, user, "morning", {meds[0].id: "taken"}, timestamp=aware(2026, 11, 4, 8, 5)
        )
        await service.log_slot_intake(
            session, user, "evening", {meds[1].id: "taken"}, timestamp=aware(2026, 11, 4, 21, 5)
        )
        await session.commit()
        updated = await service.update_medication(
            session, user, "Лозартан", "Лозартан", "50 мг", ["morning"]
        )
        await session.commit()
        assert updated is not None and len(updated) == 1
        assert updated[0].slot == "morning" and updated[0].id == meds[0].id
        logs = (await session.execute(select(MedicationLog))).scalars().all()
        assert len(logs) == 1
        assert logs[0].medication_id == meds[0].id  # лог пережившего слота цел

    async def test_added_slot_inserts_row(self, session: AsyncSession, user: User) -> None:
        meds = await service.add_medication(session, user, "Лозартан", "50 мг", ("morning",))
        await session.flush()
        await session.commit()
        updated = await service.update_medication(
            session, user, "Лозартан", "Лозартан", "50 мг", ["morning", "crisis"]
        )
        await session.commit()
        assert updated is not None
        assert [row.slot for row in updated] == ["morning", "crisis"]
        assert updated[0].id == meds[0].id  # прежняя строка нетронута
        assert updated[1].id != meds[0].id

    async def test_rename_and_rebuild_slots_combined(
        self, session: AsyncSession, user: User
    ) -> None:
        meds = await service.add_medication(
            session, user, "Лозартан", "50 мг", ("morning", "noon", "evening")
        )
        await session.flush()
        await service.log_slot_intake(
            session, user, "noon", {meds[1].id: "taken"}, timestamp=aware(2026, 11, 4, 13, 5)
        )
        await session.commit()
        updated = await service.update_medication(
            session, user, "Лозартан", "Лозап", "25 мг", ["noon", "crisis"]
        )
        await session.commit()
        assert updated is not None
        assert [row.slot for row in updated] == ["noon", "crisis"]
        assert all(row.name == "Лозап" and row.dosage == "25 мг" for row in updated)
        assert updated[0].id == meds[1].id  # переживающий слот обновлён на месте
        logs = (await session.execute(select(MedicationLog))).scalars().all()
        assert len(logs) == 1 and logs[0].medication_id == meds[1].id

    async def test_name_conflict_overlapping_slot_rejected(
        self, session: AsyncSession, user: User
    ) -> None:
        """Коллизия имени = отказ до мутаций: БД не изменена (§6.6)."""
        await service.add_medication(session, user, "Лозартан", "50 мг", ("morning",))
        await service.add_medication(session, user, "Амлодипин", "5 мг", ("morning",))
        await session.commit()
        with pytest.raises(service.MedicationNameConflictError):
            await service.update_medication(
                session, user, "Лозартан", "Амлодипин", "50 мг", ["morning", "evening"]
            )
        # Откат не нужен: конфликт бросается до мутаций (§6.6).
        rows = await service.list_medications(session, user)
        assert {(m.name, m.slot) for m in rows} == {
            ("Лозартан", "morning"),
            ("Амлодипин", "morning"),
        }

    async def test_name_conflict_non_overlapping_slot_rejected(
        self, session: AsyncSession, user: User
    ) -> None:
        """Коллизия отсекается по имени — даже если слоты не пересекаются."""
        await service.add_medication(session, user, "Лозартан", "50 мг", ("morning",))
        await service.add_medication(session, user, "Амлодипин", "5 мг", ("evening",))
        await session.commit()
        with pytest.raises(service.MedicationNameConflictError):
            await service.update_medication(
                session, user, "Лозартан", "Амлодипин", "50 мг", ["morning"]
            )
        names = {m.name for m in await service.list_medications(session, user)}
        assert names == {"Лозартан", "Амлодипин"}

    async def test_stale_group_returns_none(self, session: AsyncSession, user: User) -> None:
        """Лекарство удалили, пока шла правка → None, ничего не создано."""
        assert (
            await service.update_medication(
                session, user, "Нет такого", "Нет такого", "50 мг", ["morning"]
            )
            is None
        )
        await session.commit()
        assert await service.list_medications(session, user) == []

    async def test_isolated_between_users(self, session: AsyncSession, user: User) -> None:
        other = User(telegram_id=222_222)
        session.add(other)
        await session.flush()
        await service.add_medication(session, user, "Лозартан", "50 мг", ("morning",))
        await session.commit()
        assert (
            await service.update_medication(
                session, other, "Лозартан", "Лозап", "50 мг", ["morning"]
            )
            is None
        )
        rows = await service.medications_by_name(session, user, "Лозартан")
        assert len(rows) == 1  # чужая правка ничего не тронула

    async def test_idempotent_no_change_save(self, session: AsyncSession, user: User) -> None:
        """Сохранение без изменений — идемпотентно: тот же состав, те же id."""
        meds = await service.add_medication(
            session, user, "Лозартан", "50 мг", ("morning", "evening")
        )
        await session.flush()
        await session.commit()
        ids_before = {m.id for m in meds}
        updated = await service.update_medication(
            session, user, "Лозартан", "Лозартан", "50 мг", ["morning", "evening"]
        )
        await session.commit()
        assert updated is not None
        assert {row.id for row in updated} == ids_before
        assert len(await service.list_medications(session, user)) == 2


class TestLoggedIntakeIdsToday:
    async def test_any_status_counts(self, session: AsyncSession, user: User) -> None:
        """«✓» — у любой отметки за сутки: taken, skipped, partial (§6.3)."""
        meds = await service.add_medication(
            session, user, "Лозартан", "50 мг", ("morning", "evening")
        )
        await session.flush()
        await service.log_slot_intake(
            session, user, "morning", {meds[0].id: "taken"}, timestamp=aware(2026, 11, 4, 8, 5)
        )
        # 18:05 UTC = 21:05 Europe/Minsk — те же локальные сутки (UTC+3).
        await service.log_slot_intake(
            session, user, "evening", {meds[1].id: "skipped"}, timestamp=aware(2026, 11, 4, 18, 5)
        )
        await session.commit()
        ids = await service.logged_intake_ids_today(session, user, timestamp=aware(2026, 11, 4, 12, 0))
        assert ids == {meds[0].id, meds[1].id}

    async def test_other_day_not_counted(self, session: AsyncSession, user: User) -> None:
        """Вчерашние отметки в «✓» сегодня не попадают (локальные сутки)."""
        meds = await _add_med(session, user)
        await session.flush()
        await service.log_slot_intake(
            session, user, "morning", {meds[0].id: "taken"}, timestamp=aware(2026, 11, 4, 8, 5)
        )
        await session.commit()
        ids = await service.logged_intake_ids_today(session, user, timestamp=aware(2026, 11, 5, 12, 0))
        assert ids == set()

    async def test_other_user_excluded(self, session: AsyncSession, user: User) -> None:
        """Изоляция по пользователю: чужие отметки не видны (§7.1)."""
        other = User(telegram_id=222_222)
        session.add(other)
        await session.flush()
        meds = await _add_med(session, other)
        await session.flush()
        await service.log_slot_intake(
            session, other, "morning", {meds[0].id: "taken"}, timestamp=aware(2026, 11, 4, 8, 5)
        )
        await session.commit()
        ids = await service.logged_intake_ids_today(session, user, timestamp=aware(2026, 11, 4, 12, 0))
        assert ids == set()


class TestLogManualIntake:
    async def test_creates_taken_logs_with_med_slot(
        self, session: AsyncSession, user: User
    ) -> None:
        """«Готово» пишет по одной записи taken со слотом строки лекарства (§6.3)."""
        meds = await service.add_medication(
            session, user, "Лозартан", "50 мг", ("morning", "evening")
        )
        await session.flush()
        result = await service.log_manual_intake(
            session, user, [m.id for m in meds], timestamp=aware(2026, 11, 4, 8, 5)
        )
        await session.commit()
        assert result.taken == ["Лозартан 50 мг", "Лозартан 50 мг"]
        assert result.already_logged is False
        logs = (
            await session.execute(
                select(MedicationLog).order_by(MedicationLog.slot)
            )
        ).scalars().all()
        assert [(log.status, log.slot) for log in logs] == [
            ("taken", "evening"),
            ("taken", "morning"),
        ]

    async def test_idempotent_within_day(self, session: AsyncSession, user: User) -> None:
        """Двойная отметка тех же приёмов за сутки — один набор логов (§7.2)."""
        meds = await _add_med(session, user)
        await session.flush()
        first = await service.log_manual_intake(
            session, user, [meds[0].id], timestamp=aware(2026, 11, 4, 8, 5)
        )
        second = await service.log_manual_intake(
            session, user, [meds[0].id], timestamp=aware(2026, 11, 4, 9, 0)
        )
        await session.commit()
        assert first.already_logged is False
        assert second.already_logged is True
        assert second.taken == ["Лозартан 50 мг"]  # подпись возвращается и повторно
        logs = (await session.execute(select(MedicationLog))).scalars().all()
        assert len(logs) == 1

    async def test_new_day_writes_again(self, session: AsyncSession, user: User) -> None:
        meds = await _add_med(session, user)
        await session.flush()
        await service.log_manual_intake(
            session, user, [meds[0].id], timestamp=aware(2026, 11, 4, 8, 5)
        )
        result = await service.log_manual_intake(
            session, user, [meds[0].id], timestamp=aware(2026, 11, 5, 8, 5)
        )
        await session.commit()
        assert result.already_logged is False
        logs = (await session.execute(select(MedicationLog))).scalars().all()
        assert len(logs) == 2

    async def test_foreign_and_unknown_ids_ignored(
        self, session: AsyncSession, user: User
    ) -> None:
        """Чужие и удалённые id тихо игнорируются (§7.1)."""
        other = User(telegram_id=222_222)
        session.add(other)
        await session.flush()
        foreign_meds = await _add_med(session, other)
        await session.flush()
        result = await service.log_manual_intake(
            session,
            user,
            [foreign_meds[0].id, 999],
            timestamp=aware(2026, 11, 4, 8, 5),
        )
        await session.commit()
        assert result.taken == []
        assert result.already_logged is True
        logs = (await session.execute(select(MedicationLog))).scalars().all()
        assert logs == []

    async def test_empty_selection_writes_nothing(
        self, session: AsyncSession, user: User
    ) -> None:
        result = await service.log_manual_intake(
            session, user, [], timestamp=aware(2026, 11, 4, 8, 5)
        )
        await session.commit()
        assert result.taken == []
        assert result.already_logged is False
        logs = (await session.execute(select(MedicationLog))).scalars().all()
        assert logs == []

    async def test_previously_logged_counts_as_taken(
        self, session: AsyncSession, user: User
    ) -> None:
        """Если приём уже отмечен (даже skipped) — новый лог не пишется, подпись есть."""
        meds = await _add_med(session, user)
        await session.flush()
        await service.log_slot_intake(
            session, user, "morning", {meds[0].id: "skipped"}, timestamp=aware(2026, 11, 4, 8, 5)
        )
        result = await service.log_manual_intake(
            session, user, [meds[0].id], timestamp=aware(2026, 11, 4, 9, 0)
        )
        await session.commit()
        assert result.taken == ["Лозартан 50 мг"]
        assert result.already_logged is True
        logs = (await session.execute(select(MedicationLog))).scalars().all()
        assert len(logs) == 1 and logs[0].status == "skipped"


class TestLinkMeasurementIntake:
    """Связи «измерение ↔ отмеченные лекарства» (§4.11, §6.3)."""

    async def _links(self, session: AsyncSession) -> list[tuple[int, int]]:
        rows = (
            await session.execute(
                select(
                    MeasurementMedication.measurement_id,
                    MeasurementMedication.medication_id,
                ).order_by(MeasurementMedication.id)
            )
        ).all()
        return [(row[0], row[1]) for row in rows]

    async def test_creates_link_per_medication(
        self, session: AsyncSession, user: User
    ) -> None:
        meds = await service.add_medication(
            session, user, "Лозартан", "50 мг", ("morning", "evening")
        )
        await session.flush()
        measurement_id = await _add_measurement(session, user)
        await service.link_measurement_intake(
            session, user, measurement_id, [m.id for m in meds]
        )
        await session.commit()
        assert sorted(await self._links(session)) == [
            (measurement_id, meds[0].id),
            (measurement_id, meds[1].id),
        ]

    async def test_idempotent_no_duplicates(
        self, session: AsyncSession, user: User
    ) -> None:
        """Повторное «Готово» дублей связей не создаёт (§7.2)."""
        meds = await _add_med(session, user)
        await session.flush()
        measurement_id = await _add_measurement(session, user)
        await service.link_measurement_intake(session, user, measurement_id, [meds[0].id])
        await service.link_measurement_intake(session, user, measurement_id, [meds[0].id])
        await session.commit()
        assert await self._links(session) == [(measurement_id, meds[0].id)]

    async def test_per_measurement_not_per_day(
        self, session: AsyncSession, user: User
    ) -> None:
        """Связь — с конкретной записью: второе измерение за сутки получает свою (§4.11)."""
        meds = await _add_med(session, user)
        await session.flush()
        first_id = await _add_measurement(session, user)
        second_id = await _add_measurement(session, user)
        await service.link_measurement_intake(session, user, first_id, [meds[0].id])
        await service.link_measurement_intake(session, user, second_id, [meds[0].id])
        await session.commit()
        assert await self._links(session) == [
            (first_id, meds[0].id),
            (second_id, meds[0].id),
        ]

    async def test_foreign_and_unknown_ids_ignored(
        self, session: AsyncSession, user: User
    ) -> None:
        """Чужие и удалённые id лекарств тихо игнорируются (§7.1)."""
        other = User(telegram_id=222_222)
        session.add(other)
        await session.flush()
        foreign_meds = await _add_med(session, other)
        await session.flush()
        measurement_id = await _add_measurement(session, user)
        await service.link_measurement_intake(
            session, user, measurement_id, [foreign_meds[0].id, 999]
        )
        await session.commit()
        assert await self._links(session) == []

    async def test_foreign_measurement_ignored(
        self, session: AsyncSession, user: User
    ) -> None:
        """Чужая или удалённая запись измерения — тихо без связей (§7.1)."""
        other = User(telegram_id=222_222)
        session.add(other)
        await session.flush()
        meds = await _add_med(session, user)
        await session.flush()
        foreign_measurement_id = await _add_measurement(session, other)
        await service.link_measurement_intake(
            session, user, foreign_measurement_id, [meds[0].id]
        )
        await service.link_measurement_intake(session, user, 999, [meds[0].id])
        await session.commit()
        assert await self._links(session) == []

    async def test_empty_selection_noop(
        self, session: AsyncSession, user: User
    ) -> None:
        measurement_id = await _add_measurement(session, user)
        await service.link_measurement_intake(session, user, measurement_id, [])
        await session.commit()
        assert await self._links(session) == []


class TestIntakeLabelsByMeasurement:
    """Подписи отмеченных приёмов для «Истории записей» (§4.11, §16.2)."""

    async def test_labels_format_and_order(
        self, session: AsyncSession, user: User
    ) -> None:
        """Подписи `fmt_med_item` в порядке `list_medications` (по названию)."""
        losartan = await service.add_medication(session, user, "Лозартан", "50 мг", ("morning",))
        amlodipine = await service.add_medication(session, user, "Амлодипин", "", ("evening",))
        await session.flush()
        measurement_id = await _add_measurement(session, user)
        await service.link_measurement_intake(
            session, user, measurement_id, [losartan[0].id, amlodipine[0].id]
        )
        await session.commit()
        labels = await service.intake_labels_by_measurement(
            session, user, [measurement_id]
        )
        assert labels == {measurement_id: ["Амлодипин", "Лозартан 50 мг"]}

    async def test_without_links_absent(self, session: AsyncSession, user: User) -> None:
        measurement_id = await _add_measurement(session, user)
        await session.commit()
        labels = await service.intake_labels_by_measurement(
            session, user, [measurement_id]
        )
        assert labels == {}

    async def test_empty_ids(self, session: AsyncSession, user: User) -> None:
        assert await service.intake_labels_by_measurement(session, user, []) == {}

    async def test_foreign_links_excluded(
        self, session: AsyncSession, user: User
    ) -> None:
        """Чужие связи в результат не попадают (§7.1)."""
        other = User(telegram_id=222_222)
        session.add(other)
        await session.flush()
        foreign_meds = await _add_med(session, other)
        await session.flush()
        foreign_measurement_id = await _add_measurement(session, other)
        await service.link_measurement_intake(
            session, other, foreign_measurement_id, [foreign_meds[0].id]
        )
        await session.commit()
        labels = await service.intake_labels_by_measurement(
            session, user, [foreign_measurement_id]
        )
        assert labels == {}
