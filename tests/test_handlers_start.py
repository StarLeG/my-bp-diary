"""Тесты хендлеров /start и онбординга (bot/handlers/start.py, SPEC.md, §6.1–6.2).

Хендлеры вызываются напрямую с реальными `Message`/`CallbackQuery` aiogram;
`answer`/`edit_text` замоканы на уровне классов. FSM — реальный MemoryStorage.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, User
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, texts
from bot.config import DEFAULT_TIMEZONE
from bot.handlers import fallback_router, start, unknown_text
from bot.models import User as UserModel

TG_ID = 111_111


# ─────────────────────────── Фабрики ───────────────────────────


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


async def reload_user(session: AsyncSession) -> UserModel:
    return (
        await session.execute(
            select(UserModel)
            .where(UserModel.telegram_id == TG_ID)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


@pytest.fixture
def answer(mocker):
    return mocker.patch.object(Message, "answer", new_callable=mocker.AsyncMock)


@pytest.fixture
def edit(mocker):
    return mocker.patch.object(Message, "edit_text", new_callable=mocker.AsyncMock)


@pytest.fixture
def cb_answer(mocker):
    return mocker.patch.object(CallbackQuery, "answer", new_callable=mocker.AsyncMock)


@pytest.fixture
def reminders(mocker):
    return mocker.Mock(ensure_slot=mocker.AsyncMock())


# ─────────────────────────── /start ───────────────────────────


class TestCmdStart:
    async def test_first_start_creates_user_and_shows_welcome(
        self, session: AsyncSession, answer
    ) -> None:
        state = make_state()
        await start.cmd_start(make_message("/start"), state, session)
        row = await reload_user(session)
        assert row.onboarded is False
        assert row.timezone == DEFAULT_TIMEZONE
        text = answer.call_args.args[0]
        assert text == texts.WELCOME
        assert answer.call_args.kwargs["reply_markup"] == keyboards.onboarding_start_kb()

    async def test_start_shows_menu_when_onboarded(
        self, session: AsyncSession, user, answer
    ) -> None:
        user.onboarded = True
        await session.commit()
        await start.cmd_start(make_message("/start"), make_state(), session)
        text = answer.call_args.args[0]
        assert text == texts.MAIN_MENU
        assert answer.call_args.kwargs["reply_markup"] == keyboards.main_menu()

    async def test_restarts_interrupted_onboarding(
        self, session: AsyncSession, user, answer
    ) -> None:
        state = make_state()
        await state.set_state(start.Onboarding.tz_input)
        await start.cmd_start(make_message("/start"), state, session)
        text = answer.call_args.args[0]
        assert text.startswith(texts.ONB_RESTARTED)
        assert texts.WELCOME in text
        assert await state.get_state() is None

    async def test_foreign_state_is_not_restart(
        self, session: AsyncSession, user, answer
    ) -> None:
        state = make_state()
        await state.set_state("Measure:draft")
        await start.cmd_start(make_message("/start"), state, session)
        assert answer.call_args.args[0] == texts.WELCOME


class TestCbMenuBack:
    async def test_menu_back_edits_to_menu(self, edit, cb_answer, session) -> None:
        state = make_state()
        await state.set_state(start.Onboarding.tz_input)
        await start.cb_menu_back(make_callback("menu:back", make_message()), state, session)
        assert edit.call_args.args[0] == texts.MAIN_MENU
        assert edit.call_args.kwargs["reply_markup"] == keyboards.main_menu()
        cb_answer.assert_awaited_once()
        assert await state.get_state() is None


# ─────────────────────────── Шаги онбординга ───────────────────────────


class TestOnboardingScreens:
    async def test_onb_start_shows_tz_screen(self, edit, cb_answer, session) -> None:
        await start.cb_onb_start(make_callback("onb:start", make_message()), make_state(), session)
        assert edit.call_args.args[0] == texts.ONB_TZ_ASK
        assert edit.call_args.kwargs["reply_markup"] == keyboards.onboarding_timezone_kb()
        cb_answer.assert_awaited_once()

    async def test_onb_tz_preset_shows_targets_with_offset(
        self, edit, cb_answer
    ) -> None:
        state = make_state()
        await start.cb_onb_tz(make_callback("onb:tz:Europe/Moscow", make_message()), state)
        assert (await state.get_data())["onb_tz"] == "Europe/Moscow"
        text = edit.call_args.args[0]
        assert text.startswith(
            texts.ONB_TZ_CONFIRM.format(tz="Europe/Moscow", offset="+03:00")
        )
        assert text.endswith(texts.ONB_TARGETS_ASK)
        assert edit.call_args.kwargs["reply_markup"] == keyboards.onboarding_targets_kb()

    async def test_onb_tz_other_prompts_input(self, edit, cb_answer) -> None:
        state = make_state()
        await start.cb_onb_tz(make_callback("onb:tz:other", make_message()), state)
        assert edit.call_args.args[0] == texts.ONB_TZ_OTHER_PROMPT
        assert await state.get_state() == start.Onboarding.tz_input.state

    async def test_msg_onb_tz_valid(self, answer) -> None:
        state = make_state()
        await state.set_state(start.Onboarding.tz_input)
        await start.msg_onb_tz(make_message("Asia/Yekaterinburg"), state)
        assert (await state.get_data())["onb_tz"] == "Asia/Yekaterinburg"
        assert await state.get_state() is None
        text = answer.call_args.args[0]
        assert text.startswith(
            texts.ONB_TZ_CONFIRM.format(tz="Asia/Yekaterinburg", offset="+05:00")
        )

    async def test_msg_onb_tz_invalid_keeps_state(self, answer) -> None:
        state = make_state()
        await state.set_state(start.Onboarding.tz_input)
        await start.msg_onb_tz(make_message("НеПояс"), state)
        assert await state.get_state() == start.Onboarding.tz_input.state
        assert "Не знаю такой часовой пояс" in answer.call_args.args[0]
        assert answer.call_args.kwargs["reply_markup"] == keyboards.onboarding_skip_kb("tz")

    async def test_onb_target_preset_shows_slots(self, edit, cb_answer) -> None:
        state = make_state()
        await start.cb_onb_target(make_callback("onb:target:120-80", make_message()), state)
        assert (await state.get_data())["onb_target"] == (120, 80)
        text = edit.call_args.args[0]
        assert text.startswith(texts.ONB_TARGETS_CONFIRM.format(target="120/80"))
        assert texts.ONB_SLOTS_ASK in text
        assert texts.ONB_REMEASURE_NOTE in text
        assert edit.call_args.kwargs["reply_markup"] == keyboards.onboarding_slots_kb([])

    async def test_onb_target_other_prompts_sys(self, edit, cb_answer) -> None:
        state = make_state()
        await start.cb_onb_target(make_callback("onb:target:other", make_message()), state)
        assert edit.call_args.args[0] == texts.ONB_TARGETS_OTHER_PROMPT
        assert await state.get_state() == start.Onboarding.target_sys.state

    async def test_msg_onb_target_sys_valid(self, answer) -> None:
        state = make_state()
        await state.set_state(start.Onboarding.target_sys)
        await start.msg_onb_target_sys(make_message("125"), state)
        assert (await state.get_data())["onb_target_sys"] == 125
        assert await state.get_state() == start.Onboarding.target_dia.state
        assert answer.call_args.args[0] == texts.ONB_TARGETS_OTHER_PROMPT_DIA

    async def test_msg_onb_target_sys_invalid(self, answer) -> None:
        state = make_state()
        await state.set_state(start.Onboarding.target_sys)
        await start.msg_onb_target_sys(make_message("abc"), state)
        assert answer.call_args.args[0] == "Нужно целое число от 70 до 260"
        assert await state.get_state() == start.Onboarding.target_sys.state

    async def test_msg_onb_target_dia_valid(self, answer) -> None:
        state = make_state()
        await state.set_state(start.Onboarding.target_dia)
        await state.update_data(onb_target_sys=125)
        await start.msg_onb_target_dia(make_message("75"), state)
        assert (await state.get_data())["onb_target"] == (125, 75)
        assert await state.get_state() is None
        text = answer.call_args.args[0]
        assert text.startswith(texts.ONB_TARGETS_CONFIRM.format(target="125/75"))

    async def test_msg_onb_target_dia_invalid(self, answer) -> None:
        state = make_state()
        await state.set_state(start.Onboarding.target_dia)
        await start.msg_onb_target_dia(make_message("999"), state)
        assert answer.call_args.args[0] == (
            "Нужно диастолическое от 40 до 160, а получилось 999"
        )
        assert await state.get_state() == start.Onboarding.target_dia.state

    async def test_slot_toggle_add_and_remove(self, edit, cb_answer) -> None:
        state = make_state()
        await state.update_data(onb_target=(130, 80))
        await start.cb_onb_slot(make_callback("onb:slot:morning", make_message()), state)
        assert (await state.get_data())["onb_slots"] == ["morning"]
        await start.cb_onb_slot(make_callback("onb:slot:morning", make_message()), state)
        assert (await state.get_data())["onb_slots"] == []
        assert keyboards.onboarding_slots_kb([]) == edit.call_args.kwargs["reply_markup"]


class TestSlotsAndTime:
    async def test_slots_done_shows_first_time_screen(
        self, edit, cb_answer, session: AsyncSession, user, reminders
    ) -> None:
        state = make_state()
        await state.update_data(onb_slots=["evening", "morning"], onb_target=(130, 80))
        await start.cb_onb_slots_done(
            make_callback("onb:slots:done", make_message()), state, session, reminders
        )
        assert edit.call_args.args[0] == texts.ONB_TIMES_ASK.format(slot="Утро")
        assert (await state.get_data())["onb_pending_slot"] == "morning"
        reminders.ensure_slot.assert_not_awaited()

    async def test_slots_done_without_selection_finishes(
        self, edit, cb_answer, session: AsyncSession, user, reminders
    ) -> None:
        state = make_state()
        await state.update_data(onb_tz="Europe/Minsk", onb_target=(130, 80))
        await start.cb_onb_slots_done(
            make_callback("onb:slots:done", make_message()), state, session, reminders
        )
        assert edit.call_args.args[0] == texts.ONB_DONE
        reminders.ensure_slot.assert_not_awaited()
        assert (await reload_user(session)).onboarded is True

    async def test_slots_none_finishes_without_slots(
        self, edit, cb_answer, session: AsyncSession, user, reminders
    ) -> None:
        state = make_state()
        await start.cb_onb_slots_none(
            make_callback("onb:slots:none", make_message()), state, session, reminders
        )
        assert edit.call_args.args[0] == texts.ONB_DONE
        reminders.ensure_slot.assert_not_awaited()
        assert (await reload_user(session)).onboarded is True

    async def test_time_preset_advances_to_next_slot(
        self, edit, cb_answer, session: AsyncSession, user, reminders
    ) -> None:
        state = make_state()
        await state.update_data(
            onb_slots=["morning", "evening"], onb_target=(130, 80), onb_pending_slot="morning"
        )
        await start.cb_onb_time(
            make_callback("onb:time:morning:07:00", make_message()), state, session, reminders
        )
        data = await state.get_data()
        assert data["onb_times"] == {"morning": "07:00"}
        assert data["onb_pending_slot"] == "evening"
        assert edit.call_args.args[0] == texts.ONB_TIMES_ASK.format(slot="Вечер")
        reminders.ensure_slot.assert_not_awaited()

    async def test_time_skip_uses_default_and_finishes(
        self, edit, cb_answer, session: AsyncSession, user, reminders
    ) -> None:
        state = make_state()
        await state.update_data(
            onb_tz="Europe/Minsk",
            onb_target=(130, 80),
            onb_slots=["noon"],
            onb_pending_slot="noon",
        )
        await start.cb_onb_time(
            make_callback("onb:time:noon:skip", make_message()), state, session, reminders
        )
        assert edit.call_args.args[0] == texts.ONB_DONE
        reminders.ensure_slot.assert_awaited_once_with(
            user.id, "noon", start.DEFAULT_SLOT_TIMES["noon"], "Europe/Minsk"
        )

    async def test_time_other_prompts_input(
        self, edit, cb_answer, session, user, reminders
    ) -> None:
        state = make_state()
        await start.cb_onb_time(
            make_callback("onb:time:morning:other", make_message()),
            state,
            session,
            reminders,
        )
        assert edit.call_args.args[0] == texts.ONB_TIME_OTHER_PROMPT
        data = await state.get_data()
        assert data["onb_pending_slot"] == "morning"
        assert await state.get_state() == start.Onboarding.time_input.state

    async def test_msg_onb_time_valid_next_slot(self, answer, session, user, reminders) -> None:
        state = make_state()
        await state.set_state(start.Onboarding.time_input)
        await state.update_data(
            onb_slots=["morning", "noon"], onb_target=(130, 80), onb_pending_slot="morning"
        )
        await start.msg_onb_time(make_message("07:45"), state, session, reminders)
        assert (await state.get_data())["onb_times"] == {"morning": "07:45"}
        assert answer.call_args.args[0] == texts.ONB_TIMES_ASK.format(slot="Обед")
        assert await state.get_state() is None

    async def test_msg_onb_time_valid_finishes(
        self, answer, session, user, reminders
    ) -> None:
        state = make_state()
        await state.set_state(start.Onboarding.time_input)
        await state.update_data(
            onb_tz="Europe/Minsk",
            onb_target=(130, 80),
            onb_slots=["morning"],
            onb_pending_slot="morning",
        )
        await start.msg_onb_time(make_message("09:15"), state, session, reminders)
        assert answer.call_args.args[0] == texts.ONB_DONE
        reminders.ensure_slot.assert_awaited_once_with(
            user.id, "morning", "09:15", "Europe/Minsk"
        )
        assert (await reload_user(session)).onboarded is True

    async def test_msg_onb_time_invalid_keeps_state(self, answer) -> None:
        state = make_state()
        await state.set_state(start.Onboarding.time_input)
        await state.update_data(onb_pending_slot="morning")
        await start.msg_onb_time(make_message("25:99"), state, None, None)  # type: ignore[arg-type]
        assert await state.get_state() == start.Onboarding.time_input.state
        assert "ЧЧ:ММ" in answer.call_args.args[0]
        assert answer.call_args.kwargs["reply_markup"] == keyboards.onboarding_skip_kb(
            "time:morning"
        )


class TestSkip:
    async def test_skip_tz_uses_default(self, edit, cb_answer) -> None:
        state = make_state()
        await start.cb_onb_skip(make_callback("onb:skip:tz", make_message()), state, None, None)  # type: ignore[arg-type]
        assert (await state.get_data())["onb_tz"] == DEFAULT_TIMEZONE
        text = edit.call_args.args[0]
        assert text.startswith(texts.ONB_TZ_CONFIRM.format(tz=DEFAULT_TIMEZONE, offset="+03:00"))

    async def test_skip_targets_uses_default(self, edit, cb_answer) -> None:
        state = make_state()
        await state.update_data(onb_slots=["morning"])
        await start.cb_onb_skip(make_callback("onb:skip:targets", make_message()), state, None, None)  # type: ignore[arg-type]
        data = await state.get_data()
        assert data["onb_target"] == (130, 80)
        assert edit.call_args.kwargs["reply_markup"] == keyboards.onboarding_slots_kb(["morning"])

    async def test_skip_time_advances_and_finishes(
        self, edit, cb_answer, session, user, reminders
    ) -> None:
        state = make_state()
        await state.update_data(
            onb_tz="Europe/Minsk",
            onb_target=(130, 80),
            onb_slots=["evening"],
            onb_pending_slot="evening",
        )
        await start.cb_onb_skip(
            make_callback("onb:skip:time:evening", make_message()), state, session, reminders
        )
        assert edit.call_args.args[0] == texts.ONB_DONE
        reminders.ensure_slot.assert_awaited_once_with(
            user.id, "evening", start.DEFAULT_SLOT_TIMES["evening"], "Europe/Minsk"
        )

    async def test_skip_unknown_target_is_noop(self, edit, cb_answer) -> None:
        state = make_state()
        await start.cb_onb_skip(make_callback("onb:skip:что-то", make_message()), state, None, None)  # type: ignore[arg-type]
        edit.assert_not_called()
        cb_answer.assert_awaited_once()
        assert await state.get_state() is None


class TestFinish:
    async def test_saves_settings_and_creates_slots_in_order(
        self, edit, session: AsyncSession, user, reminders
    ) -> None:
        state = make_state()
        await state.update_data(
            onb_tz="Europe/Moscow",
            onb_target=(120, 80),
            onb_slots=["evening", "morning"],
            onb_times={"morning": "07:30"},
        )
        await start._finish(make_message(), state, session, reminders, TG_ID)
        row = await reload_user(session)
        assert row.timezone == "Europe/Moscow"
        assert (row.target_systolic, row.target_diastolic) == (120, 80)
        assert row.onboarded is True
        calls = reminders.ensure_slot.call_args_list
        assert [c.args for c in calls] == [
            (row.id, "morning", "07:30", "Europe/Moscow"),
            (row.id, "evening", "21:00", "Europe/Moscow"),
        ]
        assert edit.call_args.args[0] == texts.ONB_DONE
        assert await state.get_state() is None

    async def test_finish_via_message_uses_answer(
        self, answer, session: AsyncSession, user, reminders
    ) -> None:
        state = make_state()
        await start._finish(
            make_message(), state, session, reminders, TG_ID, via_message=True
        )
        assert answer.call_args.args[0] == texts.ONB_DONE
        assert answer.call_args.kwargs["reply_markup"] == keyboards.main_menu()
        reminders.ensure_slot.assert_not_awaited()


class TestFullOnboardingFlow:
    async def test_full_flow_two_slots(
        self, answer, edit, cb_answer, session: AsyncSession, reminders
    ) -> None:
        state = make_state()
        msg = make_message("/start")
        await start.cmd_start(msg, state, session)
        await start.cb_onb_start(make_callback("onb:start", msg), state, session)
        await start.cb_onb_tz(make_callback("onb:tz:Europe/Moscow", msg), state)
        await start.cb_onb_target(make_callback("onb:target:120-80", msg), state)
        await start.cb_onb_slot(make_callback("onb:slot:morning", msg), state)
        await start.cb_onb_slot(make_callback("onb:slot:evening", msg), state)
        await start.cb_onb_slots_done(
            make_callback("onb:slots:done", msg), state, session, reminders
        )
        await start.cb_onb_time(
            make_callback("onb:time:morning:07:00", msg), state, session, reminders
        )
        await start.cb_onb_time(
            make_callback("onb:time:evening:skip", msg), state, session, reminders
        )
        row = await reload_user(session)
        assert row.onboarded is True
        assert row.timezone == "Europe/Moscow"
        assert (row.target_systolic, row.target_diastolic) == (120, 80)
        calls = reminders.ensure_slot.call_args_list
        assert [c.args for c in calls] == [
            (row.id, "morning", "07:00", "Europe/Moscow"),
            (row.id, "evening", start.DEFAULT_SLOT_TIMES["evening"], "Europe/Moscow"),
        ]
        assert edit.call_args.args[0] == texts.ONB_DONE


# ─────────────────────────── Catch-all ───────────────────────────


class TestFallback:
    async def test_free_text_outside_fsm_shows_hint(self, answer) -> None:
        await unknown_text(make_message("просто текст"), make_state())
        assert answer.call_args.args[0] == texts.MENU_HINT
        assert answer.call_args.kwargs["reply_markup"] == keyboards.back_to_menu()

    async def test_silent_inside_fsm(self, answer) -> None:
        state = make_state()
        await state.set_state(start.Onboarding.tz_input)
        await unknown_text(make_message("просто текст"), state)
        answer.assert_not_called()

    def test_fallback_router_exists(self) -> None:
        assert fallback_router.name == "fallback"
