"""Тесты хендлеров записи давления (bot/handlers/measure.py, SPEC.md, §6.3).

Хендлеры вызываются напрямую с реальными `Message`/`CallbackQuery` aiogram;
`answer`/`edit_text` замоканы на уровне классов. FSM — реальный MemoryStorage.
Параметры хендлеру передаются по именам (как aiogram DI) — хелпер `call`.
"""
from __future__ import annotations

import inspect
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, User
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, schemas, texts
from bot.handlers import measure
from bot.models import DiaryEvent as DiaryEventModel
from bot.models import Measurement as MeasurementModel
from bot.models import Medication, MedicationLog, MeasurementMedication
from bot.models import User as UserModel
from bot.services import measurements as measurements_service
from bot.services.events import dumps_payload
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


async def start_input(state: FSMContext, target: str = "sys") -> None:
    """Перевести FSM в экран калькулятора с фокусом `target` (как `cb_measure_begin`)."""
    await state.set_state(measure.MeasureInput.active)
    await state.update_data(**{measure.M_BUF: "", measure.M_FIELD: target})


def keypad_text(
    sys_v: int | None = None, dia_v: int | None = None, pulse: int | None = None
) -> str:
    """Текст экрана калькулятора для значений полей (§6.3, мокап V3)."""
    return texts.fmt_keypad_prompt(sys_v, dia_v, pulse)


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
    async def test_begin_shows_calculator(self, edit, cb_answer) -> None:
        state = make_state()
        await call(
            measure.cb_measure_begin,
            callback=make_callback("menu:measure", make_message()),
            state=state,
        )
        assert edit.call_args.args[0] == keypad_text()
        assert edit.call_args.kwargs["reply_markup"] == keyboards.keypad_kb(
            "sys", (None, None, None)
        )
        assert await state.get_state() == measure.MeasureInput.active.state
        assert (await state.get_data())[measure.M_FIELD] == "sys"
        cb_answer.assert_awaited_once()

    async def test_begin_resets_old_draft(self, edit, cb_answer) -> None:
        state = make_state()
        await state.set_data({measure.M_SYS: 100, measure.M_ID: 42})
        await call(
            measure.cb_measure_begin,
            callback=make_callback("menu:measure", make_message()),
            state=state,
        )
        assert await state.get_data() == {measure.M_BUF: "", measure.M_FIELD: "sys"}

    @pytest.mark.parametrize("data", ["rem:write", "rem:now"])
    async def test_begin_from_reminder(self, data: str, edit, cb_answer) -> None:
        """«Записать давление» / «Записать сейчас» из напоминания — тот же сценарий 6.3."""
        state = make_state()
        await call(
            measure.cb_measure_begin,
            callback=make_callback(data, make_message()),
            state=state,
        )
        assert edit.call_args.args[0] == keypad_text()

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
    async def test_digits_accumulate(self, session, edit, cb_answer) -> None:
        state = make_state()
        await start_input(state, "sys")
        await tap(state, "m:key:1", session=session)
        await tap(state, "m:key:3", session=session)
        await tap(state, "m:key:5", session=session)
        assert (await state.get_data())[measure.M_BUF] == "135"
        # активное поле показывает буфер (§6.3)
        assert edit.call_args.args[0] == keypad_text(135, None, None)
        assert edit.call_args.kwargs["reply_markup"] == keyboards.keypad_kb(
            "sys", (135, None, None)
        )

    async def test_leading_zeros_collapse(self, session, edit, cb_answer) -> None:
        state = make_state()
        await start_input(state, "sys")
        await tap(state, "m:key:0", session=session)
        assert (await state.get_data())[measure.M_BUF] == "0"
        await tap(state, "m:key:0", session=session)
        assert (await state.get_data())[measure.M_BUF] == "0"
        await tap(state, "m:key:7", session=session)
        assert (await state.get_data())[measure.M_BUF] == "7"

    async def test_overflow_ignored(self, session, edit, cb_answer) -> None:
        state = make_state()
        await start_input(state, "sys")
        for digit in "2605":
            await tap(state, f"m:key:{digit}", session=session)
        assert (await state.get_data())[measure.M_BUF] == "260"

    async def test_backspace(self, session, edit, cb_answer) -> None:
        state = make_state()
        await start_input(state, "sys")
        await state.update_data(**{measure.M_BUF: "13"})
        await tap(state, "m:key:bksp", session=session)
        assert (await state.get_data())[measure.M_BUF] == "1"
        await tap(state, "m:key:bksp", session=session)
        assert (await state.get_data())[measure.M_BUF] == ""
        await tap(state, "m:key:bksp", session=session)  # пустой буфер не падает
        assert (await state.get_data())[measure.M_BUF] == ""

    async def test_clear(self, session, edit, cb_answer) -> None:
        state = make_state()
        await start_input(state, "sys")
        await state.update_data(**{measure.M_BUF: "135"})
        await tap(state, "m:key:clear", session=session)
        assert (await state.get_data())[measure.M_BUF] == ""
        assert edit.call_args.args[0] == keypad_text(None, None, None)


# ─────────────────────────── Калькулятор: «Готово», фокус, legacy ───────────────────────────


