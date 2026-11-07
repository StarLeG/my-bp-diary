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
from bot.models import Medication, MedicationLog
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


async def tap(
    state: FSMContext,
    data: str,
    session: AsyncSession | None = None,
    message: Message | None = None,
    cb_answer=None,
    edit=None,
) -> None:
    """Нажать кнопку калькулятора (`m:key:…`)."""
    await call(
        measure.cb_keypad,
        callback=make_callback(data, message or make_message()),
        state=state,
        session=session,
    )


async def start_input(state: FSMContext, target: str) -> None:
    """Перевести FSM в шаг ввода `target` с пустым буфером (как после `cb_measure_begin`)."""
    states = {
        "sys": measure.MeasureInput.sys_input,
        "dia": measure.MeasureInput.dia_input,
        "pulse": measure.MeasureInput.pulse_input,
    }
    await state.set_state(states[target])
    await state.update_data(**{measure.M_BUF: ""})


def keypad_text(question: str, buf: str = "") -> str:
    return texts.fmt_keypad_prompt(question, buf)


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
    async def test_begin_shows_systolic_keypad(self, edit, cb_answer) -> None:
        state = make_state()
        await call(
            measure.cb_measure_begin,
            callback=make_callback("menu:measure", make_message()),
            state=state,
        )
        assert edit.call_args.args[0] == keypad_text(texts.ASK_SYSTOLIC)
        assert edit.call_args.kwargs["reply_markup"] == keyboards.keypad_kb(
            "", allow_skip=False
        )
        assert await state.get_state() == measure.MeasureInput.sys_input.state
        cb_answer.assert_awaited_once()

    async def test_begin_resets_old_draft(self, edit, cb_answer) -> None:
        state = make_state()
        await state.set_data({measure.M_SYS: 100, measure.M_ID: 42})
        await call(
            measure.cb_measure_begin,
            callback=make_callback("menu:measure", make_message()),
            state=state,
        )
        assert await state.get_data() == {measure.M_BUF: ""}

    @pytest.mark.parametrize("data", ["rem:write", "rem:now"])
    async def test_begin_from_reminder(self, data: str, edit, cb_answer) -> None:
        """«Записать давление» / «Записать сейчас» из напоминания — тот же сценарий 6.3."""
        state = make_state()
        await call(
            measure.cb_measure_begin,
            callback=make_callback(data, make_message()),
            state=state,
        )
        assert edit.call_args.args[0] == keypad_text(texts.ASK_SYSTOLIC)

    async def test_cancel_returns_to_menu(self, edit, cb_answer) -> None:
        state = make_state()
        await start_input(state, "sys")
        await call(
            measure.cb_measure_cancel,
            callback=make_callback("m:cancel", make_message()),
            state=state,
        )
        assert edit.call_args.args[0] == texts.MAIN_MENU
        assert edit.call_args.kwargs["reply_markup"] == keyboards.main_menu()
        assert await state.get_state() is None
        assert await state.get_data() == {}


# ─────────────────────────── Калькулятор: буфер ───────────────────────────


class TestKeypadBuffer:
    async def test_digits_accumulate(self, edit, cb_answer) -> None:
        state = make_state()
        await start_input(state, "sys")
        await tap(state, "m:key:1")
        await tap(state, "m:key:3")
        await tap(state, "m:key:5")
        assert (await state.get_data())[measure.M_BUF] == "135"
        assert edit.call_args.args[0] == keypad_text(texts.ASK_SYSTOLIC, "135")
        assert edit.call_args.kwargs["reply_markup"] == keyboards.keypad_kb(
            "135", allow_skip=False
        )

    async def test_leading_zeros_collapse(self, edit, cb_answer) -> None:
        state = make_state()
        await start_input(state, "sys")
        await tap(state, "m:key:0")
        assert (await state.get_data())[measure.M_BUF] == "0"
        await tap(state, "m:key:0")
        assert (await state.get_data())[measure.M_BUF] == "0"
        await tap(state, "m:key:7")
        assert (await state.get_data())[measure.M_BUF] == "7"

    async def test_overflow_ignored(self, edit, cb_answer) -> None:
        state = make_state()
        await start_input(state, "sys")
        for digit in "2605":
            await tap(state, f"m:key:{digit}")
        assert (await state.get_data())[measure.M_BUF] == "260"

    async def test_backspace(self, edit, cb_answer) -> None:
        state = make_state()
        await start_input(state, "sys")
        await state.update_data(**{measure.M_BUF: "13"})
        await tap(state, "m:key:bksp")
        assert (await state.get_data())[measure.M_BUF] == "1"
        await tap(state, "m:key:bksp")
        assert (await state.get_data())[measure.M_BUF] == ""
        await tap(state, "m:key:bksp")  # пустой буфер не падает
        assert (await state.get_data())[measure.M_BUF] == ""

    async def test_clear(self, edit, cb_answer) -> None:
        state = make_state()
        await start_input(state, "sys")
        await state.update_data(**{measure.M_BUF: "135"})
        await tap(state, "m:key:clear")
        assert (await state.get_data())[measure.M_BUF] == ""
        assert edit.call_args.args[0] == keypad_text(texts.ASK_SYSTOLIC, "")


# ─────────────────────────── Калькулятор: «Готово» / «Пропустить» ───────────────────────────


