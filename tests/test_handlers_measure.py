"""Тесты хендлеров записи давления (bot/handlers/measure.py, SPEC.md, §6.3).

Хендлеры вызываются напрямую с реальными `Message`/`CallbackQuery` aiogram;
`answer`/`edit_text` замоканы на уровне классов. FSM — реальный MemoryStorage.
Параметры хендлеру передаются по именам (как aiogram DI) — хелпер `call`.
"""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, User
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, texts
from bot.handlers import measure
from bot.models import Measurement as MeasurementModel
from bot.models import User as UserModel
from bot.services import measurements as measurements_service
from bot.services import medications as medications_service

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


async def call(handler, **kwargs) -> None:
    """Вызвать хендлер с параметрами по именам (как aiogram DI)."""
    wanted = inspect.signature(handler).parameters
    await handler(**{name: value for name, value in kwargs.items() if name in wanted})


async def reload_user(session: AsyncSession) -> UserModel:
    return (
        await session.execute(
            select(UserModel)
            .where(UserModel.telegram_id == TG_ID)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def reload_measurement(session: AsyncSession, measurement_id: int) -> MeasurementModel:
    return (
        await session.execute(
            select(MeasurementModel)
            .where(MeasurementModel.id == measurement_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def save_draft(
    session: AsyncSession, state: FSMContext, sys_v: int = 135, dia_v: int = 85, pulse: int = 72
) -> int:
    """Сохранить измерение и наполнить черновик FSM (то же, что делает `_save`)."""
    user = await measurements_service.get_or_create_user(session, TG_ID)
    measurement = await measurements_service.save_measurement(
        session, user, sys_v, dia_v, pulse
    )
    await session.commit()
    await state.set_data(
        {
            measure.M_SYS: sys_v,
            measure.M_DIA: dia_v,
            measure.M_PULSE: pulse,
            measure.M_ID: measurement.id,
        }
    )
    return measurement.id


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
    return mocker.Mock(
        ensure_remeasure=mocker.AsyncMock(),
        cancel_remeasure_for_measurement=mocker.AsyncMock(),
    )


# ─────────────────────────── Начало и отмена ───────────────────────────


class TestBeginCancel:
    async def test_begin_shows_systolic(self, edit, cb_answer) -> None:
        state = make_state()
        await call(
            measure.cb_measure_begin,
            callback=make_callback("menu:measure", make_message()),
            state=state,
        )
        assert edit.call_args.args[0] == texts.ASK_SYSTOLIC
        assert edit.call_args.kwargs["reply_markup"] == keyboards.systolic_kb()
        cb_answer.assert_awaited_once()

    async def test_begin_resets_old_draft(self, edit, cb_answer) -> None:
        state = make_state()
        await state.set_data({measure.M_SYS: 100, measure.M_ID: 42})
        await call(
            measure.cb_measure_begin,
            callback=make_callback("menu:measure", make_message()),
            state=state,
        )
        assert await state.get_data() == {}

    @pytest.mark.parametrize("data", ["rem:write", "rem:now"])
    async def test_begin_from_reminder(self, data: str, edit, cb_answer) -> None:
        """«Записать давление» / «Записать сейчас» из напоминания — тот же сценарий 6.3."""
        state = make_state()
        await call(
            measure.cb_measure_begin,
            callback=make_callback(data, make_message()),
            state=state,
        )
        assert edit.call_args.args[0] == texts.ASK_SYSTOLIC
        assert edit.call_args.kwargs["reply_markup"] == keyboards.systolic_kb()

    async def test_cancel_returns_to_menu(self, edit, cb_answer) -> None:
        state = make_state()
        await state.set_state(measure.MeasureInput.sys_other)
        await call(
            measure.cb_measure_cancel,
            callback=make_callback("m:cancel", make_message()),
            state=state,
        )
        assert edit.call_args.args[0] == texts.MAIN_MENU
        assert edit.call_args.kwargs["reply_markup"] == keyboards.main_menu()
        assert await state.get_state() is None


# ─────────────────────────── Числовые кнопки ───────────────────────────


class TestNumericButtons:
    async def test_systolic_button(self, edit, cb_answer) -> None:
        state = make_state()
        await call(
            measure.cb_systolic,
            callback=make_callback("m:sys:135", make_message()),
            state=state,
        )
        assert edit.call_args.args[0] == texts.ASK_DIASTOLIC
        assert edit.call_args.kwargs["reply_markup"] == keyboards.diastolic_kb()
        assert (await state.get_data())["m_sys"] == 135

    async def test_systolic_other_prompts(self, edit, cb_answer) -> None:
        state = make_state()
        await call(
            measure.cb_systolic,
            callback=make_callback("m:sys:other", make_message()),
            state=state,
        )
        assert edit.call_args.args[0] == texts.PROMPT_SYSTOLIC_OTHER
        assert await state.get_state() == measure.MeasureInput.sys_other.state

    async def test_systolic_invalid_value(self, edit, cb_answer) -> None:
        state = make_state()
        await call(
            measure.cb_systolic,
            callback=make_callback("m:sys:55", make_message()),
            state=state,
        )
        edit.assert_not_awaited()
        cb_answer.assert_awaited_once()

    async def test_diastolic_button(self, edit, cb_answer) -> None:
        state = make_state()
        await call(
            measure.cb_diastolic,
            callback=make_callback("m:dia:85", make_message()),
            state=state,
        )
        assert edit.call_args.args[0] == texts.ASK_PULSE
        assert edit.call_args.kwargs["reply_markup"] == keyboards.pulse_kb()
        assert (await state.get_data())["m_dia"] == 85

    async def test_diastolic_other_prompts(self, edit, cb_answer) -> None:
        state = make_state()
        await call(
            measure.cb_diastolic,
            callback=make_callback("m:dia:other", make_message()),
            state=state,
        )
        assert edit.call_args.args[0] == texts.PROMPT_DIASTOLIC_OTHER
        assert await state.get_state() == measure.MeasureInput.dia_other.state

    async def test_diastolic_invalid_value(self, edit, cb_answer) -> None:
        state = make_state()
        await call(
            measure.cb_diastolic,
            callback=make_callback("m:dia:5", make_message()),
            state=state,
        )
        edit.assert_not_awaited()
        cb_answer.assert_awaited_once()

    async def test_pulse_button_saves(self, session, edit, cb_answer, answer) -> None:
        state = make_state()
        await state.set_data({measure.M_SYS: 135, measure.M_DIA: 85})
        await call(
            measure.cb_pulse,
            callback=make_callback("m:pulse:72", make_message()),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.fmt_saved(135, 85, 72)
        assert edit.call_args.kwargs["reply_markup"] == keyboards.saved_kb()
        user = await reload_user(session)
        rows = (
            await session.execute(
                select(MeasurementModel)
                .where(MeasurementModel.user_id == user.id)
                .execution_options(populate_existing=True)
            )
        ).scalars().all()
        assert len(rows) == 1
        assert (rows[0].systolic, rows[0].diastolic, rows[0].pulse) == (135, 85, 72)
        assert (await state.get_data())["m_id"] == rows[0].id

    async def test_pulse_skip_saves_without_pulse(self, session, edit, cb_answer) -> None:
        state = make_state()
        await state.set_data({measure.M_SYS: 135, measure.M_DIA: 85})
        await call(
            measure.cb_pulse,
            callback=make_callback("m:pulse:skip", make_message()),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.fmt_saved(135, 85, None)
        row = (
            await session.execute(
                select(MeasurementModel).execution_options(populate_existing=True)
            )
        ).scalars().one()
        assert row.pulse is None

    async def test_pulse_other_prompts(self, edit, cb_answer) -> None:
        state = make_state()
        await state.set_data({measure.M_SYS: 135, measure.M_DIA: 85})
        await call(
            measure.cb_pulse,
            callback=make_callback("m:pulse:other", make_message()),
            state=state,
            session=None,
        )
        assert edit.call_args.args[0] == texts.PROMPT_PULSE_OTHER
        assert await state.get_state() == measure.MeasureInput.pulse_other.state

    async def test_pulse_invalid_value(self, edit, cb_answer) -> None:
        state = make_state()
        await state.set_data({measure.M_SYS: 135, measure.M_DIA: 85})
        await call(
            measure.cb_pulse,
            callback=make_callback("m:pulse:10", make_message()),
            state=state,
            session=None,
        )
        edit.assert_not_awaited()
        cb_answer.assert_awaited_once()


# ─────────────────────────── Свободный ввод ───────────────────────────


class TestFreeInput:
    async def test_msg_systolic(self, answer) -> None:
        state = make_state()
        await call(measure.msg_systolic, message=make_message("132"), state=state)
        assert answer.call_args.args[0] == texts.ASK_DIASTOLIC
        assert answer.call_args.kwargs["reply_markup"] == keyboards.diastolic_kb()
        assert await state.get_state() is None

    @pytest.mark.parametrize("text", ["50", "абырвалг", ""])
    async def test_msg_systolic_invalid(self, answer, text: str) -> None:
        state = make_state()
        await state.set_state(measure.MeasureInput.sys_other)
        await call(measure.msg_systolic, message=make_message(text), state=state)
        assert answer.call_args.args[0] != texts.ASK_DIASTOLIC
        assert answer.call_args.kwargs["reply_markup"] == keyboards.input_cancel_kb()
        assert await state.get_state() == measure.MeasureInput.sys_other.state

    async def test_msg_diastolic(self, answer) -> None:
        state = make_state()
        await call(measure.msg_diastolic, message=make_message(" 88 "), state=state)
        assert answer.call_args.args[0] == texts.ASK_PULSE
        assert (await state.get_data())["m_dia"] == 88

    async def test_msg_diastolic_invalid(self, answer) -> None:
        state = make_state()
        await call(measure.msg_diastolic, message=make_message("300"), state=state)
        assert answer.call_args.kwargs["reply_markup"] == keyboards.input_cancel_kb()

    async def test_msg_pulse_saves(self, session, answer) -> None:
        state = make_state()
        await state.set_data({measure.M_SYS: 135, measure.M_DIA: 85})
        await call(measure.msg_pulse, message=make_message("66"), state=state, session=session)
        assert answer.call_args.args[0] == texts.fmt_saved(135, 85, 66)
        assert answer.call_args.kwargs["reply_markup"] == keyboards.saved_kb()
        row = (
            await session.execute(
                select(MeasurementModel).execution_options(populate_existing=True)
            )
        ).scalars().one()
        assert row.pulse == 66

    async def test_msg_pulse_invalid(self, answer) -> None:
        state = make_state()
        await call(
            measure.msg_pulse, message=make_message("999"), state=state, session=None
        )
        assert answer.call_args.kwargs["reply_markup"] == keyboards.input_cancel_kb()


# ─────────────────────────── Сохранение ───────────────────────────


class TestSave:
    async def test_high_pressure_warning_by_systolic(self, session, answer) -> None:
        state = make_state()
        await state.set_data({measure.M_SYS: 185, measure.M_DIA: 85, measure.M_PULSE: None})
        await measure._save(make_message(), state, session, TG_ID, via_message=True)
        expected = f"{texts.fmt_saved(185, 85, None)}\n{texts.HIGH_PRESSURE_WARNING}"
        assert answer.call_args.args[0] == expected

    async def test_high_pressure_warning_by_diastolic(self, session, answer) -> None:
        state = make_state()
        await state.set_data({measure.M_SYS: 140, measure.M_DIA: 112, measure.M_PULSE: None})
        await measure._save(make_message(), state, session, TG_ID, via_message=True)
        assert texts.HIGH_PRESSURE_WARNING in answer.call_args.args[0]

    async def test_normal_values_have_no_warning(self, session, answer) -> None:
        state = make_state()
        await state.set_data({measure.M_SYS: 179, measure.M_DIA: 109, measure.M_PULSE: 70})
        await measure._save(make_message(), state, session, TG_ID, via_message=True)
        assert texts.HIGH_PRESSURE_WARNING not in answer.call_args.args[0]

    async def test_duplicate_shows_notice(self, session, answer) -> None:
        state = make_state()
        await state.set_data({measure.M_SYS: 135, measure.M_DIA: 85, measure.M_PULSE: 72})
        await measure._save(make_message(), state, session, TG_ID, via_message=True)
        await state.set_data({measure.M_SYS: 135, measure.M_DIA: 85, measure.M_PULSE: 72})
        await measure._save(make_message(), state, session, TG_ID, via_message=True)
        assert answer.call_args.args[0] == texts.DUPLICATE_MEASUREMENT
        assert answer.call_args.kwargs["reply_markup"] == keyboards.back_to_menu()
        assert await state.get_state() is None
        assert await state.get_data() == {}
        rows = (await session.execute(select(MeasurementModel))).scalars().all()
        assert len(rows) == 1


# ─────────────────────────── Правка последней записи ───────────────────────────


class TestEditEntry:
    async def test_no_measurements_shows_notice(self, session, edit, cb_answer) -> None:
        state = make_state()
        await call(
            measure.cb_edit_begin,
            callback=make_callback("m:edit", make_message()),
            state=state,
            session=session,
        )
        cb_answer.assert_awaited_once_with(texts.NOT_YOUR_RECORD)
        edit.assert_not_awaited()
        assert await state.get_data() == {}

    async def test_opens_systolic_fixes_target(self, session, edit, cb_answer) -> None:
        state = make_state()
        measurement_id = await save_draft(session, state)
        await call(
            measure.cb_edit_begin,
            callback=make_callback("m:edit", make_message()),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.ASK_SYSTOLIC
        assert edit.call_args.kwargs["reply_markup"] == keyboards.systolic_kb()
        cb_answer.assert_awaited_once()
        assert await state.get_data() == {
            measure.M_EDIT_ID: measurement_id,
            measure.M_ID: measurement_id,
        }

    async def test_resets_old_draft_and_state(self, session, edit, cb_answer) -> None:
        state = make_state()
        measurement_id = await save_draft(session, state)
        await state.set_state(measure.MeasureInput.note_input)  # огрызок старого ввода
        await call(
            measure.cb_edit_begin,
            callback=make_callback("m:edit", make_message()),
            state=state,
            session=session,
        )
        assert await state.get_state() is None
        assert await state.get_data() == {
            measure.M_EDIT_ID: measurement_id,
            measure.M_ID: measurement_id,
        }

    async def test_resolves_latest_by_timestamp(self, session, edit, cb_answer) -> None:
        state = make_state()
        user = await measurements_service.get_or_create_user(session, TG_ID)
        await measurements_service.save_measurement(
            session,
            user,
            135,
            85,
            timestamp=measurements_service.utcnow() - timedelta(minutes=5),
        )
        newest = await measurements_service.save_measurement(
            session, user, 140, 90, timestamp=measurements_service.utcnow()
        )
        await session.commit()
        await call(
            measure.cb_edit_begin,
            callback=make_callback("m:edit", make_message()),
            state=state,
            session=session,
        )
        assert (await state.get_data())[measure.M_EDIT_ID] == newest.id

    async def test_foreign_newer_not_selected(self, session, edit, cb_answer) -> None:
        state = make_state()
        user = await measurements_service.get_or_create_user(session, TG_ID)
        mine = await measurements_service.save_measurement(
            session,
            user,
            135,
            85,
            timestamp=measurements_service.utcnow() - timedelta(minutes=5),
        )
        other = UserModel(telegram_id=222_222)
        session.add(other)
        await session.flush()
        await measurements_service.save_measurement(
            session, other, 200, 120, timestamp=measurements_service.utcnow()
        )
        await session.commit()
        await call(
            measure.cb_edit_begin,
            callback=make_callback("m:edit", make_message()),
            state=state,
            session=session,
        )
        assert (await state.get_data())[measure.M_EDIT_ID] == mine.id


class TestEditSave:
    async def test_button_flow_updates_record(self, session, edit, cb_answer) -> None:
        state = make_state()
        measurement_id = await save_draft(session, state)
        saved_ts = (await reload_measurement(session, measurement_id)).timestamp
        message = make_message()
        await call(
            measure.cb_edit_begin,
            callback=make_callback("m:edit", message),
            state=state,
            session=session,
        )
        await call(
            measure.cb_systolic, callback=make_callback("m:sys:140", message), state=state
        )
        await call(
            measure.cb_diastolic, callback=make_callback("m:dia:90", message), state=state
        )
        await call(
            measure.cb_pulse,
            callback=make_callback("m:pulse:70", message),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.fmt_updated(140, 90, 70)
        assert edit.call_args.kwargs["reply_markup"] == keyboards.saved_kb()
        row = await reload_measurement(session, measurement_id)
        assert (row.systolic, row.diastolic, row.pulse) == (140, 90, 70)
        assert row.timestamp == saved_ts  # время записи не меняется
        assert (await state.get_data())[measure.M_ID] == measurement_id

    async def test_free_input_systolic_in_edit(
        self, session, edit, cb_answer, answer
    ) -> None:
        """«Другое» при правке: свободный ввод систолического переиспользуется."""
        state = make_state()
        measurement_id = await save_draft(session, state)
        message = make_message()
        await call(
            measure.cb_edit_begin,
            callback=make_callback("m:edit", message),
            state=state,
            session=session,
        )
        await call(
            measure.cb_systolic, callback=make_callback("m:sys:other", message), state=state
        )
        assert edit.call_args.args[0] == texts.PROMPT_SYSTOLIC_OTHER
        await call(measure.msg_systolic, message=make_message("150"), state=state)
        assert answer.call_args.args[0] == texts.ASK_DIASTOLIC
        await call(
            measure.cb_diastolic, callback=make_callback("m:dia:90", message), state=state
        )
        await call(
            measure.cb_pulse,
            callback=make_callback("m:pulse:70", message),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.fmt_updated(150, 90, 70)
        row = await reload_measurement(session, measurement_id)
        assert (row.systolic, row.diastolic, row.pulse) == (150, 90, 70)

    async def test_skip_clears_pulse(self, session, edit, cb_answer) -> None:
        state = make_state()
        measurement_id = await save_draft(session, state)  # запись с пульсом 72
        message = make_message()
        await call(
            measure.cb_edit_begin,
            callback=make_callback("m:edit", message),
            state=state,
            session=session,
        )
        await call(
            measure.cb_systolic, callback=make_callback("m:sys:140", message), state=state
        )
        await call(
            measure.cb_diastolic, callback=make_callback("m:dia:90", message), state=state
        )
        await call(
            measure.cb_pulse,
            callback=make_callback("m:pulse:skip", message),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.fmt_updated(140, 90, None)
        row = await reload_measurement(session, measurement_id)
        assert row.pulse is None  # «Пропустить» — полная перезапись

    async def test_edit_same_minute_is_not_duplicate(
        self, session, edit, cb_answer
    ) -> None:
        """Правка записи текущей минуты — UPDATE, дубль-проверка не применяется."""
        state = make_state()
        measurement_id = await save_draft(session, state)  # timestamp — текущая минута
        message = make_message()
        await call(
            measure.cb_edit_begin,
            callback=make_callback("m:edit", message),
            state=state,
            session=session,
        )
        await call(
            measure.cb_systolic, callback=make_callback("m:sys:140", message), state=state
        )
        await call(
            measure.cb_diastolic, callback=make_callback("m:dia:90", message), state=state
        )
        await call(
            measure.cb_pulse,
            callback=make_callback("m:pulse:70", message),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.fmt_updated(140, 90, 70)
        assert texts.DUPLICATE_MEASUREMENT not in edit.call_args.args[0]
        row = await reload_measurement(session, measurement_id)
        assert (row.systolic, row.diastolic, row.pulse) == (140, 90, 70)
        rows = (await session.execute(select(MeasurementModel))).scalars().all()
        assert len(rows) == 1

    @pytest.mark.parametrize(
        ("sys_v", "dia_v", "warning"),
        [(185, 85, True), (140, 112, True), (179, 109, False)],
    )
    async def test_high_pressure_warning(
        self, session, answer, sys_v: int, dia_v: int, warning: bool
    ) -> None:
        state = make_state()
        measurement_id = await save_draft(session, state)
        await state.set_data(
            {
                measure.M_EDIT_ID: measurement_id,
                measure.M_ID: measurement_id,
                measure.M_SYS: sys_v,
                measure.M_DIA: dia_v,
                measure.M_PULSE: None,
            }
        )
        await measure._save(make_message(), state, session, TG_ID, via_message=True)
        text = answer.call_args.args[0]
        assert text.startswith(texts.fmt_updated(sys_v, dia_v, None))
        assert (texts.HIGH_PRESSURE_WARNING in text) is warning

    async def test_record_deleted_between_entry_and_save(
        self, session, edit, cb_answer
    ) -> None:
        state = make_state()
        measurement_id = await save_draft(session, state)
        message = make_message()
        await call(
            measure.cb_edit_begin,
            callback=make_callback("m:edit", message),
            state=state,
            session=session,
        )
        row = await reload_measurement(session, measurement_id)
        await session.delete(row)
        await session.commit()
        await call(
            measure.cb_systolic, callback=make_callback("m:sys:140", message), state=state
        )
        await call(
            measure.cb_diastolic, callback=make_callback("m:dia:90", message), state=state
        )
        await call(
            measure.cb_pulse,
            callback=make_callback("m:pulse:70", message),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.NOT_YOUR_RECORD
        assert edit.call_args.kwargs["reply_markup"] == keyboards.back_to_menu()
        assert await state.get_data() == {}

    async def test_cancel_discards_edit(self, session, edit, cb_answer) -> None:
        state = make_state()
        measurement_id = await save_draft(session, state)
        await state.set_data(
            {
                measure.M_EDIT_ID: measurement_id,
                measure.M_ID: measurement_id,
                measure.M_SYS: 140,
                measure.M_DIA: 90,
            }
        )
        await call(
            measure.cb_measure_cancel,
            callback=make_callback("m:cancel", make_message()),
            state=state,
        )
        assert edit.call_args.args[0] == texts.MAIN_MENU
        assert edit.call_args.kwargs["reply_markup"] == keyboards.main_menu()
        assert await state.get_data() == {}
        row = await reload_measurement(session, measurement_id)
        assert (row.systolic, row.diastolic, row.pulse) == (135, 85, 72)  # не изменена

    async def test_begin_mid_edit_saves_new_record(
        self, session, edit, cb_answer
    ) -> None:
        state = make_state()
        user = await measurements_service.get_or_create_user(session, TG_ID)
        old = await measurements_service.save_measurement(
            session,
            user,
            135,
            85,
            72,
            timestamp=measurements_service.utcnow() - timedelta(minutes=5),
        )
        await session.commit()
        await state.set_data(
            {
                measure.M_EDIT_ID: old.id,
                measure.M_ID: old.id,
                measure.M_SYS: 140,
            }
        )
        message = make_message()
        await call(
            measure.cb_measure_begin,
            callback=make_callback("menu:measure", message),
            state=state,
        )
        assert await state.get_data() == {}  # правка отменена
        await call(
            measure.cb_systolic, callback=make_callback("m:sys:110", message), state=state
        )
        await call(
            measure.cb_diastolic, callback=make_callback("m:dia:70", message), state=state
        )
        await call(
            measure.cb_pulse,
            callback=make_callback("m:pulse:60", message),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.fmt_saved(110, 70, 60)  # новая запись
        rows = (
            await session.execute(select(MeasurementModel).order_by(MeasurementModel.id))
        ).scalars().all()
        assert len(rows) == 2
        assert (rows[0].systolic, rows[0].diastolic, rows[0].pulse) == (135, 85, 72)
        assert (rows[1].systolic, rows[1].diastolic, rows[1].pulse) == (110, 70, 60)

    async def test_note_after_edit_attaches_to_edited_record(
        self, session, edit, cb_answer, answer
    ) -> None:
        state = make_state()
        measurement_id = await save_draft(session, state)
        message = make_message()
        await call(
            measure.cb_edit_begin,
            callback=make_callback("m:edit", message),
            state=state,
            session=session,
        )
        await call(
            measure.cb_systolic, callback=make_callback("m:sys:140", message), state=state
        )
        await call(
            measure.cb_diastolic, callback=make_callback("m:dia:90", message), state=state
        )
        await call(
            measure.cb_pulse,
            callback=make_callback("m:pulse:70", message),
            state=state,
            session=session,
        )
        await call(
            measure.cb_note, callback=make_callback("m:note", message), state=state
        )
        await call(
            measure.msg_note,
            message=make_message("после кофе"),
            state=state,
            session=session,
        )
        row = await reload_measurement(session, measurement_id)
        assert (row.systolic, row.diastolic, row.pulse, row.note) == (140, 90, 70, "после кофе")

    async def test_hub_shows_updated_after_edit(self, session, edit, cb_answer) -> None:
        state = make_state()
        measurement_id = await save_draft(session, state)
        await state.set_data(
            {
                measure.M_EDIT_ID: measurement_id,
                measure.M_ID: measurement_id,
                measure.M_SYS: 140,
                measure.M_DIA: 90,
                measure.M_PULSE: 70,
            }
        )
        await call(
            measure.cb_med_no_skip,
            callback=make_callback("m:med:no", make_message()),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.fmt_updated(140, 90, 70)
        assert edit.call_args.kwargs["reply_markup"] == keyboards.saved_kb()

    async def test_reedit_targets_same_record(self, session, edit, cb_answer) -> None:
        """Пока запись остаётся последней, повторное «✏️ Изменить» правит её же."""
        state = make_state()
        user = await measurements_service.get_or_create_user(session, TG_ID)
        await measurements_service.save_measurement(
            session,
            user,
            120,
            80,
            timestamp=measurements_service.utcnow() - timedelta(minutes=5),
        )
        target = await measurements_service.save_measurement(
            session,
            user,
            135,
            85,
            timestamp=measurements_service.utcnow() - timedelta(minutes=2),
        )
        await session.commit()
        message = make_message()
        await call(
            measure.cb_edit_begin,
            callback=make_callback("m:edit", message),
            state=state,
            session=session,
        )
        assert (await state.get_data())[measure.M_EDIT_ID] == target.id
        await call(
            measure.cb_systolic, callback=make_callback("m:sys:140", message), state=state
        )
        await call(
            measure.cb_diastolic, callback=make_callback("m:dia:90", message), state=state
        )
        await call(
            measure.cb_pulse,
            callback=make_callback("m:pulse:70", message),
            state=state,
            session=session,
        )
        # Повторный вход: «последняя» резолвится заново — та же запись.
        await call(
            measure.cb_edit_begin,
            callback=make_callback("m:edit", message),
            state=state,
            session=session,
        )
        assert (await state.get_data())[measure.M_EDIT_ID] == target.id


# ─────────────────────────── Заметка ───────────────────────────


class TestNote:
    async def test_note_flow_saves(self, session, edit, cb_answer, answer) -> None:
        state = make_state()
        measurement_id = await save_draft(session, state)
        await call(
            measure.cb_note,
            callback=make_callback("m:note", make_message()),
            state=state,
        )
        assert edit.call_args.args[0] == texts.PROMPT_NOTE
        assert await state.get_state() == measure.MeasureInput.note_input.state
        await call(
            measure.msg_note,
            message=make_message("после кофе"),
            state=state,
            session=session,
        )
        assert answer.call_args.args[0] == texts.NOTE_SAVED
        assert answer.call_args.kwargs["reply_markup"] == keyboards.saved_kb()
        row = await reload_measurement(session, measurement_id)
        assert row.note == "после кофе"

    @pytest.mark.parametrize("text", ["", "   ", "x" * 501])
    async def test_msg_note_invalid(self, answer, text: str) -> None:
        state = make_state()
        await state.set_state(measure.MeasureInput.note_input)
        await call(
            measure.msg_note, message=make_message(text), state=state, session=None
        )
        assert answer.call_args.kwargs["reply_markup"] == keyboards.note_cancel_kb()
        assert await state.get_state() == measure.MeasureInput.note_input.state


# ─────────────────────────── «Приняли лекарство?» ───────────────────────────


class TestMedication:
    async def test_med_ask_screen(self, session, edit, cb_answer) -> None:
        state = make_state()
        await save_draft(session, state)
        await call(
            measure.cb_med, callback=make_callback("m:med", make_message()), state=state
        )
        assert edit.call_args.args[0] == texts.MED_ASK
        assert edit.call_args.kwargs["reply_markup"] == keyboards.medication_taken_kb()

    async def test_med_yes_plans_remeasure(
        self, session, user, edit, cb_answer, answer, reminders
    ) -> None:
        await medications_service.add_medication(session, user, "Лозартан", "50 мг", ["morning"])
        await session.commit()
        state = make_state()
        measurement_id = await save_draft(session, state)
        await call(
            measure.cb_med_yes,
            callback=make_callback("m:med:yes", make_message()),
            state=state,
            session=session,
            reminders=reminders,
        )
        row = await reload_measurement(session, measurement_id)
        assert row.medication_taken is True
        reminders.ensure_remeasure.assert_awaited_once_with(user.id, measurement_id, 60)
        assert edit.call_args.args[0] == texts.MED_REMEASURE_PLANNED.format(delay=60)
        assert edit.call_args.kwargs["reply_markup"] == keyboards.no_remeasure_kb(measurement_id)
        answer.assert_not_awaited()  # лекарства уже настроены — подсказки нет

    async def test_med_yes_disabled_delay(
        self, session, user, edit, cb_answer, answer, reminders
    ) -> None:
        user.med_remeasure_delay = None
        await session.commit()
        state = make_state()
        await save_draft(session, state)
        await call(
            measure.cb_med_yes,
            callback=make_callback("m:med:yes", make_message()),
            state=state,
            session=session,
            reminders=reminders,
        )
        reminders.ensure_remeasure.assert_not_awaited()
        assert edit.call_args.args[0] == texts.MED_REMEASURE_DISABLED
        assert edit.call_args.kwargs["reply_markup"] == keyboards.saved_kb()
        # Подсказка о лекарствах не зависит от повтора: первая «Да» — показать.
        assert answer.call_args.args[0] == texts.MEDS_ASK_PROMPT

    async def test_med_yes_prompts_meds_first_time(
        self, session, edit, cb_answer, answer, reminders
    ) -> None:
        state = make_state()
        await save_draft(session, state)
        await call(
            measure.cb_med_yes,
            callback=make_callback("m:med:yes", make_message()),
            state=state,
            session=session,
            reminders=reminders,
        )
        assert answer.call_args.args[0] == texts.MEDS_ASK_PROMPT
        assert answer.call_args.kwargs["reply_markup"] == keyboards.meds_ask_kb()

    async def test_med_yes_no_prompt_on_second_time(
        self, session, edit, cb_answer, answer, reminders
    ) -> None:
        state = make_state()
        await save_draft(session, state)
        # Прошлая запись уже с «Да» — промпт был, повторять не нужно.
        user = await reload_user(session)
        first = await measurements_service.save_measurement(
            session,
            user,
            130,
            80,
            timestamp=measurements_service.utcnow() + timedelta(minutes=2),
        )
        await measurements_service.set_medication_taken(session, user, first.id, True)
        await session.commit()
        await call(
            measure.cb_med_yes,
            callback=make_callback("m:med:yes", make_message()),
            state=state,
            session=session,
            reminders=reminders,
        )
        answer.assert_not_awaited()

    async def test_med_yes_no_prompt_when_meds_configured(
        self, session, user, edit, cb_answer, answer, reminders
    ) -> None:
        await medications_service.add_medication(session, user, "Лозартан", "50 мг", ["morning"])
        await session.commit()
        state = make_state()
        await save_draft(session, state)
        await call(
            measure.cb_med_yes,
            callback=make_callback("m:med:yes", make_message()),
            state=state,
            session=session,
            reminders=reminders,
        )
        answer.assert_not_awaited()

    async def test_med_no_sets_false(self, session, edit, cb_answer) -> None:
        state = make_state()
        measurement_id = await save_draft(session, state)
        await call(
            measure.cb_med_no_skip,
            callback=make_callback("m:med:no", make_message()),
            state=state,
            session=session,
        )
        row = await reload_measurement(session, measurement_id)
        assert row.medication_taken is False
        assert edit.call_args.args[0] == texts.fmt_saved(135, 85, 72)
        assert edit.call_args.kwargs["reply_markup"] == keyboards.saved_kb()

    async def test_med_skip_sets_none(self, session, edit, cb_answer) -> None:
        state = make_state()
        measurement_id = await save_draft(session, state)
        await call(
            measure.cb_med_no_skip,
            callback=make_callback("m:med:skip", make_message()),
            state=state,
            session=session,
        )
        row = await reload_measurement(session, measurement_id)
        assert row.medication_taken is None
        assert edit.call_args.args[0] == texts.fmt_saved(135, 85, 72)

    async def test_meds_ask_dismiss(self, edit, cb_answer) -> None:
        await call(
            measure.cb_meds_ask_dismiss,
            callback=make_callback("m:medask:dismiss", make_message()),
        )
        assert edit.call_args.args[0] == texts.MEDS_ASK_DISMISSED
        cb_answer.assert_awaited_once()


# ─────────────────────────── Самочувствие ───────────────────────────


class TestFeeling:
    async def test_feeling_screen(self, session, edit, cb_answer) -> None:
        state = make_state()
        await save_draft(session, state)
        await call(
            measure.cb_feeling,
            callback=make_callback("m:feeling", make_message()),
            state=state,
        )
        assert edit.call_args.args[0] == texts.FEELING_ASK
        assert edit.call_args.kwargs["reply_markup"] == keyboards.feeling_kb()

    @pytest.mark.parametrize(
        ("value", "expected"),
        [("normal", "normal"), ("headache", "headache"), ("weakness", "weakness")],
    )
    async def test_feeling_set(
        self, session, edit, cb_answer, value: str, expected: str
    ) -> None:
        state = make_state()
        measurement_id = await save_draft(session, state)
        await call(
            measure.cb_feeling_set,
            callback=make_callback(f"m:feeling:{value}", make_message()),
            state=state,
            session=session,
        )
        row = await reload_measurement(session, measurement_id)
        assert row.feeling == expected
        assert edit.call_args.args[0] == texts.fmt_saved(135, 85, 72)

    async def test_feeling_other_prompts(self, session, edit, cb_answer) -> None:
        state = make_state()
        await save_draft(session, state)
        await call(
            measure.cb_feeling_other,
            callback=make_callback("m:feeling:other", make_message()),
            state=state,
        )
        assert edit.call_args.args[0] == texts.PROMPT_FEELING_OTHER
        assert await state.get_state() == measure.MeasureInput.feeling_other.state

    async def test_feeling_other_saves_note(self, session, answer) -> None:
        state = make_state()
        measurement_id = await save_draft(session, state)
        await state.set_state(measure.MeasureInput.feeling_other)
        await call(
            measure.msg_feeling_other,
            message=make_message("тошнота после еды"),
            state=state,
            session=session,
        )
        row = await reload_measurement(session, measurement_id)
        assert row.feeling == "other"
        assert row.note == "тошнота после еды"
        assert answer.call_args.args[0] == texts.NOTE_SAVED

    @pytest.mark.parametrize("text", ["", "   ", "x" * 501])
    async def test_msg_feeling_other_invalid(self, answer, text: str) -> None:
        state = make_state()
        await state.set_state(measure.MeasureInput.feeling_other)
        await call(
            measure.msg_feeling_other, message=make_message(text), state=state, session=None
        )
        assert answer.call_args.kwargs["reply_markup"] == keyboards.note_cancel_kb()
        assert await state.get_state() == measure.MeasureInput.feeling_other.state


# ─────────────────────────── «Не надо» ───────────────────────────


class TestNoRemeasure:
    async def test_no_remeasure_cancels(
        self, session, edit, cb_answer, reminders
    ) -> None:
        state = make_state()
        measurement_id = await save_draft(session, state)
        await call(
            measure.cb_no_remeasure,
            callback=make_callback(f"m:noremeasure:{measurement_id}", make_message()),
            session=session,
            reminders=reminders,
        )
        user = await reload_user(session)
        reminders.cancel_remeasure_for_measurement.assert_awaited_once_with(
            user.id, measurement_id
        )
        assert edit.call_args.args[0] == texts.MED_REMEASURE_CANCELLED
        assert edit.call_args.kwargs["reply_markup"] == keyboards.saved_kb()

    async def test_no_remeasure_checks_owner(
        self, session, edit, cb_answer, reminders
    ) -> None:
        # id записи передаётся в callback_data, но в сервис уходит user_id
        # нажавшего — чужая запись для него выглядит как отсутствующая.
        await call(
            measure.cb_no_remeasure,
            callback=make_callback(
                "m:noremeasure:777", make_message(user_id=222_222), user_id=222_222
            ),
            session=session,
            reminders=reminders,
        )
        user = (
            await session.execute(
                select(UserModel).where(UserModel.telegram_id == 222_222)
            )
        ).scalar_one()
        reminders.cancel_remeasure_for_measurement.assert_awaited_once_with(user.id, 777)

    async def test_no_remeasure_bad_value(self, edit, cb_answer, reminders) -> None:
        await call(
            measure.cb_no_remeasure,
            callback=make_callback("m:noremeasure:abc", make_message()),
            session=None,
            reminders=reminders,
        )
        assert edit.call_args.args[0] == texts.MAIN_MENU
        reminders.cancel_remeasure_for_measurement.assert_not_awaited()


# ─────────────────────────── Устаревшие кнопки ───────────────────────────


class TestStaleButtons:
    @pytest.mark.parametrize(
        ("data", "handler"),
        [
            ("m:note", measure.cb_note),
            ("m:med", measure.cb_med),
            ("m:med:yes", measure.cb_med_yes),
            ("m:med:no", measure.cb_med_no_skip),
            ("m:med:skip", measure.cb_med_no_skip),
            ("m:feeling", measure.cb_feeling),
            ("m:feeling:normal", measure.cb_feeling_set),
            ("m:feeling:other", measure.cb_feeling_other),
            ("m:pulse:50", measure.cb_pulse),
            ("m:pulse:other", measure.cb_pulse),
        ],
    )
    async def test_without_draft_shows_menu(
        self, session, edit, cb_answer, reminders, data: str, handler
    ) -> None:
        state = make_state()
        callback = make_callback(data, make_message())
        await call(
            handler,
            callback=callback,
            state=state,
            session=session,
            reminders=reminders,
        )
        assert edit.call_args.args[0] == texts.MAIN_MENU
        assert edit.call_args.kwargs["reply_markup"] == keyboards.main_menu()
        cb_answer.assert_awaited_once()

    async def test_save_without_draft_shows_menu(self, session, answer) -> None:
        """Устаревший `msg_pulse` без черновика не даёт KeyError — главное меню."""
        state = make_state()
        await call(
            measure.msg_pulse,
            message=make_message("66"),
            state=state,
            session=session,
        )
        assert answer.call_args.args[0] == texts.MAIN_MENU
        assert answer.call_args.kwargs["reply_markup"] == keyboards.main_menu()


# ─────────────────────────── Полный флоу ───────────────────────────


class TestFullFlow:
    async def test_end_to_end(
        self, session, answer, edit, cb_answer, reminders
    ) -> None:
        state = make_state()
        message = make_message()
        callback = make_callback("menu:measure", message)
        await call(measure.cb_measure_begin, callback=callback, state=state)
        await call(
            measure.cb_systolic,
            callback=make_callback("m:sys:135", message),
            state=state,
        )
        await call(
            measure.cb_diastolic,
            callback=make_callback("m:dia:85", message),
            state=state,
        )
        await call(
            measure.cb_pulse,
            callback=make_callback("m:pulse:72", message),
            state=state,
            session=session,
        )
        await call(
            measure.cb_note, callback=make_callback("m:note", message), state=state
        )
        await call(
            measure.msg_note,
            message=make_message("после кофе"),
            state=state,
            session=session,
        )
        row = (await session.execute(select(MeasurementModel))).scalars().one()
        assert (row.systolic, row.diastolic, row.pulse, row.note) == (135, 85, 72, "после кофе")
        assert await state.get_state() is None
