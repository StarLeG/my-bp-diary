"""«📅 Визиты и анализы» в боте (bot/handlers/appointments.py; SPEC.md, §6.13).

Время заморожено: 11.11.2026 12:00 по Минску (09:00 UTC) — в сервисе
`appointments` `datetime.now()` возвращает этот момент.
"""
from __future__ import annotations

import inspect
from datetime import date, datetime, timedelta, timezone

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import EditMessageReplyMarkup
from aiogram.types import CallbackQuery, Chat, Message, User
from sqlalchemy import select, update

from bot import keyboards, texts
from bot.handlers import appointments as h
from bot.models import Appointment, Measurement, User as UserModel
from bot.services import appointments as ap

TG_ID = 111_111
NOW = datetime(2026, 11, 11, 9, 0, tzinfo=timezone.utc)
TODAY = date(2026, 11, 11)


class Frozen(datetime):
    @classmethod
    def now(cls, tz=None):  # noqa: D102
        return NOW if tz is None else NOW.astimezone(tz)


@pytest.fixture(autouse=True)
def frozen(mocker):
    mocker.patch.object(ap, "datetime", Frozen)


def make_message(text: str | None = None, user_id: int = TG_ID) -> Message:
    return Message(
        message_id=1,
        date=NOW,
        chat=Chat(id=user_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="Тест"),
        text=text,
    )


def make_callback(data: str, user_id: int = TG_ID) -> CallbackQuery:
    return CallbackQuery(
        id="q1", from_user=User(id=user_id, is_bot=False, first_name="Тест"),
        chat_instance="ci", data=data, message=make_message(),
    )


@pytest.fixture
def state() -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=TG_ID, user_id=TG_ID))


@pytest.fixture
def edit(mocker):
    return mocker.patch.object(Message, "edit_text", new_callable=mocker.AsyncMock)


@pytest.fixture
def answer(mocker):
    return mocker.patch.object(Message, "answer", new_callable=mocker.AsyncMock)


@pytest.fixture
def cb_answer(mocker):
    return mocker.patch.object(CallbackQuery, "answer", new_callable=mocker.AsyncMock)


async def call(handler, **kwargs) -> None:
    wanted = inspect.signature(handler).parameters
    await handler(**{name: value for name, value in kwargs.items() if name in wanted})


async def press(handler, data, session, state=None, **extra) -> None:
    await call(handler, callback=make_callback(data), session=session, state=state, **extra)


async def send(handler, text, session, state) -> None:
    await call(handler, message=make_message(text), session=session, state=state)