class TestKeypadConfirm:
    async def test_ok_empty_fields(self, session, edit, cb_answer) -> None:
        state = make_state()
        await start_input(state, "sys")
        await tap(state, "m:key:ok", session=session)
        cb_answer.assert_awaited_once_with(texts.KEYPAD_EMPTY_INPUT)
        edit.assert_not_awaited()  # экран не меняется
        assert await state.get_state() == measure.MeasureInput.active.state

    async def test_ok_out_of_range(self, session, edit, cb_answer) -> None:
        state = make_state()
        await start_input(state, "sys")
        await state.update_data(**{measure.M_BUF: "300"})
        await tap(state, "m:key:ok", session=session)
        cb_answer.assert_awaited_once_with(
            "Нужно систолическое от 70 до 260, а получилось 300"
        )
        edit.assert_not_awaited()  # экран не меняется
        assert (await state.get_data())[measure.M_BUF] == "300"

    async def test_ok_commits_buffer_and_saves(self, session, edit, cb_answer) -> None:
        """«Готово» коммитит буфер активного поля и сохраняет запись (C9)."""
        state = make_state()
        await start_input(state, "pulse")
        await state.update_data(
            **{measure.M_SYS: 135, measure.M_DIA: 85, measure.M_BUF: "72"}
        )
        await tap(state, "m:key:ok", session=session)
        assert (await state.get_data())[measure.M_PULSE] == 72
        assert edit.call_args.args[0] == texts.fmt_saved(135, 85, 72)
        assert edit.call_args.kwargs["reply_markup"] == keyboards.saved_kb()
        row = (
            await session.execute(
                select(MeasurementModel).execution_options(populate_existing=True)
            )
        ).scalars().one()
        assert (row.systolic, row.diastolic, row.pulse) == (135, 85, 72)
        assert (await state.get_data())[measure.M_ID] == row.id

    async def test_ok_without_pulse_saves_none(self, session, edit, cb_answer) -> None:
        """Пустой пульс — `pulse = NULL`, без кнопки «Пропустить» (R7 = А, B1)."""
        state = make_state()
        await start_input(state, "pulse")
        await state.update_data(**{measure.M_SYS: 135, measure.M_DIA: 85})
        await tap(state, "m:key:ok", session=session)
        assert edit.call_args.args[0] == texts.fmt_saved(135, 85, None)
        row = (
            await session.execute(
                select(MeasurementModel).execution_options(populate_existing=True)
            )
        ).scalars().one()
        assert row.pulse is None

    async def test_ok_needs_both_values(self, session, edit, cb_answer) -> None:
        """Систолическое без диастолического — «Введите число», сохранения нет."""
        state = make_state()
        await start_input(state, "sys")
        await state.update_data(**{measure.M_BUF: "135"})
        await tap(state, "m:key:ok", session=session)
        cb_answer.assert_awaited_once_with(texts.KEYPAD_EMPTY_INPUT)
        edit.assert_not_awaited()
        rows = (await session.execute(select(MeasurementModel))).scalars().all()
        assert rows == []

    async def test_ok_diastolic_pair_violation(self, session, edit, cb_answer) -> None:
        state = make_state()
        await start_input(state, "dia")
        await state.update_data(**{measure.M_SYS: 120, measure.M_BUF: "120"})
        await tap(state, "m:key:ok", session=session)
        cb_answer.assert_awaited_once_with(texts.DIA_NOT_LOWER)
        edit.assert_not_awaited()  # экран не меняется
        assert (await state.get_data())[measure.M_BUF] == "120"

    async def test_ok_pair_equal_values_violation(self, session, edit, cb_answer) -> None:
        """Диастолическое строго ниже систолического — равные значения тоже ошибка (§9)."""
        state = make_state()
        await start_input(state, "dia")
        await state.update_data(**{measure.M_SYS: 100, measure.M_BUF: "100"})
        await tap(state, "m:key:ok", session=session)
        cb_answer.assert_awaited_once_with(texts.DIA_NOT_LOWER)

    async def test_field_switch_commits_buffer(self, session, edit, cb_answer) -> None:
        """Тап по числу в итоге: буфер прежнего поля коммитится (C9)."""
        state = make_state()
        await start_input(state, "sys")
        await state.update_data(**{measure.M_BUF: "135"})
        await call(
            measure.cb_field,
            callback=make_callback("m:field:dia", make_message()),
            state=state,
            session=session,
        )
        assert (await state.get_data())[measure.M_SYS] == 135
        assert (await state.get_data())[measure.M_FIELD] == "dia"
        assert (await state.get_data())[measure.M_BUF] == ""

    async def test_field_switch_rejects_invalid_buffer(self, session, edit, cb_answer) -> None:
        state = make_state()
        await start_input(state, "sys")
        await state.update_data(**{measure.M_BUF: "300"})
        await call(
            measure.cb_field,
            callback=make_callback("m:field:dia", make_message()),
            state=state,
            session=session,
        )
        cb_answer.assert_awaited_once_with(
            "Нужно систолическое от 70 до 260, а получилось 300"
        )
        assert (await state.get_data())[measure.M_FIELD] == "sys"  # фокус не сменился

    async def test_reset_clears_fields(self, session, edit, cb_answer) -> None:
        """«Изменить» (m:reset) — сброс трёх полей, фокус в систолическое (C8)."""
        state = make_state()
        await start_input(state, "pulse")
        await state.update_data(
            **{measure.M_SYS: 135, measure.M_DIA: 85, measure.M_PULSE: 72}
        )
        await call(
            measure.cb_reset,
            callback=make_callback("m:reset", make_message()),
            state=state,
            session=session,
        )
        data = await state.get_data()
        assert data[measure.M_SYS] is None
        assert data[measure.M_DIA] is None
        assert data[measure.M_PULSE] is None
        assert data[measure.M_FIELD] == "sys"
        assert edit.call_args.args[0] == keypad_text()

    async def test_skip_legacy_goes_to_menu(self, session, edit, cb_answer) -> None:
        """`m:key:skip` — legacy (C14): старые сообщения ведут в меню."""
        state = make_state()
        await start_input(state, "pulse")
        await state.update_data(**{measure.M_SYS: 135, measure.M_DIA: 85})
        await tap(state, "m:key:skip", session=session)
        assert edit.call_args.args[0] == texts.MAIN_MENU

    async def test_unknown_key_goes_to_menu(self, session, edit, cb_answer) -> None:
        state = make_state()
        await start_input(state, "sys")
        await tap(state, "m:key:foo", session=session)
        assert edit.call_args.args[0] == texts.MAIN_MENU

    async def test_key_without_state_goes_to_menu(self, edit, cb_answer) -> None:
        state = make_state()
        await tap(state, "m:key:5", session=None)
        assert edit.call_args.args[0] == texts.MAIN_MENU


# ─────────────────────────── Число сообщением в чат ───────────────────────────