class TestKeypadConfirm:
    async def test_ok_empty_buffer(self, edit, cb_answer) -> None:
        state = make_state()
        await start_input(state, "sys")
        await tap(state, "m:key:ok")
        cb_answer.assert_awaited_once_with(texts.KEYPAD_EMPTY_INPUT)
        edit.assert_not_awaited()  # экран не меняется
        assert await state.get_state() == measure.MeasureInput.sys_input.state

    async def test_ok_out_of_range(self, edit, cb_answer) -> None:
        state = make_state()
        await start_input(state, "sys")
        await state.update_data(**{measure.M_BUF: "300"})
        await tap(state, "m:key:ok")
        cb_answer.assert_awaited_once_with(
            "Нужно систолическое от 70 до 260, а получилось 300"
        )
        edit.assert_not_awaited()  # экран не меняется
        assert (await state.get_data())[measure.M_BUF] == "300"

    async def test_ok_systolic_advances(self, edit, cb_answer) -> None:
        state = make_state()
        await start_input(state, "sys")
        await state.update_data(**{measure.M_BUF: "135"})
        await tap(state, "m:key:ok", session=None)
        assert (await state.get_data())[measure.M_SYS] == 135
        assert await state.get_state() == measure.MeasureInput.dia_input.state
        assert edit.call_args.args[0] == keypad_text(texts.ASK_DIASTOLIC, "")
        assert edit.call_args.kwargs["reply_markup"] == keyboards.keypad_kb(
            "", allow_skip=False
        )

    async def test_ok_diastolic_advances_to_pulse(self, edit, cb_answer) -> None:
        state = make_state()
        await start_input(state, "dia")
        await state.update_data(**{measure.M_SYS: 135, measure.M_BUF: "85"})
        await tap(state, "m:key:ok", session=None)
        assert (await state.get_data())[measure.M_DIA] == 85
        assert await state.get_state() == measure.MeasureInput.pulse_input.state
        assert edit.call_args.kwargs["reply_markup"] == keyboards.keypad_kb(
            "", allow_skip=True
        )

    async def test_ok_diastolic_pair_violation(self, edit, cb_answer) -> None:
        state = make_state()
        await start_input(state, "dia")
        await state.update_data(**{measure.M_SYS: 120, measure.M_BUF: "120"})
        await tap(state, "m:key:ok", session=None)
        cb_answer.assert_awaited_once_with(texts.DIA_NOT_LOWER)
        edit.assert_not_awaited()  # экран не меняется
        assert await state.get_state() == measure.MeasureInput.dia_input.state
        assert measure.M_DIA not in await state.get_data()

    async def test_ok_pair_equal_values_violation(self, edit, cb_answer) -> None:
        """Диастолическое строго ниже систолического — равные значения тоже ошибка (§9)."""
        state = make_state()
        await start_input(state, "dia")
        await state.update_data(**{measure.M_SYS: 100, measure.M_BUF: "100"})
        await tap(state, "m:key:ok", session=None)
        cb_answer.assert_awaited_once_with(texts.DIA_NOT_LOWER)

    async def test_ok_pulse_saves(self, session, edit, cb_answer) -> None:
        state = make_state()
        await start_input(state, "pulse")
        await state.update_data(**{measure.M_SYS: 135, measure.M_DIA: 85, measure.M_BUF: "72"})
        await tap(state, "m:key:ok", session=session)
        assert edit.call_args.args[0] == texts.fmt_saved(135, 85, 72)
        assert edit.call_args.kwargs["reply_markup"] == keyboards.saved_kb()
        row = (
            await session.execute(
                select(MeasurementModel).execution_options(populate_existing=True)
            )
        ).scalars().one()
        assert (row.systolic, row.diastolic, row.pulse) == (135, 85, 72)
        assert (await state.get_data())[measure.M_ID] == row.id

    async def test_skip_pulse_saves_without_pulse(self, session, edit, cb_answer) -> None:
        state = make_state()
        await start_input(state, "pulse")
        await state.update_data(**{measure.M_SYS: 135, measure.M_DIA: 85})
        await tap(state, "m:key:skip", session=session)
        assert edit.call_args.args[0] == texts.fmt_saved(135, 85, None)
        row = (
            await session.execute(
                select(MeasurementModel).execution_options(populate_existing=True)
            )
        ).scalars().one()
        assert row.pulse is None

    async def test_skip_only_on_pulse(self, edit, cb_answer) -> None:
        state = make_state()
        await start_input(state, "sys")
        await tap(state, "m:key:skip", session=None)
        assert edit.call_args.args[0] == texts.MAIN_MENU  # устаревшие кнопки — в меню
        assert await state.get_state() == measure.MeasureInput.sys_input.state

    async def test_unknown_key_goes_to_menu(self, edit, cb_answer) -> None:
        state = make_state()
        await start_input(state, "sys")
        await tap(state, "m:key:foo", session=None)
        assert edit.call_args.args[0] == texts.MAIN_MENU

    async def test_key_without_state_goes_to_menu(self, edit, cb_answer) -> None:
        state = make_state()
        await tap(state, "m:key:5", session=None)
        assert edit.call_args.args[0] == texts.MAIN_MENU


# ─────────────────────────── Число сообщением в чат ───────────────────────────


