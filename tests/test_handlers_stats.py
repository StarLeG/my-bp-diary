"""Тесты хендлеров статистики и экспорта CSV (bot/handlers/stats.py, SPEC.md, §6.5, §10)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from aiogram.types import CallbackQuery, Chat, Message, User
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, texts
from bot.handlers import stats
from bot.models import (
    Measurement,
    Medication,
    MedicationLog,
    User as UserModel,
)
from bot.services import csv_export

TG_ID = 111_111


def make_message(text: str | None = None, user_id: int = TG_ID) -> Message:
    return Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=user_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="Тест"),
        text=text,
    )


def make_callback(data: str, message: Message, user_id: int = TG_ID) -> CallbackQuery:
    return CallbackQuery(
        id="q1",
        from_user=User(id=user_id, is_bot=False, first_name="Тест"),
        chat_instance="ci",
        data=data,
        message=message,
    )


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture
def edit(mocker):
    return mocker.patch.object(Message, "edit_text", new_callable=mocker.AsyncMock)


@pytest.fixture
def doc(mocker):
    return mocker.patch.object(Message, "answer_document", new_callable=mocker.AsyncMock)


@pytest.fixture
def cb_answer(mocker):
    return mocker.patch.object(CallbackQuery, "answer", new_callable=mocker.AsyncMock)


async def add_row(
    session: AsyncSession,
    user: UserModel,
    systolic: int = 135,
    diastolic: int = 85,
    pulse: int | None = 72,
    timestamp: datetime | None = None,
) -> Measurement:
    row = Measurement(
        user_id=user.id,
        timestamp=timestamp or utcnow(),
        systolic=systolic,
        diastolic=diastolic,
        pulse=pulse,
    )
    session.add(row)
    await session.commit()
    return row


def today_of(user: UserModel):
    return datetime.now(ZoneInfo(user.timezone)).date()


class TestPeriodsScreen:
    async def test_menu_shows_periods(self, session, edit, cb_answer) -> None:
        await stats.cb_stats_periods(make_callback("menu:stats", make_message()))
        assert edit.call_args.args[0] == texts.BTN_STATS
        assert edit.call_args.kwargs["reply_markup"] == keyboards.stats_period_kb()

    async def test_periods_returns(self, session, edit, cb_answer) -> None:
        await stats.cb_stats_periods(make_callback("stats:periods", make_message()))
        assert edit.call_args.args[0] == texts.BTN_STATS
        assert edit.call_args.kwargs["reply_markup"] == keyboards.stats_period_kb()


class TestSummary:
    async def test_empty_shows_prompt(self, session, user, edit, cb_answer) -> None:
        await stats.cb_stats_show(make_callback("stats:7", make_message()), session)
        assert edit.call_args.args[0] == texts.STATS_EMPTY
        assert edit.call_args.kwargs["reply_markup"] == keyboards.stats_period_kb()

    async def test_shows_summary(self, session, user, edit, cb_answer) -> None:
        today = today_of(user)
        await add_row(session, user, systolic=125, diastolic=75)
        await add_row(session, user, systolic=135, diastolic=85)
        await stats.cb_stats_show(make_callback("stats:7", make_message()), session)
        text = edit.call_args.args[0]
        assert texts.STATS_HEADER.format(
            days=7,
            date_from=(today - timedelta(days=6)).strftime("%d.%m"),
            date_to=today.strftime("%d.%m"),
        ) in text
        assert texts.STATS_COUNT.format(count=2) in text
        assert texts.STATS_SYSTOLIC.format(avg=130, min=125, max=135) in text
        assert texts.STATS_DIASTOLIC.format(avg=80, min=75, max=85) in text
        assert texts.STATS_GOAL.format(
            target=f"{user.target_systolic}/{user.target_diastolic}",
            verdict=texts.STATS_GOAL_OK,
        ) in text
        assert texts.STATS_FOOTER_SUPPORT not in text  # записей < 3
        assert edit.call_args.kwargs["reply_markup"] == keyboards.stats_result_kb()

    async def test_foreign_measurements_hidden(
        self, session, user, edit, cb_answer
    ) -> None:
        other_user = UserModel(telegram_id=222_222)
        session.add(other_user)
        await session.commit()
        await add_row(session, user)
        await add_row(session, other_user, systolic=200)
        await stats.cb_stats_show(make_callback("stats:7", make_message()), session)
        text = edit.call_args.args[0]
        assert texts.STATS_COUNT.format(count=1) in text

    async def test_adherence_line_with_medications(
        self, session, user, edit, cb_answer
    ) -> None:
        med = Medication(user_id=user.id, name="Лозартан", dosage="50 мг", slot="morning")
        session.add(med)
        await session.commit()
        session.add(
            MedicationLog(
                user_id=user.id,
                medication_id=med.id,
                timestamp=utcnow(),
                status="taken",
                slot="morning",
            )
        )
        await session.commit()
        await add_row(session, user)
        await stats.cb_stats_show(make_callback("stats:7", make_message()), session)
        text = edit.call_args.args[0]
        # 1 лекарство × 7 дней = 7 запланированных приёмов
        assert texts.STATS_ADHERENCE.format(taken=1, planned=7) in text

    async def test_no_adherence_line_without_medications(
        self, session, user, edit, cb_answer
    ) -> None:
        await add_row(session, user)
        await stats.cb_stats_show(make_callback("stats:7", make_message()), session)
        assert "Лекарства: принято" not in edit.call_args.args[0]


class TestSupportFooter:
    async def test_shown_when_enough_and_never_shown(
        self, session, user, edit, cb_answer
    ) -> None:
        for _ in range(3):
            await add_row(session, user)
        assert user.last_support_shown_at is None
        await stats.cb_stats_show(make_callback("stats:7", make_message()), session)
        assert texts.STATS_FOOTER_SUPPORT in edit.call_args.args[0]
        await session.refresh(user)
        assert user.last_support_shown_at is not None

    async def test_hidden_below_threshold(
        self, session, user, edit, cb_answer
    ) -> None:
        await add_row(session, user)
        await add_row(session, user)
        await stats.cb_stats_show(make_callback("stats:7", make_message()), session)
        assert texts.STATS_FOOTER_SUPPORT not in edit.call_args.args[0]
        await session.refresh(user)
        assert user.last_support_shown_at is None

    async def test_hidden_within_a_day(
        self, session, user, edit, cb_answer
    ) -> None:
        shown = utcnow() - timedelta(hours=1)
        user.last_support_shown_at = shown
        await session.commit()
        for _ in range(3):
            await add_row(session, user)
        await stats.cb_stats_show(make_callback("stats:7", make_message()), session)
        assert texts.STATS_FOOTER_SUPPORT not in edit.call_args.args[0]
        await session.refresh(user)
        assert user.last_support_shown_at == shown

    async def test_shown_again_after_a_day(
        self, session, user, edit, cb_answer
    ) -> None:
        user.last_support_shown_at = utcnow() - timedelta(hours=25)
        await session.commit()
        for _ in range(3):
            await add_row(session, user)
        await stats.cb_stats_show(make_callback("stats:7", make_message()), session)
        assert texts.STATS_FOOTER_SUPPORT in edit.call_args.args[0]
        await session.refresh(user)
        assert user.last_support_shown_at > utcnow() - timedelta(minutes=1)

    async def test_naive_timestamp_is_normalized(
        self, session, user, edit, cb_answer
    ) -> None:
        """SQLite может вернуть naive datetime — поле трактуется как UTC."""
        naive = datetime.now(timezone.utc).replace(tzinfo=None)
        user.last_support_shown_at = naive - timedelta(hours=1)
        await session.commit()
        for _ in range(3):
            await add_row(session, user)
        await stats.cb_stats_show(make_callback("stats:7", make_message()), session)
        assert texts.STATS_FOOTER_SUPPORT not in edit.call_args.args[0]
        await session.refresh(user)
        user.last_support_shown_at = naive - timedelta(hours=25)
        await session.commit()
        await stats.cb_stats_show(make_callback("stats:7", make_message()), session)
        assert texts.STATS_FOOTER_SUPPORT in edit.call_args.args[0]


class TestCsv:
    async def test_csv_menu(self, session, edit, cb_answer) -> None:
        await stats.cb_csv_menu(make_callback("stats:csv", make_message()))
        assert edit.call_args.args[0] == texts.CSV_ASK
        assert edit.call_args.kwargs["reply_markup"] == keyboards.csv_period_kb()

    async def test_export_7_sends_two_documents(
        self, session, user, doc, cb_answer
    ) -> None:
        await add_row(session, user)
        today = today_of(user)
        await stats.cb_csv_export(make_callback("csv:7", make_message()), session)
        assert doc.await_count == 2
        start = today - timedelta(days=6)
        first, second = doc.await_args_list
        assert first.args[0].filename == (
            f"bp_diary_{start:%Y%m%d}-{today:%Y%m%d}.csv"
        )
        assert second.args[0].filename == (
            f"bp_meds_{start:%Y%m%d}-{today:%Y%m%d}.csv"
        )
        for call in (first, second):
            assert call.kwargs["caption"] == texts.CSV_FILE_CAPTION

    async def test_export_all_time_uses_actual_range(
        self, session, user, doc, cb_answer
    ) -> None:
        stamp = utcnow().replace(second=0, microsecond=0)
        await add_row(session, user, timestamp=stamp)
        await stats.cb_csv_export(make_callback("csv:all", make_message()), session)
        assert doc.await_count == 2
        day = stamp.astimezone(ZoneInfo(user.timezone)).date()
        assert doc.await_args_list[0].args[0].filename == (
            f"bp_diary_{day:%Y%m%d}-{day:%Y%m%d}.csv"
        )

    async def test_export_all_time_empty(
        self, session, user, doc, cb_answer
    ) -> None:
        today = today_of(user)
        await stats.cb_csv_export(make_callback("csv:all", make_message()), session)
        assert doc.await_count == 2
        assert doc.await_args_list[0].args[0].filename == (
            f"bp_diary_{today:%Y%m%d}-{today:%Y%m%d}.csv"
        )

    async def test_export_too_many(
        self, session, user, edit, doc, cb_answer, mocker
    ) -> None:
        mocker.patch.object(csv_export, "ALL_TIME_LIMIT", 1)
        await add_row(session, user)
        await add_row(session, user)
        await stats.cb_csv_export(make_callback("csv:all", make_message()), session)
        assert edit.call_args.args[0] == texts.CSV_TOO_MANY
        assert edit.call_args.kwargs["reply_markup"] == keyboards.csv_period_kb()
        doc.assert_not_awaited()

    async def test_export_hides_foreign_data(
        self, session, user, doc, cb_answer
    ) -> None:
        other_user = UserModel(telegram_id=222_222)
        session.add(other_user)
        await session.commit()
        await add_row(session, user)
        await add_row(session, other_user, systolic=200)
        await stats.cb_csv_export(make_callback("csv:7", make_message()), session)
        content = doc.await_args_list[0].args[0].data
        assert b"135" in content
        assert b"200" not in content
