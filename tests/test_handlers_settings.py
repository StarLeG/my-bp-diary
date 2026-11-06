"""Тесты хендлеров настроек (bot/handlers/settings.py, SPEC.md, §6.6)."""
from __future__ import annotations

import inspect
from datetime import datetime, timezone

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, User
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, schemas, texts
from bot.handlers import settings
from bot.models import Medication, MedicationLog, Reminder, User as UserModel

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


async def call(handler, **kwargs) -> None:
    """Вызвать хендлер как aiogram DI: только параметры из его сигнатуры."""
    wanted = inspect.signature(handler).parameters
    await handler(**{name: value for name, value in kwargs.items() if name in wanted})


@pytest.fixture
def edit(mocker):
    return mocker.patch.object(Message, "edit_text", new_callable=mocker.AsyncMock)


@pytest.fixture
def state() -> FSMContext:
    return make_state()


@pytest.fixture
def answer(mocker):
    return mocker.patch.object(Message, "answer", new_callable=mocker.AsyncMock)


@pytest.fixture
def cb_answer(mocker):
    return mocker.patch.object(CallbackQuery, "answer", new_callable=mocker.AsyncMock)


@pytest.fixture
def reminders(mocker):
    return mocker.Mock(
        ensure_slot=mocker.AsyncMock(),
        disable_slot=mocker.AsyncMock(),
    )


async def add_med(
    session: AsyncSession,
    user: UserModel,
    name: str = "Лозартан",
    dosage: str = "50 мг",
    slot: str = "morning",
) -> Medication:
    row = Medication(user_id=user.id, name=name, dosage=dosage, slot=slot)
    session.add(row)
    await session.commit()
    return row


async def add_med_group(
    session: AsyncSession,
    user: UserModel,
    name: str = "Лозартан",
    dosage: str = "50 мг",
    slots: tuple[str, ...] = ("morning",),
) -> list[Medication]:
    """Лекарство целиком: строка на каждый слот (группа (user_id, name), §4.4)."""
    return [
        await add_med(session, user, name=name, dosage=dosage, slot=slot)
        for slot in slots
    ]


async def add_log(
    session: AsyncSession, user: UserModel, medication: Medication, slot: str
) -> MedicationLog:
    """Лог приёма (для проверки каскада medication_logs при правке/удалении)."""
    log = MedicationLog(
        user_id=user.id,
        medication_id=medication.id,
        timestamp=utcnow(),
        status="taken",
        slot=slot,
    )
    session.add(log)
    await session.commit()
    return log


async def add_reminder(
    session: AsyncSession,
    user: UserModel,
    slot: str = "morning",
    local_time: str = "09:30",
    enabled: bool = True,
) -> Reminder:
    row = Reminder(
        user_id=user.id,
        type=slot,
        local_time=local_time,
        next_trigger_time=utcnow(),
        enabled=enabled,
    )
    session.add(row)
    await session.commit()
    return row


async def meds_of(session: AsyncSession, owner_id: int) -> list[Medication]:
    """Строки medications владельца (для проверок изоляции и удаления)."""
    return list(
        (
            await session.execute(
                select(Medication).where(Medication.user_id == owner_id)
            )
        ).scalars().all()
    )


class TestSettingsMenu:
    async def test_menu(self, session, edit, cb_answer) -> None:
        await call(settings.cb_settings, callback=make_callback("menu:settings", make_message()))
        assert edit.call_args.args[0] == texts.SETTINGS_MENU
        assert edit.call_args.kwargs["reply_markup"] == keyboards.settings_kb()

    async def test_back(self, session, edit, cb_answer) -> None:
        await call(settings.cb_settings, callback=make_callback("set:back", make_message()))
        assert edit.call_args.args[0] == texts.SETTINGS_MENU


class TestTargets:
    async def test_targets_screen(self, session, edit, cb_answer, state) -> None:
        await call(
            settings.cb_targets,
            callback=make_callback("set:targets", make_message()),
            state=state,
        )
        assert edit.call_args.args[0] == texts.TARGETS_ASK
        assert edit.call_args.kwargs["reply_markup"] == keyboards.targets_kb()

    async def test_preset_saves(self, session, user, edit, cb_answer, state) -> None:
        await call(
            settings.cb_target_preset,
            callback=make_callback("set:target:130-80", make_message()),
            state=state,
            session=session,
        )
        await session.refresh(user)
        assert (user.target_systolic, user.target_diastolic) == (130, 80)
        assert edit.call_args.args[0] == texts.TARGETS_SAVED.format(target="130/80")
        assert edit.call_args.kwargs["reply_markup"] == keyboards.settings_kb()

    async def test_other_prompts_input(self, session, edit, cb_answer, state) -> None:
        await call(
            settings.cb_target_other,
            callback=make_callback("set:target:other", make_message()),
            state=state,
        )
        assert edit.call_args.args[0] == texts.TARGETS_OTHER_PROMPT
        assert await state.get_state() == settings.SettingsInput.target_sys.state

    async def test_free_sys_then_dia(self, session, user, answer, state) -> None:
        await state.set_state(settings.SettingsInput.target_sys)
        await call(
            settings.msg_target_sys,
            message=make_message("125"),
            state=state,
        )
        assert answer.call_args.args[0] == texts.TARGETS_OTHER_PROMPT_DIA
        assert await state.get_state() == settings.SettingsInput.target_dia.state
        await call(
            settings.msg_target_dia,
            message=make_message("75"),
            state=state,
            session=session,
        )
        await session.refresh(user)
        assert (user.target_systolic, user.target_diastolic) == (125, 75)
        assert answer.call_args.args[0] == texts.TARGETS_SAVED.format(target="125/75")

    @pytest.mark.parametrize("raw", ["abc", "30"])
    async def test_free_sys_invalid(
        self, session, answer, state, raw: str
    ) -> None:
        await state.set_state(settings.SettingsInput.target_sys)
        await call(settings.msg_target_sys, message=make_message(raw), state=state)
        assert answer.call_args.args[0].startswith("Нужно")
        assert await state.get_state() == settings.SettingsInput.target_sys.state

    @pytest.mark.parametrize("raw", ["abc", "999"])
    async def test_free_dia_invalid(
        self, session, answer, state, raw: str
    ) -> None:
        await state.set_state(settings.SettingsInput.target_dia)
        await call(
            settings.msg_target_dia,
            message=make_message(raw),
            state=state,
            session=session,
        )
        assert answer.call_args.args[0].startswith("Нужно")