class TestTextInput:
    async def test_msg_systolic_advances(self, answer) -> None:
        state = make_state()
        await start_input(state, "sys")
        await call(
            measure.msg_systolic, message=make_message(" 120 "), state=state, session=None
        )
        assert answer.call_args.args[0] == keypad_text(texts.ASK_DIASTOLIC, "")
        assert answer.call_args.kwargs["reply_markup"] == keyboards.keypad_kb(
            "", allow_skip=False
        )
        assert (await state.get_data())[measure.M_SYS] == 120
        assert await state.get_state() == measure.MeasureInput.dia_input.state

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("12.5", "Нужно целое число от 70 до 260"),
            ("120/80", "Нужно целое число от 70 до 260"),
            ("абырвалг", "Нужно целое число от 70 до 260"),
            ("", "Нужно целое число от 70 до 260"),
            ("300", "Нужно систолическое от 70 до 260, а получилось 300"),
        ],
    )
    async def test_msg_systolic_invalid(self, answer, text: str, expected: str) -> None:
        state = make_state()
        await start_input(state, "sys")
        await call(
            measure.msg_systolic, message=make_message(text), state=state, session=None
        )
        answer.assert_awaited_once_with(expected)  # короткая ошибка, повтор ввода
        assert await state.get_state() == measure.MeasureInput.sys_input.state

    async def test_msg_diastolic_advances(self, answer) -> None:
        state = make_state()
        await start_input(state, "dia")
        await state.update_data(**{measure.M_SYS: 135})
        await call(
            measure.msg_diastolic, message=make_message("88"), state=state, session=None
        )
        assert answer.call_args.args[0] == keypad_text(texts.ASK_PULSE, "")
        assert answer.call_args.kwargs["reply_markup"] == keyboards.keypad_kb(
            "", allow_skip=True
        )
        assert (await state.get_data())[measure.M_DIA] == 88

    async def test_msg_diastolic_pair_violation(self, answer) -> None:
        state = make_state()
        await start_input(state, "dia")
        await state.update_data(**{measure.M_SYS: 120})
        await call(
            measure.msg_diastolic, message=make_message("125"), state=state, session=None
        )
        answer.assert_awaited_once_with(texts.DIA_NOT_LOWER)
        assert await state.get_state() == measure.MeasureInput.dia_input.state

    async def test_msg_pulse_saves(self, session, answer) -> None:
        state = make_state()
        await start_input(state, "pulse")
        await state.update_data(**{measure.M_SYS: 135, measure.M_DIA: 85})
        await call(
            measure.msg_pulse, message=make_message("66"), state=state, session=session
        )
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
        await start_input(state, "pulse")
        await state.update_data(**{measure.M_SYS: 135, measure.M_DIA: 85})
        await call(
            measure.msg_pulse, message=make_message("999"), state=state, session=None
        )
        answer.assert_awaited_once_with("Нужно пульс от 30 до 220, а получилось 999")
        assert await state.get_state() == measure.MeasureInput.pulse_input.state


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

    async def test_summary_format_exact(self, session, answer) -> None:
        """Сводка — ровно как в SPEC §6.3 (код-блок, без пульса — «Пульс: —»)."""
        state = make_state()
        await state.set_data({measure.M_SYS: 135, measure.M_DIA: 85, measure.M_PULSE: None})
        await measure._save(make_message(), state, session, TG_ID, via_message=True)
        assert answer.call_args.args[0] == (
            "✅ Записано\nСистолическое: 135\nДиастолическое: 85\nПульс: —"
        )

    async def test_save_without_draft_shows_menu(self, session, answer) -> None:
        state = make_state()
        await measure._save(make_message(), state, session, TG_ID, via_message=True)
        assert answer.call_args.args[0] == texts.MAIN_MENU


# ─────────────────────────── Правка последней записи ───────────────────────────


