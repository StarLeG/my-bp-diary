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
        await diary.cb_diary(make_callback("menu:diary", make_message()), session)
        assert edit.call_args.args[0] == texts.DIARY_EMPTY
        assert edit.call_args.kwargs["reply_markup"] == keyboards.diary_kb(is_empty=True)

    async def test_lists_today_in_user_timezone(
        self, session, user, edit, cb_answer
    ) -> None:
        user.timezone = "Asia/Yekaterinburg"
        await session.commit()
        stamp = utcnow().replace(second=0, microsecond=0)
        await add_row(session, user, timestamp=stamp)
        await diary.cb_diary(make_callback("menu:diary", make_message()), session)
        text = edit.call_args.args[0]
        local = stamp.astimezone(ZoneInfo("Asia/Yekaterinburg")).strftime("%H:%M")
        assert text.startswith(f"{local} — 135/85, пульс 72")
        assert texts.DIARY_TOTAL.format(count=1) in text
        assert edit.call_args.kwargs["reply_markup"] == keyboards.diary_kb()

    async def test_shows_flags(self, session, user, edit, cb_answer) -> None:
        await add_row(session, user, medication_taken=True, feeling="headache")
        await diary.cb_diary(make_callback("menu:diary", make_message()), session)
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
        await diary.cb_diary(make_callback("menu:diary", make_message()), session)
        text = edit.call_args.args[0]
        assert "135/85" in text
        assert "200/" not in text
        assert texts.DIARY_TOTAL.format(count=1) in text

    async def test_no_pulse_line(self, session, user, edit, cb_answer) -> None:
        await add_row(session, user, pulse=None)
        await diary.cb_diary(make_callback("menu:diary", make_message()), session)
        line = edit.call_args.args[0].splitlines()[0]
        assert "135/85" in line
        assert "пульс" not in line

    async def test_edit_button_in_non_empty_diary(
        self, session, user, edit, cb_answer
    ) -> None:
        await add_row(session, user)
        await diary.cb_diary(make_callback("menu:diary", make_message()), session)
        markup = edit.call_args.kwargs["reply_markup"]
        data_by_text = {
            button.text: button.callback_data
            for row in markup.inline_keyboard
            for button in row
        }
        assert data_by_text[texts.BTN_EDIT_LAST] == "m:edit"

    async def test_no_edit_button_in_empty_diary(self, session, edit, cb_answer) -> None:
        await diary.cb_diary(make_callback("menu:diary", make_message()), session)
        markup = edit.call_args.kwargs["reply_markup"]
        data = {
            button.callback_data
            for row in markup.inline_keyboard
            for button in row
            if button.callback_data
        }
        assert "m:edit" not in data


# ─────────────────────── Лекарства на сегодня (§6.4, 0.5.5, U6) ───────────────────────

from datetime import date, time, timedelta  # noqa: E402

from bot.models import Medication, MedicationLog  # noqa: E402


@pytest.fixture
def reminders_svc(mocker):
    return mocker.Mock(ensure_remeasure=mocker.AsyncMock())


async def add_med(session, user, name="Лозартан", dosage="50 мг", slot="morning") -> Medication:
    row = Medication(user_id=user.id, name=name, dosage=dosage, slot=slot)
    session.add(row)
    await session.commit()
    return row


async def log(session, user, med, status="taken", when: datetime | None = None) -> None:
    session.add(MedicationLog(
        user_id=user.id, medication_id=med.id, timestamp=when or utcnow(),
        status=status, slot=med.slot,
    ))
    await session.commit()


def today_of(user) -> date:
    return datetime.now(ZoneInfo(user.timezone)).date()


def ymd(day: date) -> str:
    return day.strftime("%Y%m%d")


def msg_with(markup) -> Message:
    return Message(
        message_id=5, date=utcnow(), chat=Chat(id=TG_ID, type="private"),
        from_user=User(id=TG_ID, is_bot=True, first_name="Бот"), text="…", reply_markup=markup,
    )