def labels(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


def datas(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


async def add(session, user, **kw) -> Appointment:
    params = {"kind": "doctor", "title": "Кардиолог", "starts_on": TODAY + timedelta(days=3), "starts_at": "10:30", "note": ""}
    params.update(kw)
    appt = await ap.create(session, user, params["kind"], params["title"], params["starts_on"],
                           params["starts_at"], params["note"], now=NOW)
    await session.commit()
    return appt


async def rows(session, user) -> list[Appointment]:
    return list((await session.execute(
        select(Appointment).where(Appointment.user_id == user.id).order_by(Appointment.id)
        .execution_options(populate_existing=True)
    )).scalars())


async def past_row(session, user, days_ago=1, status="planned", kind="doctor", title="Кардиолог") -> Appointment:
    appt = Appointment(user_id=user.id, kind=kind, title=title, starts_on=TODAY - timedelta(days=days_ago),
                       starts_at="10:00", status=status)
    session.add(appt)
    await session.commit()
    return appt


# ─────────────────────────── Список ───────────────────────────


class TestList:
    async def test_empty(self, session, user, edit, cb_answer, state) -> None:
        await press(h.cb_list, "ap:list", session, state)
        assert edit.call_args.args[0] == f"{texts.AP_HEADER}\n\n{texts.AP_EMPTY}"
        assert labels(edit.call_args.kwargs["reply_markup"]) == [texts.BTN_AP_ADD, texts.BTN_IN_MENU]

    async def test_upcoming_and_past(self, session, user, edit, cb_answer, state) -> None:
        a = await add(session, user, note="каб. 212")
        await past_row(session, user)
        await press(h.cb_list, "ap:list", session, state)
        assert edit.call_args.args[0] == (
            "📅 Визиты и анализы\n\nСкоро:\n🩺 Кардиолог — сб 14.11, 10:30\n   каб. 212"
        )
        markup = edit.call_args.kwargs["reply_markup"]
        assert labels(markup) == ["🩺 Кардиолог · 14.11", texts.BTN_AP_ADD, texts.BTN_AP_PAST, texts.BTN_IN_MENU]
        assert f"ap:open:{a.id}" in datas(markup)

    async def test_command(self, session, user, answer, state) -> None:
        await state.set_state(h.AppointmentInput.note)
        await call(h.cmd_visits, message=make_message("/visits"), session=session, state=state)
        assert answer.call_args.args[0].startswith(texts.AP_HEADER)
        assert await state.get_state() is None

    async def test_past(self, session, user, edit, cb_answer, state) -> None:
        await past_row(session, user, days_ago=2, status="done", kind="lab", title="Биохимия")
        await past_row(session, user, days_ago=5)
        await press(h.cb_past, "ap:past", session, state)
        assert edit.call_args.args[0] == (
            "🗂 Прошедшие\n\n🧪 Биохимия — 09.11.2026 · сдано ✓\n🩺 Кардиолог — 06.11.2026 · без отметки"
        )
        assert labels(edit.call_args.kwargs["reply_markup"])[-1] == texts.BTN_BACK

    async def test_past_more(self, session, user, edit, cb_answer, state) -> None:
        for index in range(ap.PAST_LIMIT + 1):
            await past_row(session, user, days_ago=index + 1, title=f"Т{index}")
        await press(h.cb_past, "ap:past", session, state)
        assert edit.call_args.args[0].endswith(texts.AP_PAST_MORE)


# ─────────────────────────── Добавление ───────────────────────────


class TestAdd:
    async def test_full_flow(self, session, user, edit, cb_answer, answer, state) -> None:
        await press(h.cb_add, "ap:add", session, state)
        assert edit.call_args.args[0] == texts.AP_KIND_ASK
        await press(h.cb_kind, "ap:kind:doctor", session, state)
        assert edit.call_args.args[0] == "К какому врачу?"
        assert labels(edit.call_args.kwargs["reply_markup"])[:2] == ["Терапевт", "Кардиолог"]
        await press(h.cb_title, "ap:title:doctor:1", session, state)
        assert edit.call_args.args[0] == texts.AP_DAY_ASK
        cal = datas(edit.call_args.kwargs["reply_markup"])
        assert "ap:day:20261111" in cal and "ap:day:20261110" not in cal and "ap:cal:202612" in cal
        await press(h.cb_day, "ap:day:20261114", session, state)
        assert edit.call_args.args[0] == texts.AP_TIME_ASK
        await press(h.cb_time, "ap:time:1000", session, state)
        assert edit.call_args.args[0] == texts.AP_NOTE_ASK
        assert await state.get_state() == h.AppointmentInput.note.state
        await send(h.msg_note, "  поликлиника №5  ", session, state)
        assert answer.call_args.args[0] == (
            "Готово ✓\n🩺 Кардиолог — сб 14.11, 10:00\n   поликлиника №5\nНапомню накануне в 20:00 и за 2 часа."
        )
        [row] = await rows(session, user)
        assert (row.kind, row.title, row.starts_on, row.starts_at, row.note) == (
            "doctor", "Кардиолог", date(2026, 11, 14), "10:00", "поликлиника №5",
        )
        assert await state.get_state() is None

    async def test_other_title_no_time_skip(self, session, user, edit, cb_answer, answer, state) -> None:
        await press(h.cb_add, "ap:add", session, state)
        await press(h.cb_kind, "ap:kind:lab", session, state)
        await press(h.cb_title, "ap:title:other", session, state)
        assert edit.call_args.args[0] == texts.AP_TITLE_PROMPT
        await send(h.msg_title, "я" * 61, session, state)
        assert answer.call_args.args[0] == texts.AP_TITLE_TOO_LONG
        await send(h.msg_title, "   ", session, state)
        assert answer.call_args.args[0] == texts.AP_TITLE_PROMPT
        await send(h.msg_title, "Глюкоза", session, state)
        assert answer.call_args.args[0] == texts.AP_DAY_ASK
        await press(h.cb_day, "ap:day:20261111", session, state)
        await press(h.cb_time, "ap:time:none", session, state)
        await press(h.cb_note_skip, "ap:note:skip", session, state)
        assert edit.call_args.args[0].startswith("Готово ✓\n🧪 Глюкоза — ср 11.11, без времени\n")
        await press(h.cb_note_skip, "ap:note:skip", session, state)  # двойное нажатие
        cb_answer.assert_awaited_with(texts.STALE_MESSAGE)
        assert len(await rows(session, user)) == 1

    async def test_other_time(self, session, user, edit, cb_answer, answer, state) -> None:
        await press(h.cb_add, "ap:add", session, state)
        await press(h.cb_kind, "ap:kind:exam", session, state)
        await press(h.cb_title, "ap:title:exam:0", session, state)
        await press(h.cb_day, "ap:day:20261111", session, state)
        await press(h.cb_time, "ap:time:other", session, state)
        assert edit.call_args.args[0] == texts.AP_TIME_PROMPT
        await send(h.msg_time, "abc", session, state)
        assert answer.call_args.args[0] == texts.AP_TIME_INVALID
        await send(h.msg_time, "11:00", session, state)
        assert answer.call_args.args[0] == texts.AP_TIME_PASSED_INPUT
        await send(h.msg_time, "19:45", session, state)
        assert answer.call_args.args[0] == texts.AP_NOTE_ASK

    async def test_time_passed_toast(self, session, user, edit, cb_answer, state) -> None:
        await press(h.cb_add, "ap:add", session, state)
        await press(h.cb_kind, "ap:kind:doctor", session, state)
        await press(h.cb_title, "ap:title:doctor:0", session, state)
        await press(h.cb_day, "ap:day:20261111", session, state)
        await press(h.cb_time, "ap:time:0900", session, state)
        cb_answer.assert_awaited_with(texts.AP_TIME_PASSED)

    @pytest.mark.parametrize(("day", "toast"), [("20261110", texts.AP_DAY_PAST), ("20281112", texts.AP_DAY_TOO_FAR)])
    async def test_day_problems(self, session, user, edit, cb_answer, state, day, toast) -> None:
        await press(h.cb_add, "ap:add", session, state)
        await press(h.cb_kind, "ap:kind:doctor", session, state)
        await press(h.cb_title, "ap:title:doctor:0", session, state)
        await press(h.cb_day, f"ap:day:{day}", session, state)
        cb_answer.assert_awaited_with(toast)

    async def test_limit(self, session, user, edit, cb_answer, state) -> None:
        for index in range(ap.UPCOMING_LIMIT):
            await add(session, user, title=f"В{index}")
        await press(h.cb_add, "ap:add", session, state)
        cb_answer.assert_awaited_with(texts.AP_LIMIT)
        edit.assert_not_awaited()

    async def test_limit_at_save(self, session, user, edit, cb_answer, state) -> None:
        await press(h.cb_add, "ap:add", session, state)
        await press(h.cb_kind, "ap:kind:doctor", session, state)
        await press(h.cb_title, "ap:title:doctor:0", session, state)
        await press(h.cb_day, "ap:day:20261120", session, state)
        await press(h.cb_time, "ap:time:1000", session, state)
        for index in range(ap.UPCOMING_LIMIT):
            await add(session, user, title=f"В{index}")
        await press(h.cb_note_skip, "ap:note:skip", session, state)
        cb_answer.assert_awaited_with(texts.AP_LIMIT)

    async def test_calendar_navigation(self, session, user, edit, cb_answer, state) -> None:
        await press(h.cb_add, "ap:add", session, state)
        await press(h.cb_kind, "ap:kind:doctor", session, state)
        await press(h.cb_title, "ap:title:doctor:0", session, state)
        await press(h.cb_calendar_month, "ap:cal:202811", session, state)
        cal = datas(edit.call_args.kwargs["reply_markup"])
        assert "ap:day:20281111" in cal and "ap:day:20281112" not in cal and not any(d.startswith("ap:cal:202812") for d in cal)
        for bad in ("ap:cal:202610", "ap:cal:202812", "ap:cal:2026", "ap:cal:202613"):
            cb_answer.reset_mock()
            await press(h.cb_calendar_month, bad, session, state)
            cb_answer.assert_awaited_with(texts.STALE_MESSAGE)

    @pytest.mark.parametrize(
        ("handler", "data"),
        [
            ("cb_kind", "ap:kind:dentist"),
            ("cb_kind", "ap:kind:doctor"),
            ("cb_title", "ap:title:doctor:0"),
            ("cb_calendar_month", "ap:cal:202612"),
            ("cb_day", "ap:day:20261114"),
            ("cb_time", "ap:time:1000"),
            ("cb_time", "ap:time:other"),
            ("cb_note_skip", "ap:note:skip"),
            ("cb_back", "ap:back"),
        ],
    )
    async def test_without_draft_stale(self, session, user, edit, cb_answer, state, handler, data) -> None:
        await press(getattr(h, handler), data, session, state)
        cb_answer.assert_awaited_with(texts.STALE_MESSAGE)

    @pytest.mark.parametrize("data", ["ap:title:doctor:9", "ap:title:lab:0", "ap:title:doctor:x"])
    async def test_bad_title_stale(self, session, user, edit, cb_answer, state, data) -> None:
        await press(h.cb_add, "ap:add", session, state)
        await press(h.cb_kind, "ap:kind:doctor", session, state)
        await press(h.cb_title, data, session, state)
        cb_answer.assert_awaited_with(texts.STALE_MESSAGE)

    @pytest.mark.parametrize("data", ["ap:day:2026111x", "ap:time:0630", "ap:time:1930", "ap:time:1015", "ap:time:zz"])
    async def test_bad_day_time_stale(self, session, user, edit, cb_answer, state, data) -> None:
        await press(h.cb_add, "ap:add", session, state)
        await press(h.cb_kind, "ap:kind:doctor", session, state)
        await press(h.cb_title, "ap:title:doctor:0", session, state)
        if data.startswith("ap:time"):
            await press(h.cb_day, "ap:day:20261120", session, state)
            await press(h.cb_time, data, session, state)
        else:
            await press(h.cb_day, data, session, state)
        cb_answer.assert_awaited_with(texts.STALE_MESSAGE)

    async def test_note_too_long(self, session, user, edit, cb_answer, answer, state) -> None:
        await state.set_state(h.AppointmentInput.note)
        await send(h.msg_note, "я" * 201, session, state)
        assert answer.call_args.args[0] == texts.AP_NOTE_TOO_LONG

    async def test_free_input_without_draft(self, session, user, answer, state) -> None:
        await state.set_state(h.AppointmentInput.title)
        await send(h.msg_title, "Кардиолог", session, state)
        assert answer.call_args.args[0] == texts.STALE_MESSAGE
        await state.set_state(h.AppointmentInput.time)
        await send(h.msg_time, "10:00", session, state)
        assert answer.call_args.args[0] == texts.STALE_MESSAGE
        await state.set_state(h.AppointmentInput.note)
        await send(h.msg_note, "каб. 1", session, state)
        assert answer.call_args.args[0] == texts.STALE_MESSAGE


class TestBack:
    async def test_add_steps(self, session, user, edit, cb_answer, state) -> None:
        await press(h.cb_add, "ap:add", session, state)
        await press(h.cb_kind, "ap:kind:doctor", session, state)
        await press(h.cb_title, "ap:title:doctor:0", session, state)
        await press(h.cb_day, "ap:day:20261120", session, state)
        await press(h.cb_time, "ap:time:1000", session, state)
        expected = [texts.AP_TIME_ASK, texts.AP_DAY_ASK, "К какому врачу?", texts.AP_KIND_ASK]
        seen = []
        for _ in expected:
            await press(h.cb_back, "ap:back", session, state)
            seen.append(edit.call_args.args[0])
        assert seen == expected

    async def test_from_free_input(self, session, user, edit, cb_answer, state) -> None:
        await press(h.cb_add, "ap:add", session, state)
        await press(h.cb_kind, "ap:kind:lab", session, state)
        await press(h.cb_title, "ap:title:other", session, state)
        await press(h.cb_back, "ap:back", session, state)
        assert edit.call_args.args[0] == "Какие анализы?" and await state.get_state() is None
        await press(h.cb_title, "ap:title:lab:0", session, state)
        await press(h.cb_day, "ap:day:20261120", session, state)
        await press(h.cb_time, "ap:time:other", session, state)
        await press(h.cb_back, "ap:back", session, state)
        assert edit.call_args.args[0] == texts.AP_TIME_ASK

    async def test_edit_first_step_returns_card(self, session, user, edit, cb_answer, state) -> None:
        appt = await add(session, user)
        await press(h.cb_edit, f"ap:edit:{appt.id}:when", session, state)
        await press(h.cb_back, "ap:back", session, state)
        assert edit.call_args.args[0].startswith("🩺 Кардиолог\n")
        await press(h.cb_edit, f"ap:edit:{appt.id}:when", session, state)
        await press(h.cb_day, "ap:day:20261120", session, state)
        await press(h.cb_back, "ap:back", session, state)
        assert edit.call_args.args[0] == texts.AP_DAY_ASK

    async def test_deleted_meanwhile(self, session, user, edit, cb_answer, state) -> None:
        appt = await add(session, user)
        await press(h.cb_edit, f"ap:edit:{appt.id}:note", session, state)
        await ap.remove(session, user, appt.id)
        await session.commit()
        await press(h.cb_back, "ap:back", session, state)
        cb_answer.assert_awaited_with(texts.STALE_MESSAGE)


# ─────────────────────────── Карточка и правка ───────────────────────────


class TestCard:
    async def test_states(self, session, user, edit, cb_answer, state) -> None:
        up = await add(session, user)
        await press(h.cb_open, f"ap:open:{up.id}", session, state)
        assert labels(edit.call_args.kwargs["reply_markup"]) == [
            texts.BTN_AP_REPORT, texts.BTN_AP_EDIT, "Отменить визит", texts.BTN_AP_DELETE, texts.BTN_BACK,
        ]
        waiting = await past_row(session, user, days_ago=1, kind="lab", title="Биохимия")
        await press(h.cb_open, f"ap:open:{waiting.id}", session, state)
        assert labels(edit.call_args.kwargs["reply_markup"])[:3] == ["✓ Сдал(а)", "Перенёс(ла)", "Не сдал(а)"]
        assert datas(edit.call_args.kwargs["reply_markup"])[-1] == "ap:past"
        closed = await past_row(session, user, days_ago=10)
        await press(h.cb_open, f"ap:open:{closed.id}", session, state)
        assert labels(edit.call_args.kwargs["reply_markup"]) == [texts.BTN_AP_DELETE, texts.BTN_BACK]
        assert edit.call_args.args[0].endswith("Статус: без отметки")

    async def test_foreign_stale(self, session, user, edit, cb_answer, state) -> None:
        other = UserModel(telegram_id=222, timezone="Europe/Minsk")
        session.add(other)
        await session.commit()
        foreign = await add(session, other)
        for handler, data in [
            (h.cb_open, f"ap:open:{foreign.id}"), (h.cb_edit, f"ap:edit:{foreign.id}"),
            (h.cb_delete, f"ap:del:{foreign.id}:ok"), (h.cb_cancel, f"ap:cancel:{foreign.id}:ok"),
            (h.cb_seen, f"ap:seen:{foreign.id}"), (h.cb_move, f"ap:move:{foreign.id}"),
            (h.cb_after, f"ap:after:{foreign.id}:done"), (h.cb_next, f"ap:next:{foreign.id}:1"),
            (h.cb_report, f"ap:report:{foreign.id}"), (h.cb_send, f"ap:send:{foreign.id}"),
            (h.cb_open, "ap:open:abc"),
        ]:
            cb_answer.reset_mock()
            await press(handler, data, session, state, bot=None)
            cb_answer.assert_awaited_with(texts.STALE_MESSAGE)
        assert (await rows(session, other))[0].status == "planned"

    async def test_edit_menu_when(self, session, user, edit, cb_answer, state) -> None:
        appt = await add(session, user)
        await press(h.cb_edit, f"ap:edit:{appt.id}", session, state)
        assert edit.call_args.args[0] == texts.AP_EDIT_ASK
        await press(h.cb_edit, f"ap:edit:{appt.id}:when", session, state)
        await press(h.cb_day, "ap:day:20261125", session, state)
        await press(h.cb_time, "ap:time:none", session, state)
        assert edit.call_args.args[0] == "Готово ✓\n🩺 Кардиолог — ср 25.11, без времени\nНапомню накануне в 20:00 и в день события в 07:00."
        [row] = await rows(session, user)
        assert (row.starts_on, row.starts_at) == (date(2026, 11, 25), None)

    async def test_edit_title(self, session, user, edit, cb_answer, answer, state) -> None:
        appt = await add(session, user)
        await press(h.cb_edit, f"ap:edit:{appt.id}:title", session, state)
        assert edit.call_args.args[0] == "К какому врачу?"
        await press(h.cb_title, "ap:title:doctor:0", session, state)
        assert edit.call_args.args[0].startswith("🩺 Терапевт\n")
        await press(h.cb_edit, f"ap:edit:{appt.id}:title", session, state)
        await press(h.cb_title, "ap:title:other", session, state)
        await send(h.msg_title, "Флеболог", session, state)
        assert answer.call_args.args[0].startswith("🩺 Флеболог\n")

    async def test_edit_note(self, session, user, edit, cb_answer, answer, state) -> None:
        appt = await add(session, user)
        await press(h.cb_edit, f"ap:edit:{appt.id}:note", session, state)
        assert texts.BTN_AP_NOTE_CLEAR not in labels(edit.call_args.kwargs["reply_markup"])
        await send(h.msg_note, "каб. 7", session, state)
        assert "каб. 7" in answer.call_args.args[0]
        await press(h.cb_edit, f"ap:edit:{appt.id}:note", session, state)
        assert texts.BTN_AP_NOTE_CLEAR in labels(edit.call_args.kwargs["reply_markup"])
        await press(h.cb_note_skip, "ap:note:skip", session, state)
        assert "каб. 7" in edit.call_args.args[0]  # «Пропустить» при правке — без изменений
        await press(h.cb_edit, f"ap:edit:{appt.id}:note:clear", session, state)
        assert (await rows(session, user))[0].note == ""

    async def test_edit_stale(self, session, user, edit, cb_answer, state) -> None:
        appt = await add(session, user)
        past = await past_row(session, user)
        for data in (f"ap:edit:{appt.id}:color", f"ap:edit:{past.id}", "ap:edit"):
            cb_answer.reset_mock()
            await press(h.cb_edit, data, session, state)
            cb_answer.assert_awaited_with(texts.STALE_MESSAGE)

    async def test_note_deleted_meanwhile(self, session, user, edit, cb_answer, state) -> None:
        appt = await add(session, user)
        await press(h.cb_edit, f"ap:edit:{appt.id}:note", session, state)
        await ap.remove(session, user, appt.id)
        await session.commit()
        await press(h.cb_note_skip, "ap:note:skip", session, state)
        cb_answer.assert_awaited_with(texts.STALE_MESSAGE)

    async def test_title_deleted_meanwhile(self, session, user, edit, cb_answer, state) -> None:
        appt = await add(session, user)
        await press(h.cb_edit, f"ap:edit:{appt.id}:title", session, state)
        await ap.remove(session, user, appt.id)
        await session.commit()
        await press(h.cb_title, "ap:title:doctor:0", session, state)
        cb_answer.assert_awaited_with(texts.STALE_MESSAGE)

    async def test_delete(self, session, user, edit, cb_answer, state) -> None:
        appt = await add(session, user)
        await press(h.cb_delete, f"ap:del:{appt.id}", session, state)
        assert edit.call_args.args[0] == "Удалить «Кардиолог — 14.11»? Это нельзя отменить."
        await press(h.cb_delete, f"ap:del:{appt.id}:zz", session, state)
        cb_answer.assert_awaited_with(texts.STALE_MESSAGE)
        await press(h.cb_delete, f"ap:del:{appt.id}:ok", session, state)
        assert edit.call_args.args[0].startswith(f"{texts.AP_DELETED}\n\n{texts.AP_HEADER}")
        assert await rows(session, user) == []

    async def test_cancel(self, session, user, edit, cb_answer, state) -> None:
        appt = await add(session, user)
        await press(h.cb_cancel, f"ap:cancel:{appt.id}", session, state)
        assert edit.call_args.args[0] == "Отменить: Кардиолог — сб 14.11, 10:30?"
        await press(h.cb_cancel, f"ap:cancel:{appt.id}:ok", session, state)
        assert edit.call_args.args[0] == "Отменено: Кардиолог — 14.11."
        assert (await rows(session, user))[0].status == "cancelled"
        cb_answer.reset_mock()
        await press(h.cb_cancel, f"ap:cancel:{appt.id}:ok", session, state)
        cb_answer.assert_awaited_with(texts.STALE_MESSAGE)

    async def test_cancel_lost_race(self, session, user, edit, cb_answer, state, mocker) -> None:
        appt = await add(session, user)
        mocker.patch.object(ap, "set_status", mocker.AsyncMock(return_value=False))
        await press(h.cb_cancel, f"ap:cancel:{appt.id}:ok", session, state)
        cb_answer.assert_awaited_with(texts.STALE_MESSAGE)


# ─────────────────────────── Кнопки уведомлений ───────────────────────────


class TestNoticeButtons:
    async def test_seen(self, session, user, cb_answer, state, mocker) -> None:
        appt = await add(session, user)
        clear = mocker.patch.object(Message, "edit_reply_markup", new_callable=mocker.AsyncMock)
        await press(h.cb_seen, f"ap:seen:{appt.id}", session, state)
        clear.assert_awaited_once_with(reply_markup=None)
        clear.side_effect = TelegramBadRequest(method=EditMessageReplyMarkup(), message="message is not modified")
        await press(h.cb_seen, f"ap:seen:{appt.id}", session, state)  # без падения
        assert (await rows(session, user))[0].status == "planned"

    async def test_move(self, session, user, edit, cb_answer, state) -> None:
        appt = await add(session, user)
        await press(h.cb_move, f"ap:move:{appt.id}", session, state)
        assert edit.call_args.args[0] == texts.AP_DAY_ASK
        await press(h.cb_day, "ap:day:20261116", session, state)
        await press(h.cb_time, "ap:time:1200", session, state)
        row = (await rows(session, user))[0]
        assert (row.starts_on, row.starts_at, row.status) == (date(2026, 11, 16), "12:00", "planned")
        past = await past_row(session, user)
        cb_answer.reset_mock()
        await press(h.cb_move, f"ap:move:{past.id}", session, state)
        cb_answer.assert_awaited_with(texts.STALE_MESSAGE)

    async def test_move_after_cancelled_stale(self, session, user, edit, cb_answer, state) -> None:
        appt = await add(session, user)
        await press(h.cb_move, f"ap:move:{appt.id}", session, state)
        await press(h.cb_day, "ap:day:20261116", session, state)
        await ap.set_status(session, appt, "cancelled")
        await session.commit()
        await press(h.cb_time, "ap:time:1200", session, state)
        cb_answer.assert_awaited_with(texts.STALE_MESSAGE)

    async def test_after_done_and_next(self, session, user, edit, cb_answer, state) -> None:
        appt = await past_row(session, user, days_ago=1)
        await press(h.cb_after, f"ap:after:{appt.id}:done", session, state)
        assert edit.call_args.args[0] == texts.AP_DONE
        assert labels(edit.call_args.kwargs["reply_markup"]) == [
            "Через месяц", "Через 3 мес", "Через полгода", "Через год", "Не нужно",
        ]
        await press(h.cb_next, f"ap:next:{appt.id}:3", session, state)
        assert edit.call_args.args[0].startswith("Готово ✓\n🩺 Кардиолог — ср 10.02, 10:00")
        cb_answer.reset_mock()
        await press(h.cb_next, f"ap:next:{appt.id}:3", session, state)  # повтор
        cb_answer.assert_awaited_with(texts.STALE_MESSAGE)
        await press(h.cb_next, f"ap:next:{appt.id}:0", session, state)
        assert edit.call_args.args[0] == texts.AP_NEXT_NO
        cb_answer.reset_mock()
        await press(h.cb_after, f"ap:after:{appt.id}:missed", session, state)  # уже отвечено
        cb_answer.assert_awaited_with(texts.STALE_MESSAGE)

    async def test_after_missed(self, session, user, edit, cb_answer, state) -> None:
        appt = await past_row(session, user, days_ago=0)
        await press(h.cb_after, f"ap:after:{appt.id}:missed", session, state)
        assert edit.call_args.args[0] == texts.AP_MISSED
        assert (await rows(session, user))[0].status == "missed"

    async def test_after_moved(self, session, user, edit, cb_answer, state) -> None:
        appt = await past_row(session, user, days_ago=1, kind="exam", title="ЭКГ")
        await press(h.cb_after, f"ap:after:{appt.id}:moved", session, state)
        assert edit.call_args.args[0] == texts.AP_DAY_ASK
        await press(h.cb_back, "ap:back", session, state)  # отмена шагов — без изменений
        assert (await rows(session, user))[0].status == "planned"
        await press(h.cb_after, f"ap:after:{appt.id}:moved", session, state)
        await press(h.cb_day, "ap:day:20261118", session, state)
        await press(h.cb_time, "ap:time:0900", session, state)
        old, new = await rows(session, user)
        assert old.status == "moved" and (new.title, new.starts_on, new.starts_at, new.status) == (
            "ЭКГ", date(2026, 11, 18), "09:00", "planned",
        )

    async def test_after_moved_limit(self, session, user, edit, cb_answer, state) -> None:
        appt = await past_row(session, user, days_ago=1)
        await press(h.cb_after, f"ap:after:{appt.id}:moved", session, state)
        await press(h.cb_day, "ap:day:20261118", session, state)
        for index in range(ap.UPCOMING_LIMIT):
            await add(session, user, title=f"В{index}")
        await press(h.cb_time, "ap:time:0900", session, state)
        cb_answer.assert_awaited_with(texts.AP_LIMIT)
        await session.refresh(user)  # хендлер откатил общую с тестом сессию
        assert (await rows(session, user))[0].status == "planned"

    async def test_after_moved_race(self, session, user, edit, cb_answer, state, mocker) -> None:
        appt = await past_row(session, user, days_ago=1)
        await press(h.cb_after, f"ap:after:{appt.id}:moved", session, state)
        await press(h.cb_day, "ap:day:20261118", session, state)
        mocker.patch.object(ap, "moved_after", mocker.AsyncMock(return_value=None))
        await press(h.cb_time, "ap:time:0900", session, state)
        cb_answer.assert_awaited_with(texts.STALE_MESSAGE)

    async def test_after_moved_expired(self, session, user, edit, cb_answer, state) -> None:
        appt = await past_row(session, user, days_ago=1)
        await press(h.cb_after, f"ap:after:{appt.id}:moved", session, state)
        await press(h.cb_day, "ap:day:20261118", session, state)
        await ap.set_status(session, appt, "done")
        await session.commit()
        await press(h.cb_time, "ap:time:0900", session, state)
        cb_answer.assert_awaited_with(texts.STALE_MESSAGE)

    @pytest.mark.parametrize("days_ago,answer_value", [(4, "done"), (1, "maybe")])
    async def test_after_stale(self, session, user, edit, cb_answer, state, days_ago, answer_value) -> None:
        appt = await past_row(session, user, days_ago=days_ago)
        await press(h.cb_after, f"ap:after:{appt.id}:{answer_value}", session, state)
        cb_answer.assert_awaited_with(texts.STALE_MESSAGE)

    async def test_after_lost_race(self, session, user, edit, cb_answer, state, mocker) -> None:
        appt = await past_row(session, user, days_ago=1)
        mocker.patch.object(ap, "set_status", mocker.AsyncMock(return_value=False))
        await press(h.cb_after, f"ap:after:{appt.id}:done", session, state)
        cb_answer.assert_awaited_with(texts.STALE_MESSAGE)

    async def test_next_limit_and_bad(self, session, user, edit, cb_answer, state) -> None:
        appt = await past_row(session, user, days_ago=1, status="done")
        await press(h.cb_next, f"ap:next:{appt.id}:2", session, state)
        cb_answer.assert_awaited_with(texts.STALE_MESSAGE)
        for index in range(ap.UPCOMING_LIMIT):
            await add(session, user, title=f"В{index}")
        await press(h.cb_next, f"ap:next:{appt.id}:12", session, state)
        cb_answer.assert_awaited_with(texts.AP_LIMIT)
        await session.refresh(user)  # хендлер откатил общую с тестом сессию
        planned = await past_row(session, user, days_ago=1)
        await press(h.cb_next, f"ap:next:{planned.id}:1", session, state)  # не «Был(а)»
        cb_answer.assert_awaited_with(texts.STALE_MESSAGE)


class TestReport:
    async def test_empty(self, session, user, cb_answer, state) -> None:
        appt = await add(session, user)
        await press(h.cb_report, f"ap:report:{appt.id}", session, state)
        cb_answer.assert_awaited_with(texts.PDF_EMPTY)

    async def test_sends_pdf(self, session, user, cb_answer, state, mocker) -> None:
        appt = await add(session, user)
        session.add(Measurement(user_id=user.id, timestamp=NOW - timedelta(days=2), systolic=130, diastolic=85))
        await session.commit()
        document = mocker.patch.object(Message, "answer_document", new_callable=mocker.AsyncMock)
        await press(h.cb_report, f"ap:report:{appt.id}", session, state)
        assert document.await_args.kwargs["caption"].startswith("Отчёт по давлению за")
        assert document.await_args.args[0].filename.endswith(".pdf")

    async def test_too_large_and_in_progress(self, session, user, cb_answer, state, mocker) -> None:
        from bot.services import pdf_report

        appt = await add(session, user)
        mocker.patch.object(pdf_report, "collect_report", mocker.AsyncMock(side_effect=pdf_report.ReportTooLargeError))
        await press(h.cb_report, f"ap:report:{appt.id}", session, state)
        cb_answer.assert_awaited_with(texts.CSV_TOO_MANY)
        h._report_in_progress.add(TG_ID)
        try:
            await press(h.cb_report, f"ap:report:{appt.id}", session, state)
            cb_answer.assert_awaited_with(texts.PDF_PREPARING)
        finally:
            h._report_in_progress.discard(TG_ID)

    async def test_send_to_doctor(self, session, user, cb_answer, answer, state, mocker) -> None:
        from bot.handlers import doctor

        appt = await add(session, user)
        mocker.patch.object(doctor, "report_text", mocker.AsyncMock(return_value="Ссылка для врача"))
        await press(h.cb_send, f"ap:send:{appt.id}", session, state, bot=None)
        assert answer.call_args.args[0] == "Ссылка для врача"


class TestKeyboards:
    def test_notice_kb(self) -> None:
        doctor = keyboards.ap_notice_kb(5, "doctor", "eve", with_doctor=True)
        assert labels(doctor) == [texts.BTN_AP_REPORT, texts.BTN_AP_SEND, texts.BTN_AP_SEEN, texts.BTN_AP_MOVE, "Отменить визит"]
        assert datas(doctor)[:2] == ["ap:report:5", "ap:send:5"]
        lab = keyboards.ap_notice_kb(6, "lab", "morning")
        assert labels(lab) == [texts.BTN_AP_SEEN, texts.BTN_AP_MOVE, "Отменить"]
        after = keyboards.ap_notice_kb(7, "exam", "after")
        assert datas(after) == ["ap:after:7:done", "ap:after:7:moved", "ap:after:7:missed"]

    def test_time_and_calendar(self) -> None:
        time_kb = keyboards.ap_time_kb("ap:back")
        assert labels(time_kb)[:4] == ["07:00", "08:00", "09:00", "10:00"] and "18:00" in labels(time_kb)
        cal = keyboards.ap_calendar_kb(2026, 11, TODAY, date(2028, 11, 11), "ap:back")
        assert not any(d.startswith("ap:cal:202610") for d in datas(cal))
        assert all(len(d.encode()) <= 64 for d in datas(cal))

    def test_callback_lengths(self) -> None:
        for markup in (keyboards.ap_edit_kb(123456), keyboards.ap_next_kb(123456),
                       keyboards.ap_note_kb("ap:back", clear_id=123456),
                       keyboards.ap_card_kb(123456, "lab", "waiting", "ap:past")):
            assert all(len(d.encode()) <= 64 for d in datas(markup))


class TestEdges:
    async def test_not_modified_ignored_other_raised(self, session, user, edit, cb_answer, state) -> None:
        from aiogram.methods import EditMessageText

        edit.side_effect = TelegramBadRequest(method=EditMessageText(text="x"), message="message is not modified")
        await press(h.cb_list, "ap:list", session, state)
        cb_answer.assert_awaited()
        edit.side_effect = TelegramBadRequest(method=EditMessageText(text="x"), message="chat not found")
        with pytest.raises(TelegramBadRequest):
            await press(h.cb_list, "ap:list", session, state)

    async def test_when_deleted_meanwhile(self, session, user, edit, cb_answer, state) -> None:
        appt = await add(session, user)
        await press(h.cb_edit, f"ap:edit:{appt.id}:when", session, state)
        await press(h.cb_day, "ap:day:20261120", session, state)
        await ap.remove(session, user, appt.id)
        await session.commit()
        await press(h.cb_time, "ap:time:1000", session, state)
        cb_answer.assert_awaited_with(texts.STALE_MESSAGE)

    def test_card_keyboards(self) -> None:
        assert labels(keyboards.ap_card_kb(1, "lab", "upcoming", "ap:list")) == [
            texts.BTN_AP_EDIT, "Отменить", texts.BTN_AP_DELETE, texts.BTN_BACK,
        ]
        assert labels(keyboards.ap_notice_kb(2, "doctor", "soon")) == [
            texts.BTN_AP_REPORT, texts.BTN_AP_SEEN, texts.BTN_AP_MOVE, "Отменить визит",
        ]
        assert labels(keyboards.main_menu())[2] == texts.BTN_APPOINTMENTS