async def make_measurement(
    session: AsyncSession,
    telegram_id: int = TG_ID,
    sys_v: int = 120,
    dia_v: int = 80,
    pulse: int | None = 66,
    ts: datetime | None = None,
) -> int:
    user = await measurements_service.get_or_create_user(session, telegram_id)
    row = MeasurementModel(
        user_id=user.id,
        timestamp=ts or datetime.now(timezone.utc),
        systolic=sys_v,
        diastolic=dia_v,
        pulse=pulse,
    )
    session.add(row)
    await session.commit()
    return row.id


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

    async def test_opens_keypad_fixes_target(self, session, edit, cb_answer) -> None:
        measurement_id = await make_measurement(session)
        state = make_state()
        await call(
            measure.cb_edit_begin,
            callback=make_callback("m:edit", make_message()),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == keypad_text(texts.ASK_SYSTOLIC)
        data = await state.get_data()
        assert data[measure.M_EDIT_ID] == measurement_id
        assert data[measure.M_ID] == measurement_id
        assert await state.get_state() == measure.MeasureInput.sys_input.state

    async def test_resets_old_draft_and_state(self, session, edit, cb_answer) -> None:
        measurement_id = await make_measurement(session)
        state = make_state()
        await state.set_state(measure.MeasureInput.note_input)
        await state.set_data({measure.M_SYS: 999, measure.M_ID: 0})
        await call(
            measure.cb_edit_begin,
            callback=make_callback("m:edit", make_message()),
            state=state,
            session=session,
        )
        data = await state.get_data()
        assert measure.M_SYS not in data
        assert data[measure.M_EDIT_ID] == measurement_id

    async def test_resolves_latest_by_timestamp(self, session, edit, cb_answer) -> None:
        now = datetime.now(timezone.utc)
        older = await make_measurement(session, sys_v=120, ts=now - timedelta(minutes=5))
        newer = await make_measurement(session, sys_v=130, ts=now)
        state = make_state()
        await call(
            measure.cb_edit_begin,
            callback=make_callback("m:edit", make_message()),
            state=state,
            session=session,
        )
        assert (await state.get_data())[measure.M_EDIT_ID] == newer
        assert (await state.get_data())[measure.M_EDIT_ID] != older

    async def test_foreign_newer_not_selected(self, session, edit, cb_answer) -> None:
        """«Последняя» — только своя запись (§7.1)."""
        mine = await make_measurement(session)
        await make_measurement(session, telegram_id=222_222, sys_v=180)
        state = make_state()
        await call(
            measure.cb_edit_begin,
            callback=make_callback("m:edit", make_message()),
            state=state,
            session=session,
        )
        assert (await state.get_data())[measure.M_EDIT_ID] == mine


class TestEditSave:
    async def test_update_keeps_timestamp(self, session, edit, cb_answer) -> None:
        measurement_id = await save_draft(session, make_state())
        before = await reload_measurement(session, measurement_id)
        ts = before.timestamp
        state = make_state()
        await state.set_data(
            {
                measure.M_SYS: 140,
                measure.M_DIA: 90,
                measure.M_PULSE: None,
                measure.M_EDIT_ID: measurement_id,
                measure.M_ID: measurement_id,
            }
        )
        await measure._save(make_message(), state, session, TG_ID)
        assert edit.call_args.args[0] == texts.fmt_updated(140, 90, None)
        row = await reload_measurement(session, measurement_id)
        assert (row.systolic, row.diastolic, row.pulse) == (140, 90, None)
        assert row.timestamp == ts

    async def test_skip_clears_pulse(self, session, edit, cb_answer) -> None:
        """«Пропустить» в правке очищает пульс (§6.3)."""
        measurement_id = await save_draft(session, make_state(), pulse=72)
        state = make_state()
        await state.set_data(
            {
                measure.M_SYS: 135,
                measure.M_DIA: 85,
                measure.M_PULSE: None,
                measure.M_EDIT_ID: measurement_id,
                measure.M_ID: measurement_id,
            }
        )
        await measure._save(make_message(), state, session, TG_ID)
        row = await reload_measurement(session, measurement_id)
        assert row.pulse is None

    async def test_edit_same_minute_is_not_duplicate(self, session, edit) -> None:
        """Правка в ту же минуту — UPDATE, а не дедуп (§7.2)."""
        measurement_id = await save_draft(session, make_state())
        state = make_state()
        await state.set_data(
            {
                measure.M_SYS: 135,
                measure.M_DIA: 85,
                measure.M_PULSE: 72,
                measure.M_EDIT_ID: measurement_id,
                measure.M_ID: measurement_id,
            }
        )
        await measure._save(make_message(), state, session, TG_ID)
        assert texts.DUPLICATE_MEASUREMENT not in edit.call_args.args[0]
        assert edit.call_args.args[0] == texts.fmt_updated(135, 85, 72)

    async def test_record_deleted_between_entry_and_save(
        self, session, edit, cb_answer
    ) -> None:
        measurement_id = await save_draft(session, make_state())
        row = await reload_measurement(session, measurement_id)
        await session.delete(row)
        await session.commit()
        state = make_state()
        await state.set_data(
            {
                measure.M_SYS: 140,
                measure.M_DIA: 90,
                measure.M_PULSE: 70,
                measure.M_EDIT_ID: measurement_id,
                measure.M_ID: measurement_id,
            }
        )
        await measure._save(make_message(), state, session, TG_ID)
        assert edit.call_args.args[0] == texts.NOT_YOUR_RECORD
        assert edit.call_args.kwargs["reply_markup"] == keyboards.back_to_menu()

    async def test_cancel_discards_edit(self, session, edit, cb_answer) -> None:
        """«Отмена» в правке не сохраняет (§6.3)."""
        measurement_id = await save_draft(session, make_state())
        state = make_state()
        await state.set_state(measure.MeasureInput.sys_input)
        await state.set_data({measure.M_EDIT_ID: measurement_id, measure.M_ID: measurement_id})
        await call(
            measure.cb_measure_cancel,
            callback=make_callback("m:cancel", make_message()),
            state=state,
        )
        row = await reload_measurement(session, measurement_id)
        assert (row.systolic, row.diastolic) == (135, 85)
        assert edit.call_args.args[0] == texts.MAIN_MENU

    async def test_begin_mid_edit_saves_new_record(self, session, edit, cb_answer) -> None:
        """«Записать давление» посреди правки сбрасывает цель — сохранится новая запись."""
        await save_draft(session, make_state())
        state = make_state()
        await state.set_data({measure.M_EDIT_ID: 1, measure.M_ID: 1})
        await call(
            measure.cb_measure_begin,
            callback=make_callback("menu:measure", make_message()),
            state=state,
        )
        data = await state.get_data()
        assert measure.M_EDIT_ID not in data

    async def test_hub_shows_updated_after_edit(self, session, edit, cb_answer) -> None:
        measurement_id = await save_draft(session, make_state())
        state = make_state()
        await state.set_data(
            {
                measure.M_SYS: 140,
                measure.M_DIA: 90,
                measure.M_PULSE: 70,
                measure.M_EDIT_ID: measurement_id,
                measure.M_ID: measurement_id,
            }
        )
        await measure._hub(make_message(), await state.get_data())
        assert edit.call_args.args[0] == texts.fmt_updated(140, 90, 70)

    async def test_reedit_targets_same_record(self, session, edit, cb_answer) -> None:
        """Повторное «✏️ Изменить» правит ту же запись (§6.3)."""
        measurement_id = await save_draft(session, make_state())
        state = make_state()
        await state.set_data(
            {
                measure.M_SYS: 140,
                measure.M_DIA: 90,
                measure.M_PULSE: 70,
                measure.M_EDIT_ID: measurement_id,
                measure.M_ID: measurement_id,
            }
        )
        await call(
            measure.cb_edit_begin,
            callback=make_callback("m:edit", make_message()),
            state=state,
            session=session,
        )
        assert (await state.get_data())[measure.M_EDIT_ID] == measurement_id


# ─────────────────────────── Заметка ───────────────────────────


class TestNote:
    async def test_note_flow_saves(self, session, edit, cb_answer, answer) -> None:
        measurement_id = await save_draft(session, make_state())
        state = make_state()
        await state.set_data({measure.M_ID: measurement_id})
        await call(
            measure.cb_note,
            callback=make_callback("m:note", make_message()),
            state=state,
        )
        assert edit.call_args.args[0] == texts.PROMPT_NOTE
        assert await state.get_state() == measure.MeasureInput.note_input.state
        await call(
            measure.msg_note,
            message=make_message("После прогулки"),
            state=state,
            session=session,
        )
        assert answer.call_args.args[0] == texts.NOTE_SAVED
        row = await reload_measurement(session, measurement_id)
        assert row.note == "После прогулки"

    @pytest.mark.parametrize("text", ["", "   ", "х" * 501])
    async def test_msg_note_invalid(self, answer, text: str) -> None:
        state = make_state()
        await state.set_state(measure.MeasureInput.note_input)
        await call(measure.msg_note, message=make_message(text), state=state, session=None)
        answer.assert_awaited_once()
        assert await state.get_state() == measure.MeasureInput.note_input.state

    async def test_note_without_draft_goes_to_menu(self, edit, cb_answer) -> None:
        state = make_state()
        await call(
            measure.cb_note,
            callback=make_callback("m:note", make_message()),
            state=state,
        )
        assert edit.call_args.args[0] == texts.MAIN_MENU


# ─────────────────────────── «Приняли лекарство?» ───────────────────────────


async def add_med(
    session: AsyncSession,
    name: str = "Лозартан",
    dosage: str = "50 мг",
    slot: str = "morning",
) -> int:
    user = await measurements_service.get_or_create_user(session, TG_ID)
    rows = await medications_service.add_medication(session, user, name, dosage, [slot])
    await session.commit()
    return rows[0].id


class TestMedication:
    async def test_med_ask_screen(self, session, edit, cb_answer) -> None:
        await save_draft(session, make_state())
        state = make_state()
        await state.set_data({measure.M_ID: 1})
        await call(
            measure.cb_med,
            callback=make_callback("m:med", make_message()),
            state=state,
        )
        assert edit.call_args.args[0] == texts.MED_ASK
        assert edit.call_args.kwargs["reply_markup"] == keyboards.medication_taken_kb()

    async def test_med_yes_plans_remeasure(
        self, session, edit, cb_answer, answer, reminders
    ) -> None:
        measurement_id = await save_draft(session, make_state())
        user = await reload_user(session)
        user.med_remeasure_delay = 60
        await session.commit()
        state = make_state()
        await state.set_data({measure.M_ID: measurement_id})
        await call(
            measure.cb_med_yes,
            callback=make_callback("m:med:yes", make_message()),
            state=state,
            session=session,
            reminders=reminders,
        )
        reminders.ensure_remeasure.assert_awaited_once_with(user.id, measurement_id, 60)
        assert edit.call_args.args[0] == texts.MED_REMEASURE_PLANNED.format(delay=60)
        assert edit.call_args.kwargs["reply_markup"] == keyboards.no_remeasure_kb(
            measurement_id
        )

    async def test_med_yes_disabled_delay(
        self, session, edit, cb_answer, answer, reminders
    ) -> None:
        measurement_id = await save_draft(session, make_state())
        user = await reload_user(session)
        user.med_remeasure_delay = None
        await session.commit()
        state = make_state()
        await state.set_data({measure.M_ID: measurement_id})
        await call(
            measure.cb_med_yes,
            callback=make_callback("m:med:yes", make_message()),
            state=state,
            session=session,
            reminders=reminders,
        )
        reminders.ensure_remeasure.assert_not_awaited()
        assert edit.call_args.args[0] == texts.MED_REMEASURE_DISABLED

    async def test_med_yes_sets_flag_true(
        self, session, edit, cb_answer, answer, reminders
    ) -> None:
        measurement_id = await save_draft(session, make_state())
        state = make_state()
        await state.set_data({measure.M_ID: measurement_id})
        await call(
            measure.cb_med_yes,
            callback=make_callback("m:med:yes", make_message()),
            state=state,
            session=session,
            reminders=reminders,
        )
        row = await reload_measurement(session, measurement_id)
        assert row.medication_taken is True

    async def test_med_yes_prompts_meds_first_time(
        self, session, edit, cb_answer, answer, reminders
    ) -> None:
        measurement_id = await save_draft(session, make_state())
        state = make_state()
        await state.set_data({measure.M_ID: measurement_id})
        await call(
            measure.cb_med_yes,
            callback=make_callback("m:med:yes", make_message()),
            state=state,
            session=session,
            reminders=reminders,
        )
        answer.assert_awaited_once_with(
            texts.MEDS_ASK_PROMPT, reply_markup=keyboards.meds_ask_kb()
        )

    async def test_med_yes_no_prompt_on_second_time(
        self, session, edit, cb_answer, answer, reminders
    ) -> None:
        """Подсказка — один раз: после второй записи с «Да» её нет (§6.3)."""
        first_id = await make_measurement(
            session, ts=datetime.now(timezone.utc) - timedelta(minutes=5)
        )
        user = await reload_user(session)
        await measurements_service.set_medication_taken(session, user, first_id, True)
        await session.commit()
        second_id = await save_draft(session, make_state())
        state = make_state()
        await state.set_data({measure.M_ID: second_id})
        await call(
            measure.cb_med_yes,
            callback=make_callback("m:med:yes", make_message()),
            state=state,
            session=session,
            reminders=reminders,
        )
        answer.assert_not_awaited()

    async def test_med_yes_no_prompt_when_meds_configured(
        self, session, edit, cb_answer, answer, reminders
    ) -> None:
        await add_med(session)
        measurement_id = await save_draft(session, make_state())
        user = await reload_user(session)
        user.med_remeasure_delay = None
        await session.commit()
        state = make_state()
        await state.set_data({measure.M_ID: measurement_id})
        await call(
            measure.cb_med_yes,
            callback=make_callback("m:med:yes", make_message()),
            state=state,
            session=session,
            reminders=reminders,
        )
        assert edit.call_args.args[0] == texts.MEDS_INTAKE_ASK
        answer.assert_not_awaited()

    async def test_med_no_sets_false(self, session, edit, cb_answer) -> None:
        measurement_id = await save_draft(session, make_state())
        state = make_state()
        await state.set_data(
            {measure.M_ID: measurement_id, measure.M_SYS: 135, measure.M_DIA: 85}
        )
        await call(
            measure.cb_med_no,
            callback=make_callback("m:med:no", make_message()),
            state=state,
            session=session,
        )
        row = await reload_measurement(session, measurement_id)
        assert row.medication_taken is False
        assert edit.call_args.args[0] == texts.fmt_saved(135, 85, None)

    async def test_meds_ask_dismiss(self, edit, cb_answer) -> None:
        await call(
            measure.cb_meds_ask_dismiss,
            callback=make_callback("m:medask:dismiss", make_message()),
        )
        assert edit.call_args.args[0] == texts.MEDS_ASK_DISMISSED


# ─────────────────── Мультивыбор «Какие лекарства приняли?» ───────────────────


class TestMedsMultiselect:
    async def test_shows_multiselect_with_empty_checkmarks(
        self, session, edit, cb_answer, answer, reminders
    ) -> None:
        med_id = await add_med(session)
        measurement_id = await save_draft(session, make_state())
        user = await reload_user(session)
        user.med_remeasure_delay = None
        await session.commit()
        state = make_state()
        await state.set_data({measure.M_ID: measurement_id})
        await call(
            measure.cb_med_yes,
            callback=make_callback("m:med:yes", make_message()),
            state=state,
            session=session,
            reminders=reminders,
        )
        meds = await medications_service.list_medications(
            session, await reload_user(session)
        )
        assert edit.call_args.args[0] == texts.MEDS_INTAKE_ASK
        assert edit.call_args.kwargs["reply_markup"] == keyboards.intake_meds_kb(set(), meds)
        assert (await state.get_data())[measure.M_MED_CHECKED] == []

    async def test_prechecks_logged_today(
        self, session, edit, cb_answer, answer, reminders
    ) -> None:
        """«✓» — у уже отмеченных за сегодня (§6.3)."""
        med_id = await add_med(session)
        user = await reload_user(session)
        await medications_service.log_manual_intake(session, user, {med_id})
        await session.commit()
        measurement_id = await save_draft(session, make_state())
        user = await reload_user(session)
        user.med_remeasure_delay = None
        await session.commit()
        state = make_state()
        await state.set_data({measure.M_ID: measurement_id})
        await call(
            measure.cb_med_yes,
            callback=make_callback("m:med:yes", make_message()),
            state=state,
            session=session,
            reminders=reminders,
        )
        meds = await medications_service.list_medications(session, await reload_user(session))
        assert edit.call_args.kwargs["reply_markup"] == keyboards.intake_meds_kb(
            {med_id}, meds
        )
        assert (await state.get_data())[measure.M_MED_CHECKED] == [med_id]

    async def test_with_delay_two_screens(
        self, session, edit, cb_answer, answer, reminders
    ) -> None:
        """«Напомню…» + «Не надо» — одним экраном, мультивыбор — следующим сообщением."""
        med_id = await add_med(session)
        measurement_id = await save_draft(session, make_state())
        user = await reload_user(session)
        user.med_remeasure_delay = 60
        await session.commit()
        state = make_state()
        await state.set_data({measure.M_ID: measurement_id})
        await call(
            measure.cb_med_yes,
            callback=make_callback("m:med:yes", make_message()),
            state=state,
            session=session,
            reminders=reminders,
        )
        reminders.ensure_remeasure.assert_awaited_once()
        assert edit.call_args.args[0] == texts.MED_REMEASURE_PLANNED.format(delay=60)
        assert edit.call_args.kwargs["reply_markup"] == keyboards.no_remeasure_kb(
            measurement_id
        )
        answer.assert_awaited_once()
        assert answer.call_args.args[0] == texts.MEDS_INTAKE_ASK

    async def test_pick_toggles_checkmark(self, session, edit, cb_answer) -> None:
        med_id = await add_med(session)
        await save_draft(session, make_state())
        state = make_state()
        await state.set_data({measure.M_ID: 1, measure.M_MED_CHECKED: []})
        await call(
            measure.cb_med_pick,
            callback=make_callback(f"m:med:pick:{med_id}", make_message()),
            state=state,
            session=session,
        )
        assert (await state.get_data())[measure.M_MED_CHECKED] == [med_id]
        meds = await medications_service.list_medications(
            session, await reload_user(session)
        )
        assert edit.call_args.kwargs["reply_markup"] == keyboards.intake_meds_kb(
            {med_id}, meds
        )
        await call(  # повторное нажатие снимает отметку
            measure.cb_med_pick,
            callback=make_callback(f"m:med:pick:{med_id}", make_message()),
            state=state,
            session=session,
        )
        assert (await state.get_data())[measure.M_MED_CHECKED] == []

    async def test_pick_foreign_id_rejected(self, session, edit, cb_answer) -> None:
        await add_med(session)
        await save_draft(session, make_state())
        state = make_state()
        await state.set_data({measure.M_ID: 1, measure.M_MED_CHECKED: []})
        await call(
            measure.cb_med_pick,
            callback=make_callback("m:med:pick:9999", make_message()),
            state=state,
            session=session,
        )
        cb_answer.assert_awaited_once_with(texts.NOT_YOUR_RECORD)
        edit.assert_not_awaited()
        assert (await state.get_data())[measure.M_MED_CHECKED] == []

    async def test_pick_without_draft_goes_to_menu(self, edit, cb_answer) -> None:
        state = make_state()
        await call(
            measure.cb_med_pick,
            callback=make_callback("m:med:pick:1", make_message()),
            state=state,
            session=None,
        )
        assert edit.call_args.args[0] == texts.MAIN_MENU

    async def test_pick_bad_value_goes_to_menu(self, edit, cb_answer) -> None:
        state = make_state()
        await state.set_data({measure.M_ID: 1})
        await call(
            measure.cb_med_pick,
            callback=make_callback("m:med:pick:abc", make_message()),
            state=state,
            session=None,
        )
        assert edit.call_args.args[0] == texts.MAIN_MENU

    async def test_done_empty_selection(self, session, edit, cb_answer) -> None:
        await save_draft(session, make_state())
        state = make_state()
        await state.set_data({measure.M_ID: 1, measure.M_MED_CHECKED: []})
        await call(
            measure.cb_med_done,
            callback=make_callback("m:med:done", make_message()),
            state=state,
            session=session,
        )
        cb_answer.assert_awaited_once_with(texts.MEDS_INTAKE_EMPTY)
        edit.assert_not_awaited()  # экран не меняется

    async def test_done_logs_taken(self, session, edit, cb_answer) -> None:
        med_id = await add_med(session, name="Лозартан", dosage="50 мг", slot="morning")
        await save_draft(session, make_state())
        state = make_state()
        await state.set_data({measure.M_ID: 1, measure.M_MED_CHECKED: [med_id]})
        await call(
            measure.cb_med_done,
            callback=make_callback("m:med:done", make_message()),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.fmt_meds_intake(["Лозартан 50 мг"])
        assert edit.call_args.kwargs["reply_markup"] == keyboards.saved_kb()
        logs = (
            await session.execute(
                select(MedicationLog).execution_options(populate_existing=True)
            )
        ).scalars().all()
        assert len(logs) == 1
        assert (logs[0].status, logs[0].slot, logs[0].medication_id) == (
            "taken",
            "morning",
            med_id,
        )

    async def test_done_twice_logs_once(self, session, edit, cb_answer) -> None:
        """Двойное «Готово» = один набор логов за локальные сутки (§6.3, §7.2)."""
        med_id = await add_med(session)
        await save_draft(session, make_state())
        state = make_state()
        await state.set_data({measure.M_ID: 1, measure.M_MED_CHECKED: [med_id]})
        await call(
            measure.cb_med_done,
            callback=make_callback("m:med:done", make_message()),
            state=state,
            session=session,
        )
        await state.update_data(**{measure.M_MED_CHECKED: [med_id]})
        await call(
            measure.cb_med_done,
            callback=make_callback("m:med:done", make_message()),
            state=state,
            session=session,
        )
        logs = (
            await session.execute(
                select(MedicationLog).execution_options(populate_existing=True)
            )
        ).scalars().all()
        assert len(logs) == 1
        assert edit.call_args.args[0] == texts.fmt_meds_intake(["Лозартан 50 мг"])

    async def test_done_all_foreign_ids(self, session, edit, cb_answer) -> None:
        await save_draft(session, make_state())
        state = make_state()
        await state.set_data({measure.M_ID: 1, measure.M_MED_CHECKED: [9999]})
        await call(
            measure.cb_med_done,
            callback=make_callback("m:med:done", make_message()),
            state=state,
            session=session,
        )
        cb_answer.assert_awaited_once_with(texts.MEDS_INTAKE_EMPTY)
        logs = (await session.execute(select(MedicationLog))).scalars().all()
        assert logs == []

    async def test_done_without_draft_goes_to_menu(self, edit, cb_answer) -> None:
        state = make_state()
        await call(
            measure.cb_med_done,
            callback=make_callback("m:med:done", make_message()),
            state=state,
            session=None,
        )
        assert edit.call_args.args[0] == texts.MAIN_MENU

    async def test_cancel_returns_to_hub_without_logs(
        self, session, edit, cb_answer
    ) -> None:
        """«Отмена» — назад к сводке без записей (§6.3)."""
        med_id = await add_med(session)
        await save_draft(session, make_state())
        state = make_state()
        await state.set_data(
            {
                measure.M_ID: 1,
                measure.M_SYS: 135,
                measure.M_DIA: 85,
                measure.M_MED_CHECKED: [med_id],
            }
        )
        await call(
            measure.cb_med_cancel,
            callback=make_callback("m:med:cancel", make_message()),
            state=state,
        )
        assert edit.call_args.args[0] == texts.fmt_saved(135, 85, None)
        assert edit.call_args.kwargs["reply_markup"] == keyboards.saved_kb()
        logs = (await session.execute(select(MedicationLog))).scalars().all()
        assert logs == []

    async def test_cancel_without_draft_goes_to_menu(self, edit, cb_answer) -> None:
        state = make_state()
        await call(
            measure.cb_med_cancel,
            callback=make_callback("m:med:cancel", make_message()),
            state=state,
        )
        assert edit.call_args.args[0] == texts.MAIN_MENU


# ─────────────────────────── Самочувствие ───────────────────────────


class TestFeeling:
    async def test_feeling_screen(self, session, edit, cb_answer) -> None:
        await save_draft(session, make_state())
        state = make_state()
        await state.set_data({measure.M_ID: 1})
        await call(
            measure.cb_feeling,
            callback=make_callback("m:feeling", make_message()),
            state=state,
        )
        assert edit.call_args.args[0] == texts.FEELING_ASK
        assert edit.call_args.kwargs["reply_markup"] == keyboards.feeling_kb()

    @pytest.mark.parametrize("value", ["normal", "headache", "weakness"])
    async def test_feeling_set(self, session, edit, cb_answer, value: str) -> None:
        measurement_id = await save_draft(session, make_state())
        state = make_state()
        await state.set_data(
            {
                measure.M_ID: measurement_id,
                measure.M_SYS: 135,
                measure.M_DIA: 85,
                measure.M_PULSE: 72,
            }
        )
        await call(
            measure.cb_feeling_set,
            callback=make_callback(f"m:feeling:{value}", make_message()),
            state=state,
            session=session,
        )
        row = await reload_measurement(session, measurement_id)
        assert row.feeling == value
        assert edit.call_args.args[0] == texts.fmt_saved(135, 85, 72)

    async def test_feeling_other_saves_note(self, session, edit, cb_answer, answer) -> None:
        measurement_id = await save_draft(session, make_state())
        state = make_state()
        await state.set_data({measure.M_ID: measurement_id})
        await call(
            measure.cb_feeling_other,
            callback=make_callback("m:feeling:other", make_message()),
            state=state,
        )
        assert await state.get_state() == measure.MeasureInput.feeling_other.state
        await call(
            measure.msg_feeling_other,
            message=make_message("Немного кружится голова"),
            state=state,
            session=session,
        )
        row = await reload_measurement(session, measurement_id)
        assert row.feeling == "other"
        assert row.note == "Немного кружится голова"
        assert answer.call_args.args[0] == texts.NOTE_SAVED

    @pytest.mark.parametrize("text", ["", "   ", "х" * 501])
    async def test_msg_feeling_other_invalid(self, answer, text: str) -> None:
        state = make_state()
        await state.set_state(measure.MeasureInput.feeling_other)
        await call(
            measure.msg_feeling_other, message=make_message(text), state=state, session=None
        )
        answer.assert_awaited_once()
        assert await state.get_state() == measure.MeasureInput.feeling_other.state


# ─────────────────────────── «Не надо» ───────────────────────────


class TestNoRemeasure:
    async def test_no_remeasure_cancels(
        self, session, edit, cb_answer, reminders
    ) -> None:
        user = await measurements_service.get_or_create_user(session, TG_ID)
        state = make_state()
        await call(
            measure.cb_no_remeasure,
            callback=make_callback("m:noremeasure:42", make_message()),
            session=session,
            reminders=reminders,
        )
        reminders.cancel_remeasure_for_measurement.assert_awaited_once_with(user.id, 42)
        assert edit.call_args.args[0] == texts.MED_REMEASURE_CANCELLED

    async def test_no_remeasure_bad_value(self, edit, cb_answer, reminders) -> None:
        state = make_state()
        await call(
            measure.cb_no_remeasure,
            callback=make_callback("m:noremeasure:abc", make_message()),
            state=state,
            session=None,
            reminders=reminders,
        )
        reminders.cancel_remeasure_for_measurement.assert_not_awaited()
        assert edit.call_args.args[0] == texts.MAIN_MENU


# ─────────────────────────── Устаревшие кнопки ───────────────────────────


class TestStaleButtons:
    @pytest.mark.parametrize(
        "data",
        ["m:med:done", "m:med:cancel", "m:note", "m:med", "m:feeling", "m:med:no"],
    )
    async def test_without_draft_shows_menu(self, data, edit, cb_answer) -> None:
        """Кнопки без черновика — в главное меню, без ошибок (§6.3)."""
        handlers = {
            "m:med:done": measure.cb_med_done,
            "m:med:cancel": measure.cb_med_cancel,
            "m:note": measure.cb_note,
            "m:med": measure.cb_med,
            "m:feeling": measure.cb_feeling,
            "m:med:no": measure.cb_med_no,
        }
        state = make_state()
        await call(
            handlers[data],
            callback=make_callback(data, make_message()),
            state=state,
            session=None,
        )
        assert edit.call_args.args[0] == texts.MAIN_MENU


# ─────────────────────────── Полный флоу ───────────────────────────


class TestFullFlow:
    async def test_keypad_end_to_end(self, session, edit, cb_answer) -> None:
        """135 → 85 → 72 калькулятором: три шага и сводка (§6.3)."""
        state = make_state()
        await call(
            measure.cb_measure_begin,
            callback=make_callback("menu:measure", make_message()),
            state=state,
        )
        for digit in "135":
            await tap(state, f"m:key:{digit}")
        await tap(state, "m:key:ok")
        for digit in "85":
            await tap(state, f"m:key:{digit}")
        await tap(state, "m:key:ok")
        for digit in "72":
            await tap(state, f"m:key:{digit}")
        await tap(state, "m:key:ok", session=session)
        assert edit.call_args.args[0] == texts.fmt_saved(135, 85, 72)
        assert edit.call_args.kwargs["reply_markup"] == keyboards.saved_kb()
        row = (
            await session.execute(
                select(MeasurementModel).execution_options(populate_existing=True)
            )
        ).scalars().one()
        assert (row.systolic, row.diastolic, row.pulse) == (135, 85, 72)

    async def test_mixed_text_and_keypad(self, session, edit, cb_answer, answer) -> None:
        """Систолическое — сообщением, остальное — калькулятором (§6.3)."""
        state = make_state()
        await call(
            measure.cb_measure_begin,
            callback=make_callback("menu:measure", make_message()),
            state=state,
        )
        await call(
            measure.msg_systolic, message=make_message("135"), state=state, session=None
        )
        for digit in "85":
            await tap(state, f"m:key:{digit}")
        await tap(state, "m:key:ok")
        await tap(state, "m:key:skip", session=session)
        assert edit.call_args.args[0] == texts.fmt_saved(135, 85, None)
        row = (
            await session.execute(
                select(MeasurementModel).execution_options(populate_existing=True)
            )
        ).scalars().one()
        assert row.pulse is None