class TestTextInput:
    async def test_pressure_line_fills_fields(self, session, answer) -> None:
        """`120/80 72` — все три поля, фокус — на пульс, сохранения нет (B5, C17)."""
        state = make_state()
        await start_input(state, "sys")
        await call(
            measure.msg_value, message=make_message(" 120/80 72 "), state=state, session=session
        )
        data = await state.get_data()
        assert (data[measure.M_SYS], data[measure.M_DIA], data[measure.M_PULSE]) == (
            120,
            80,
            72,
        )
        assert data[measure.M_FIELD] == "pulse"
        assert answer.call_args.args[0] == keypad_text(120, 80, 72)
        rows = (await session.execute(select(MeasurementModel))).scalars().all()
        assert rows == []  # сохранение — только «Готово»

    @pytest.mark.parametrize(
        "text",
        ["120/80", "120/80/72", "120 / 80", "083/70", "120/80/072"],
    )
    async def test_pressure_line_variants(self, session, answer, text: str) -> None:
        """R4 = Б: лишние пробелы, ведущие нули, `САД/ДАД/пульс` (C12)."""
        state = make_state()
        await start_input(state, "sys")
        await call(
            measure.msg_value, message=make_message(text), state=state, session=session
        )
        data = await state.get_data()
        assert data[measure.M_SYS] is not None
        assert data[measure.M_DIA] is not None
        assert answer.call_args.kwargs["reply_markup"] is not None

    async def test_pressure_line_without_pulse(self, session, answer) -> None:
        state = make_state()
        await start_input(state, "sys")
        await call(
            measure.msg_value, message=make_message("120/80"), state=state, session=session
        )
        assert (await state.get_data())[measure.M_PULSE] is None

    @pytest.mark.parametrize("text", ["120.80", "120,80 72", "12.5", "абырвалг", ""])
    async def test_not_a_pressure_line(self, session, answer, text: str) -> None:
        """Точка/запятая и мусор — «Например: 120/80 72» (I1), экран не меняется."""
        state = make_state()
        await start_input(state, "sys")
        await call(
            measure.msg_value, message=make_message(text), state=state, session=session
        )
        answer.assert_awaited_once_with(schemas.PRESSURE_LINE_HINT)
        assert (await state.get_data()).get(measure.M_SYS) is None

    async def test_pressure_line_pair_violation(self, session, answer) -> None:
        state = make_state()
        await start_input(state, "sys")
        await call(
            measure.msg_value, message=make_message("80/120"), state=state, session=session
        )
        answer.assert_awaited_once_with(texts.DIA_NOT_LOWER)

    async def test_single_number_goes_to_active_field(self, session, answer) -> None:
        """Одиночное число — в активное поле (C7)."""
        state = make_state()
        await start_input(state, "sys")
        await call(
            measure.msg_value, message=make_message(" 120 "), state=state, session=session
        )
        assert (await state.get_data())[measure.M_SYS] == 120
        assert answer.call_args.args[0] == keypad_text(120, None, None)

    async def test_single_number_pair_violation(self, session, answer) -> None:
        state = make_state()
        await start_input(state, "dia")
        await state.update_data(**{measure.M_SYS: 120})
        await call(
            measure.msg_value, message=make_message("125"), state=state, session=session
        )
        answer.assert_awaited_once_with(texts.DIA_NOT_LOWER)

    async def test_single_number_out_of_range(self, session, answer) -> None:
        state = make_state()
        await start_input(state, "pulse")
        await call(
            measure.msg_value, message=make_message("999"), state=state, session=session
        )
        answer.assert_awaited_once_with("Нужно пульс от 30 до 220, а получилось 999")


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
        """Сводка — одна строка (§6.3, R9, 0.6.0), без пульса — без суффикса."""
        state = make_state()
        await state.set_data({measure.M_SYS: 135, measure.M_DIA: 85, measure.M_PULSE: None})
        await measure._save(make_message(), state, session, TG_ID, via_message=True)
        assert answer.call_args.args[0] == "✅ 135/85"

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
        # правка показывает текущие значения записи (B1)
        assert edit.call_args.args[0] == keypad_text(120, 80, 66)
        data = await state.get_data()
        assert data[measure.M_EDIT_ID] == measurement_id
        assert data[measure.M_ID] == measurement_id
        assert await state.get_state() == measure.MeasureInput.active.state

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
        # черновик перезаписан текущими значениями записи (B1)
        assert (data[measure.M_SYS], data[measure.M_DIA], data[measure.M_PULSE]) == (
            120,
            80,
            66,
        )
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
        await state.set_state(measure.MeasureInput.active)
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
    async def test_med_no_meds_shows_prompt_once(self, session, edit, cb_answer) -> None:
        """K2/M1: без лекарств — подсказка один раз, затем «Сначала добавьте…»."""
        measurement_id = await save_draft(session, make_state())
        state = make_state()
        await state.set_data({measure.M_ID: measurement_id})
        await call(
            measure.cb_med,
            callback=make_callback("m:med", make_message()),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.MEDS_ASK_PROMPT
        assert edit.call_args.kwargs["reply_markup"] == keyboards.meds_ask_kb()
        user = await reload_user(session)
        assert user.meds_ask_shown_at is not None  # маркер «первого раза» (M1)
        await call(
            measure.cb_med,
            callback=make_callback("m:med", make_message()),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.MEDS_NONE_HINT
        assert edit.call_args.kwargs["reply_markup"] == keyboards.meds_none_kb()

    async def test_med_shows_multiselect(self, session, edit, cb_answer) -> None:
        """Лекарства есть, привязок к записи нет — мультивыбор (B2 = А)."""
        measurement_id = await save_draft(session, make_state())
        await add_med(session, name="Лозартан", dosage="50 мг", slot="morning")
        state = make_state()
        await state.set_data({measure.M_ID: measurement_id})
        await call(
            measure.cb_med,
            callback=make_callback("m:med", make_message()),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.MEDS_INTAKE_ASK

    async def test_med_linked_shows_readonly_with_more(self, session, edit, cb_answer, reminders) -> None:
        """Привязка к записи есть — «💊 Принято: …» + «➕ Добавить ещё» (B2/C6)."""
        med_id = await add_med(session, name="Лозартан", dosage="50 мг", slot="morning")
        measurement_id = await save_draft(session, make_state())
        state = make_state()
        await state.set_data({measure.M_ID: measurement_id, measure.M_MED_CHECKED: [med_id]})
        await call(
            measure.cb_med_done,
            callback=make_callback("m:med:done", make_message()),
            state=state,
            session=session,
            reminders=reminders,
        )
        await call(
            measure.cb_med,
            callback=make_callback("m:med", make_message()),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.fmt_meds_intake(["Лозартан 50 мг"])
        buttons = {
            button.callback_data: button.text
            for row in edit.call_args.kwargs["reply_markup"].inline_keyboard
            for button in row
        }
        assert buttons["m:med:more"] == texts.BTN_ADD_MORE
        # «Добавить ещё» открывает мультивыбор (C6)
        await call(
            measure.cb_med_more,
            callback=make_callback("m:med:more", make_message()),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.MEDS_INTAKE_ASK

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

    async def test_meds_ask_dismiss(self, session, edit, cb_answer) -> None:
        """«Не сейчас» — «Хорошо…» и возврат к сводке записи (§6.3)."""
        measurement_id = await save_draft(session, make_state())
        state = make_state()
        await state.set_data({measure.M_ID: measurement_id})
        await call(
            measure.cb_meds_ask_dismiss,
            callback=make_callback("m:medask:dismiss", make_message()),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.MEDS_ASK_DISMISSED
        assert edit.call_args.kwargs["reply_markup"] == keyboards.saved_kb()


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

    async def test_opens_empty_with_taken_today_marker(
        self, session, edit, cb_answer, answer, reminders
    ) -> None:
        """Выбор пустой; у принятого сегодня — «· сегодня ✓» без галочки (§6.3, B1 А)."""
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
        markup = edit.call_args.kwargs["reply_markup"]
        assert markup == keyboards.intake_meds_kb(set(), meds, {med_id})
        label = markup.inline_keyboard[0][0].text
        assert label == f"{texts.fmt_intake_item('Лозартан', '50 мг', 'morning')} · сегодня ✓"
        data = await state.get_data()
        assert data[measure.M_MED_CHECKED] == []
        assert data[measure.M_MED_TAKEN_TODAY] == [med_id]

    async def test_skipped_today_not_marked(
        self, session, edit, cb_answer, answer, reminders
    ) -> None:
        """«Пропускаю» по напоминанию — не «принято»: пометки нет (B2 А)."""
        med_id = await add_med(session)
        user = await reload_user(session)
        await medications_service.log_slot_intake(session, user, "morning", {med_id: "skipped"})
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
        assert edit.call_args.kwargs["reply_markup"] == keyboards.intake_meds_kb(set(), meds)
        assert (await state.get_data())[measure.M_MED_TAKEN_TODAY] == []

    async def test_pick_keeps_taken_today_marker(self, session, edit, cb_answer) -> None:
        """Перерисовка после нажатия сохраняет пометку «· сегодня ✓»."""
        taken_id = await add_med(session, name="Лозартан")
        other_id = await add_med(session, name="Амлодипин", slot="evening")
        await save_draft(session, make_state())
        state = make_state()
        await state.set_data(
            {measure.M_ID: 1, measure.M_MED_CHECKED: [], measure.M_MED_TAKEN_TODAY: [taken_id]}
        )
        await call(
            measure.cb_med_pick,
            callback=make_callback(f"m:med:pick:{other_id}", make_message()),
            state=state,
            session=session,
        )
        meds = await medications_service.list_medications(session, await reload_user(session))
        assert edit.call_args.kwargs["reply_markup"] == keyboards.intake_meds_kb(
            {other_id}, meds, {taken_id}
        )

    async def test_done_links_only_picked_now(
        self, session, edit, cb_answer, answer, reminders
    ) -> None:
        """Принятое утром не привязывается к новой записи без нажатия (жалоба, B1 А)."""
        morning_id = await add_med(session, name="Канверс плюс", dosage="16/12,5")
        evening_id = await add_med(session, name="ЛерканНАН", dosage="10 мг", slot="evening")
        user = await reload_user(session)
        await medications_service.log_manual_intake(session, user, {morning_id})
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
        await call(
            measure.cb_med_pick,
            callback=make_callback(f"m:med:pick:{evening_id}", make_message()),
            state=state,
            session=session,
        )
        await call(
            measure.cb_med_done,
            callback=make_callback("m:med:done", make_message()),
            state=state,
            session=session,
            reminders=reminders,
        )
        links = (
            await session.execute(
                select(MeasurementMedication).execution_options(populate_existing=True)
            )
        ).scalars().all()
        assert [(link.measurement_id, link.medication_id) for link in links] == [
            (measurement_id, evening_id)
        ]

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

    async def test_pick_without_draft_goes_to_menu(self, session, edit, cb_answer) -> None:
        state = make_state()
        await call(
            measure.cb_med_pick,
            callback=make_callback("m:med:pick:1", make_message()),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.MAIN_MENU

    async def test_pick_bad_value_goes_to_menu(self, session, edit, cb_answer) -> None:
        state = make_state()
        await state.set_data({measure.M_ID: 1})
        await call(
            measure.cb_med_pick,
            callback=make_callback("m:med:pick:abc", make_message()),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.MAIN_MENU

    async def test_done_empty_selection(self, session, edit, cb_answer, reminders) -> None:
        await save_draft(session, make_state())
        state = make_state()
        await state.set_data({measure.M_ID: 1, measure.M_MED_CHECKED: []})
        await call(
            measure.cb_med_done,
            callback=make_callback("m:med:done", make_message()),
            state=state,
            session=session,
            reminders=reminders,
        )
        cb_answer.assert_awaited_once_with(texts.MEDS_INTAKE_EMPTY)
        edit.assert_not_awaited()  # экран не меняется

    async def test_done_logs_taken(self, session, edit, cb_answer, reminders) -> None:
        med_id = await add_med(session, name="Лозартан", dosage="50 мг", slot="morning")
        measurement_id = await save_draft(session, make_state())
        state = make_state()
        await state.set_data({measure.M_ID: measurement_id, measure.M_MED_CHECKED: [med_id]})
        await call(
            measure.cb_med_done,
            callback=make_callback("m:med:done", make_message()),
            state=state,
            session=session,
            reminders=reminders,
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
        # Отмеченный приём связан с записью — перечень для истории (§4.11).
        links = (
            await session.execute(
                select(MeasurementMedication).execution_options(populate_existing=True)
            )
        ).scalars().all()
        assert [(link.measurement_id, link.medication_id) for link in links] == [
            (measurement_id, med_id)
        ]

    async def test_done_twice_logs_once(self, session, edit, cb_answer, reminders) -> None:
        """Двойное «Готово» = один набор логов и одна связь (§6.3, §7.2)."""
        med_id = await add_med(session)
        measurement_id = await save_draft(session, make_state())
        state = make_state()
        await state.set_data({measure.M_ID: measurement_id, measure.M_MED_CHECKED: [med_id]})
        await call(
            measure.cb_med_done,
            callback=make_callback("m:med:done", make_message()),
            state=state,
            session=session,
            reminders=reminders,
        )
        await state.update_data(**{measure.M_MED_CHECKED: [med_id]})
        await call(
            measure.cb_med_done,
            callback=make_callback("m:med:done", make_message()),
            state=state,
            session=session,
            reminders=reminders,
        )
        logs = (
            await session.execute(
                select(MedicationLog).execution_options(populate_existing=True)
            )
        ).scalars().all()
        assert len(logs) == 1
        assert edit.call_args.args[0] == texts.fmt_meds_intake(["Лозартан 50 мг"])
        links = (
            await session.execute(
                select(MeasurementMedication).execution_options(populate_existing=True)
            )
        ).scalars().all()
        assert len(links) == 1  # связь с записью дублей не создаёт (§4.11)

    async def test_done_all_foreign_ids(self, session, edit, cb_answer, reminders) -> None:
        await save_draft(session, make_state())
        state = make_state()
        await state.set_data({measure.M_ID: 1, measure.M_MED_CHECKED: [9999]})
        await call(
            measure.cb_med_done,
            callback=make_callback("m:med:done", make_message()),
            state=state,
            session=session,
            reminders=reminders,
        )
        cb_answer.assert_awaited_once_with(texts.MEDS_INTAKE_EMPTY)
        logs = (await session.execute(select(MedicationLog))).scalars().all()
        assert logs == []
        links = (await session.execute(select(MeasurementMedication))).scalars().all()
        assert links == []

    async def test_done_without_draft_goes_to_menu(self, session, edit, cb_answer, reminders) -> None:
        state = make_state()
        await call(
            measure.cb_med_done,
            callback=make_callback("m:med:done", make_message()),
            state=state,
            session=session,
            reminders=reminders,
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
            session=session,
        )
        assert edit.call_args.args[0] == texts.fmt_saved(135, 85, None)
        assert edit.call_args.kwargs["reply_markup"] == keyboards.saved_kb()
        logs = (await session.execute(select(MedicationLog))).scalars().all()
        assert logs == []

    async def test_cancel_without_draft_goes_to_menu(self, session, edit, cb_answer) -> None:
        state = make_state()
        await call(
            measure.cb_med_cancel,
            callback=make_callback("m:med:cancel", make_message()),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.MAIN_MENU


# ─────────────────────────── Самочувствие ───────────────────────────


class TestFactor:
    """«➕ Фактор» (§6.3, 0.6.0): мультивыбор меток и события с привязкой (B6)."""

    @staticmethod
    def _buttons(markup) -> dict[str, str]:
        return {
            button.callback_data: button.text
            for row in markup.inline_keyboard
            for button in row
        }

    async def test_fact_opens_multiselect(self, session, edit, cb_answer) -> None:
        state = make_state()
        await save_draft(session, state)
        await call(
            measure.cb_fact,
            callback=make_callback("m:fact", make_message()),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.FACTOR_ASK
        assert set(self._buttons(edit.call_args.kwargs["reply_markup"])) == {
            "m:fact:pick:salt",
            "m:fact:pick:coffee",
            "m:fact:pick:alcohol",
            "m:fact:pick:stress",
            "m:fact:pick:sleep",
            "m:fact:done",
            "m:fact:cancel",
        }

    async def test_pick_toggles_check(self, session, edit, cb_answer) -> None:
        state = make_state()
        await save_draft(session, state)
        await call(
            measure.cb_fact,
            callback=make_callback("m:fact", make_message()),
            state=state,
            session=session,
        )
        await call(
            measure.cb_fact_pick,
            callback=make_callback("m:fact:pick:salt", make_message()),
            state=state,
            session=session,
        )
        assert (await state.get_data())[measure.M_FACT_CHECKED] == ["salt"]
        assert self._buttons(edit.call_args.kwargs["reply_markup"])["m:fact:pick:salt"].startswith("✓ ")
        await call(
            measure.cb_fact_pick,
            callback=make_callback("m:fact:pick:salt", make_message()),
            state=state,
            session=session,
        )
        assert (await state.get_data())[measure.M_FACT_CHECKED] == []

    async def test_done_creates_grouped_events(self, session, edit, cb_answer) -> None:
        """Метки одной группы — одно событие, разные — по событию на тип (B6)."""
        state = make_state()
        measurement_id = await save_draft(session, state)
        await state.update_data(**{measure.M_FACT_CHECKED: ["salt", "coffee", "stress"]})
        await call(
            measure.cb_fact_done,
            callback=make_callback("m:fact:done", make_message()),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.FACTOR_DONE
        assert edit.call_args.kwargs["reply_markup"] == keyboards.saved_kb()
        rows = (
            await session.execute(
                select(DiaryEventModel).order_by(DiaryEventModel.type)
            )
        ).scalars().all()
        assert [(row.type, row.measurement_id) for row in rows] == [
            ("food", measurement_id),
            ("stress", measurement_id),
        ]
        assert rows[0].payload == dumps_payload({"marks": ["coffee", "salt"]})

    async def test_done_idempotent_double_tap(self, session, edit, cb_answer) -> None:
        state = make_state()
        await save_draft(session, state)
        await state.update_data(**{measure.M_FACT_CHECKED: ["sleep"]})
        for _ in range(2):
            await call(
                measure.cb_fact_done,
                callback=make_callback("m:fact:done", make_message()),
                state=state,
                session=session,
            )
        rows = (await session.execute(select(DiaryEventModel))).scalars().all()
        assert len(rows) == 1  # повторное «Готово» не создаёт дублей (M4)

    async def test_done_empty_marks_toast(self, session, edit, cb_answer) -> None:
        state = make_state()
        await save_draft(session, state)
        await call(
            measure.cb_fact_done,
            callback=make_callback("m:fact:done", make_message()),
            state=state,
            session=session,
        )
        cb_answer.assert_awaited_once_with(texts.FACTOR_EMPTY)
        edit.assert_not_awaited()
        rows = (await session.execute(select(DiaryEventModel))).scalars().all()
        assert rows == []

    async def test_cancel_returns_to_summary(self, session, edit, cb_answer) -> None:
        state = make_state()
        await save_draft(session, state)
        await state.update_data(**{measure.M_FACT_CHECKED: ["salt"]})
        await call(
            measure.cb_fact_cancel,
            callback=make_callback("m:fact:cancel", make_message()),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.fmt_saved(135, 85, 72)
        rows = (await session.execute(select(DiaryEventModel))).scalars().all()
        assert rows == []

    async def test_pick_unknown_mark_goes_to_menu(self, session, edit, cb_answer) -> None:
        state = make_state()
        await save_draft(session, state)
        await call(
            measure.cb_fact_pick,
            callback=make_callback("m:fact:pick:sugar", make_message()),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.MAIN_MENU

    async def test_archive_draft_shows_archive_summary(self, session, edit, cb_answer) -> None:
        """У архивного черновика устаревшая кнопка фактора показывает сводку архива."""
        state = make_state()
        await save_draft(session, state)
        await state.update_data(
            **{measure.M_ARCH_TS: "2024-03-12T05:30:00+00:00", measure.M_ARCH_LABEL: "12.03.2024, 08:00"}
        )
        await call(
            measure.cb_fact,
            callback=make_callback("m:fact", make_message()),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.fmt_archive_saved("12.03.2024, 08:00", 135, 85, 72)


class TestFeeling:
    async def test_feeling_screen(self, session, edit, cb_answer) -> None:
        await save_draft(session, make_state())
        state = make_state()
        await state.set_data({measure.M_ID: 1})
        await call(
            measure.cb_feeling,
            callback=make_callback("m:feeling", make_message()),
            state=state,
            session=session,
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


class TestMedsAddContext:
    """M3: «Добавить» из «💊 Лекарство» — после сохранения возврат к сводке записи."""

    async def test_add_keeps_context_and_returns_to_summary(
        self, session, edit, cb_answer
    ) -> None:
        from bot.handlers import settings as settings_handlers

        measurement_id = await save_draft(session, make_state())
        state = make_state()
        await state.set_data({measure.M_ID: measurement_id})
        await call(
            settings_handlers.cb_med_add,
            callback=make_callback("med:add", make_message()),
            state=state,
        )
        assert (await state.get_data())["med_ctx_measurement_id"] == measurement_id
        # финал добавления: черновик лекарства + флаг контекста
        await state.update_data(
            **{
                settings_handlers.M_NAME: "Лозартан",
                settings_handlers.M_DOSAGE: "50 мг",
                settings_handlers.M_SLOTS: ["morning"],
                "med_ctx_measurement_id": measurement_id,
            }
        )
        await call(
            settings_handlers.cb_med_slots_done,
            callback=make_callback("med:slots:done", make_message()),
            state=state,
            session=session,
        )
        # сводка записи восстановлена (кнопки сводки снова работают)
        assert edit.call_args.args[0] == texts.fmt_saved(135, 85, 72)
        assert edit.call_args.kwargs["reply_markup"] == keyboards.saved_kb()
        data = await state.get_data()
        assert data[measure.M_ID] == measurement_id

    async def test_add_without_context_shows_meds_list(
        self, session, edit, cb_answer
    ) -> None:
        """Обычное добавление из «Моих лекарств» — экран списка (§6.6)."""
        from bot.handlers import settings as settings_handlers

        state = make_state()
        await state.set_state(settings_handlers.SettingsInput.med_name)
        await state.update_data(
            **{
                settings_handlers.M_NAME: "Амлодипин",
                settings_handlers.M_DOSAGE: "5 мг",
                settings_handlers.M_SLOTS: ["evening"],
            }
        )
        await call(
            settings_handlers.cb_med_slots_done,
            callback=make_callback("med:slots:done", make_message()),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.MEDS_ADD_DONE


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
    async def test_without_draft_shows_menu(self, data, session, edit, cb_answer, reminders) -> None:
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
            session=session,
            reminders=reminders,
        )
        assert edit.call_args.args[0] == texts.MAIN_MENU


class TestStaleRegularButtonsOnArchive:
    """Кнопки обычной сводки при архивном черновике (§6.10).

    Старое сообщение обычной сводки остаётся в чате; после архивного
    сохранения `M_ID` указывает на архивную запись — «Да» не должна
    планировать повторный замер и писать `medication_logs`.
    """

    async def _archive_state(self, session) -> tuple[FSMContext, int]:
        user = await measurements_service.get_or_create_user(session, TG_ID)
        user.med_remeasure_delay = 60
        measurement = await measurements_service.save_measurement(
            session, user, 135, 85, 72,
            timestamp=datetime(2024, 3, 12, 5, 30, tzinfo=timezone.utc),
        )
        await session.commit()
        state = make_state()
        await archive_draft(state, "2024-03-12T05:30:00+00:00", **{measure.M_ID: measurement.id})
        return state, measurement.id

    @pytest.mark.parametrize(
        ("data", "handler"),
        [
            ("m:med", "cb_med"),
            ("m:med:yes", "cb_med_yes"),
            ("m:med:pick:1", "cb_med_pick"),
            ("m:med:done", "cb_med_done"),
        ],
    )
    async def test_shows_archive_summary(
        self, data, handler, session, edit, cb_answer, answer, reminders
    ) -> None:
        state, measurement_id = await self._archive_state(session)
        med_id = await add_med(session)
        await state.update_data(**{measure.M_MED_CHECKED: [med_id]})
        await call(
            getattr(measure, handler),
            callback=make_callback(data, make_message()),
            state=state,
            session=session,
            reminders=reminders,
        )
        reminders.ensure_remeasure.assert_not_awaited()
        logs = (
            await session.execute(
                select(MedicationLog).execution_options(populate_existing=True)
            )
        ).scalars().all()
        assert logs == []
        row = await reload_measurement(session, measurement_id)
        assert row.medication_taken is None
        assert edit.call_args.args[0] == texts.fmt_archive_saved(
            "12.03.2025, 08:00", 135, 85, 72
        )
        assert edit.call_args.kwargs["reply_markup"] == keyboards.archive_saved_kb()
        cb_answer.assert_awaited_once()


# ─────────────────────────── Полный флоу ───────────────────────────


class TestFullFlow:
    async def test_keypad_end_to_end(self, session, edit, cb_answer) -> None:
        """135 → 85 → 72 калькулятором: фокус полей и сводка (§6.3)."""
        state = make_state()
        await call(
            measure.cb_measure_begin,
            callback=make_callback("menu:measure", make_message()),
            state=state,
        )
        for digit in "135":
            await tap(state, f"m:key:{digit}", session=session)
        await call(  # фокус → ДАД, буфер систолического коммитится (C9)
            measure.cb_field,
            callback=make_callback("m:field:dia", make_message()),
            state=state,
            session=session,
        )
        for digit in "85":
            await tap(state, f"m:key:{digit}", session=session)
        await call(
            measure.cb_field,
            callback=make_callback("m:field:pulse", make_message()),
            state=state,
            session=session,
        )
        for digit in "72":
            await tap(state, f"m:key:{digit}", session=session)
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
        """Быстрая строка `135/85` + пульс калькулятором (§6.3, B5, C17)."""
        state = make_state()
        await call(
            measure.cb_measure_begin,
            callback=make_callback("menu:measure", make_message()),
            state=state,
        )
        await call(
            measure.msg_value, message=make_message("135/85"), state=state, session=session
        )
        data = await state.get_data()
        assert data[measure.M_FIELD] == "pulse"  # фокус — на пульсе после строки
        for digit in "72":
            await tap(state, f"m:key:{digit}", session=session)
        await tap(state, "m:key:ok", session=session)
        assert edit.call_args.args[0] == texts.fmt_saved(135, 85, 72)
        row = (
            await session.execute(
                select(MeasurementModel).execution_options(populate_existing=True)
            )
        ).scalars().one()
        assert row.pulse == 72


# ─────────────────── Архивные записи (SPEC.md, §6.10) ───────────────────


def today_minsk() -> date:
    """Сегодняшняя дата в таймзоне пользователя теста (Europe/Minsk)."""
    return datetime.now(ZoneInfo("Europe/Minsk")).date()


async def archive_draft(state: FSMContext, moment_iso: str, **extra) -> None:
    """Черновик архива: момент + значения (то же, что делает `_archive_set_moment` + калькулятор)."""
    await state.update_data(
        **{
            measure.M_SYS: 135,
            measure.M_DIA: 85,
            measure.M_PULSE: 72,
            measure.M_ARCH_TS: moment_iso,
            measure.M_ARCH_LABEL: "12.03.2025, 08:00",
            measure.M_BUF: "72",
            **extra,
        }
    )


class TestArchiveEntry:
    """Шаги «Когда?» и «Во сколько?» (§6.10)."""

    async def test_add_opens_date_step(self, edit, cb_answer) -> None:
        state = make_state()
        await call(
            measure.cb_archive_begin,
            callback=make_callback("set:archive:add", make_message()),
            state=state,
        )
        assert edit.call_args.args[0] == texts.ARCHIVE_DATE_ASK
        assert edit.call_args.kwargs["reply_markup"] == keyboards.archive_date_kb()
        assert await state.get_state() == measure.ArchiveInput.date.state

    async def test_more_opens_date_step_too(self, edit, cb_answer) -> None:
        state = make_state()
        await call(
            measure.cb_archive_begin,
            callback=make_callback("m:arch:more", make_message()),
            state=state,
        )
        assert edit.call_args.args[0] == texts.ARCHIVE_DATE_ASK
        assert await state.get_state() == measure.ArchiveInput.date.state

    async def test_date_preset_yesterday(self, session, edit, cb_answer) -> None:
        state = make_state()
        await call(
            measure.cb_archive_date,
            callback=make_callback("set:archive:date:yesterday", make_message()),
            state=state,
            session=session,
        )
        data = await state.get_data()
        expected = (today_minsk() - timedelta(days=1)).isoformat()
        assert data[measure.A_DATE] == expected
        assert await state.get_state() == measure.ArchiveInput.time.state
        assert edit.call_args.args[0] == texts.ARCHIVE_TIME_ASK
        assert edit.call_args.kwargs["reply_markup"] == keyboards.archive_time_kb()

    @pytest.mark.parametrize(
        ("action", "days"),
        [("set:archive:date:today", 0), ("set:archive:date:3d", 3), ("set:archive:date:week", 7)],
    )
    async def test_date_presets_offsets(
        self, session, edit, cb_answer, action: str, days: int
    ) -> None:
        state = make_state()
        await call(
            measure.cb_archive_date,
            callback=make_callback(action, make_message()),
            state=state,
            session=session,
        )
        expected = (today_minsk() - timedelta(days=days)).isoformat()
        assert (await state.get_data())[measure.A_DATE] == expected

    async def test_date_other_prompts_free_input(self, session, edit, cb_answer) -> None:
        state = make_state()
        await call(
            measure.cb_archive_date,
            callback=make_callback("set:archive:date:other", make_message()),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.ARCHIVE_DATE_OTHER_PROMPT
        assert await state.get_state() == measure.ArchiveInput.date.state

    async def test_date_stale_action_ignored(self, session, edit, cb_answer) -> None:
        state = make_state()
        await call(
            measure.cb_archive_date,
            callback=make_callback("set:archive:date:stale", make_message()),
            state=state,
            session=session,
        )
        edit.assert_not_called()
        cb_answer.assert_awaited_once()

    async def test_msg_date_valid(self, session, answer) -> None:
        state = make_state()
        await state.set_state(measure.ArchiveInput.date)
        await call(
            measure.msg_archive_date,
            message=make_message("12.03.2024"),
            state=state,
            session=session,
        )
        assert (await state.get_data())[measure.A_DATE] == "2024-03-12"
        assert await state.get_state() == measure.ArchiveInput.time.state
        assert answer.call_args.args[0] == texts.ARCHIVE_TIME_ASK

    async def test_msg_date_short_year(self, session, answer) -> None:
        state = make_state()
        await state.set_state(measure.ArchiveInput.date)
        await call(
            measure.msg_archive_date,
            message=make_message("01.02"),
            state=state,
            session=session,
        )
        year = today_minsk().year
        assert (await state.get_data())[measure.A_DATE] == f"{year}-02-01"

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("абырвалг", texts.ARCHIVE_DATE_FORMAT_HINT),
            ("31.02.2025", texts.ARCHIVE_DATE_FORMAT_HINT),
            ("01.01.2999", texts.ARCHIVE_FUTURE_ERROR),
        ],
    )
    async def test_msg_date_invalid(self, session, answer, text: str, expected: str) -> None:
        state = make_state()
        await state.set_state(measure.ArchiveInput.date)
        await call(
            measure.msg_archive_date,
            message=make_message(text),
            state=state,
            session=session,
        )
        answer.assert_awaited_once_with(expected, reply_markup=keyboards.note_cancel_kb())
        assert await state.get_state() == measure.ArchiveInput.date.state

    async def test_time_other_prompts_free_input(self, session, edit, cb_answer) -> None:
        state = make_state()
        await call(
            measure.cb_archive_time,
            callback=make_callback("set:archive:time:other", make_message()),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.ARCHIVE_TIME_OTHER_PROMPT
        assert await state.get_state() == measure.ArchiveInput.time.state

    async def test_time_preset_opens_calculator(self, session, edit, cb_answer) -> None:
        state = make_state()
        await state.update_data(**{measure.A_DATE: "2024-03-12"})
        await call(
            measure.cb_archive_time,
            callback=make_callback("set:archive:time:08:00", make_message()),
            state=state,
            session=session,
        )
        data = await state.get_data()
        assert data[measure.M_ARCH_LABEL] == "12.03.2024, 08:00"
        assert await state.get_state() == measure.MeasureInput.active.state
        assert data[measure.M_BUF] == ""
        assert edit.call_args.kwargs["reply_markup"] == keyboards.keypad_kb(
            "sys", (None, None, None)
        )

    async def test_time_preset_future_is_toast(self, session, cb_answer) -> None:
        state = make_state()
        tomorrow = (today_minsk() + timedelta(days=1)).isoformat()
        await state.update_data(**{measure.A_DATE: tomorrow})
        await call(
            measure.cb_archive_time,
            callback=make_callback("set:archive:time:08:00", make_message()),
            state=state,
            session=session,
        )
        cb_answer.assert_awaited_once_with(texts.ARCHIVE_FUTURE_ERROR)
        assert await state.get_state() != measure.MeasureInput.active.state

    async def test_time_preset_bad_format_is_toast(self, session, cb_answer) -> None:
        state = make_state()
        await state.update_data(**{measure.A_DATE: "2024-03-12"})
        await call(
            measure.cb_archive_time,
            callback=make_callback("set:archive:time:25:99", make_message()),
            state=state,
            session=session,
        )
        cb_answer.assert_awaited_once_with(schemas.TIME_FORMAT_HINT)

    async def test_time_preset_without_date_goes_to_menu(self, session, edit, cb_answer) -> None:
        state = make_state()
        await call(
            measure.cb_archive_time,
            callback=make_callback("set:archive:time:08:00", make_message()),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.MAIN_MENU

    async def test_msg_time_valid(self, session, answer) -> None:
        state = make_state()
        await state.set_state(measure.ArchiveInput.time)
        await state.update_data(**{measure.A_DATE: "2024-03-12"})
        await call(
            measure.msg_archive_time,
            message=make_message("23:30"),
            state=state,
            session=session,
        )
        data = await state.get_data()
        assert data[measure.M_ARCH_LABEL] == "12.03.2024, 23:30"
        assert await state.get_state() == measure.MeasureInput.active.state

    @pytest.mark.parametrize("text", ["25:00", "абырвалг", ""])
    async def test_msg_time_invalid(self, session, answer, text: str) -> None:
        state = make_state()
        await state.set_state(measure.ArchiveInput.time)
        await state.update_data(**{measure.A_DATE: "2024-03-12"})
        await call(
            measure.msg_archive_time,
            message=make_message(text),
            state=state,
            session=session,
        )
        answer.assert_awaited_once_with(
            schemas.TIME_FORMAT_HINT, reply_markup=keyboards.note_cancel_kb()
        )
        assert await state.get_state() == measure.ArchiveInput.time.state

    async def test_msg_time_future(self, session, answer) -> None:
        state = make_state()
        await state.set_state(measure.ArchiveInput.time)
        tomorrow = (today_minsk() + timedelta(days=1)).isoformat()
        await state.update_data(**{measure.A_DATE: tomorrow})
        await call(
            measure.msg_archive_time,
            message=make_message("08:00"),
            state=state,
            session=session,
        )
        answer.assert_awaited_once_with(
            texts.ARCHIVE_FUTURE_ERROR, reply_markup=keyboards.note_cancel_kb()
        )


class TestArchiveQuick:
    """Быстрый ввод одной строкой (§6.10)."""

    async def test_quick_prompts(self, edit, cb_answer) -> None:
        state = make_state()
        await call(
            measure.cb_archive_quick,
            callback=make_callback("set:archive:quick", make_message()),
            state=state,
        )
        assert edit.call_args.args[0] == texts.ARCHIVE_QUICK_PROMPT
        assert await state.get_state() == measure.ArchiveInput.quick.state

    async def test_quick_saves_with_given_moment(self, session, answer) -> None:
        state = make_state()
        await state.set_state(measure.ArchiveInput.quick)
        await call(
            measure.msg_archive_quick,
            message=make_message("12.03.2024 08:30 135/85 72"),
            state=state,
            session=session,
        )
        expected_text = texts.fmt_archive_saved("12.03.2024, 08:30", 135, 85, 72)
        assert answer.call_args.args[0] == expected_text
        assert answer.call_args.kwargs["reply_markup"] == keyboards.archive_saved_kb()
        row = (
            await session.execute(
                select(MeasurementModel).execution_options(populate_existing=True)
            )
        ).scalars().one()
        # 08:30 Europe/Minsk (UTC+3) = 05:30 UTC
        assert row.timestamp == datetime(2024, 3, 12, 5, 30, tzinfo=timezone.utc)
        assert (row.systolic, row.diastolic, row.pulse) == (135, 85, 72)
        data = await state.get_data()
        assert data[measure.M_ID] == row.id
        assert data[measure.M_ARCH_LABEL] == "12.03.2024, 08:30"

    async def test_quick_no_pulse(self, session, answer) -> None:
        state = make_state()
        await state.set_state(measure.ArchiveInput.quick)
        await call(
            measure.msg_archive_quick,
            message=make_message("12.03.2024 08:30 135/85"),
            state=state,
            session=session,
        )
        row = (
            await session.execute(select(MeasurementModel).execution_options(populate_existing=True))
        ).scalars().one()
        assert row.pulse is None

    async def test_quick_single_digit_time_accepted(self, session, answer) -> None:
        """«8:30» — то же, что «08:30» (минимум трения, §2)."""
        state = make_state()
        await state.set_state(measure.ArchiveInput.quick)
        await call(
            measure.msg_archive_quick,
            message=make_message("12.03.2024 8:30 135/85 72"),
            state=state,
            session=session,
        )
        row = (
            await session.execute(select(MeasurementModel).execution_options(populate_existing=True))
        ).scalars().one()
        # 08:30 Europe/Minsk (UTC+3) = 05:30 UTC
        assert row.timestamp == datetime(2024, 3, 12, 5, 30, tzinfo=timezone.utc)
        assert (row.systolic, row.diastolic, row.pulse) == (135, 85, 72)

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("абырвалг", texts.ARCHIVE_QUICK_FORMAT_HINT),
            ("12.03.2024 08:30", texts.ARCHIVE_QUICK_FORMAT_HINT),
            ("01.01.2999 08:30 135/85", texts.ARCHIVE_FUTURE_ERROR),
        ],
    )
    async def test_quick_invalid(self, session, answer, text: str, expected: str) -> None:
        state = make_state()
        await state.set_state(measure.ArchiveInput.quick)
        await call(
            measure.msg_archive_quick, message=make_message(text), state=state, session=session
        )
        answer.assert_awaited_once_with(expected, reply_markup=keyboards.note_cancel_kb())
        rows = (
            await session.execute(select(MeasurementModel).execution_options(populate_existing=True))
        ).scalars().all()
        assert rows == []
        assert await state.get_state() == measure.ArchiveInput.quick.state

    async def test_quick_duplicate_rejected(self, session, answer) -> None:
        state = make_state()
        await state.set_state(measure.ArchiveInput.quick)
        for _ in range(2):
            await call(
                measure.msg_archive_quick,
                message=make_message("12.03.2024 08:30 135/85 72"),
                state=state,
                session=session,
            )
        rows = (
            await session.execute(select(MeasurementModel).execution_options(populate_existing=True))
        ).scalars().all()
        assert len(rows) == 1
        assert answer.call_args.args[0] == texts.DUPLICATE_MEASUREMENT


class TestArchiveSave:
    """Сохранение архива через калькулятор: сводка и ограничения (§6.10)."""

    async def test_saved_summary_is_archive(self, session, edit, cb_answer) -> None:
        state = make_state()
        await start_input(state, "pulse")
        await archive_draft(state, "2024-03-12T05:30:00+00:00")
        await tap(state, "m:key:ok", session=session)
        assert edit.call_args.args[0] == texts.fmt_archive_saved(
            "12.03.2025, 08:00", 135, 85, 72
        )
        assert edit.call_args.kwargs["reply_markup"] == keyboards.archive_saved_kb()

    async def test_saved_uses_given_timestamp(self, session, edit, cb_answer) -> None:
        state = make_state()
        await start_input(state, "pulse")
        await archive_draft(state, "2024-03-12T05:30:00+00:00", **{measure.M_PULSE: None})
        await tap(state, "m:key:ok", session=session)
        row = (
            await session.execute(select(MeasurementModel).execution_options(populate_existing=True))
        ).scalars().one()
        assert row.timestamp == datetime(2024, 3, 12, 5, 30, tzinfo=timezone.utc)

    async def test_no_high_pressure_warning_in_archive(self, session, edit, cb_answer) -> None:
        state = make_state()
        await start_input(state, "pulse")
        await archive_draft(
            state, "2024-03-12T05:30:00+00:00", **{measure.M_SYS: 190, measure.M_DIA: 120}
        )
        await tap(state, "m:key:ok", session=session)
        assert texts.HIGH_PRESSURE_WARNING not in edit.call_args.args[0]

    async def test_duplicate_archive_moment(self, session, edit, cb_answer) -> None:
        user = await measurements_service.get_or_create_user(session, TG_ID)
        await measurements_service.save_measurement(
            session, user, 135, 85, 72,
            timestamp=datetime(2024, 3, 12, 5, 30, tzinfo=timezone.utc),
        )
        await session.commit()
        state = make_state()
        await start_input(state, "pulse")
        await archive_draft(state, "2024-03-12T05:30:45+00:00")  # та же минута
        await tap(state, "m:key:ok", session=session)
        assert edit.call_args.args[0] == texts.DUPLICATE_MEASUREMENT


class TestArchiveSummaryActions:
    """Кнопки сводки архива `m:arch:*` (§6.10)."""

    async def test_edit_targets_context_record(self, session, edit, cb_answer) -> None:
        """«✏️ Изменить» правит запись из контекста, а не последнюю по времени."""
        user = await measurements_service.get_or_create_user(session, TG_ID)
        older = await measurements_service.save_measurement(
            session, user, 120, 80,
            timestamp=datetime(2024, 3, 12, 5, 30, tzinfo=timezone.utc),
        )
        await measurements_service.save_measurement(
            session, user, 130, 85,
            timestamp=datetime(2025, 1, 1, 10, 0, tzinfo=timezone.utc),
        )
        await session.commit()
        state = make_state()
        await archive_draft(state, "2024-03-12T05:30:00+00:00", **{measure.M_ID: older.id})
        await call(
            measure.cb_arch_edit,
            callback=make_callback("m:arch:edit", make_message()),
            state=state,
            session=session,
        )
        data = await state.get_data()
        assert data[measure.M_EDIT_ID] == older.id
        assert data[measure.M_ARCH_TS] == "2024-03-12T05:30:00+00:00"
        assert await state.get_state() == measure.MeasureInput.active.state

    async def test_edit_without_context_goes_to_menu(self, session, edit, cb_answer) -> None:
        state = make_state()
        await call(
            measure.cb_arch_edit,
            callback=make_callback("m:arch:edit", make_message()),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.MAIN_MENU

    async def test_med_yes_sets_flag_only(self, session, edit, cb_answer) -> None:
        user = await measurements_service.get_or_create_user(session, TG_ID)
        measurement = await measurements_service.save_measurement(session, user, 135, 85, 72)
        await session.commit()
        state = make_state()
        await archive_draft(state, "2024-03-12T05:30:00+00:00", **{measure.M_ID: measurement.id})
        await call(
            measure.cb_arch_med_set,
            callback=make_callback("m:arch:med:yes", make_message()),
            state=state,
            session=session,
        )
        row = await reload_measurement(session, measurement.id)
        assert row.medication_taken is True
        # Повторный замер и логи приёмов для архива не создаются (§6.10).
        logs = (
            await session.execute(
                select(MedicationLog).execution_options(populate_existing=True)
            )
        ).scalars().all()
        assert logs == []
        rows = (
            await session.execute(select(MeasurementModel).execution_options(populate_existing=True))
        ).scalars().all()
        assert len(rows) == 1
        assert edit.call_args.args[0] == texts.fmt_archive_saved(
            "12.03.2025, 08:00", 135, 85, 72
        )
        assert edit.call_args.kwargs["reply_markup"] == keyboards.archive_saved_kb()

    async def test_med_no_sets_false(self, session, edit, cb_answer) -> None:
        user = await measurements_service.get_or_create_user(session, TG_ID)
        measurement = await measurements_service.save_measurement(session, user, 135, 85, 72)
        await session.commit()
        state = make_state()
        await archive_draft(state, "2024-03-12T05:30:00+00:00", **{measure.M_ID: measurement.id})
        await call(
            measure.cb_arch_med_set,
            callback=make_callback("m:arch:med:no", make_message()),
            state=state,
            session=session,
        )
        assert (await reload_measurement(session, measurement.id)).medication_taken is False

    async def test_feel_set(self, session, edit, cb_answer) -> None:
        user = await measurements_service.get_or_create_user(session, TG_ID)
        measurement = await measurements_service.save_measurement(session, user, 135, 85, 72)
        await session.commit()
        state = make_state()
        await archive_draft(state, "2024-03-12T05:30:00+00:00", **{measure.M_ID: measurement.id})
        await call(
            measure.cb_arch_feel_set,
            callback=make_callback("m:arch:feel:headache", make_message()),
            state=state,
            session=session,
        )
        assert (await reload_measurement(session, measurement.id)).feeling == "headache"

    async def test_feel_other_text_goes_to_note(self, session, answer) -> None:
        user = await measurements_service.get_or_create_user(session, TG_ID)
        measurement = await measurements_service.save_measurement(session, user, 135, 85, 72)
        await session.commit()
        state = make_state()
        await archive_draft(state, "2024-03-12T05:30:00+00:00", **{measure.M_ID: measurement.id})
        await state.set_state(measure.MeasureInput.feeling_other)
        await call(
            measure.msg_feeling_other,
            message=make_message("лёгкое недомогание"),
            state=state,
            session=session,
        )
        row = await reload_measurement(session, measurement.id)
        assert row.feeling == "other"
        assert row.note == "лёгкое недомогание"
        assert answer.call_args.kwargs["reply_markup"] == keyboards.archive_saved_kb()

    async def test_note_keeps_archive_buttons(self, session, answer) -> None:
        user = await measurements_service.get_or_create_user(session, TG_ID)
        measurement = await measurements_service.save_measurement(session, user, 135, 85, 72)
        await session.commit()
        state = make_state()
        await archive_draft(state, "2024-03-12T05:30:00+00:00", **{measure.M_ID: measurement.id})
        await state.set_state(measure.MeasureInput.note_input)
        await call(
            measure.msg_note,
            message=make_message("после кофе"),
            state=state,
            session=session,
        )
        assert (await reload_measurement(session, measurement.id)).note == "после кофе"
        assert answer.call_args.kwargs["reply_markup"] == keyboards.archive_saved_kb()

    async def test_menu_resets_state(self, edit, cb_answer) -> None:
        state = make_state()
        await archive_draft(state, "2024-03-12T05:30:00+00:00")
        await call(
            measure.cb_arch_menu,
            callback=make_callback("m:arch:menu", make_message()),
            state=state,
        )
        assert edit.call_args.args[0] == texts.MAIN_MENU
        assert await state.get_data() == {}

    async def test_actions_without_context_go_to_menu(self, session, edit, cb_answer) -> None:
        handlers = {
            "m:arch:note": measure.cb_arch_note,
            "m:arch:med": measure.cb_arch_med,
            "m:arch:feel": measure.cb_arch_feel,
            "m:arch:feel:other": measure.cb_arch_feel_other,
        }
        for action, handler in handlers.items():
            state = make_state()
            await call(
                handler, callback=make_callback(action, make_message()), state=state,
                session=session,
            )
            assert edit.call_args.args[0] == texts.MAIN_MENU

    async def test_med_ask_screen(self, session, edit, cb_answer) -> None:
        state = make_state()
        await archive_draft(state, "2024-03-12T05:30:00+00:00", **{measure.M_ID: 1})
        await call(
            measure.cb_arch_med,
            callback=make_callback("m:arch:med", make_message()),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.MED_ASK
        assert edit.call_args.kwargs["reply_markup"] == keyboards.archive_medication_taken_kb()

    async def test_feel_ask_screen(self, session, edit, cb_answer) -> None:
        state = make_state()
        await archive_draft(state, "2024-03-12T05:30:00+00:00", **{measure.M_ID: 1})
        await call(
            measure.cb_arch_feel,
            callback=make_callback("m:arch:feel", make_message()),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.FEELING_ASK
        assert edit.call_args.kwargs["reply_markup"] == keyboards.archive_feeling_kb()


class TestBeginFromSlotReminder:
    """`rem:write:{slot}:{д}` (0.5.6): «Записать давление» — ответ, повтор отменяется."""

    async def test_cancels_followup(self, session, user, edit, cb_answer, mocker) -> None:
        svc = mocker.Mock(cancel_followup=mocker.AsyncMock())
        state = make_state()
        await call(
            measure.cb_measure_begin,
            callback=make_callback("rem:write:evening:20261110", make_message()),
            state=state,
            session=session,
            reminders=svc,
        )
        svc.cancel_followup.assert_awaited_once_with(user.id, "evening")
        assert edit.call_args.args[0] == keypad_text()

    async def test_unknown_slot_no_cancel(self, session, user, edit, cb_answer, mocker) -> None:
        svc = mocker.Mock(cancel_followup=mocker.AsyncMock())
        await call(
            measure.cb_measure_begin,
            callback=make_callback("rem:write:night:20261110", make_message()),
            state=make_state(),
            session=session,
            reminders=svc,
        )
        svc.cancel_followup.assert_not_awaited()
        assert edit.call_args.args[0] == keypad_text()


# ─────────────── Крупный режим: сводки и экранирование (§11, UI4/UI5) ───────────────


class TestLargeSummaries:
    """Сводки измерений: значения `<b>` в крупном, plain — в обычном (§13, R9)."""

    def test_fmt_saved_large(self) -> None:
        assert texts.fmt_saved(135, 85, 72, large=True) == (
            "✅ <b>135/85</b>, пульс <b>72</b>"
        )

    def test_fmt_updated_large(self) -> None:
        assert texts.fmt_updated(140, 90, 70, large=True) == (
            "✅ Обновлено: <b>140/90</b>, пульс <b>70</b>"
        )

    def test_fmt_archive_saved_large(self) -> None:
        assert texts.fmt_archive_saved("12.03.2025, 08:00", 135, 85, 72, large=True) == (
            "✅ <b>135/85</b>, пульс <b>72</b> · за 12.03.2025, 08:00"
        )

    def test_fmt_saved_normal_has_no_bold(self) -> None:
        text = texts.fmt_saved(135, 85, 72)
        assert "<b>" not in text
        assert text == "✅ 135/85, пульс 72"

    def test_fmt_saved_large_empty_pulse_bold(self) -> None:
        text = texts.fmt_saved(135, 85, None, large=True)
        assert text == "✅ <b>135/85</b>"

    async def test_save_shows_large_summary_with_html(self, session, answer) -> None:
        """Показ сводки в крупном — parse_mode="HTML" (значения жирные, §11)."""
        user = await measurements_service.get_or_create_user(session, TG_ID)
        user.ui_large = True
        await session.commit()
        state = make_state()
        await state.set_data({measure.M_SYS: 135, measure.M_DIA: 85, measure.M_PULSE: 72})
        await measure._save(make_message(), state, session, TG_ID, via_message=True)
        assert answer.call_args.args[0] == texts.fmt_saved(135, 85, 72, large=True)
        assert answer.call_args.kwargs["parse_mode"] == "HTML"

    async def test_save_normal_summary_plain(self, session, answer) -> None:
        """Обычный режим: сводка без тегов и без parse_mode (§11)."""
        state = make_state()
        await state.set_data({measure.M_SYS: 135, measure.M_DIA: 85, measure.M_PULSE: 72})
        await measure._save(make_message(), state, session, TG_ID, via_message=True)
        assert answer.call_args.args[0] == texts.fmt_saved(135, 85, 72)
        assert answer.call_args.kwargs["parse_mode"] is None


class TestMedsIntakeEscaping:
    """`fmt_meds_intake` / `fmt_intake_result`: экранирование и `<b>` в крупном (§11)."""

    def test_fmt_meds_intake_escapes(self) -> None:
        assert texts.fmt_meds_intake(["A<b>C 50 мг"]) == "💊 Принято: A&lt;b&gt;C 50 мг"

    def test_fmt_meds_intake_large_bold(self) -> None:
        assert texts.fmt_meds_intake(["Лозартан 50 мг"], large=True) == (
            "💊 Принято: <b>Лозартан 50 мг</b>"
        )

    def test_fmt_intake_result_escapes_taken(self) -> None:
        assert texts.fmt_intake_result(["A<b>C 50 мг"], [], []) == (
            "Отметил: принято: A&lt;b&gt;C 50 мг"
        )

    def test_fmt_intake_result_escapes_both_parts(self) -> None:
        assert texts.fmt_intake_result(["A<b>C 50 мг"], ["X&Y 5 мг"], []) == (
            "Отметил: принято: A&lt;b&gt;C 50 мг; пропущено: X&amp;Y 5 мг"
        )

    def test_fmt_intake_result_escapes_already(self) -> None:
        assert texts.fmt_intake_result([], [], ["A<b>C 50 мг"]) == (
            "Эти лекарства уже отмечены сегодня ✓: A&lt;b&gt;C 50 мг"
        )

    async def test_med_done_uses_html_parse_mode(self, session, edit, cb_answer, reminders) -> None:
        """Показ «💊 Принято: …» — всегда parse_mode="HTML" (названия экранированы)."""
        med_id = await add_med(session, name="A<b>C", dosage="50 мг")
        measurement_id = await save_draft(session, make_state())
        state = make_state()
        await state.set_data({measure.M_ID: measurement_id, measure.M_MED_CHECKED: [med_id]})
        await call(
            measure.cb_med_done,
            callback=make_callback("m:med:done", make_message()),
            state=state,
            session=session,
            reminders=reminders,
        )
        assert edit.call_args.args[0] == texts.fmt_meds_intake(["A<b>C 50 мг"])
        assert "A&lt;b&gt;C" in edit.call_args.args[0]
        assert edit.call_args.kwargs["parse_mode"] == "HTML"