class TestReminders:
    async def test_screen_without_rows(self, session, user, edit, cb_answer) -> None:
        await call(
            settings.cb_reminders,
            callback=make_callback("set:reminders", make_message()),
            session=session,
        )
        assert edit.call_args.args[0] == texts.REMINDERS_MENU
        assert edit.call_args.kwargs["reply_markup"] == keyboards.reminders_settings_kb(
            {}, {}, user.med_remeasure_delay
        )

    async def test_screen_shows_enabled_slot(
        self, session, user, edit, cb_answer
    ) -> None:
        await add_reminder(session, user, slot="evening", local_time="21:30", enabled=True)
        await call(
            settings.cb_reminders,
            callback=make_callback("set:reminders", make_message()),
            session=session,
        )
        markup = edit.call_args.kwargs["reply_markup"]
        labels = [btn.text for row in markup.inline_keyboard for btn in row]
        assert "✅ Вечер" in labels
        assert "⏰ 21:30" in labels
        assert "☐ Утро" in labels

    async def test_toggle_enables_with_default_time(
        self, session, user, cb_answer, edit, reminders
    ) -> None:
        await call(
            settings.cb_slot_toggle,
            callback=make_callback("set:slot:morning", make_message()),
            session=session,
            reminders=reminders,
        )
        reminders.ensure_slot.assert_awaited_once_with(
            user.id, "morning", "08:00", user.timezone
        )
        reminders.disable_slot.assert_not_awaited()

    async def test_toggle_enables_with_saved_time(
        self, session, user, cb_answer, edit, reminders
    ) -> None:
        await add_reminder(session, user, slot="morning", local_time="09:30", enabled=False)
        await call(
            settings.cb_slot_toggle,
            callback=make_callback("set:slot:morning", make_message()),
            session=session,
            reminders=reminders,
        )
        reminders.ensure_slot.assert_awaited_once_with(
            user.id, "morning", "09:30", user.timezone
        )

    async def test_toggle_disables(
        self, session, user, cb_answer, edit, reminders
    ) -> None:
        await add_reminder(session, user, slot="morning", enabled=True)
        await call(
            settings.cb_slot_toggle,
            callback=make_callback("set:slot:morning", make_message()),
            session=session,
            reminders=reminders,
        )
        reminders.disable_slot.assert_awaited_once_with(user.id, "morning")
        reminders.ensure_slot.assert_not_awaited()

    async def test_toggle_unknown_slot_ignored(
        self, session, user, cb_answer, edit, reminders
    ) -> None:
        await call(
            settings.cb_slot_toggle,
            callback=make_callback("set:slot:junk", make_message()),
            session=session,
            reminders=reminders,
        )
        reminders.ensure_slot.assert_not_awaited()
        reminders.disable_slot.assert_not_awaited()
        edit.assert_not_awaited()

    async def test_time_show(self, session, edit, cb_answer, reminders, state) -> None:
        await call(
            settings.cb_slot_time,
            callback=make_callback("set:time:morning:show", make_message()),
            state=state,
            session=session,
            reminders=reminders,
        )
        assert edit.call_args.args[0] == texts.SLOT_TIME_ASK.format(slot="Утро")
        assert edit.call_args.kwargs["reply_markup"] == keyboards.slot_time_kb("morning")

    async def test_time_preset(self, session, user, edit, cb_answer, reminders, state) -> None:
        await call(
            settings.cb_slot_time,
            callback=make_callback("set:time:morning:07:00", make_message()),
            state=state,
            session=session,
            reminders=reminders,
        )
        reminders.ensure_slot.assert_awaited_once_with(
            user.id, "morning", "07:00", user.timezone
        )
        assert edit.call_args.args[0] == texts.REMINDERS_MENU

    async def test_time_other_prompts(
        self, session, edit, cb_answer, reminders, state
    ) -> None:
        await call(
            settings.cb_slot_time,
            callback=make_callback("set:time:morning:other", make_message()),
            state=state,
            session=session,
            reminders=reminders,
        )
        assert edit.call_args.args[0] == texts.SLOT_TIME_ASK.format(slot="Утро")
        assert await state.get_state() == settings.SettingsInput.slot_time.state

    async def test_time_invalid(
        self, session, cb_answer, edit, reminders, state
    ) -> None:
        await call(
            settings.cb_slot_time,
            callback=make_callback("set:time:morning:junk", make_message()),
            state=state,
            session=session,
            reminders=reminders,
        )
        reminders.ensure_slot.assert_not_awaited()
        assert cb_answer.call_args.args[0] == schemas.TIME_FORMAT_HINT
        edit.assert_not_awaited()

    async def test_time_unknown_slot_ignored(
        self, session, cb_answer, edit, reminders, state
    ) -> None:
        await call(
            settings.cb_slot_time,
            callback=make_callback("set:time:junk:show", make_message()),
            state=state,
            session=session,
            reminders=reminders,
        )
        edit.assert_not_awaited()

    async def test_free_time(
        self, session, user, answer, reminders, state
    ) -> None:
        await state.set_state(settings.SettingsInput.slot_time)
        await state.update_data(**{settings.T_SLOT: "evening"})
        await call(
            settings.msg_slot_time,
            message=make_message("22:15"),
            state=state,
            session=session,
            reminders=reminders,
        )
        reminders.ensure_slot.assert_awaited_once_with(
            user.id, "evening", "22:15", user.timezone
        )
        assert answer.call_args.args[0] == texts.REMINDERS_MENU

    async def test_free_time_invalid(
        self, session, answer, reminders, state
    ) -> None:
        await state.set_state(settings.SettingsInput.slot_time)
        await call(
            settings.msg_slot_time,
            message=make_message("25:99"),
            state=state,
            session=session,
            reminders=reminders,
        )
        reminders.ensure_slot.assert_not_awaited()
        assert answer.call_args.args[0] == schemas.TIME_FORMAT_HINT

    async def test_remeasure_menu(self, session, edit, cb_answer) -> None:
        await call(
            settings.cb_remeasure_menu,
            callback=make_callback("set:remeasure", make_message()),
        )
        assert edit.call_args.args[0] == texts.REMEASURE_ASK
        assert edit.call_args.kwargs["reply_markup"] == keyboards.remeasure_delay_kb()

    async def test_remeasure_off(self, session, user, edit, cb_answer) -> None:
        await call(
            settings.cb_remeasure_set,
            callback=make_callback("set:remeasure:off", make_message()),
            session=session,
        )
        await session.refresh(user)
        assert user.med_remeasure_delay is None
        assert edit.call_args.args[0] == texts.REMEASURE_SAVED.format(
            value=texts.REMEASURE_OFF
        )

    async def test_remeasure_minutes(self, session, user, edit, cb_answer) -> None:
        await call(
            settings.cb_remeasure_set,
            callback=make_callback("set:remeasure:60", make_message()),
            session=session,
        )
        await session.refresh(user)
        assert user.med_remeasure_delay == 60
        assert edit.call_args.args[0] == texts.REMEASURE_SAVED.format(value="60 минут")


