"""Тесты экспорта CSV (bot/services/csv_export.py)."""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import DiaryEvent, Measurement, MedicationLog, User
from bot.services import csv_export as service
from bot.services import events as events_service
from bot.services import measurements as measurements_service
from bot.services import medications as medications_service

UTC = timezone.utc

BOM = b"\xef\xbb\xbf"


def aware(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def decode(content: bytes) -> list[list[str]]:
    """Разобрать CSV-байты в строки (BOM срезается)."""
    text = content.decode("utf-8-sig")
    return list(csv.reader(io.StringIO(text), delimiter=service.CSV_DELIMITER))


def make_measurement(**overrides) -> Measurement:
    data = {
        "user_id": 1,
        "timestamp": aware(2026, 11, 4, 5, 30),  # 08:30 по Минску
        "systolic": 135,
        "diastolic": 85,
        "pulse": 72,
        "medication_taken": True,
        "feeling": "normal",
        "note": None,
    }
    data.update(overrides)
    return Measurement(**data)


class TestBuildMeasurementsCsv:
    def test_header_and_bom(self) -> None:
        content = service.build_measurements_csv([], "Europe/Minsk")
        assert content.startswith(BOM)
        rows = decode(content)
        assert rows[0] == list(service.MEASUREMENTS_HEADER)

    def test_local_timestamp_format(self) -> None:
        content = service.build_measurements_csv(
            [make_measurement()], "Europe/Minsk"
        )
        row = decode(content)[1]
        assert row[0] == "04.11.2026 08:30"

    def test_utc_timezone(self) -> None:
        content = service.build_measurements_csv([make_measurement()], "UTC")
        row = decode(content)[1]
        assert row[0] == "04.11.2026 05:30"

    def test_values(self) -> None:
        content = service.build_measurements_csv(
            [make_measurement(note="после кофе")], "Europe/Minsk"
        )
        row = decode(content)[1]
        assert row == [
            "04.11.2026 08:30",
            "135",
            "85",
            "72",
            "да",
            "норма",
            "после кофе",
        ]

    def test_optional_fields_empty(self) -> None:
        measurement = make_measurement(
            pulse=None, medication_taken=None, feeling=None, note=None
        )
        row = decode(service.build_measurements_csv([measurement], "Europe/Minsk"))[1]
        assert row[3] == ""
        assert row[4] == ""
        assert row[5] == ""
        assert row[6] == ""

    def test_medication_taken_false(self) -> None:
        row = decode(
            service.build_measurements_csv(
                [make_measurement(medication_taken=False)], "Europe/Minsk"
            )
        )[1]
        assert row[4] == "нет"

    def test_feeling_translated(self) -> None:
        row = decode(
            service.build_measurements_csv(
                [make_measurement(feeling="headache")], "Europe/Minsk"
            )
        )[1]
        assert row[5] == "головная боль"

    def test_note_with_semicolon_quoted(self) -> None:
        content = service.build_measurements_csv(
            [make_measurement(note="давление; после кофе")], "Europe/Minsk"
        )
        rows = decode(content)
        assert rows[1][6] == "давление; после кофе"

    def test_crlf_line_terminator(self) -> None:
        content = service.build_measurements_csv([make_measurement()], "Europe/Minsk")
        assert b"\r\n" in content


class TestBuildMedsCsv:
    def _log(self, **overrides) -> MedicationLog:
        data = {
            "user_id": 1,
            "medication_id": 1,
            "timestamp": aware(2026, 11, 4, 5, 35),
            "status": "taken",
            "slot": "morning",
        }
        data.update(overrides)
        return MedicationLog(**data)

    def test_header(self) -> None:
        content = service.build_meds_csv([], "Europe/Minsk")
        assert decode(content)[0] == list(service.MEDS_HEADER)

    def test_row(self) -> None:
        content = service.build_meds_csv(
            [(self._log(), "Лозартан", "50 мг")], "Europe/Minsk"
        )
        row = decode(content)[1]
        assert row == ["04.11.2026 08:35", "Лозартан", "50 мг", "taken"]


class TestFileName:
    def test_format(self) -> None:
        assert (
            service.file_name("bp_diary", date(2026, 10, 29), date(2026, 11, 4))
            == "bp_diary_20261029-20261104.csv"
        )

    def test_meds_format(self) -> None:
        assert (
            service.file_name("med_intake", date(2026, 11, 1), date(2026, 11, 7))
            == "med_intake_20261101-20261107.csv"
        )


class TestExport:
    async def _populate(self, session: AsyncSession, user: User) -> None:
        for day in (1, 2, 3):
            await measurements_service.save_measurement(
                session, user, 135, 85, 72, timestamp=aware(2026, 11, day, 5, 30)
            )
        meds = await medications_service.add_medication(
            session, user, "Лозартан", "50 мг", ("morning",)
        )
        await session.flush()
        await medications_service.log_slot_intake(
            session, user, "morning", {meds[0].id: "taken"}, timestamp=aware(2026, 11, 1, 5, 35)
        )
        await session.flush()

    async def test_export_period(self, session: AsyncSession, user: User) -> None:
        await self._populate(session, user)
        content = await service.export_measurements(
            session, user, date(2026, 11, 1), date(2026, 11, 2)
        )
        assert content is not None
        rows = decode(content)
        assert len(rows) == 3  # заголовок + 2 записи
        assert rows[1][0] == "01.11.2026 08:30"

    async def test_export_all_time(self, session: AsyncSession, user: User) -> None:
        await self._populate(session, user)
        content = await service.export_measurements(session, user, None, None)
        assert content is not None
        assert len(decode(content)) == 4

    async def test_export_limit_exceeded(self, session: AsyncSession, user: User) -> None:
        await self._populate(session, user)
        content = await service.export_measurements(
            session, user, None, None, limit=service.ALL_TIME_LIMIT
        )
        # 3 записи ≤ 5000 — экспорт проходит; проверим и обратную ветку.
        assert content is not None
        limited = await service.export_measurements(session, user, None, None, limit=2)
        assert limited is None

    async def test_export_meds(self, session: AsyncSession, user: User) -> None:
        await self._populate(session, user)
        content = await service.export_medications(session, user, None, None)
        assert content is not None
        rows = decode(content)
        assert rows[1] == ["01.11.2026 08:35", "Лозартан", "50 мг", "taken"]

    async def test_export_meds_period(self, session: AsyncSession, user: User) -> None:
        await self._populate(session, user)
        content = await service.export_medications(
            session, user, date(2026, 11, 2), date(2026, 11, 7)
        )
        assert content is not None
        assert decode(content) == [list(service.MEDS_HEADER)]

    async def test_export_meds_limit(self, session: AsyncSession, user: User) -> None:
        await self._populate(session, user)
        assert await service.export_medications(session, user, None, None, limit=0) is None

    async def test_isolated_between_users(self, session: AsyncSession, user: User) -> None:
        other = User(telegram_id=222_222)
        session.add(other)
        await session.flush()
        await self._populate(session, user)
        content = await service.export_measurements(session, other, None, None)
        assert content is not None
        assert len(decode(content)) == 1  # только заголовок


class TestBuildEventsCsv:
    def _event(
        self,
        type_: str,
        payload: str | None = None,
        note: str | None = None,
    ) -> DiaryEvent:
        return DiaryEvent(
            user_id=1,
            type=type_,
            moment=aware(2026, 11, 4, 7, 5),
            note=note,
            payload=payload,
        )

    def test_header(self) -> None:
        content = service.build_events_csv([], "Europe/Minsk")
        assert decode(content)[0] == list(service.EVENTS_HEADER)

    def test_food_summary_order_and_text(self) -> None:
        # порядок меток — канон §4.18 (соль, кофе, алкоголь), не порядок в payload
        event = self._event(
            "food", payload=events_service.event_payload("food", marks=["coffee", "salt"])
        )
        row = decode(service.build_events_csv([event], "Europe/Minsk"))[1]
        assert row == ["04.11.2026 10:05", "food", "Солёное, Кофе", ""]

    def test_stress_summary_with_note(self) -> None:
        event = self._event(
            "stress",
            payload=events_service.event_payload("stress", variant="anxiety"),
            note="болела голова",
        )
        row = decode(service.build_events_csv([event], "Europe/Minsk"))[1]
        assert row == ["04.11.2026 10:05", "stress", "Тревога", "болела голова"]

    def test_sleep_and_activity_summary(self) -> None:
        sleep = self._event("sleep", payload=events_service.event_payload("sleep", rating="normal"))
        walk = self._event(
            "activity", payload=events_service.event_payload("activity", duration_min=20)
        )
        rows = decode(service.build_events_csv([sleep, walk], "Europe/Minsk"))
        assert rows[1][2] == "Нормально"
        assert rows[2][2] == "20 минут"

    def test_note_event_summary_empty(self) -> None:
        event = self._event("note", note="текст с ; и \"кавычками\"")
        row = decode(service.build_events_csv([event], "Europe/Minsk"))[1]
        assert row == ["04.11.2026 10:05", "note", "", 'текст с ; и "кавычками"']

    def test_rows_sorted_by_moment(self) -> None:
        early = self._event("note", note="раньше")
        early.moment = aware(2026, 11, 4, 7, 5)
        late = self._event("note", note="позже")
        late.moment = aware(2026, 11, 4, 9, 5)
        rows = decode(service.build_events_csv([late, early], "Europe/Minsk"))
        assert [r[3] for r in rows[1:]] == ["раньше", "позже"]


class TestExportEvents:
    async def _populate(self, session: AsyncSession, user: User) -> None:
        await events_service.create_event(
            session,
            user,
            "food",
            aware(2026, 11, 1, 7, 5),
            payload=events_service.event_payload("food", marks=["salt"]),
        )
        await events_service.create_event(
            session, user, "note", aware(2026, 11, 3, 9, 5), note="заметка"
        )
        await session.flush()

    async def test_export_period(self, session: AsyncSession, user: User) -> None:
        await self._populate(session, user)
        content = await service.export_events(session, user, date(2026, 11, 1), date(2026, 11, 1))
        assert content is not None
        rows = decode(content)
        assert len(rows) == 2  # заголовок + одно событие
        assert rows[1][1] == "food"

    async def test_export_all_time_and_limit(self, session: AsyncSession, user: User) -> None:
        await self._populate(session, user)
        content = await service.export_events(session, user, None, None)
        assert content is not None
        assert len(decode(content)) == 3
        assert await service.export_events(session, user, None, None, limit=1) is None

    async def test_export_foreign_events_hidden(self, session: AsyncSession, user: User) -> None:
        other = User(telegram_id=222_222)
        session.add(other)
        await session.flush()
        await self._populate(session, user)
        await events_service.create_event(
            session, other, "note", aware(2026, 11, 2, 9, 5), note="чужое"
        )
        await session.flush()
        content = await service.export_events(session, user, None, None)
        assert content is not None
        text = content.decode("utf-8-sig")
        assert "заметка" in text and "чужое" not in text
