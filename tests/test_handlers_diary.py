"""Тесты хендлера «Дневник за сегодня» (bot/handlers/diary.py, SPEC.md, §6.4)."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, User
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, texts
from bot.handlers import diary
from bot.models import Measurement, User as UserModel

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


def make_state(user_id: int = TG_ID) -> FSMContext:
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=storage, key=key)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture
def edit(mocker):
    return mocker.patch.object(Message, "edit_text", new_callable=mocker.AsyncMock)


@pytest.fixture
def cb_answer(mocker):
    return mocker.patch.object(CallbackQuery, "answer", new_callable=mocker.AsyncMock)


async def add_row(
    session: AsyncSession,
    user: UserModel,
    systolic: int = 135,
    diastolic: int = 85,
    pulse: int | None = 72,
    medication_taken: bool | None = None,
    feeling: str | None = None,
    timestamp: datetime | None = None,
) -> Measurement:
    row = Measurement(
        user_id=user.id,
        timestamp=timestamp or utcnow(),
        systolic=systolic,
        diastolic=diastolic,
        pulse=pulse,
        medication_taken=medication_taken,
        feeling=feeling,
    )
    session.add(row)
    await session.commit()
    return row


class TestDiary:
    async def test_empty_shows_prompt(self, session, edit, cb_answer) -> None:
        await diary.cb_diary(make_callback("menu:diary", make_message()), make_state(), session)
        assert edit.call_args.args[0] == texts.DIARY_EMPTY
        assert edit.call_args.kwargs["reply_markup"] == keyboards.diary_kb(is_empty=True)

    async def test_lists_today_in_user_timezone(
        self, session, user, edit, cb_answer
    ) -> None:
        user.timezone = "Asia/Yekaterinburg"
        await session.commit()
        stamp = utcnow().replace(second=0, microsecond=0)
        await add_row(session, user, timestamp=stamp)
        await diary.cb_diary(make_callback("menu:diary", make_message()), make_state(), session)
        text = edit.call_args.args[0]
        local = stamp.astimezone(ZoneInfo("Asia/Yekaterinburg")).strftime("%H:%M")
        assert text.startswith(f"{local} — 135/85, пульс 72")
        assert texts.DIARY_TOTAL.format(count=1) in text
        assert edit.call_args.kwargs["reply_markup"] == keyboards.diary_kb()

    async def test_shows_flags(self, session, user, edit, cb_answer) -> None:
        await add_row(session, user, medication_taken=True, feeling="headache")
        await diary.cb_diary(make_callback("menu:diary", make_message()), make_state(), session)
        text = edit.call_args.args[0]
        assert "лекарство: да" in text
        assert "головная боль" in text

    async def test_foreign_measurements_hidden(
        self, session, user, edit, cb_answer
    ) -> None:
        other_user = UserModel(telegram_id=222_222)
        session.add(other_user)
        await session.commit()
        await add_row(session, user)
        await add_row(session, other_user, systolic=200)
        await diary.cb_diary(make_callback("menu:diary", make_message()), make_state(), session)
        text = edit.call_args.args[0]
        assert "135/85" in text
        assert "200/" not in text
        assert texts.DIARY_TOTAL.format(count=1) in text

    async def test_no_pulse_line(self, session, user, edit, cb_answer) -> None:
        await add_row(session, user, pulse=None)
        await diary.cb_diary(make_callback("menu:diary", make_message()), make_state(), session)
        line = edit.call_args.args[0].splitlines()[0]
        assert "135/85" in line
        assert "пульс" not in line