class TestTimezone:
    async def test_tz_screen(self, session, edit, cb_answer) -> None:
        await call(settings.cb_tz_menu, callback=make_callback("set:tz", make_message()))
        assert edit.call_args.args[0] == texts.TZ_ASK
        assert edit.call_args.kwargs["reply_markup"] == keyboards.timezone_kb()

    async def test_tz_preset(self, session, user, edit, cb_answer, state) -> None:
        await call(
            settings.cb_tz_set,
            callback=make_callback("set:tz:Europe/Moscow", make_message()),
            state=state,
            session=session,
        )
        await session.refresh(user)
        assert user.timezone == "Europe/Moscow"
        assert edit.call_args.args[0] == texts.TZ_SAVED.format(tz="Europe/Moscow", offset="+03:00")

    async def test_tz_invalid(self, session, cb_answer, edit, state) -> None:
        await call(
            settings.cb_tz_set,
            callback=make_callback("set:tz:Mars/Olympus", make_message()),
            state=state,
            session=session,
        )
        edit.assert_not_awaited()
        assert cb_answer.call_args.args[0].startswith("Не знаю")

    async def test_tz_other_prompts(self, session, edit, cb_answer, state) -> None:
        await call(
            settings.cb_tz_other,
            callback=make_callback("set:tz:other", make_message()),
            state=state,
        )
        assert edit.call_args.args[0] == texts.TZ_OTHER_PROMPT
        assert await state.get_state() == settings.SettingsInput.tz_input.state

    async def test_free_tz(self, session, user, answer, state) -> None:
        await state.set_state(settings.SettingsInput.tz_input)
        await call(
            settings.msg_tz,
            message=make_message("Asia/Yekaterinburg"),
            state=state,
            session=session,
        )
        await session.refresh(user)
        assert user.timezone == "Asia/Yekaterinburg"
        assert answer.call_args.args[0].startswith("Часовой пояс: Asia/Yekaterinburg")

    @pytest.mark.parametrize("raw", ["", "Mars/Olympus"])
    async def test_free_tz_invalid(self, session, answer, state, raw: str) -> None:
        await state.set_state(settings.SettingsInput.tz_input)
        await call(settings.msg_tz, message=make_message(raw), state=state, session=session)
        assert await state.get_state() == settings.SettingsInput.tz_input.state


class TestCsv:
    async def test_csv_screen(self, session, edit, cb_answer) -> None:
        await call(settings.cb_csv, callback=make_callback("set:csv", make_message()))
        assert edit.call_args.args[0] == texts.CSV_ASK
        assert edit.call_args.kwargs["reply_markup"] == keyboards.csv_period_kb()