def kb_data(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


async def open_diary(session, edit) -> tuple[str, object]:
    await diary.cb_diary(make_callback("menu:diary", make_message()), session)
    return edit.call_args.args[0], edit.call_args.kwargs["reply_markup"]


class TestDiaryMedsLine:
    async def test_no_meds_no_line(self, session, user, edit, cb_answer) -> None:
        text, markup = await open_diary(session, edit)
        assert "💊" not in text
        assert "d:mt" not in kb_data(markup)

    async def test_crisis_only_no_line(self, session, user, edit, cb_answer) -> None:
        await add_med(session, user, slot="crisis")
        text, _ = await open_diary(session, edit)
        assert "💊" not in text

    async def test_statuses(self, session, user, edit, cb_answer) -> None:
        a = await add_med(session, user, "Лозартан", slot="morning")
        b = await add_med(session, user, "Аспирин", "100 мг", slot="noon")
        c = await add_med(session, user, "Амлодипин", "5 мг", slot="noon")
        d = await add_med(session, user, "Индапамид", "2,5 мг", slot="evening")
        await add_med(session, user, "Каптоприл", "25 мг", slot="crisis")
        await log(session, user, a, "taken")
        await log(session, user, b, "taken")
        await log(session, user, c, "skipped")
        text, markup = await open_diary(session, edit)
        assert text.splitlines()[-1] == (
            "💊 Лекарства: утро ✓ · обед — частично · вечер — ещё не отмечено"
        )
        assert text.splitlines()[0] == texts.DIARY_EMPTY
        assert "d:mt" in kb_data(markup)
        assert d.id

    async def test_all_skipped_and_partial_unlogged(self, session, user, edit, cb_answer) -> None:
        a = await add_med(session, user, "Лозартан", slot="morning")
        b = await add_med(session, user, "Аспирин", "100 мг", slot="evening")
        await add_med(session, user, "Амлодипин", "5 мг", slot="evening")
        await log(session, user, a, "skipped")
        await log(session, user, b, "taken")  # второй вечерний не отмечен
        text, _ = await open_diary(session, edit)
        assert text.splitlines()[-1] == "💊 Лекарства: утро — пропущено · вечер — частично"

    async def test_all_taken_no_button(self, session, user, edit, cb_answer) -> None:
        a = await add_med(session, user)
        await log(session, user, a, "taken")
        await add_row(session, user)
        text, markup = await open_diary(session, edit)
        assert text.splitlines()[-1] == "💊 Лекарства: утро ✓"
        assert texts.DIARY_TOTAL.format(count=1) in text
        assert "d:mt" not in kb_data(markup)
        assert "m:edit" in kb_data(markup)

    async def test_yesterday_log_not_counted(self, session, user, edit, cb_answer) -> None:
        a = await add_med(session, user)
        yesterday = datetime.combine(today_of(user) - timedelta(days=1), time(12), ZoneInfo(user.timezone))
        await log(session, user, a, "taken", when=yesterday.astimezone(timezone.utc))
        text, _ = await open_diary(session, edit)
        assert text.splitlines()[-1] == "💊 Лекарства: утро — ещё не отмечено"


class TestMarkIntake:
    async def test_single_slot_opens_list(self, session, user, edit, cb_answer) -> None:
        med = await add_med(session, user)
        await diary.cb_mark_intake(make_callback("d:mt", make_message()), session)
        assert edit.call_args.args[0] == texts.PARTIAL_PROMPT
        assert edit.call_args.kwargs["reply_markup"] == keyboards.partial_meds_kb(
            "morning", today_of(user), {med.id}, [(med.id, "Лозартан", "50 мг")], area="d"
        )

    async def test_several_slots_choose(self, session, user, edit, cb_answer) -> None:
        a = await add_med(session, user, slot="morning")
        await add_med(session, user, "Аспирин", "100 мг", slot="noon")
        await add_med(session, user, "Индапамид", "2,5 мг", slot="evening")
        await log(session, user, a, "taken")
        await diary.cb_mark_intake(make_callback("d:mt", make_message()), session)
        assert edit.call_args.args[0] == texts.MARK_INTAKE_ASK
        assert kb_data(edit.call_args.kwargs["reply_markup"]) == ["d:mt:noon", "d:mt:evening", "menu:diary"]

    async def test_choose_slot(self, session, user, edit, cb_answer) -> None:
        await add_med(session, user, slot="morning")
        med = await add_med(session, user, "Аспирин", "100 мг", slot="noon")
        await diary.cb_mark_intake(make_callback("d:mt:noon", make_message()), session)
        assert kb_data(edit.call_args.kwargs["reply_markup"])[0] == f"d:pt:noon:{ymd(today_of(user))}:{med.id}"

    @pytest.mark.parametrize("data", ["d:mt", "d:mt:morning", "d:mt:crisis", "d:mt:zzz"])
    async def test_nothing_to_mark_stale(self, session, user, edit, cb_answer, data) -> None:
        a = await add_med(session, user)
        await log(session, user, a, "taken")
        await diary.cb_mark_intake(make_callback(data, make_message()), session)
        edit.assert_not_called()
        cb_answer.assert_awaited_once_with(texts.STALE_MESSAGE)

    async def test_toggle(self, session, user, edit, cb_answer) -> None:
        med = await add_med(session, user)
        day = today_of(user)
        markup = keyboards.partial_meds_kb("morning", day, {med.id}, [(med.id, "Лозартан", "50 мг")], area="d")
        await diary.cb_diary_toggle(make_callback(f"d:pt:morning:{ymd(day)}:{med.id}", msg_with(markup)), session)
        assert keyboards.partial_checked_ids(edit.call_args.kwargs["reply_markup"]) == set()

    @pytest.mark.parametrize("suffix", ["{y}:{m}", "{t}:x", "{t}:999999", "{t}"])
    async def test_toggle_stale(self, session, user, edit, cb_answer, suffix) -> None:
        med = await add_med(session, user)
        t = ymd(today_of(user))
        y = ymd(today_of(user) - timedelta(days=1))
        data = "d:pt:morning:" + suffix.format(t=t, y=y, m=med.id)
        await diary.cb_diary_toggle(make_callback(data, make_message()), session)
        edit.assert_not_called()
        cb_answer.assert_awaited_once_with(texts.STALE_MESSAGE)

    async def test_done_returns_diary_with_result(self, session, user, edit, cb_answer, reminders_svc) -> None:
        user.med_remeasure_delay = 60
        a = await add_med(session, user, "Лозартан")
        b = await add_med(session, user, "Амлодипин", "5 мг")
        day = today_of(user)
        markup = keyboards.partial_meds_kb(
            "morning", day, {a.id}, [(a.id, "Лозартан", "50 мг"), (b.id, "Амлодипин", "5 мг")], area="d"
        )
        await diary.cb_diary_done(make_callback(f"d:pd:morning:{ymd(day)}", msg_with(markup)), session, reminders_svc)
        lines = edit.call_args.args[0].splitlines()
        assert lines[0] == "Отметил: принято: Лозартан 50 мг; пропущено: Амлодипин 5 мг"
        assert lines[-1] == "💊 Лекарства: утро — частично"
        assert "d:mt" in kb_data(edit.call_args.kwargs["reply_markup"])
        reminders_svc.ensure_remeasure.assert_awaited_once_with(user.id, None, 60)

    async def test_remark_skipped_as_taken(self, session, user, edit, cb_answer, reminders_svc) -> None:
        """«Пропущено» можно переотметить как «принято» (§6.4)."""
        user.med_remeasure_delay = None
        a = await add_med(session, user)
        await log(session, user, a, "skipped")
        day = today_of(user)
        markup = keyboards.partial_meds_kb("morning", day, {a.id}, [(a.id, "Лозартан", "50 мг")], area="d")
        await diary.cb_diary_done(make_callback(f"d:pd:morning:{ymd(day)}", msg_with(markup)), session, reminders_svc)
        lines = edit.call_args.args[0].splitlines()
        assert lines[0] == "Отметил: принято: Лозартан 50 мг"
        assert lines[-1] == "💊 Лекарства: утро ✓"
        rows = (await session.execute(
            MedicationLog.__table__.select().where(MedicationLog.user_id == user.id))).all()
        assert [r.status for r in rows] == ["taken"]
        reminders_svc.ensure_remeasure.assert_not_awaited()

    async def test_done_nothing_new_already(self, session, user, edit, cb_answer, reminders_svc) -> None:
        a = await add_med(session, user)
        await log(session, user, a, "taken")
        day = today_of(user)
        markup = keyboards.partial_meds_kb("morning", day, {a.id}, [(a.id, "Лозартан", "50 мг")], area="d")
        await diary.cb_diary_done(make_callback(f"d:pd:morning:{ymd(day)}", msg_with(markup)), session, reminders_svc)
        assert edit.call_args.args[0].splitlines()[0] == "Эти лекарства уже отмечены сегодня ✓: Лозартан 50 мг"

    @pytest.mark.parametrize("data", ["d:pd:morning:20200101", "d:pd:zzz:{t}", "d:pd:noon:{t}"])
    async def test_done_stale(self, session, user, edit, cb_answer, reminders_svc, data) -> None:
        await add_med(session, user)
        await diary.cb_diary_done(
            make_callback(data.format(t=ymd(today_of(user))), make_message()), session, reminders_svc
        )
        edit.assert_not_called()
        cb_answer.assert_awaited_once_with(texts.STALE_MESSAGE)

    async def test_cancel_returns_diary(self, session, user, edit, cb_answer) -> None:
        await add_med(session, user)
        await diary.cb_diary_cancel(make_callback(f"d:pc:morning:{ymd(today_of(user))}", make_message()), session)
        assert edit.call_args.args[0].splitlines()[-1] == "💊 Лекарства: утро — ещё не отмечено"

    async def test_cancel_stale(self, session, user, edit, cb_answer) -> None:
        await diary.cb_diary_cancel(make_callback("d:pc:morning:20200101", make_message()), session)
        cb_answer.assert_awaited_once_with(texts.STALE_MESSAGE)