class TestMeds:
    async def test_list_empty(self, session, edit, cb_answer) -> None:
        await call(settings.cb_meds, callback=make_callback("set:meds", make_message()), session=session)
        assert edit.call_args.args[0] == texts.MEDS_LIST_EMPTY
        assert edit.call_args.kwargs["reply_markup"] == keyboards.medications_list_kb(False)

    async def test_list_shows_item(self, session, user, edit, cb_answer) -> None:
        await add_med(session, user, name="Лозартан", dosage="50 мг", slot="evening")
        await call(settings.cb_meds, callback=make_callback("set:meds", make_message()), session=session)
        text = edit.call_args.args[0]
        assert texts.MEDS_LIST_HEADER in text
        assert texts.MEDS_ITEM.format(
            med=texts.fmt_med_item("Лозартан", "50 мг"), slots="Вечер"
        ) in text
        assert edit.call_args.kwargs["reply_markup"] == keyboards.medications_list_kb(True)

    async def test_add_prompts_name(self, session, edit, cb_answer, state) -> None:
        await call(settings.cb_med_add, callback=make_callback("med:add", make_message()), state=state)
        assert edit.call_args.args[0] == texts.MEDS_ADD_NAME_PROMPT
        assert await state.get_state() == settings.SettingsInput.med_name.state

    async def test_name_then_dosage(self, session, answer, state) -> None:
        await state.set_state(settings.SettingsInput.med_name)
        await call(
            settings.msg_med_name, message=make_message("  Лозартан "), state=state, session=session
        )
        assert answer.call_args.args[0] == texts.MEDS_ADD_DOSAGE_PROMPT
        assert answer.call_args.kwargs["reply_markup"] == keyboards.dosage_kb()
        data = await state.get_data()
        assert data[settings.M_NAME] == "Лозартан"

    @pytest.mark.parametrize("raw", ["", "   ", "x" * 101])
    async def test_name_invalid(self, session, answer, state, raw: str) -> None:
        await state.set_state(settings.SettingsInput.med_name)
        await call(settings.msg_med_name, message=make_message(raw), state=state, session=session)
        assert await state.get_state() == settings.SettingsInput.med_name.state

    async def test_dose_preset_shows_slots(self, session, edit, cb_answer, state) -> None:
        await state.set_state(settings.SettingsInput.med_dosage)
        await state.update_data(**{settings.M_NAME: "Лозартан"})
        await call(
            settings.cb_med_dose,
            callback=make_callback("med:dose:50 мг", make_message()),
            state=state,
        )
        assert edit.call_args.args[0] == texts.MEDS_ADD_SLOTS_PROMPT
        assert edit.call_args.kwargs["reply_markup"] == keyboards.slots_multiselect_kb([])
        data = await state.get_data()
        assert data[settings.M_DOSAGE] == "50 мг"

    async def test_dose_other_prompts(self, session, edit, cb_answer, state) -> None:
        await call(
            settings.cb_med_dose_other,
            callback=make_callback("med:dose:other", make_message()),
            state=state,
        )
        assert edit.call_args.args[0] == texts.MEDS_ADD_DOSAGE_OTHER_PROMPT
        assert await state.get_state() == settings.SettingsInput.med_dosage.state

    async def test_free_dosage(self, session, answer, state) -> None:
        await state.set_state(settings.SettingsInput.med_dosage)
        await state.update_data(**{settings.M_NAME: "Лозартан"})
        await call(settings.msg_med_dosage, message=make_message("25 мг"), state=state)
        assert answer.call_args.args[0] == texts.MEDS_ADD_SLOTS_PROMPT
        data = await state.get_data()
        assert data[settings.M_DOSAGE] == "25 мг"

    @pytest.mark.parametrize("raw", ["", "x" * 51])
    async def test_free_dosage_invalid(self, session, answer, state, raw: str) -> None:
        await state.set_state(settings.SettingsInput.med_dosage)
        await call(settings.msg_med_dosage, message=make_message(raw), state=state)
        assert await state.get_state() == settings.SettingsInput.med_dosage.state

    async def test_dose_none_shows_slots(self, session, edit, cb_answer, state) -> None:
        """«Без дозировки» — пустая дозировка, дальше выбор слотов."""
        await state.set_state(settings.SettingsInput.med_dosage)
        await state.update_data(**{settings.M_NAME: "Лозартан"})
        await call(
            settings.cb_med_dose_none,
            callback=make_callback("med:dose:none", make_message()),
            state=state,
        )
        assert edit.call_args.args[0] == texts.MEDS_ADD_SLOTS_PROMPT
        assert edit.call_args.kwargs["reply_markup"] == keyboards.slots_multiselect_kb([])
        data = await state.get_data()
        assert data[settings.M_DOSAGE] == ""

    async def test_slot_toggle(self, session, edit, cb_answer, state) -> None:
        await call(
            settings.cb_med_slot,
            callback=make_callback("med:slot:morning", make_message()),
            state=state,
        )
        markup = edit.call_args.kwargs["reply_markup"]
        labels = [btn.text for row in markup.inline_keyboard for btn in row]
        assert "✓ Утро" in labels
        await call(
            settings.cb_med_slot,
            callback=make_callback("med:slot:morning", make_message()),
            state=state,
        )
        markup = edit.call_args.kwargs["reply_markup"]
        labels = [btn.text for row in markup.inline_keyboard for btn in row]
        assert "✓ Утро" not in labels

    async def test_slot_unknown_ignored(self, session, edit, cb_answer, state) -> None:
        await call(
            settings.cb_med_slot,
            callback=make_callback("med:slot:junk", make_message()),
            state=state,
        )
        edit.assert_not_awaited()

    async def test_slot_crisis_toggles(self, session, edit, cb_answer, state) -> None:
        """«Кризис» — такой же переключаемый слот (§6.6)."""
        await call(
            settings.cb_med_slot,
            callback=make_callback("med:slot:crisis", make_message()),
            state=state,
        )
        markup = edit.call_args.kwargs["reply_markup"]
        labels = [btn.text for row in markup.inline_keyboard for btn in row]
        assert "✓ Кризис" in labels

    async def test_done_without_slots_prompts(
        self, session, user, edit, cb_answer, state
    ) -> None:
        await state.set_state(settings.SettingsInput.med_dosage)
        await state.update_data(**{settings.M_NAME: "Лозартан", settings.M_DOSAGE: "50 мг"})
        await call(
            settings.cb_med_slots_done,
            callback=make_callback("med:slots:done", make_message()),
            state=state,
            session=session,
        )
        assert cb_answer.call_args.args[0] == texts.MEDS_ADD_NO_SLOTS
        edit.assert_not_awaited()
        assert await meds_of(session, user.id) == []

    async def test_done_saves(self, session, user, edit, cb_answer, state) -> None:
        await state.set_state(settings.SettingsInput.med_dosage)
        await state.update_data(
            **{settings.M_NAME: "Лозартан", settings.M_DOSAGE: "50 мг", settings.M_SLOTS: ["morning", "evening"]}
        )
        await call(
            settings.cb_med_slots_done,
            callback=make_callback("med:slots:done", make_message()),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.MEDS_ADD_DONE
        rows = await meds_of(session, user.id)
        assert {(r.name, r.dosage, r.slot) for r in rows} == {
            ("Лозартан", "50 мг", "morning"),
            ("Лозартан", "50 мг", "evening"),
        }
        assert await state.get_state() is None

    async def test_done_saves_without_dosage(self, session, user, edit, cb_answer, state) -> None:
        """«Без дозировки» — строка сохраняется с пустой дозировкой (§4.4)."""
        await state.update_data(
            **{
                settings.M_NAME: "Лозартан",
                settings.M_DOSAGE: "",
                settings.M_SLOTS: ["morning"],
            }
        )
        await call(
            settings.cb_med_slots_done,
            callback=make_callback("med:slots:done", make_message()),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.MEDS_ADD_DONE
        rows = await meds_of(session, user.id)
        assert {(r.name, r.dosage, r.slot) for r in rows} == {("Лозартан", "", "morning")}

    async def test_done_crisis_only_warns_no_reminders(
        self, session, user, edit, cb_answer, state
    ) -> None:
        """Только «Кризис» — сохраняется, но напоминаний не будет (§6.7)."""
        await state.update_data(
            **{
                settings.M_NAME: "Каптоприл",
                settings.M_DOSAGE: "25 мг",
                settings.M_SLOTS: ["crisis"],
            }
        )
        await call(
            settings.cb_med_slots_done,
            callback=make_callback("med:slots:done", make_message()),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.MEDS_ADD_DONE_CRISIS
        rows = await meds_of(session, user.id)
        assert {(r.name, r.dosage, r.slot) for r in rows} == {("Каптоприл", "25 мг", "crisis")}

    async def test_done_crisis_with_slot_uses_normal_done(
        self, session, user, edit, cb_answer, state
    ) -> None:
        """Кризис + расписание слот — обычный текст: напоминания будут (§6.7)."""
        await state.update_data(
            **{
                settings.M_NAME: "Каптоприл",
                settings.M_DOSAGE: "25 мг",
                settings.M_SLOTS: ["crisis", "morning"],
            }
        )
        await call(
            settings.cb_med_slots_done,
            callback=make_callback("med:slots:done", make_message()),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.MEDS_ADD_DONE

    async def test_done_stale_buttons(self, session, user, edit, cb_answer, state) -> None:
        """«Готово» без черновика (устаревшие кнопки) — мягко к списку."""
        await call(
            settings.cb_med_slots_done,
            callback=make_callback("med:slots:done", make_message()),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.MEDS_LIST_EMPTY
        assert edit.call_args.kwargs["reply_markup"] == keyboards.medications_list_kb(False)

    async def test_cancel_returns_to_list(self, session, user, edit, cb_answer, state) -> None:
        await state.set_state(settings.SettingsInput.med_name)
        await add_med(session, user)
        await call(
            settings.cb_med_cancel,
            callback=make_callback("med:cancel", make_message()),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0].startswith(texts.MEDS_LIST_HEADER)
        assert await state.get_state() is None

    # ── Удаление (§6.6): выбор из списка → сразу, без подтверждения ──

    async def test_del_menu_shows_pick_list(self, session, user, edit, cb_answer, state) -> None:
        rows = await add_med_group(session, user, slots=("morning", "evening"))
        await call(
            settings.cb_med_del_menu,
            callback=make_callback("med:del", make_message()),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.MEDS_DEL_PICK
        # Одна кнопка на лекарство (группу строк), «Назад» — к списку.
        markup = edit.call_args.kwargs["reply_markup"]
        data = [btn.callback_data for row in markup.inline_keyboard for btn in row]
        assert data == [f"med:del:{rows[0].id}", "set:meds"]

    async def test_del_menu_empty_soft_fallback(self, session, edit, cb_answer, state) -> None:
        """Устаревшая кнопка «Удалить» — лекарств уже нет, мягко к списку."""
        await call(
            settings.cb_med_del_menu,
            callback=make_callback("med:del", make_message()),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.MEDS_LIST_EMPTY
        assert edit.call_args.kwargs["reply_markup"] == keyboards.medications_list_kb(False)

    async def test_del_pick_removes_whole_group(
        self, session, user, edit, cb_answer, state
    ) -> None:
        """Удаление снимает все слоты лекарства (группу строк, §6.6)."""
        rows = await add_med_group(session, user, slots=("morning", "evening"))
        await call(
            settings.cb_med_del_pick,
            callback=make_callback(f"med:del:{rows[1].id}", make_message()),
            state=state,
            session=session,
        )
        assert await meds_of(session, user.id) == []

    async def test_del_pick_removes_logs(self, session, user, edit, cb_answer, state) -> None:
        rows = await add_med_group(session, user, slots=("morning", "evening"))
        await add_log(session, user, rows[0], "morning")
        await add_log(session, user, rows[1], "evening")
        await call(
            settings.cb_med_del_pick,
            callback=make_callback(f"med:del:{rows[0].id}", make_message()),
            state=state,
            session=session,
        )
        logs = (await session.execute(select(MedicationLog))).scalars().all()
        assert logs == []

    async def test_del_pick_toast_and_list(self, session, user, edit, cb_answer, state) -> None:
        """Ответ — тост «Лекарство удалено» и обновлённый список (§6.6)."""
        rows = await add_med_group(session, user, slots=("morning",))
        await call(
            settings.cb_med_del_pick,
            callback=make_callback(f"med:del:{rows[0].id}", make_message()),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.MEDS_LIST_EMPTY
        assert edit.call_args.kwargs["reply_markup"] == keyboards.medications_list_kb(False)
        assert cb_answer.call_args.args[0] == texts.MEDS_DELETED

    async def test_del_pick_foreign_shows_list(self, session, user, edit, cb_answer, state) -> None:
        other = UserModel(telegram_id=222_222)
        session.add(other)
        await session.commit()
        foreign = await add_med(session, other)
        await call(
            settings.cb_med_del_pick,
            callback=make_callback(f"med:del:{foreign.id}", make_message()),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.MEDS_LIST_EMPTY
        assert len(await meds_of(session, other.id)) == 1  # чужое не удалено

    async def test_del_pick_garbage_shows_list(self, session, edit, cb_answer, state) -> None:
        await call(
            settings.cb_med_del_pick,
            callback=make_callback("med:del:abc", make_message()),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.MEDS_LIST_EMPTY

    async def test_del_pick_stale_shows_list(self, session, user, edit, cb_answer, state) -> None:
        """Уже удалённое лекарство (двойное нажатие) — идемпотентно, мягко к списку."""
        row = await add_med(session, user)
        await session.delete(row)
        await session.commit()
        await call(
            settings.cb_med_del_pick,
            callback=make_callback(f"med:del:{row.id}", make_message()),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.MEDS_LIST_EMPTY
        assert cb_answer.call_args.args == ()  # без тоста «удалено» — нечего удалять

    # ── Изменение (§6.6): выбор из списка → те же шаги с текущими значениями ──

    async def test_edit_menu_shows_pick_list(self, session, user, edit, cb_answer, state) -> None:
        rows = await add_med_group(session, user, slots=("morning", "evening"))
        await call(
            settings.cb_med_edit_menu,
            callback=make_callback("med:edit", make_message()),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.MEDS_EDIT_PICK
        markup = edit.call_args.kwargs["reply_markup"]
        data = [btn.callback_data for row in markup.inline_keyboard for btn in row]
        assert data == [f"med:edit:{rows[0].id}", "set:meds"]

    async def test_edit_menu_empty_soft_fallback(self, session, edit, cb_answer, state) -> None:
        await call(
            settings.cb_med_edit_menu,
            callback=make_callback("med:edit", make_message()),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.MEDS_LIST_EMPTY

    async def test_edit_pick_prompts_name_with_current(
        self, session, user, edit, cb_answer, state
    ) -> None:
        """Черновик правки предзаполняется текущими значениями (§6.6)."""
        rows = await add_med_group(session, user, slots=("morning", "evening"))
        await call(
            settings.cb_med_edit_pick,
            callback=make_callback(f"med:edit:{rows[1].id}", make_message()),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.MEDS_EDIT_NAME_PROMPT.format(name="Лозартан")
        assert edit.call_args.kwargs["reply_markup"] == keyboards.med_name_input_kb()
        assert await state.get_state() == settings.SettingsInput.med_name.state
        data = await state.get_data()
        assert data[settings.M_EDIT_NAME] == "Лозартан"
        assert data[settings.M_NAME] == "Лозартан"
        assert data[settings.M_DOSAGE] == "50 мг"
        assert data[settings.M_SLOTS] == ["morning", "evening"]  # порядок SLOTS

    async def test_edit_pick_foreign_shows_list(self, session, user, edit, cb_answer, state) -> None:
        other = UserModel(telegram_id=222_222)
        session.add(other)
        await session.commit()
        foreign = await add_med(session, other)
        await call(
            settings.cb_med_edit_pick,
            callback=make_callback(f"med:edit:{foreign.id}", make_message()),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.MEDS_LIST_EMPTY
        assert await state.get_data() == {}

    async def test_edit_pick_garbage_shows_list(self, session, edit, cb_answer, state) -> None:
        await call(
            settings.cb_med_edit_pick,
            callback=make_callback("med:edit:abc", make_message()),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.MEDS_LIST_EMPTY

    async def test_edit_pick_stale_shows_list(self, session, user, edit, cb_answer, state) -> None:
        row = await add_med(session, user)
        await session.delete(row)
        await session.commit()
        await call(
            settings.cb_med_edit_pick,
            callback=make_callback(f"med:edit:{row.id}", make_message()),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.MEDS_LIST_EMPTY

    async def test_edit_keep_name_skips_to_dosage(
        self, session, edit, cb_answer, state
    ) -> None:
        """«Оставить как есть» на шаге названия → шаг дозировки с текущей (§6.6)."""
        await state.set_state(settings.SettingsInput.med_name)
        await state.update_data(
            **{
                settings.M_EDIT_NAME: "Лозартан",
                settings.M_NAME: "Лозартан",
                settings.M_DOSAGE: "50 мг",
                settings.M_SLOTS: ["morning"],
            }
        )
        await call(
            settings.cb_med_name_keep,
            callback=make_callback("med:name:same", make_message()),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.MEDS_EDIT_DOSAGE_PROMPT.format(current="50 мг")
        assert edit.call_args.kwargs["reply_markup"] == keyboards.dosage_kb(keep=True)
        assert await state.get_state() == settings.SettingsInput.med_dosage.state
        assert (await state.get_data())[settings.M_NAME] == "Лозартан"

    async def test_edit_keep_name_shows_no_dosage_label(
        self, session, edit, cb_answer, state
    ) -> None:
        """Без дозировки в подсказке — «Без дозировки» (§6.6)."""
        await state.set_state(settings.SettingsInput.med_name)
        await state.update_data(
            **{
                settings.M_EDIT_NAME: "Лозартан",
                settings.M_NAME: "Лозартан",
                settings.M_DOSAGE: "",
                settings.M_SLOTS: ["morning"],
            }
        )
        await call(
            settings.cb_med_name_keep,
            callback=make_callback("med:name:same", make_message()),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.MEDS_EDIT_DOSAGE_PROMPT.format(
            current=texts.BTN_NO_DOSAGE
        )

    async def test_edit_keep_name_without_draft_soft_list(
        self, session, edit, cb_answer, state
    ) -> None:
        await call(
            settings.cb_med_name_keep,
            callback=make_callback("med:name:same", make_message()),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.MEDS_LIST_EMPTY

    async def test_edit_same_name_text_skips_collision(
        self, session, user, answer, state
    ) -> None:
        """То же название при правке — занятое-собственное не конфликт (§6.6)."""
        await add_med_group(session, user, slots=("morning",))
        await state.set_state(settings.SettingsInput.med_name)
        await state.update_data(
            **{
                settings.M_EDIT_NAME: "Лозартан",
                settings.M_NAME: "Лозартан",
                settings.M_DOSAGE: "50 мг",
                settings.M_SLOTS: ["morning"],
            }
        )
        await call(
            settings.msg_med_name, message=make_message("Лозартан"), state=state, session=session
        )
        assert answer.call_args.args[0] == texts.MEDS_EDIT_DOSAGE_PROMPT.format(current="50 мг")
        assert (await state.get_data())[settings.M_NAME] == "Лозартан"

    async def test_edit_new_name_free_prompts_dosage(
        self, session, user, answer, state
    ) -> None:
        """Свободное новое название принимается; слоты не перезаполняются."""
        await add_med_group(session, user, slots=("morning",))
        await state.set_state(settings.SettingsInput.med_name)
        await state.update_data(
            **{
                settings.M_EDIT_NAME: "Лозартан",
                settings.M_NAME: "Лозартан",
                settings.M_DOSAGE: "50 мг",
                settings.M_SLOTS: ["morning"],
            }
        )
        await call(
            settings.msg_med_name, message=make_message("Лозап"), state=state, session=session
        )
        assert answer.call_args.args[0] == texts.MEDS_EDIT_DOSAGE_PROMPT.format(current="50 мг")
        assert answer.call_args.kwargs["reply_markup"] == keyboards.dosage_kb(keep=True)
        data = await state.get_data()
        assert data[settings.M_NAME] == "Лозап"
        assert data[settings.M_SLOTS] == ["morning"]

    async def test_edit_new_name_collision_prompts_again(
        self, session, user, answer, state
    ) -> None:
        """Занятое название — «введите другое», остаёмся на шаге названия (§6.6)."""
        await add_med_group(session, user, name="Лозартан", slots=("morning",))
        await add_med_group(session, user, name="Амлодипин", dosage="5 мг", slots=("morning",))
        await state.set_state(settings.SettingsInput.med_name)
        await state.update_data(
            **{
                settings.M_EDIT_NAME: "Лозартан",
                settings.M_NAME: "Лозартан",
                settings.M_DOSAGE: "50 мг",
                settings.M_SLOTS: ["morning"],
            }
        )
        await call(
            settings.msg_med_name, message=make_message("Амлодипин"), state=state, session=session
        )
        assert answer.call_args.args[0] == texts.MEDS_EDIT_NAME_TAKEN
        assert answer.call_args.kwargs["reply_markup"] == keyboards.med_name_input_kb()
        assert await state.get_state() == settings.SettingsInput.med_name.state

    async def test_edit_name_invalid_keeps_keep_button(
        self, session, answer, state
    ) -> None:
        await state.set_state(settings.SettingsInput.med_name)
        await state.update_data(**{settings.M_EDIT_NAME: "Лозартан"})
        await call(
            settings.msg_med_name, message=make_message("x" * 101), state=state, session=session
        )
        assert answer.call_args.kwargs["reply_markup"] == keyboards.med_name_input_kb()
        assert await state.get_state() == settings.SettingsInput.med_name.state

    async def test_edit_dose_keep_skips_to_slots(
        self, session, edit, cb_answer, state
    ) -> None:
        """«Оставить как есть» на шаге дозировки → слоты с текущим выбором (§6.6)."""
        await state.set_state(settings.SettingsInput.med_dosage)
        await state.update_data(
            **{
                settings.M_EDIT_NAME: "Лозартан",
                settings.M_NAME: "Лозартан",
                settings.M_DOSAGE: "50 мг",
                settings.M_SLOTS: ["morning", "evening"],
            }
        )
        await call(
            settings.cb_med_dose_keep,
            callback=make_callback("med:dose:same", make_message()),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.MEDS_EDIT_SLOTS_PROMPT.format(
            slots="Утро, Вечер"
        )
        assert edit.call_args.kwargs["reply_markup"] == keyboards.slots_multiselect_kb(
            ["morning", "evening"]
        )
        assert await state.get_state() is None

    async def test_edit_dose_keep_without_draft_soft_list(
        self, session, edit, cb_answer, state
    ) -> None:
        await call(
            settings.cb_med_dose_keep,
            callback=make_callback("med:dose:same", make_message()),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.MEDS_LIST_EMPTY

    async def test_edit_slots_prompt_shows_current(
        self, session, edit, cb_answer, state
    ) -> None:
        """Шаг слотов при правке показывает «Сейчас: …» (§6.6)."""
        await state.update_data(
            **{
                settings.M_EDIT_NAME: "Лозартан",
                settings.M_NAME: "Лозартан",
                settings.M_DOSAGE: "50 мг",
                settings.M_SLOTS: ["evening"],
            }
        )
        await call(
            settings.cb_med_slot,
            callback=make_callback("med:slot:morning", make_message()),
            state=state,
        )
        assert edit.call_args.args[0] == texts.MEDS_EDIT_SLOTS_PROMPT.format(
            slots="Утро, Вечер"
        )

    async def test_edit_done_updates_in_place(
        self, session, user, edit, cb_answer, state
    ) -> None:
        """Переживающие слоты обновляются на месте: id строк сохраняются (§6.6)."""
        rows = await add_med_group(session, user, slots=("morning", "evening"))
        ids_before = {row.id for row in rows}
        await state.update_data(
            **{
                settings.M_EDIT_NAME: "Лозартан",
                settings.M_NAME: "Лозап",
                settings.M_DOSAGE: "100 мг",
                settings.M_SLOTS: ["morning", "evening"],
            }
        )
        await call(
            settings.cb_med_slots_done,
            callback=make_callback("med:slots:done", make_message()),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.MEDS_EDIT_DONE
        result = await meds_of(session, user.id)
        assert {row.id for row in result} == ids_before
        assert {(row.name, row.dosage) for row in result} == {("Лозап", "100 мг")}
        assert await state.get_state() is None

    async def test_edit_done_removes_slot_keeps_surviving_logs(
        self, session, user, edit, cb_answer, state
    ) -> None:
        """Снятый слот удаляется с логами; логи переживающего слота целы (§6.6)."""
        rows = await add_med_group(session, user, slots=("morning", "evening"))
        await add_log(session, user, rows[0], "morning")
        await add_log(session, user, rows[1], "evening")
        await state.update_data(
            **{
                settings.M_EDIT_NAME: "Лозартан",
                settings.M_NAME: "Лозартан",
                settings.M_DOSAGE: "50 мг",
                settings.M_SLOTS: ["morning"],
            }
        )
        await call(
            settings.cb_med_slots_done,
            callback=make_callback("med:slots:done", make_message()),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.MEDS_EDIT_DONE
        result = await meds_of(session, user.id)
        assert [row.slot for row in result] == ["morning"]
        assert result[0].id == rows[0].id
        logs = (await session.execute(select(MedicationLog))).scalars().all()
        assert len(logs) == 1
        assert logs[0].medication_id == rows[0].id

    async def test_edit_done_adds_slot(self, session, user, edit, cb_answer, state) -> None:
        rows = await add_med_group(session, user, slots=("morning",))
        await state.update_data(
            **{
                settings.M_EDIT_NAME: "Лозартан",
                settings.M_NAME: "Лозартан",
                settings.M_DOSAGE: "50 мг",
                settings.M_SLOTS: ["morning", "crisis"],
            }
        )
        await call(
            settings.cb_med_slots_done,
            callback=make_callback("med:slots:done", make_message()),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.MEDS_EDIT_DONE
        by_slot = {row.slot: row for row in await meds_of(session, user.id)}
        assert set(by_slot) == {"morning", "crisis"}
        assert by_slot["morning"].id == rows[0].id  # прежняя строка нетронута

    async def test_edit_done_crisis_only(self, session, user, edit, cb_answer, state) -> None:
        """Только «Кризис» после правки — предупреждение без напоминаний (§6.7)."""
        await add_med_group(session, user, slots=("morning",))
        await state.update_data(
            **{
                settings.M_EDIT_NAME: "Лозартан",
                settings.M_NAME: "Лозартан",
                settings.M_DOSAGE: "50 мг",
                settings.M_SLOTS: ["crisis"],
            }
        )
        await call(
            settings.cb_med_slots_done,
            callback=make_callback("med:slots:done", make_message()),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.MEDS_EDIT_DONE_CRISIS

    async def test_edit_done_no_slots_prompts(
        self, session, user, edit, cb_answer, state
    ) -> None:
        await add_med_group(session, user, slots=("morning",))
        await state.update_data(
            **{
                settings.M_EDIT_NAME: "Лозартан",
                settings.M_NAME: "Лозартан",
                settings.M_DOSAGE: "50 мг",
                settings.M_SLOTS: [],
            }
        )
        await call(
            settings.cb_med_slots_done,
            callback=make_callback("med:slots:done", make_message()),
            state=state,
            session=session,
        )
        assert cb_answer.call_args.args[0] == texts.MEDS_ADD_NO_SLOTS
        edit.assert_not_awaited()
        assert len(await meds_of(session, user.id)) == 1  # группа не тронута

    async def test_edit_done_stale_group_soft_list(
        self, session, user, edit, cb_answer, state
    ) -> None:
        """Группу удалили, пока шла правка — мягко к списку (§6.6)."""
        await state.update_data(
            **{
                settings.M_EDIT_NAME: "Нет такого",
                settings.M_NAME: "Нет такого",
                settings.M_DOSAGE: "50 мг",
                settings.M_SLOTS: ["morning"],
            }
        )
        await call(
            settings.cb_med_slots_done,
            callback=make_callback("med:slots:done", make_message()),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.MEDS_LIST_EMPTY
        assert await state.get_state() is None

    async def test_edit_done_name_conflict_race(
        self, session, user, edit, cb_answer, state
    ) -> None:
        """Гонка: имя заняли после шага названия — тост, остаёмся на слотах (§6.6)."""
        await add_med_group(session, user, name="Лозартан", slots=("morning",))
        await add_med_group(session, user, name="Амлодипин", dosage="5 мг", slots=("morning",))
        await state.update_data(
            **{
                settings.M_EDIT_NAME: "Лозартан",
                settings.M_NAME: "Амлодипин",
                settings.M_DOSAGE: "50 мг",
                settings.M_SLOTS: ["morning"],
            }
        )
        await call(
            settings.cb_med_slots_done,
            callback=make_callback("med:slots:done", make_message()),
            state=state,
            session=session,
        )
        assert cb_answer.call_args.args[0] == texts.MEDS_EDIT_NAME_TAKEN
        edit.assert_not_awaited()
        assert (await state.get_data())[settings.M_EDIT_NAME] == "Лозартан"  # черновик цел
        rows = await meds_of(session, user.id)
        assert {row.name for row in rows} == {"Лозартан", "Амлодипин"}  # ничего не изменено

    async def test_cancel_from_edit_returns_to_list(
        self, session, user, edit, cb_answer, state
    ) -> None:
        await state.set_state(settings.SettingsInput.med_name)
        await state.update_data(
            **{
                settings.M_EDIT_NAME: "Лозартан",
                settings.M_NAME: "Лозартан",
                settings.M_DOSAGE: "50 мг",
                settings.M_SLOTS: ["morning"],
            }
        )
        await add_med(session, user)
        await call(
            settings.cb_med_cancel,
            callback=make_callback("med:cancel", make_message()),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0].startswith(texts.MEDS_LIST_HEADER)
        assert await state.get_state() is None
        assert await state.get_data() == {}


class TestDosageButtonFilter:
    """Регресс: фильтр кнопок дозировки срабатывает многократно.

    Раньше в `F.data.in_(...)` передавался генератор: он исчерпывался после
    первого прохода, и следующее нажатие любой кнопки дозировки фильтр не
    проходило — бот молчал, дозировку можно было дать только текстом.
    Проверяется фильтр, зарегистрированный в роутере (та же инстанция
    `FilterObject`, что обслуживает реальные callback'и).
    """

    @staticmethod
    def _dose_filter():
        for handler in settings.router.callback_query.handlers:
            if handler.callback.__name__ == "cb_med_dose":
                assert handler.filters, "у cb_med_dose не осталось фильтров"
                return handler.filters[0]
        raise AssertionError("cb_med_dose не зарегистрирован в роутере")

    async def test_every_preset_matches_repeatedly(self) -> None:
        flt = self._dose_filter()
        for preset in keyboards.DOSAGE_PRESETS:
            event = make_callback(f"med:dose:{preset}", make_message())
            assert await flt.call(event) is True
            assert await flt.call(event) is True  # повтор: генератор здесь уже «пуст»

    async def test_foreign_callback_is_rejected(self) -> None:
        flt = self._dose_filter()
        assert await flt.call(make_callback("med:slot:morning", make_message())) is False


class TestDeleteAccount:
    """Удаление аккаунта (§19): предупреждение, порядок «job'ы → данные», идемпотентность."""

    async def test_delete_warning(self, session, edit, cb_answer) -> None:
        await call(
            settings.cb_delete_account,
            callback=make_callback("set:delete", make_message()),
        )
        assert edit.call_args.args[0] == texts.DELETE_ACCOUNT_WARNING
        assert (
            edit.call_args.kwargs["reply_markup"] == keyboards.delete_account_kb()
        )

    async def test_confirm_removes_jobs_then_data(
        self, session, user, edit, cb_answer, state, mocker
    ) -> None:
        order: list[str] = []

        async def remove_jobs(user_id: int) -> int:
            order.append("jobs")
            return 1

        reminders = mocker.Mock()
        reminders.remove_user_jobs = remove_jobs

        real_delete = session.delete

        async def tracked_delete(obj) -> None:
            order.append("delete")
            await real_delete(obj)

        mocker.patch.object(session, "delete", tracked_delete)
        await call(
            settings.cb_delete_account_confirm,
            callback=make_callback("set:delete:ok", make_message()),
            state=state,
            session=session,
            reminders=reminders,
        )
        # Порядок обязателен (§19): сначала job'ы, потом удаление данных.
        assert order == ["jobs", "delete"]
        assert edit.call_args.args[0] == texts.DELETE_ACCOUNT_DONE
        assert edit.call_args.kwargs["reply_markup"] == keyboards.back_to_menu()
        remaining = (
            await session.execute(
                select(UserModel).where(UserModel.telegram_id == TG_ID)
            )
        ).scalar_one_or_none()
        assert remaining is None

    async def test_confirm_double_press_is_idempotent(
        self, session, user, edit, cb_answer, state, mocker
    ) -> None:
        reminders = mocker.Mock()
        reminders.remove_user_jobs = mocker.AsyncMock(return_value=1)
        callback = make_callback("set:delete:ok", make_message())
        for _ in range(2):
            await call(
                settings.cb_delete_account_confirm,
                callback=callback,
                state=state,
                session=session,
                reminders=reminders,
            )
        # Повторное нажатие не трогает job'ы и показывает тот же финал.
        reminders.remove_user_jobs.assert_awaited_once()
        assert [c.args[0] for c in edit.call_args_list] == [
            texts.DELETE_ACCOUNT_DONE,
            texts.DELETE_ACCOUNT_DONE,
        ]

    async def test_confirm_without_user(
        self, session, edit, cb_answer, state, mocker
    ) -> None:
        reminders = mocker.Mock()
        reminders.remove_user_jobs = mocker.AsyncMock(return_value=0)
        await call(
            settings.cb_delete_account_confirm,
            callback=make_callback("set:delete:ok", make_message()),
            state=state,
            session=session,
            reminders=reminders,
        )
        reminders.remove_user_jobs.assert_not_awaited()
        assert edit.call_args.args[0] == texts.DELETE_ACCOUNT_DONE
